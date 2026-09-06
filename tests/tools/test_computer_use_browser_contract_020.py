"""Behavior coverage for the cua-driver 0.20 public browser contract."""

import copy
import json

import pytest
from typing import Any, Dict
from unittest.mock import Mock

from tools.computer_use.browser_route import CuaTypedBrowserRoute
from tools.computer_use.schema import COMPUTER_USE_SCHEMA
from tools.computer_use.tool import _dispatch


class _Driver:
    def __init__(self, responses: list[Dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, Dict[str, Any]]] = []

    def has_tool(self, _name: str) -> bool:
        return True

    def call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, dict(args)))
        return self.responses.pop(0)


def _route(driver: _Driver) -> CuaTypedBrowserRoute:
    return CuaTypedBrowserRoute(
        session_id="hermes-browser-contract",
        call_tool=driver.call,
        has_tool=driver.has_tool,
    )


def test_public_schema_exposes_020_state_and_type_options():
    properties = COMPUTER_USE_SCHEMA["parameters"]["properties"]

    assert properties["include_screenshot"]["type"] == "boolean"
    assert properties["replace"]["type"] == "boolean"
    assert properties["browser_type_mode"]["enum"] == ["insert_text", "keystrokes"]
    assert "approval_token" not in properties


def test_browser_state_forwards_screenshot_request_and_preserves_mcp_image():
    driver = _Driver([
        {
            "structuredContent": {
                "status": "ok",
                "target_id": "target-a",
                "binding_quality": "exact",
                "mutation_allowed": True,
                "tabs": [{"tab_id": "tab-a"}],
            },
            "images": ["/9j/browser-shot"],
            "image_mime_types": ["image/jpeg"],
        }
    ])

    result = _route(driver).observe(
        pid=101,
        window_id=202,
        include_screenshot=True,
    )

    assert driver.calls == [
        (
            "get_browser_state",
            {
                "pid": 101,
                "window_id": 202,
                "include_screenshot": True,
                "session": "hermes-browser-contract",
            },
        )
    ]
    assert result["_mcp_images"] == [
        {"data": "/9j/browser-shot", "mime_type": "image/jpeg"}
    ]
    assert "screenshot_deferred" not in result


def test_browser_bind_reports_screenshot_deferred_only_when_no_image_returned():
    driver = _Driver([
        {
            "structuredContent": {
                "status": "ok",
                "target_id": "target-a",
                "binding_quality": "exact",
                "mutation_allowed": True,
                "tabs": [{"tab_id": "tab-a"}],
            },
        }
    ])

    result = _route(driver).observe(
        pid=101,
        window_id=202,
        include_screenshot=True,
    )

    assert result["screenshot_deferred"] is True
    assert "_mcp_images" not in result


def test_browser_state_dispatch_returns_mcp_image_as_multimodal_content():
    backend = Mock()
    backend.typed_browser_state.return_value = {
        "status": "ok",
        "url": "https://example.test/",
        "_mcp_images": [{"data": "iVBORbrowser-shot", "mime_type": "image/png"}],
    }

    result = _dispatch(
        backend,
        "cua_browser_state",
        {"tab_id": "tab-a", "include_screenshot": True},
    )

    backend.typed_browser_state.assert_called_once_with(
        tab_id="tab-a", include_screenshot=True
    )
    assert result["_multimodal"] is True
    assert json.loads(result["content"][0]["text"])["url"] == "https://example.test/"
    assert result["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,iVBORbrowser-shot"},
    }
    assert "iVBORbrowser-shot" not in result["text_summary"]


def test_browser_type_replace_reaches_typed_browser_backend():
    backend = Mock()
    backend.typed_browser_action.return_value = {"status": "ok"}

    result = _dispatch(
        backend,
        "cua_browser_type",
        {
            "tab_id": "tab-a",
            "ref": "field-a",
            "text": "replacement",
            "browser_type_mode": "keystrokes",
            "replace": True,
        },
    )

    assert json.loads(result)["status"] == "ok"
    backend.typed_browser_action.assert_called_once_with(
        "browser_type",
        tab_id="tab-a",
        args={
            "ref": "field-a",
            "text": "replacement",
            "replace": True,
            "mode": "keystrokes",
        },
    )


def _preparation():
    # Shape observed from the real 0.23.2 public prepare response; synthetic PID.
    return {
        "status": "ok",
        "prepared": True,
        "prepared_pid": 301,
        "action": "launched_isolated_browser",
        "attachment": None,
        "endpoint_ownership": {
            "detail": "driver-owned profile port file plus live loopback socket owner",
            "method": "spawned_by_driver",
            "owner_pid": 301,
        },
        "side_effects": {"launched_browser": True, "created_profile": True},
    }


def _window():
    return {"pid": 301, "window_id": 401, "is_on_screen": True}


class Driver:
    def __init__(self, preparation=None, discovery=None, missing=None):
        self.preparation = _preparation() if preparation is None else preparation
        self.discovery = {"windows": [_window()]} if discovery is None else discovery
        self.missing = missing
        self.calls = []

    def has_tool(self, name):
        return name != self.missing

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "browser_prepare":
            return {"structuredContent": self.preparation}
        if name == "list_windows":
            if isinstance(self.discovery, Exception):
                raise self.discovery
            return self.discovery
        assert name == "get_browser_state"
        return {
            "structuredContent": {
                "status": "ok",
                "target_id": "target-new",
                "binding_quality": "exact",
                "mutation_allowed": True,
                "tabs": [{"tab_id": "tab-new"}],
            }
        }


def _prepared_route(driver):
    return CuaTypedBrowserRoute(
        session_id="private-session", call_tool=driver.call, has_tool=driver.has_tool
    )


