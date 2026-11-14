"""Vault writes into a cross-origin out-of-process iframe (OOPIF).

A frameset login (top page on one origin, the form in a frame on another) is only reachable inside the
frame. These tests pin the security model of that path:

- this holds for out-of-process iframes (own CDP session) and for cross-origin frames in the page's own
  process (same-site, reached through an isolated world on the page session);
- the frame's OWN origin (read inside the frame) must exactly equal a saved origin of the item;
- the top page's origin never authorises the write, and the write never runs in the top page;
- the fill script re-asserts the expected origin inside that frame right before writing;
- two matching frames are refused as ambiguous;
- enter_code with a handle refuses a page/frame whose origin is not the handle's.

Supervisor-level tests drive ``CDPSupervisor`` against a fake CDP: frame selection follows the
auto-attach session chain of the focused tab, bounded by FRAME_TREE_MAX_OOPIF_DEPTH.
All items are synthetic and live in a temporary vault dir; no browser and no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.vault_store import VaultStore

TOP = "https://books.example.test"        # frameset page
LOGIN = "https://login20.example.test"    # the item's saved origin: the login frame
OTHER = "https://ads.example.test"
PASSWORD = "synthetic-pw-7c1e"

PASSWORD_CONTROLS = [
    {"autocomplete": "username", "formIndex": 0, "index": 0, "label": "", "name": "user", "type": "text"},
    {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw", "type": "password"},
]
OTP_CONTROLS = [{"autocomplete": "one-time-code", "formIndex": 0, "index": 0, "label": "Code", "name": "otp", "type": "text"}]


@pytest.fixture()
def store(tmp_path):
    return VaultStore(base_dir=tmp_path / "vault")


def _add_login(store, origin=LOGIN, otp=False):
    secret = {"identifier_type": "username", "identifier": "felix", "password": PASSWORD}
    if otp:
        secret["otp_secret"] = "JBSWY3DPEHPK3PXP"
    return store.add_item("login", "Books", secret, origin=origin)


class _Context:
    def __init__(self, origin, controls):
        self.origin, self.controls = origin, controls


class FakeBrowser:
    """Stands in for the supervisor: a top page plus OOPIF contexts keyed by session id. Executes the
    inspection and the fill script far enough to honour the in-script origin assert."""

    def __init__(self, top_origin, top_controls=(), frames=None, in_process=None):
        self.top = _Context(top_origin, list(top_controls))
        self.frames = {sid: _Context(o, list(c)) for sid, (o, c) in (frames or {}).items()}
        # in-process cross-origin frames: isolated-world context id on the page session -> content
        self.in_process = {cid: _Context(o, list(c)) for cid, (o, c) in (in_process or {}).items()}
        self.calls: list[tuple[str, str]] = []  # (where, what)
        self.written: list[tuple[str, str]] = []  # (where, expression) of fill scripts that wrote

    def _ctx(self, session_id, context_id=None):
        if context_id:
            return self.in_process[context_id]
        return self.frames[session_id] if session_id else self.top

    def focus_page(self, origin, *, accept=None, timeout=10.0):
        has_form = any(c["type"] == "password" or c["autocomplete"] == "one-time-code" for c in self.top.controls)
        ok = (not origin or origin == self.top.origin) and has_form
        return {"ok": True, "url": self.top.origin + "/"} if ok else {"ok": False, "error": "no open page"}

    def focus_frame(self, origins, *, accept=None, timeout=10.0):
        want_otp = bool(accept and "one-time-code" in accept)

        def holds_form(ctx):
            return ctx.origin in origins and any(
                (c["autocomplete"] == "one-time-code") if want_otp else (c["type"] == "password") for c in ctx.controls)

        found = [{"frame_id": f"F-{sid}", "session_id": sid, "context_id": None, "origin": ctx.origin,
                  "url": ctx.origin + "/v1login"} for sid, ctx in self.frames.items() if holds_form(ctx)]
        found += [{"frame_id": f"IP-{cid}", "session_id": "PAGE", "context_id": cid, "origin": ctx.origin,
                   "url": ctx.origin + "/v1login"} for cid, ctx in self.in_process.items() if holds_form(ctx)]
        return {"ok": True, "frames": found} if found else {"ok": False, "error": "no frame"}

    def evaluate_runtime(self, expression, *, session_id=None, context_id=None, **_):
        where = f"{session_id}#{context_id}" if context_id else (session_id or "top")
        if context_id and (session_id != "PAGE" or context_id not in self.in_process):
            return {"ok": False, "error": "Cannot find context with specified id"}
        if session_id and not context_id and session_id not in self.frames:
            return {"ok": False, "error": "frame session is no longer attached"}
        ctx = self._ctx(session_id, context_id)
        if expression == "window.location.href":
            self.calls.append((where, "href"))
            return {"ok": True, "result": ctx.origin + "/bh/"}
        if "expectedOrigin" in expression:
            self.calls.append((where, "fill"))
            expected = json.loads(re.search(r"const expectedOrigin = (\"[^\"]*\");", expression).group(1))
            if expected != ctx.origin:  # the in-frame assert
                return {"ok": True, "result": json.dumps({"refused": "origin_changed", "found": ctx.origin})}
            self.written.append((where, expression))
            return {"ok": True, "result": json.dumps({"filled": len(re.findall(r'"index":', expression))})}
        if "querySelectorAll" in expression:
            self.calls.append((where, "inspect"))
            return {"ok": True, "result": json.dumps(ctx.controls)}
        return {"ok": False, "error": "unexpected expression"}


def _run(fn, browser, store):
    from tools import browser_vault_tool

    with patch("agent.vault_store.get_vault_store", return_value=store), \
         patch.object(browser_vault_tool, "_ensure_supervisor", return_value=browser), \
         patch.object(browser_vault_tool, "_bot_desktop_browser_session", return_value=False), \
         patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
        reg.get.return_value = browser
        raw = fn()
    assert PASSWORD not in raw
    return raw, json.loads(raw)


# ---------------------------------------------------------------------------
# browser_vault fill
# ---------------------------------------------------------------------------

class TestFillInCrossOriginFrame:
    def test_matching_frame_is_inspected_and_filled_with_its_own_origin(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, frames={"S-login": (LOGIN, PASSWORD_CONTROLS)})
        raw, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is True and out["filled_fields"] == 1
        assert out["origin"] == LOGIN and out["in_frame"] is True
        # inspection and the write both ran in the frame; the top page only answered its origin
        assert ("S-login", "inspect") in browser.calls and ("S-login", "fill") in browser.calls
        assert ("top", "inspect") not in browser.calls and ("top", "fill") not in browser.calls
        (where, expr), = browser.written
        assert where == "S-login"
        assert f'const expectedOrigin = "{LOGIN}";' in expr and TOP not in expr
        assert PASSWORD in expr  # the secret went to the page, not the result

    def test_frame_on_a_different_origin_is_refused(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, frames={"S-ads": (OTHER, PASSWORD_CONTROLS)})
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is False and out["error_type"] == "origin_mismatch"
        assert browser.written == [] and not any(what == "fill" for _, what in browser.calls)

    def test_sibling_subdomain_frame_is_not_a_match(self, store):
        """Exact origin only: the top page's site, or a sibling host of the saved one, never stands in."""
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, frames={"S-sib": ("https://login21.example.test", PASSWORD_CONTROLS)})
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["error_type"] == "origin_mismatch" and browser.written == []

    def test_top_origin_never_authorises_or_receives_the_write_when_only_a_frame_matches(self, store):
        """Even with a password field on the top page, a frame-only match writes in the frame, and the
        in-frame assert is bound to the frame's origin."""
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, top_controls=PASSWORD_CONTROLS, frames={"S-login": (LOGIN, PASSWORD_CONTROLS)})
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is True and out["origin"] == LOGIN
        assert [w for w, _ in browser.written] == ["S-login"]
        assert ("top", "inspect") not in browser.calls

    def test_item_saved_for_frameset_and_frame_fills_the_frame_holding_the_form(self, store):
        """A manager item listing both the frameset page and the login frame: the formless top page must not
        win just because its origin is saved too."""
        from agent.vault_store import VaultItemMeta
        from tools import browser_vault_tool

        meta = VaultItemMeta(id="op:books", kind="login", label="Books", origin=TOP, created_at="2026-01-01T00:00:00Z",
                             identifier_type="username", identifier="felix", allowed_origins=(TOP, LOGIN))

        class _Backend:
            name, display_name, needs_unlock = "onepassword", "1Password", False

            def is_unlocked(self):
                return True

            def get_meta(self, handle):
                return meta if handle == meta.id else None

            def resolve_password(self, handle):
                return PASSWORD

            def fill_context(self):
                return contextlib.nullcontext()

        browser = FakeBrowser(TOP, frames={"S-login": (LOGIN, PASSWORD_CONTROLS)})
        with patch("agent.vault_backends.backend_for_handle", return_value=_Backend()):
            _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is True and out["origin"] == LOGIN and out["in_frame"] is True
        assert [w for w, _ in browser.written] == ["S-login"]

    def test_two_matching_frames_are_refused_as_ambiguous(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, frames={"S-a": (LOGIN, PASSWORD_CONTROLS), "S-b": (LOGIN, PASSWORD_CONTROLS)})
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is False and out["error_type"] == "ambiguous_frame"
        assert browser.written == [] and not any(what in {"inspect", "fill"} for _, what in browser.calls)

    def test_frame_that_navigated_away_before_the_write_gets_nothing(self, store):
        """The in-frame origin assert is the last word: a frame that moved off the saved origin between
        selection and write refuses inside the frame."""
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, frames={"S-login": (LOGIN, PASSWORD_CONTROLS)})
        real_eval = browser.evaluate_runtime

        def navigate_then_eval(expression, **kw):
            if "expectedOrigin" in expression:
                browser.frames["S-login"].origin = OTHER
            return real_eval(expression, **kw)

        browser.evaluate_runtime = navigate_then_eval
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is False and out["error_type"] == "origin_changed"
        assert browser.written == []

    def test_frame_session_gone_before_the_write_refuses(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, frames={"S-login": (LOGIN, PASSWORD_CONTROLS)})
        real_eval = browser.evaluate_runtime

        def detach_then_eval(expression, **kw):
            if "expectedOrigin" in expression:
                browser.frames.pop("S-login")
            return real_eval(expression, **kw)

        browser.evaluate_runtime = detach_then_eval
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is False and browser.written == []

    def test_top_page_on_the_saved_origin_keeps_the_top_page_path(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store, origin=TOP)
        browser = FakeBrowser(TOP, top_controls=PASSWORD_CONTROLS, frames={"S-login": (TOP, PASSWORD_CONTROLS)})
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is True and "in_frame" not in out
        assert [w for w, _ in browser.written] == ["top"]

    def test_human_takeover_at_the_write_refuses_in_frame_too(self, store):
        """The Bot Desktop lease re-admit at the write applies to frame writes as well."""
        from tools import browser_vault_tool
        from tools.bot_desktop import lease as bd_lease

        meta = _add_login(store)
        browser = FakeBrowser(TOP, frames={"S-login": (LOGIN, PASSWORD_CONTROLS)})

        def human_has_it():
            raise bd_lease.HumanHasControl("the user took over the screen")

        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch.object(browser_vault_tool, "_ensure_supervisor", return_value=browser), \
             patch.object(browser_vault_tool, "_bot_desktop_browser_session", return_value=True), \
             patch.object(bd_lease, "assert_agent_may_act", side_effect=human_has_it), \
             patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg:
            reg.get.return_value = browser
            out = json.loads(browser_vault_tool.browser_vault_fill(meta.id))
        assert out["success"] is False and out["error_type"] == "human_has_control"
        assert browser.written == []


