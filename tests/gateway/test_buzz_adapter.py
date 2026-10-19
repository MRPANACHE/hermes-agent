"""Tests for the Buzz platform adapter plugin."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

# Load plugins/platforms/buzz/adapter.py under a unique module name
# (plugin_adapter_buzz) so it cannot collide with other plugin adapters
# loaded by sibling tests in the same xdist worker.
_buzz_mod = load_plugin_adapter("buzz")

BuzzAdapter = _buzz_mod.BuzzAdapter
hex_to_npub = _buzz_mod.hex_to_npub
npub_to_hex = _buzz_mod.npub_to_hex
_normalize_user_ref = _buzz_mod._normalize_user_ref
_cli_error_message = _buzz_mod._cli_error_message
_resolve_private_key = _buzz_mod._resolve_private_key
check_requirements = _buzz_mod.check_requirements
validate_config = _buzz_mod.validate_config
register = _buzz_mod.register
_env_enablement = _buzz_mod._env_enablement
_standalone_send = _buzz_mod._standalone_send
_resolve_buzz_event_read_adapter = _buzz_mod._resolve_buzz_event_read_adapter
_handle_buzz_event_read = _buzz_mod._handle_buzz_event_read
_handle_buzz_send_image = _buzz_mod._handle_buzz_send_image

# Real key pair (Chip's public identity — public information, not a secret)
SELF_PUBKEY = "9fd5c7ba6d3ef224da78f541e0fcb9c50f72cc63edb19aae76ac6a0474dfa860"
SELF_NPUB = "npub1nl2u0wnd8mezfknc74q7pl9ec58h9nrrakce4tnk434qgaxl4psqe5twr6"
OTHER_PUBKEY = "a" * 64
CHANNEL = "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"
# Real DM conversation as materialized by a hosted relay: `dms list` returns
# [] for it (#68871) while `channels list` shows it as name "DM", empty
# description, indistinguishable from a channel except via message p-tags.
DM_CHANNEL = "6468cc16-a114-4f23-8b8c-02c1655cbf6b"

_ENV_VARS = (
    "BUZZ_RELAY_URL",
    "BUZZ_PRIVATE_KEY",
    "BUZZ_CHANNELS",
    "BUZZ_HOME_CHANNEL",
    "BUZZ_ALLOWED_USERS",
    "BUZZ_ALLOW_ALL_USERS",
    "BUZZ_POLL_INTERVAL",
    "BUZZ_CLI_PATH",
    "BUZZ_CREDENTIALS_FILE",
    "BUZZ_AUTH_TAG",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Keep tests hermetic: no ambient Buzz env vars or real credentials."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_buzz_mod, "_DEFAULT_CREDENTIALS_DIR", tmp_path / "no-creds")
    yield


def _event(event_id, pubkey=OTHER_PUBKEY, content="hello", created_at=1000, kind=9):
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": created_at,
        "kind": kind,
        "tags": [["h", CHANNEL]],
    }


def _make_adapter(extra=None):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={"relay_url": "https://test.relay", **(extra or {})})
    adapter = BuzzAdapter(cfg)
    adapter._self_pubkey = SELF_PUBKEY
    adapter._self_npub = SELF_NPUB
    adapter._display_name = "Chip"
    adapter._private_key = "nsec1test"
    return adapter


class _ScriptedCli:
    """Fake ``_run_cli`` that routes on the buzz subcommand and records calls."""

    def __init__(self):
        self.responses = {}  # (group, cmd) -> list of (code, stdout, stderr)
        self.calls = []

    def script(self, group, cmd, payload, code=0, stderr=""):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        self.responses.setdefault((group, cmd), []).append((code, stdout, stderr))

    async def __call__(self, args, *, input_text=None):
        self.calls.append((list(args), input_text))
        queue = self.responses.get((args[0], args[1]), [])
        if len(queue) > 1:
            return queue.pop(0)
        if queue:
            return queue[0]
        return 0, "[]", ""


# ── bech32 / identity helpers ─────────────────────────────────────────────


class TestBech32Helpers:

    def test_hex_to_npub_known_pair(self):
        assert hex_to_npub(SELF_PUBKEY) == SELF_NPUB

    def test_npub_to_hex_known_pair(self):
        assert npub_to_hex(SELF_NPUB) == SELF_PUBKEY


# ── Adapter init / config precedence ──────────────────────────────────────


class TestBuzzAdapterInit:


    def test_init_from_config_extra(self):
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "relay_url": "https://cfg.relay",
                "channels": ["ccc"],
                "poll_interval": 2,
                "home_channel": "ccc",
            },
        )
        adapter = BuzzAdapter(cfg)
        assert adapter.relay_url == "https://cfg.relay"
        assert adapter.channels == ["ccc"]
        assert adapter.poll_interval == 2.0
        assert adapter.home_channel == "ccc"

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://env.relay")
        from gateway.config import PlatformConfig
        adapter = BuzzAdapter(PlatformConfig(enabled=True, extra={"relay_url": "https://cfg.relay"}))
        assert adapter.relay_url == "https://env.relay"

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ([CHANNEL, "other"], {CHANNEL, "other"}),
            (f"{CHANNEL}, other", {CHANNEL, "other"}),
        ],
    )
    def test_free_response_channels_accept_list_or_csv(self, configured, expected):
        adapter = _make_adapter({"free_response_channels": configured})
        assert adapter.free_response_channels == expected

    def test_interactive_resume_defaults_true(self):
        """Existing Buzz users keep interactive auto-resume unless opted out."""
        adapter = _make_adapter()
        assert adapter.interactive_resume is True

    def test_interactive_resume_config_false_yields_false(self):
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={"relay_url": "https://test.relay", "interactive_resume": False},
        )
        adapter = BuzzAdapter(cfg)
        assert adapter.interactive_resume is False

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
    def test_interactive_resume_string_false_yields_false(self, raw):
        """YAML strings parse like require_mention (same boolean style)."""
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={"relay_url": "https://test.relay", "interactive_resume": raw},
        )
        adapter = BuzzAdapter(cfg)
        assert adapter.interactive_resume is False


# ── CLI error contract ────────────────────────────────────────────────────


class TestCliErrorContract:

    def test_parses_json_error(self):
        msg = _cli_error_message('{"error":"relay_error","message":"boom","retryable":false}', 2)
        assert "relay_error" in msg and "boom" in msg and "exit 2" in msg


# ── Seeding / high-water mark / de-dupe ───────────────────────────────────


class TestPollingDedupe:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        return a

    @pytest.mark.asyncio
    async def test_seed_sets_high_water_mark_without_dispatch(self, adapter):
        cli = _ScriptedCli()
        cli.script("messages", "get", [
            _event("e1", content="@Chip old history", created_at=100),
            _event("e2", content="@Chip newer history", created_at=200),
        ])
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")

        state = adapter._channel_state[CHANNEL]
        assert state["last_ts"] == 200
        assert set(state["seen"]) == {"e1", "e2"}
        # Seeding must never replay history into the agent
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_new_event_dispatched_once(self, adapter):
        cli = _ScriptedCli()
        cli.script("messages", "get", [_event("e1", content="@Chip hi", created_at=100)])
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")

        # Poll 1: seeded event + a genuinely new mention
        cli.responses.clear()
        cli.script("messages", "get", [
            _event("e1", content="@Chip hi", created_at=100),
            _event("e2", content="hey @Chip, ping", created_at=150),
        ])
        await adapter._poll_channel(CHANNEL)
        assert [d["message_id"] for d in adapter._dispatched] == ["e2"]
        assert adapter._dispatched[0]["text"] == "hey @Chip, ping"
        assert adapter._channel_state[CHANNEL]["last_ts"] == 150

        # Poll 2: identical response — the seen-id set must de-dupe
        await adapter._poll_channel(CHANNEL)
        assert len(adapter._dispatched) == 1


# ── Mention gating / DMs / authorization ──────────────────────────────────


class TestMentionGating:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(CHANNEL)

    @pytest.mark.asyncio
    async def test_unaddressed_channel_message_ignored(self, adapter):
        await self._poll_with(adapter, _event("e1", content="just chatting", created_at=10))
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_name_mention_dispatched(self, adapter):
        await self._poll_with(adapter, _event("e1", content="hey @Chip can you help?", created_at=10))
        assert len(adapter._dispatched) == 1

    @pytest.mark.asyncio
    async def test_structured_pubkey_mention_dispatched(self, adapter):
        adapter._channel_meta[CHANNEL] = {
            "channel_id": CHANNEL,
            "name": "portfolio-intelligence",
            "description": "Permanent repository channel.",
        }
        event = _event("e1", content="review the referenced readiness events", created_at=10)
        event["tags"].append(["p", SELF_PUBKEY])

        await self._poll_with(adapter, event)

        assert [message["message_id"] for message in adapter._dispatched] == ["e1"]

    @pytest.mark.asyncio
    async def test_unaddressed_message_dispatched_in_free_response_channel(self):
        adapter = _make_adapter({"free_response_channels": [CHANNEL]})
        adapter._dispatched = []

        async def capture(**kwargs):
            adapter._dispatched.append(kwargs)

        adapter._dispatch_message = capture
        adapter._message_handler = AsyncMock()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}

        await self._poll_with(
            adapter,
            _event("e1", content="natural follow-up without a mention", created_at=10),
        )

        assert [message["message_id"] for message in adapter._dispatched] == ["e1"]


    @pytest.mark.asyncio
    async def test_allowlist_blocks_unauthorized(self, adapter):
        adapter._allowed_pubkeys = {"b" * 64}
        await self._poll_with(adapter, _event("e1", content="@Chip hello", created_at=10))
        assert adapter._dispatched == []


# ── DM classification via p-tags (issue #68871) ──────────────────────────
#
# `buzz dms list` returns [] on some hosted relays, so DM conversations leak
# in via `channels list` and get seeded chat_type="group".  The adapter must
# reclassify them from the Nostr tags of real traffic: DM messages are
# p-tagged to our own pubkey WITHOUT the text mentioning us, while channel
# messages only ever p-tag us when the text visibly @mentions us.


def _tagged_event(event_id, channel, *, content, pubkey=OTHER_PUBKEY,
                  created_at=1000, kind=9, p=None, reply_to=None):
    """Event with the tag shapes observed on a live relay (h/p/e tags)."""
    tags = [["h", channel]]
    if reply_to:
        tags.append(["e", reply_to, "", "reply"])
    if p:
        tags.append(["p", p])
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
    }


class TestDmClassification:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        # Metadata exactly as `channels list` returns it on the hosted relay.
        a._channel_meta = {
            DM_CHANNEL: {"channel_id": DM_CHANNEL, "name": "DM", "description": ""},
            CHANNEL: {
                "channel_id": CHANNEL,
                "name": "general",
                "description": "General conversation and community updates.",
            },
        }
        a._channel_names = {DM_CHANNEL: "DM", CHANNEL: "general"}
        # Both leaked in as group — the bug under test.
        a._channel_state[DM_CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, channel, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(channel)

    @pytest.mark.asyncio
    async def test_unmentioned_ptagged_dm_latches_and_dispatches(self, adapter):
        """The reported bug: a DM without an @mention must dispatch."""
        await self._poll_with(
            adapter, DM_CHANNEL,
            _tagged_event("e1", DM_CHANNEL, content="here's a test message", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[DM_CHANNEL]["chat_type"] == "dm"
        assert [d["message_id"] for d in adapter._dispatched] == ["e1"]
        assert adapter._dispatched[0]["chat_type"] == "dm"


    @pytest.mark.asyncio
    async def test_general_reply_ptagging_self_stays_channel(self, adapter):
        """A #general reply to us p-tags our pubkey (observed live) — that
        must NOT reclassify the channel; mention gating still applies."""
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="@chip what's up?",
                          p=SELF_PUBKEY, reply_to="root-event"),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        # It carried a mention, so it dispatches — but as a group message.
        assert [d["chat_type"] for d in adapter._dispatched] == ["group"]

        # And once the mention is absent, the channel gate drops the message
        # even though the earlier reply p-tagged us.
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e2", CHANNEL, content="thanks everyone", created_at=1001),
        )
        assert len(adapter._dispatched) == 1


    @pytest.mark.asyncio
    async def test_channel_like_metadata_blocks_dm_latch_for_structured_mention(self, adapter):
        """A structured mention in a known channel dispatches as a group."""
        adapter._channel_meta[CHANNEL]["description"] = ""
        adapter._channel_meta[CHANNEL]["name"] = "announcements"
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="fyi everyone", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        assert [message["message_id"] for message in adapter._dispatched] == ["e1"]
        assert adapter._dispatched[0]["chat_type"] == "group"


    @pytest.mark.asyncio
    async def test_dm_shaped_channel_discovered_when_dms_list_empty(self):
        """Fallback discovery: with `dms list` broken (returns []), a
        DM-shaped `channels list` entry gets watched; real channels not
        already watched are left alone."""
        a = _make_adapter()
        cli = _ScriptedCli()
        cli.script("dms", "list", [])
        cli.script("channels", "list", [
            {"channel_id": DM_CHANNEL, "name": "DM", "description": "", "created_at": 1},
            {"channel_id": CHANNEL, "name": "general",
             "description": "General conversation and community updates.", "created_at": 2},
        ])
        a._run_cli = cli
        await a._discover_dms(seed=False)
        # Watched as group; the p-tag latch flips it on the first real DM.
        assert a._channel_state[DM_CHANNEL]["chat_type"] == "group"
        assert a._may_reclassify_as_dm(DM_CHANNEL) is True
        assert CHANNEL not in a._channel_state
        assert a._may_reclassify_as_dm(CHANNEL) is False


# ── Dispatch source carries the Buzz event id ─────────────────────────────


class TestDispatchSourceMessageId:

    @pytest.mark.asyncio
    async def test_dispatch_source_carries_buzz_event_id(self):
        """_dispatch_message must pass message_id into build_source so
        SessionSource (HERMES_SESSION_MESSAGE_ID) exposes the exact Buzz
        event id for Buzz-to-OOS correlation."""
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        sent = []

        async def fake_handle(event):
            sent.append(event)

        adapter.handle_message = fake_handle
        await adapter._dispatch_message(
            text="hello",
            chat_id=CHANNEL,
            chat_type="group",
            user_id=OTHER_PUBKEY,
            user_name="Tester",
            message_id="evt-src-1",
            created_at=1000,
        )
        assert len(sent) == 1
        assert sent[0].source.message_id == "evt-src-1"
        assert sent[0].message_id == "evt-src-1"


# ── Sending ───────────────────────────────────────────────────────────────


class TestBuzzAdapterSend:

    @pytest.mark.asyncio
    async def test_send_success_via_stdin(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt123", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send(CHANNEL, "hello **markdown**")
        assert result.success is True
        assert result.message_id == "evt123"

        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "send"]
        assert args[args.index("--channel") + 1] == CHANNEL
        # Content travels via stdin (--content -), never argv
        assert args[args.index("--content") + 1] == "-"
        assert stdin_text == "hello **markdown**"
        # Our own event id is marked seen for echo suppression
        assert "evt123" in adapter._channel_state[CHANNEL]["seen"]


    @pytest.mark.asyncio
    async def test_send_image_local_file_uses_file_flag(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt126", "message": ""})
        adapter._run_cli = cli
        result = await adapter.send_image(CHANNEL, str(img), caption="screenshot")
        assert result.success is True
        args, _stdin = cli.calls[0]
        assert args[args.index("--file") + 1] == str(img)

    @pytest.mark.asyncio
    async def test_gateway_final_multiple_local_images_use_file_upload(self, tmp_path):
        images = [tmp_path / "one.jpg", tmp_path / "two.jpg"]
        for image in images:
            image.write_bytes(b"image bytes")
        adapter = _make_adapter()
        adapter.validate_media_delivery_path = lambda file_path: file_path
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "event-one"})
        cli.script("messages", "send", {"accepted": True, "event_id": "event-two"})
        adapter._run_cli = cli

        await adapter.send_multiple_images(
            CHANNEL, [(f"file://{image}", "") for image in images]
        )

        assert len(cli.calls) == 2
        assert [args[args.index("--file") + 1] for args, _stdin in cli.calls] == [
            str(image) for image in images
        ]

    @pytest.mark.asyncio
    async def test_missing_local_image_does_not_send_path_as_text(self, tmp_path):
        adapter = _make_adapter()
        adapter._run_cli = AsyncMock()
        result = await adapter.send_image_file(CHANNEL, str(tmp_path / "missing.jpg"))
        assert result.success is False
        adapter._run_cli.assert_not_awaited()


# ── Lifecycle ─────────────────────────────────────────────────────────────


class TestBuzzAdapterLifecycle:


    @pytest.mark.asyncio
    async def test_disconnect_releases_scoped_lock(self, monkeypatch):
        """The identity lock taken in connect() must be released on disconnect."""
        import gateway.status as gateway_status

        released = []
        monkeypatch.setattr(
            gateway_status,
            "release_scoped_lock",
            lambda platform, key: released.append((platform, key)),
        )
        adapter = _make_adapter()
        adapter._lock_key = "wss://relay.example:" + SELF_PUBKEY
        await adapter.disconnect()
        assert released == [("buzz", "wss://relay.example:" + SELF_PUBKEY)]
        assert adapter._lock_key is None

    @pytest.mark.asyncio
    async def test_connect_fails_when_identity_lock_held(self, monkeypatch):
        """A second profile using the same relay+pubkey must fail fast."""
        import gateway.status as gateway_status

        monkeypatch.setattr(
            gateway_status, "acquire_scoped_lock", lambda platform, key: False
        )
        adapter = _make_adapter()
        adapter.cli_path = "/fake/buzz"
        monkeypatch.setattr(_buzz_mod, "_resolve_private_key", lambda extra=None: "nsec1test")
        cli = _ScriptedCli()
        cli.script(
            "users", "get",
            [{"pubkey": SELF_PUBKEY, "display_name": "Chip"}],
        )
        adapter._run_cli = cli
        assert await adapter.connect() is False
        assert adapter._lock_key is None


# ── Credentials / requirements ────────────────────────────────────────────


class TestCredentialResolution:

    def test_env_key_wins(self, monkeypatch):
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1fromenv")
        assert _resolve_private_key() == "nsec1fromenv"

    def test_credentials_file_fallback(self, monkeypatch, tmp_path):
        creds = tmp_path / "agent_credentials.json"
        creds.write_text(json.dumps({"nsec": "nsec1fromfile", "npub": "npub1x"}), encoding="utf-8")
        monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(creds))
        assert _resolve_private_key() == "nsec1fromfile"


# ── Env enablement / registration / standalone send ──────────────────────


class TestEnvEnablement:

    def test_returns_none_when_unconfigured(self):
        assert _env_enablement() is None


class TestBuzzEventRead:

    @pytest.fixture
    def adapter(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return adapter

    @pytest.mark.asyncio
    async def test_returns_exact_event_from_watched_channel(self, adapter):
        root = _event("a" * 64, content="thread root", created_at=10)
        requested = _event("b" * 64, content="canonical receipt", created_at=11)
        requested["tags"].append(["e", "a" * 64, "", "root"])
        reply = _event("c" * 64, content="later reply", created_at=12)
        reply["tags"].append(["e", "a" * 64, "", "root"])
        adapter._run_cli = AsyncMock(
            return_value=(0, json.dumps([root, requested, reply]), "")
        )

        result = await adapter.read_event(CHANNEL, "b" * 64)

        assert result == {
            "id": "b" * 64,
            "pubkey": OTHER_PUBKEY,
            "kind": 9,
            "tags": requested["tags"],
            "content": "canonical receipt",
            "channel": CHANNEL,
        }
        adapter._run_cli.assert_awaited_once_with([
            "messages", "thread", "--channel", CHANNEL,
            "--event", "b" * 64, "--limit", "100",
        ])

    @pytest.mark.asyncio
    async def test_rejects_unwatched_channel_without_cli_call(self, adapter):
        adapter._run_cli = AsyncMock()

        result = await adapter.read_event("not-watched", "a" * 64)

        assert result == {"error": "channel_not_watched", "channel": "not-watched"}
        adapter._run_cli.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_not_found_for_watched_channel(self, adapter):
        adapter._run_cli = AsyncMock(return_value=(0, "[]", ""))

        result = await adapter.read_event(CHANNEL, "a" * 64)

        assert result == {
            "error": "event_not_found",
            "id": "a" * 64,
            "channel": CHANNEL,
        }

    @pytest.mark.asyncio
    async def test_mismatched_response_id_fails_closed(self, adapter):
        wrong_event = _event("b" * 64, content="wrong event", created_at=10)
        adapter._run_cli = AsyncMock(return_value=(0, json.dumps([wrong_event]), ""))

        result = await adapter.read_event(CHANNEL, "a" * 64)

        assert result == {
            "error": "event_not_found",
            "id": "a" * 64,
            "channel": CHANNEL,
        }

    @pytest.mark.asyncio
    async def test_response_from_wrong_channel_fails_closed(self, adapter):
        wrong_channel_event = _event("a" * 64, content="wrong channel", created_at=10)
        wrong_channel_event["tags"] = [["h", "different-channel"]]
        adapter._run_cli = AsyncMock(
            return_value=(0, json.dumps([wrong_channel_event]), "")
        )

        result = await adapter.read_event(CHANNEL, "a" * 64)

        assert result == {
            "error": "event_channel_mismatch",
            "id": "a" * 64,
            "channel": CHANNEL,
        }

    @pytest.mark.asyncio
    async def test_cli_failure_does_not_return_or_log_stderr(self, adapter, caplog):
        secret_stderr = "relay rejected auth signature-do-not-expose"
        adapter._run_cli = AsyncMock(return_value=(2, "", secret_stderr))

        result = await adapter.read_event(CHANNEL, "a" * 64)

        assert result == {
            "error": "event_read_failed",
            "id": "a" * 64,
            "channel": CHANNEL,
            "code": 2,
        }
        assert secret_stderr not in json.dumps(result)
        assert secret_stderr not in caplog.text

    @pytest.mark.asyncio
    async def test_never_exposes_private_key_or_auth_tag(self, adapter, monkeypatch, caplog):
        private_key = "private-key-must-not-escape"
        auth_tag = '["auth","owner","kind","signature-must-not-escape"]'
        adapter._private_key = private_key
        monkeypatch.setenv("BUZZ_AUTH_TAG", auth_tag)
        event = _event(
            "a" * 64,
            content=f"receipt {private_key} {auth_tag} signature-must-not-escape",
            created_at=10,
        )
        event["tags"].extend([
            json.loads(auth_tag),
            ["x", private_key],
        ])
        adapter._run_cli = AsyncMock(return_value=(0, json.dumps([event]), ""))

        result = await adapter.read_event(CHANNEL, "a" * 64)

        rendered = json.dumps(result)
        assert private_key not in rendered
        assert auth_tag not in rendered
        assert "signature-must-not-escape" not in rendered
        assert private_key not in caplog.text
        assert auth_tag not in caplog.text
        cli_args = adapter._run_cli.await_args.args[0]
        assert private_key not in json.dumps(cli_args)
        assert auth_tag not in json.dumps(cli_args)
        assert not any(tag and tag[0] == "auth" for tag in result["tags"])

    def test_resolves_existing_profile_adapter_without_module_state(
        self, adapter, monkeypatch
    ):
        from gateway import run as gateway_run
        from gateway import session_context
        from gateway.config import Platform

        adapter._running = True
        runner = MagicMock()
        runner._authorization_adapter.return_value = adapter
        monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
        monkeypatch.setattr(
            session_context,
            "get_session_env",
            lambda name, default="": (
                "otto" if name == "HERMES_SESSION_PROFILE" else default
            ),
        )

        assert _resolve_buzz_event_read_adapter() is adapter
        runner._authorization_adapter.assert_called_once_with(
            Platform("buzz"), profile="otto"
        )

    @pytest.mark.asyncio
    async def test_tool_handler_uses_only_channel_and_event_id(
        self, adapter, monkeypatch
    ):
        adapter.read_event = AsyncMock(
            return_value={
                "id": "a" * 64,
                "pubkey": OTHER_PUBKEY,
                "kind": 9,
                "tags": [["h", CHANNEL]],
                "content": "receipt",
                "channel": CHANNEL,
            }
        )
        monkeypatch.setattr(
            _buzz_mod, "_resolve_buzz_event_read_adapter", lambda: adapter
        )

        rendered = await _handle_buzz_event_read(
            {"channel": CHANNEL, "event_id": "A" * 64}
        )

        assert json.loads(rendered)["id"] == "a" * 64
        adapter.read_event.assert_awaited_once_with(CHANNEL, "A" * 64)

    @pytest.mark.asyncio
    async def test_tool_handler_ignores_dispatch_kwargs_without_leaking_them(
        self, adapter, monkeypatch
    ):
        dispatch_task_id = "task-id-must-not-reach-buzz"
        event = _event("a" * 64, content="canonical receipt", created_at=10)
        adapter._run_cli = AsyncMock(
            return_value=(0, json.dumps([event]), "")
        )
        monkeypatch.setattr(
            _buzz_mod, "_resolve_buzz_event_read_adapter", lambda: adapter
        )

        rendered = await _handle_buzz_event_read(
            {"channel": CHANNEL, "event_id": "a" * 64},
            task_id=dispatch_task_id,
        )

        assert json.loads(rendered)["content"] == "canonical receipt"
        assert dispatch_task_id not in rendered
        assert dispatch_task_id not in json.dumps(adapter._run_cli.await_args.args)


class TestBuzzSendImage:

    @pytest.fixture
    def adapter(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group", "last_ts": 0, "seen": {}
        }
        adapter._running = True
        return adapter

    @pytest.mark.asyncio
    async def test_sends_to_current_buzz_channel_and_returns_native_event_id(
        self, adapter, monkeypatch
    ):
        from gateway import session_context

        monkeypatch.setattr(
            session_context,
            "get_session_env",
            lambda name, default="": {
                "HERMES_SESSION_PLATFORM": "buzz",
                "HERMES_SESSION_CHAT_ID": CHANNEL,
            }.get(name, default),
        )
        monkeypatch.setattr(
            _buzz_mod, "_resolve_buzz_event_read_adapter", lambda: adapter
        )
        adapter.send_image_file = AsyncMock(
            return_value=_buzz_mod.SendResult(
                success=True, message_id="a" * 64
            )
        )

        rendered = await _handle_buzz_send_image(
            {"image_path": "/home/agent/final.png", "caption": "Review"}
        )

        assert json.loads(rendered) == {
            "success": True,
            "message_id": "a" * 64,
        }
        adapter.send_image_file.assert_awaited_once_with(
            CHANNEL, "/home/agent/final.png", caption="Review"
        )

    @pytest.mark.asyncio
    async def test_accepted_send_without_native_event_id_is_unknown(self, adapter, monkeypatch):
        from gateway import session_context

        monkeypatch.setattr(
            session_context,
            "get_session_env",
            lambda name, default="": {
                "HERMES_SESSION_PLATFORM": "buzz",
                "HERMES_SESSION_CHAT_ID": CHANNEL,
            }.get(name, default),
        )
        monkeypatch.setattr(
            _buzz_mod, "_resolve_buzz_event_read_adapter", lambda: adapter
        )
        adapter.send_image_file = AsyncMock(
            return_value=_buzz_mod.SendResult(success=True, message_id=None)
        )

        response = json.loads(
            await _handle_buzz_send_image({"image_path": "/home/agent/final.png"})
        )

        assert response["success"] is False
        assert "delivery outcome is unknown" in response["error"]
        assert "no native event ID" in response["error"]

    @pytest.mark.asyncio
    async def test_relay_failure_preserves_error_and_any_returned_event_id(
        self, adapter, monkeypatch
    ):
        from gateway import session_context

        relay_error = "relay error 422: unsupported media metadata"
        event_id = "b" * 64
        monkeypatch.setattr(
            session_context,
            "get_session_env",
            lambda name, default="": {
                "HERMES_SESSION_PLATFORM": "buzz",
                "HERMES_SESSION_CHAT_ID": CHANNEL,
            }.get(name, default),
        )
        monkeypatch.setattr(
            _buzz_mod, "_resolve_buzz_event_read_adapter", lambda: adapter
        )
        adapter.send_image_file = AsyncMock(
            return_value=_buzz_mod.SendResult(
                success=False, message_id=event_id, error=relay_error
            )
        )

        response = json.loads(
            await _handle_buzz_send_image({"image_path": "/home/agent/final.png"})
        )

        assert response == {
            "success": False,
            "message_id": event_id,
            "error": relay_error,
        }

    @pytest.mark.asyncio
    async def test_rejects_non_buzz_session_without_sending(self, adapter, monkeypatch):
        from gateway import session_context

        monkeypatch.setattr(
            session_context,
            "get_session_env",
            lambda name, default="": {
                "HERMES_SESSION_PLATFORM": "terminal",
                "HERMES_SESSION_CHAT_ID": CHANNEL,
            }.get(name, default),
        )
        monkeypatch.setattr(
            _buzz_mod, "_resolve_buzz_event_read_adapter", lambda: adapter
        )
        adapter.send_image_file = AsyncMock()

        rendered = await _handle_buzz_send_image(
            {"image_path": "/home/agent/final.png"}
        )

        assert json.loads(rendered) == {
            "success": False,
            "error": "buzz_session_unavailable",
        }
        adapter.send_image_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_channel_outside_active_adapter_without_sending(
        self, adapter, monkeypatch
    ):
        from gateway import session_context

        monkeypatch.setattr(
            session_context,
            "get_session_env",
            lambda name, default="": {
                "HERMES_SESSION_PLATFORM": "buzz",
                "HERMES_SESSION_CHAT_ID": "not-watched",
            }.get(name, default),
        )
        monkeypatch.setattr(
            _buzz_mod, "_resolve_buzz_event_read_adapter", lambda: adapter
        )
        adapter.send_image_file = AsyncMock()

        rendered = await _handle_buzz_send_image(
            {"image_path": "/home/agent/final.png"}
        )

        assert json.loads(rendered) == {
            "success": False,
            "error": "buzz_session_unavailable",
        }
        adapter.send_image_file.assert_not_awaited()


class TestBuzzPluginRegistration:

    def test_register_platform_contract(self):
        from gateway.platform_registry import platform_registry

        platform_registry.unregister("buzz")
        ctx = MagicMock()
        register(ctx)
        ctx.register_platform.assert_called_once()
        kwargs = ctx.register_platform.call_args.kwargs
        assert kwargs["name"] == "buzz"
        assert kwargs["cron_deliver_env_var"] == "BUZZ_HOME_CHANNEL"
        assert kwargs["allowed_users_env"] == "BUZZ_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "BUZZ_ALLOW_ALL_USERS"
        assert callable(kwargs["standalone_sender_fn"])
        assert callable(kwargs["env_enablement_fn"])
        assert set(kwargs["required_env"]) == {"BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY"}
        tool_calls = {
            call.kwargs["name"]: call.kwargs
            for call in ctx.register_tool.call_args_list
        }
        assert set(tool_calls) == {"buzz_event_read", "buzz_send_image"}
        read_tool = tool_calls["buzz_event_read"]
        assert read_tool["toolset"] == "buzz"
        assert read_tool["is_async"] is True
        assert set(read_tool["schema"]["parameters"]["properties"]) == {
            "channel",
            "event_id",
        }
        assert set(read_tool["schema"]["parameters"]["required"]) == {
            "channel",
            "event_id",
        }
        assert callable(read_tool["handler"])
        assert callable(read_tool["check_fn"])
        send_tool = tool_calls["buzz_send_image"]
        assert send_tool["toolset"] == "buzz"
        assert send_tool["is_async"] is True
        assert set(send_tool["schema"]["parameters"]["properties"]) == {
            "image_path",
            "caption",
        }
        assert send_tool["schema"]["parameters"]["required"] == ["image_path"]
        assert callable(send_tool["handler"])
        assert callable(send_tool["check_fn"])


class TestStandaloneSend:

    @pytest.mark.asyncio
    async def test_standalone_send_success(self, monkeypatch, tmp_path):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))

        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, input_text=None, timeout=30.0):
            captured.update(cli_path=cli_path, args=args, relay_url=relay_url, input_text=input_text)
            return 0, json.dumps({"accepted": True, "event_id": "evt-cron", "message": ""}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = await _standalone_send(PlatformConfig(enabled=True, extra={}), CHANNEL, "cron says hi")
        assert result == {"success": True, "message_id": "evt-cron"}
        assert captured["args"][:2] == ["messages", "send"]
        assert captured["input_text"] == "cron says hi"
        # The private key must never be part of argv
        assert all("nsec1x" not in str(a) for a in captured["args"])

    @pytest.mark.asyncio
    async def test_standalone_media_tuple_uploads_real_file(self, monkeypatch, tmp_path):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        image = tmp_path / "photo.jpg"
        image.write_bytes(b"image bytes")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        monkeypatch.setattr(
            _buzz_mod.BasePlatformAdapter,
            "validate_media_delivery_path",
            staticmethod(lambda file_path: file_path),
        )
        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, input_text=None, timeout=30.0):
            captured["args"] = args
            captured["input_text"] = input_text
            return 0, json.dumps({"accepted": True, "event_id": "event-file"}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)
        result = await _standalone_send(
            PlatformConfig(enabled=True, extra={}), CHANNEL, "caption",
            media_files=[(str(image), False)],
        )
        assert result == {"success": True, "message_id": "event-file"}
        assert captured["args"][captured["args"].index("--file") + 1] == str(image)
        assert captured["input_text"] == "caption"