def _prepare(route, mode="isolated_new", **kwargs):
    return route.prepare(
        pid=101,
        window_id=201,
        profile_mode=mode,
        profile_name="test-profile" if mode == "isolated_named" else None,
        allow_launch=True,
        **kwargs,
    )


@pytest.mark.parametrize("mode", ["isolated_new", "isolated_named"])
@pytest.mark.parametrize("envelope", ["data", "structuredContent"])
def test_isolated_prepare_returns_exact_window_then_requires_ordinary_binding(
    mode, envelope
):
    driver = Driver(discovery={envelope: {"windows": [_window()]}})
    route = _prepared_route(driver)
    result = _prepare(route, mode)
    assert result == {**_preparation(), "prepared_window_id": 401}
    assert driver.calls[0][0] == "browser_prepare"
    assert driver.calls[0][1]["pid"] == 101
    assert driver.calls[0][1]["profile"] == (
        {"mode": mode}
        if mode == "isolated_new"
        else {"mode": mode, "name": "test-profile"}
    )
    assert driver.calls[1] == (
        "list_windows",
        {"pid": 301, "on_screen_only": True, "session": "private-session"},
    )
    assert route.state.target_id is None and route.state.refs == {}
    assert (
        route.mutate("browser_navigate", args={"url": "http://127.0.0.1:8123"})["code"]
        == "browser_mutation_unproven"
    )
    observed = route.observe(
        pid=result["prepared_pid"], window_id=result["prepared_window_id"]
    )
    assert observed["snapshot_required"] is True
    assert driver.calls[-1] == (
        "get_browser_state",
        {"pid": 301, "window_id": 401, "session": "private-session"},
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "refused", "code": "not_allowed"},
        {"status": "error", "message": "driver unavailable"},
        {"status": "ok", "isError": True},
        {"status": "ok", "refusal": {"code": "not_allowed"}},
    ],
)
def test_prepare_errors_are_preserved_without_discovery(payload):
    driver = Driver(preparation=payload)
    assert _prepare(_prepared_route(driver)) == payload
    assert [name for name, _ in driver.calls] == ["browser_prepare"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("prepared", None),
        ("prepared", False),
        ("prepared", 1),
        ("prepared_pid", None),
        ("prepared_pid", False),
        ("prepared_pid", "301"),
        ("prepared_pid", 301.0),
        ("prepared_pid", 0),
        ("prepared_pid", -1),
    ],
)
def test_driver_preparation_requires_strict_identity_before_discovery(field, value):
    preparation = _preparation()
    if value is None:
        preparation.pop(field)
    else:
        preparation[field] = value
    driver = Driver(preparation=preparation)
    result = _prepare(_prepared_route(driver))
    assert result["code"] == "browser_prepared_window_unproven"
    assert result["preparation"] == preparation
    assert [name for name, _ in driver.calls] == ["browser_prepare"]


@pytest.mark.parametrize(
    "windows",
    [
        None,
        {},
        [],
        [_window(), _window()],
        [None],
        [{"pid": 301, "window_id": 401}],
        [{**_window(), "pid": "301"}],
        [{**_window(), "pid": 301.0}],
        [{**_window(), "pid": True}],
        [{**_window(), "pid": 999}],
        [{**_window(), "window_id": 0}],
        [{**_window(), "window_id": True}],
        [{**_window(), "window_id": "401"}],
        [{**_window(), "window_id": 401.0}],
        [{**_window(), "is_on_screen": False}],
        [{**_window(), "is_on_screen": 1}],
    ],
)
def test_discovery_refuses_missing_ambiguous_foreign_and_malformed_windows(windows):
    driver = Driver(discovery={"structuredContent": {"windows": windows}})
    route = _prepared_route(driver)
    route.state.target_id = "old-target"
    route.state.refs = {"old-ref": {"click"}}
    result = _prepare(route)
    assert result["code"] == "browser_prepared_window_unproven"
    assert result["preparation"] == _preparation()
    assert "prepared_window_id" not in result
    assert route.state.target_id is None and route.state.refs == {}
    assert [name for name, _ in driver.calls] == ["browser_prepare", "list_windows"]


@pytest.mark.parametrize(
    "discovery",
    [
        {},
        {"isError": True, "structuredContent": {"windows": [_window()]}},
        {"data": {"status": "refused", "windows": [_window()]}},
        {"data": {"code": "window_unavailable", "windows": [_window()]}},
        RuntimeError("private transport error"),
    ],
)
def test_discovery_errors_preserve_launched_preparation_without_retry(discovery):
    driver = Driver(discovery=copy.deepcopy(discovery))
    route = _prepared_route(driver)
    result = _prepare(route)
    assert result["code"] == "browser_prepared_window_unproven"
    assert result["preparation"]["side_effects"]["launched_browser"] is True
    assert "private transport error" not in str(result)
    assert [name for name, _ in driver.calls] == ["browser_prepare", "list_windows"]


def test_discovery_tool_must_exist_before_any_launch():
    driver = Driver(missing="list_windows")
    result = _prepare(_prepared_route(driver))
    assert result["code"] == "typed_browser_unavailable"
    assert driver.calls == []


def test_existing_profile_does_not_require_or_use_discovery():
    driver = Driver(preparation={"status": "ok"}, missing="list_windows")
    assert _prepare(
        _prepared_route(driver), "existing_profile", grant_existing_profile=True
    ) == {"status": "ok"}
    assert [name for name, _ in driver.calls] == ["browser_prepare"]


def test_missing_launch_approval_never_prepares_or_discovers():
    driver = Driver()
    result = _prepared_route(driver).prepare(
        pid=101, profile_mode="isolated_new", allow_launch=False
    )
    assert result["code"] == "browser_launch_not_approved"
    assert driver.calls == []