class TestFillInInProcessFrame:
    """Same-site cross-origin frames (frameset page on secure.*, login frame on secure20.*) run in the page's
    process: no own session, reached through an isolated world on the page session."""

    def test_matching_in_process_frame_fills_in_its_own_context(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, in_process={7: (LOGIN, PASSWORD_CONTROLS)})
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is True and out["origin"] == LOGIN and out["in_frame"] is True
        assert ("PAGE#7", "inspect") in browser.calls and ("top", "inspect") not in browser.calls
        (where, expr), = browser.written
        assert where == "PAGE#7"
        assert f'const expectedOrigin = "{LOGIN}";' in expr and TOP not in expr

    def test_in_process_frame_on_a_different_origin_is_refused(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, in_process={7: (OTHER, PASSWORD_CONTROLS)})
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is False and out["error_type"] == "origin_mismatch"
        assert browser.written == [] and not any(what == "fill" for _, what in browser.calls)

    def test_one_oopif_plus_one_in_process_match_is_ambiguous(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, frames={"S-login": (LOGIN, PASSWORD_CONTROLS)}, in_process={7: (LOGIN, PASSWORD_CONTROLS)})
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is False and out["error_type"] == "ambiguous_frame"
        assert browser.written == [] and not any(what in {"inspect", "fill"} for _, what in browser.calls)

    def test_in_process_frame_that_navigated_before_the_write_gets_nothing(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, in_process={7: (LOGIN, PASSWORD_CONTROLS)})
        real_eval = browser.evaluate_runtime

        def navigate_then_eval(expression, **kw):
            if "expectedOrigin" in expression:
                browser.in_process[7].origin = OTHER
            return real_eval(expression, **kw)

        browser.evaluate_runtime = navigate_then_eval
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["error_type"] == "origin_changed" and browser.written == []

    def test_item_bound_to_the_top_origin_fills_the_nested_login_frame_not_the_frameset(self, store):
        """Item bound to the frameset's own origin, the form in a nested frame back on that origin: the write
        goes into the frame (whose own origin matches), never into the formless frameset document."""
        from tools import browser_vault_tool

        meta = _add_login(store, origin=TOP)
        browser = FakeBrowser(TOP, in_process={9: (TOP, PASSWORD_CONTROLS), 8: (LOGIN, [])})
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is True and out["origin"] == TOP and out["in_frame"] is True
        assert [w for w, _ in browser.written] == ["PAGE#9"]

    def test_in_process_context_destroyed_before_the_write_refuses(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store)
        browser = FakeBrowser(TOP, in_process={7: (LOGIN, PASSWORD_CONTROLS)})
        real_eval = browser.evaluate_runtime

        def reload_then_eval(expression, **kw):
            if "expectedOrigin" in expression:
                browser.in_process.pop(7)  # the frame reloaded: its isolated world is gone
            return real_eval(expression, **kw)

        browser.evaluate_runtime = reload_then_eval
        _, out = _run(lambda: browser_vault_tool.browser_vault_fill(meta.id), browser, store)
        assert out["success"] is False and browser.written == []

    def test_enter_code_goes_into_a_matching_in_process_frame(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store, origin=LOGIN, otp=True)
        browser = FakeBrowser(TOP, in_process={7: (LOGIN, OTP_CONTROLS)})
        raw, out = _run(lambda: browser_vault_tool.browser_vault_enter_code(meta.id, task_id="t"), browser, store)
        assert out["success"] is True and out["origin"] == LOGIN and out["in_frame"] is True
        (where, expr), = browser.written
        assert where == "PAGE#7" and f'const expectedOrigin = "{LOGIN}";' in expr
        assert re.search(r'"value": "(\d{6})"', expr).group(1) not in raw


# ---------------------------------------------------------------------------
# browser_vault enter_code
# ---------------------------------------------------------------------------

class TestEnterCodeOrigin:
    def test_code_refused_when_page_origin_is_not_the_handles(self, store):
        from agent.vault_backends import unlock as unlock_mod
        from tools import browser_vault_tool

        meta = _add_login(store, origin=LOGIN, otp=True)
        asked = []
        unlock_mod.set_code_prompt_callback(lambda site, hint: asked.append(site) or "123456")
        try:
            browser = FakeBrowser(OTHER, top_controls=OTP_CONTROLS)
            _, out = _run(lambda: browser_vault_tool.browser_vault_enter_code(meta.id, task_id="t"), browser, store)
        finally:
            unlock_mod.set_code_prompt_callback(None)
        assert out["success"] is False and out["error_type"] == "origin_mismatch"
        assert browser.written == [] and asked == []

    def test_code_refused_for_a_frame_on_another_origin(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store, origin=LOGIN, otp=True)
        browser = FakeBrowser(TOP, frames={"S-ads": (OTHER, OTP_CONTROLS)})
        _, out = _run(lambda: browser_vault_tool.browser_vault_enter_code(meta.id, task_id="t"), browser, store)
        assert out["error_type"] == "origin_mismatch" and browser.written == []

    def test_code_goes_into_the_matching_frame(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store, origin=LOGIN, otp=True)
        browser = FakeBrowser(TOP, frames={"S-login": (LOGIN, OTP_CONTROLS)})
        raw, out = _run(lambda: browser_vault_tool.browser_vault_enter_code(meta.id, task_id="t"), browser, store)
        assert out["success"] is True and out["source"] == "local"
        assert out["origin"] == LOGIN and out["in_frame"] is True
        (where, expr), = browser.written
        assert where == "S-login" and f'const expectedOrigin = "{LOGIN}";' in expr
        code = re.search(r'"value": "(\d{6})"', expr).group(1)
        assert code not in raw

    def test_unknown_handle_asks_the_user_and_writes_only_to_the_top_page(self, store):
        """No saved login behind the handle: the user types the code they see; no frame route is taken."""
        from agent.vault_backends import unlock as unlock_mod
        from tools import browser_vault_tool

        asked = []
        unlock_mod.set_code_prompt_callback(lambda site, hint: asked.append(site) or "123456")
        try:
            browser = FakeBrowser(TOP, top_controls=OTP_CONTROLS, frames={"S-login": (LOGIN, OTP_CONTROLS)})
            with patch("agent.vault_backends.unlock.can_prompt_here", return_value=True):
                _, out = _run(lambda: browser_vault_tool.browser_vault_enter_code("vault_nope", task_id="t"), browser, store)
        finally:
            unlock_mod.set_code_prompt_callback(None)
        assert out["success"] is True and out["source"] == "user" and "in_frame" not in out
        assert [w for w, _ in browser.written] == ["top"] and asked == [TOP.split("://", 1)[1]]


# ---------------------------------------------------------------------------
# CDPSupervisor: frame selection and frame-targeted evaluate (fake CDP)
# ---------------------------------------------------------------------------

def _supervisor(cdp_fn, page_sid="PAGE"):
    from tools.browser_supervisor import CDPSupervisor

    sup = object.__new__(CDPSupervisor)
    sup._state_lock = threading.Lock()
    sup._active = True
    sup._page_session_id = page_sid
    sup._frames = {}
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True)
    thread.start()
    sup._cdp = cdp_fn  # type: ignore[method-assign]
    sup._loop, sup._thread = loop, thread
    return sup


def _stop(sup):
    sup._loop.call_soon_threadsafe(sup._loop.stop)
    sup._thread.join(timeout=2)


def _oopif(frame_id, sid, parent_sid, url=""):
    from tools.browser_supervisor_frames import FrameInfo

    return FrameInfo(frame_id=frame_id, url=url, origin="", parent_frame_id="TOPFRAME", is_oopif=True,
                     cdp_session_id=sid, parent_session_id=parent_sid)


class _FakeCDP:
    """Answers the frame probe per session from ``in_frame`` = {sid: (origin, has_form)}."""

    def __init__(self, in_frame, targets=(), on_attach=None, current_target=None, frame_trees=None):
        self.in_frame, self.targets, self.on_attach = in_frame, list(targets), on_attach
        self.current_target = current_target
        self.frame_trees = frame_trees or {}  # page session -> Page.getFrameTree frameTree
        self.worlds: dict[int, str] = {}       # isolated-world context id -> frame id
        self.calls: list[tuple[str, str | None, dict]] = []

    async def __call__(self, method, params=None, *, session_id=None, timeout=10.0):
        self.calls.append((method, session_id, params or {}))
        if method == "Page.getFrameTree":
            return {"result": {"frameTree": self.frame_trees.get(session_id, {"frame": {"id": "TOPFRAME"}})}}
        if method == "Page.createIsolatedWorld":
            context_id = 100 + len(self.worlds)
            self.worlds[context_id] = params["frameId"]
            return {"result": {"executionContextId": context_id}}
        if method == "Runtime.evaluate" and "form: !!(" in (params or {}).get("expression", ""):
            key = ("frame", self.worlds[params["contextId"]]) if "contextId" in params else session_id
            origin, form = self.in_frame[key]
            return {"result": {"result": {"type": "object", "value": {"origin": origin, "form": form}}}}
        if method == "Runtime.evaluate":
            return {"result": {"result": {"type": "number", "value": 1}}}
        if method == "Target.getTargets":
            return {"result": {"targetInfos": self.targets}}
        if method == "Target.getTargetInfo":
            return {"result": {"targetInfo": {"targetId": self.current_target}}} if self.current_target else {"result": {}}
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "NEWPAGE-" + params["targetId"]}}
        if method == "Target.setAutoAttach" and self.on_attach:
            self.on_attach(session_id)
        return {"result": {}}


class TestSupervisorFrameSelection:
    def test_only_frames_of_the_focused_tab_with_the_exact_in_frame_origin_match(self):
        cdp = _FakeCDP({"S1": (LOGIN, True), "S2": (OTHER, True), "S3": (LOGIN, True), "S4": (LOGIN, False)})
        sup = _supervisor(cdp)
        try:
            sup._frames = {
                "F1": _oopif("F1", "S1", "PAGE"),
                "F2": _oopif("F2", "S2", "PAGE", url=LOGIN + "/stale"),  # tracked url lies; the frame answers
                "F3": _oopif("F3", "S3", "OTHER-TAB"),                  # another tab
                "F4": _oopif("F4", "S4", "PAGE"),                        # right origin, no form
            }
            out = sup.focus_frame([LOGIN], accept="!!document.querySelector('input[type=password]')")
        finally:
            _stop(sup)
        assert out["ok"] is True
        assert [(f["frame_id"], f["session_id"], f["origin"]) for f in out["frames"]] == [("F1", "S1", LOGIN)]
        probed = {sid for m, sid, p in cdp.calls if m == "Runtime.evaluate"}
        assert "S3" not in probed  # frames of other tabs are not even asked

    def test_nested_frames_follow_the_session_chain_up_to_the_depth_cap(self):
        cdp = _FakeCDP({"D1": (OTHER, False), "D2": (LOGIN, True), "D3": (LOGIN, True)})
        sup = _supervisor(cdp)
        try:
            sup._frames = {"F1": _oopif("F1", "D1", "PAGE"), "F2": _oopif("F2", "D2", "D1"),
                           "F3": _oopif("F3", "D3", "D2")}  # depth 3 > FRAME_TREE_MAX_OOPIF_DEPTH
            out = sup.focus_frame([LOGIN], accept="true")
        finally:
            _stop(sup)
        assert [f["frame_id"] for f in out["frames"]] == ["F2"]

    def test_two_matching_frames_are_both_reported(self):
        cdp = _FakeCDP({"S1": (LOGIN, True), "S2": (LOGIN, True)})
        sup = _supervisor(cdp)
        try:
            sup._frames = {"F1": _oopif("F1", "S1", "PAGE"), "F2": _oopif("F2", "S2", "PAGE")}
            out = sup.focus_frame([LOGIN], accept="true")
        finally:
            _stop(sup)
        assert len(out["frames"]) == 2

    def test_other_tab_is_attached_and_focused_when_the_focused_tab_has_no_match(self):
        from tools.browser_supervisor_frames import FrameInfo

        sup = None

        def auto_attach(session_id):  # Chrome reports the tab's existing OOPIF on the fresh session
            if session_id == "NEWPAGE-T2":
                sup._frames["F9"] = FrameInfo("F9", LOGIN + "/v1login", "", None, True, "S9", parent_session_id=session_id)

        cdp = _FakeCDP({"S9": (LOGIN, True)},
                       targets=[{"type": "page", "targetId": "T2", "url": TOP + "/bh/"},
                                {"type": "page", "targetId": "T3", "url": "about:blank"}],
                       on_attach=auto_attach)
        sup = _supervisor(cdp)
        try:
            out = sup.focus_frame([LOGIN], accept="true")
            focused = sup._page_session_id
        finally:
            _stop(sup)
        assert [f["session_id"] for f in out["frames"]] == ["S9"]
        assert focused == "NEWPAGE-T2"
        assert not any(m == "Target.attachToTarget" and p.get("targetId") == "T3" for m, _, p in cdp.calls)

    def test_focused_tab_is_not_reattached_and_ambiguous_tabs_are_released(self):
        from tools.browser_supervisor_frames import FrameInfo

        sup = None

        def auto_attach(session_id):
            if session_id in {"NEWPAGE-T2", "NEWPAGE-T3"}:
                fid = "F-" + session_id
                sup._frames[fid] = FrameInfo(fid, "", "", None, True, "S-" + session_id, parent_session_id=session_id)

        cdp = _FakeCDP({"S-NEWPAGE-T2": (LOGIN, True), "S-NEWPAGE-T3": (LOGIN, True)},
                       targets=[{"type": "page", "targetId": t, "url": TOP + "/"} for t in ("T1", "T2", "T3")],
                       on_attach=auto_attach, current_target="T1")
        sup = _supervisor(cdp)
        try:
            out = sup.focus_frame([LOGIN], accept="true")
            focused = sup._page_session_id
        finally:
            _stop(sup)
        attached = [p["targetId"] for m, _, p in cdp.calls if m == "Target.attachToTarget"]
        detached = [p["sessionId"] for m, _, p in cdp.calls if m == "Target.detachFromTarget"]
        assert attached == ["T2", "T3"]  # T1 is the focused tab, already checked
        assert len(out["frames"]) == 2 and focused == "PAGE"
        assert sorted(detached) == ["NEWPAGE-T2", "NEWPAGE-T3"]

    def test_in_process_frames_are_probed_in_an_isolated_world_on_the_page_session(self):
        def node(fid, url, *children):
            return {"frame": {"id": fid, "url": url}, "childFrames": list(children)}

        tree = node("TOPFRAME", TOP + "/bh/",
                    node("MENU", TOP + "/menu"),
                    node("MAIN", LOGIN + "/v1login", node("DEEP", LOGIN + "/x", node("DEEPER", LOGIN + "/y"))),
                    node("F1", LOGIN + "/oopif"))  # also tracked as an OOPIF: probed once, via its own session
        cdp = _FakeCDP({("frame", "MENU"): (TOP, True), ("frame", "MAIN"): (LOGIN, True),
                        ("frame", "DEEP"): (LOGIN, False), "S1": (OTHER, True)},
                       frame_trees={"PAGE": tree})
        sup = _supervisor(cdp)
        try:
            sup._frames = {"F1": _oopif("F1", "S1", "PAGE")}
            out = sup.focus_frame([LOGIN], accept="!!document.querySelector('input[type=password]')")
        finally:
            _stop(sup)
        assert [(f["frame_id"], f["session_id"]) for f in out["frames"]] == [("MAIN", "PAGE")]
        context_id = out["frames"][0]["context_id"]
        assert cdp.worlds[context_id] == "MAIN"
        worlds = [p["frameId"] for m, _, p in cdp.calls if m == "Page.createIsolatedWorld"]
        assert worlds == ["MENU", "MAIN", "DEEP"]  # top frame, the OOPIF and depth 3 are not entered
        assert all(sid == "PAGE" for m, sid, _ in cdp.calls if m == "Page.createIsolatedWorld")

    def test_login_frame_nested_inside_a_cross_origin_frame_on_the_top_origin(self):
        """The live layout: frameset on secure., its main frame on secure20., the password form in a frame
        nested inside that one and back on secure. — an item bound to secure. fills that nested frame."""
        tree = {"frame": {"id": "TOPFRAME", "url": TOP + "/bh/"}, "childFrames": [
            {"frame": {"id": "MAIN", "url": LOGIN + "/v1login"}, "childFrames": [
                {"frame": {"id": "INLOG", "url": TOP + "/bh/inloggen.asp"}}]}]}
        cdp = _FakeCDP({("frame", "MAIN"): (LOGIN, False), ("frame", "INLOG"): (TOP, True)}, frame_trees={"PAGE": tree})
        sup = _supervisor(cdp)
        try:
            out = sup.focus_frame([TOP], accept="!!document.querySelector('input[type=password]')")
        finally:
            _stop(sup)
        assert [(f["frame_id"], f["origin"]) for f in out["frames"]] == [("INLOG", TOP)]

    def test_oopif_and_in_process_matches_are_counted_together(self):
        tree = {"frame": {"id": "TOPFRAME"}, "childFrames": [{"frame": {"id": "MAIN", "url": LOGIN + "/v1login"}}]}
        cdp = _FakeCDP({("frame", "MAIN"): (LOGIN, True), "S1": (LOGIN, True)}, frame_trees={"PAGE": tree})
        sup = _supervisor(cdp)
        try:
            sup._frames = {"F1": _oopif("F1", "S1", "PAGE")}
            out = sup.focus_frame([LOGIN], accept="true")
        finally:
            _stop(sup)
        assert sorted(f["frame_id"] for f in out["frames"]) == ["F1", "MAIN"]

    def test_no_match_leaves_focus_and_reports_failure(self):
        cdp = _FakeCDP({"S1": (OTHER, True)})
        sup = _supervisor(cdp)
        try:
            sup._frames = {"F1": _oopif("F1", "S1", "PAGE")}
            out = sup.focus_frame([LOGIN], accept="true")
            focused = sup._page_session_id
        finally:
            _stop(sup)
        assert out["ok"] is False and focused == "PAGE"

    def test_evaluate_runtime_routes_into_a_tracked_frame_session_only(self):
        cdp = _FakeCDP({})
        sup = _supervisor(cdp)
        try:
            sup._frames = {"F1": _oopif("F1", "S1", "PAGE")}
            ok = sup.evaluate_runtime("1", session_id="S1")
            gone = sup.evaluate_runtime("1", session_id="S-unknown")
            in_process = sup.evaluate_runtime("1", session_id="PAGE", context_id=101)
            top = sup.evaluate_runtime("1")
        finally:
            _stop(sup)
        assert ok["ok"] and not gone["ok"] and in_process["ok"] and top["ok"]
        evals = [(sid, p.get("contextId")) for m, sid, p in cdp.calls if m == "Runtime.evaluate"]
        assert evals == [("S1", None), ("PAGE", 101), ("PAGE", None)]  # the unknown session never reached the wire

    def test_target_attached_records_the_parent_session(self):
        from tools.browser_supervisor import CDPSupervisor

        sup = _supervisor(_FakeCDP({}))

        async def _noop(sid):
            return None

        try:
            sup._enable_child_domains = _noop  # type: ignore[method-assign]
            params = {"sessionId": "CHILD", "targetInfo": {"type": "iframe", "targetId": "F1", "url": LOGIN + "/v1login"}}
            asyncio.run_coroutine_threadsafe(CDPSupervisor._on_target_attached(sup, params, "PAGE"), sup._loop).result(2)
            frame = sup._frames["F1"]
            sup._on_frame_navigated({"frame": {"id": "F1", "url": LOGIN + "/v1login", "securityOrigin": LOGIN}}, "CHILD")
            after_nav = sup._frames["F1"]
        finally:
            _stop(sup)
        assert frame.is_oopif and frame.cdp_session_id == "CHILD" and frame.parent_session_id == "PAGE"
        assert after_nav.parent_session_id == "PAGE" and after_nav.origin == LOGIN
        assert "parent_session_id" not in frame.to_dict()  # frame_tree payload unchanged
