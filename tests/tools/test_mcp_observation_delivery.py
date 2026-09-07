"""Durable sink against real private SQLite and synthetic loopback HTTP."""

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest


def observation():
    return dict(agent_run_id="run-1", runtime_owner="hermes:magnus", native_session_id="session-1",
                execution_id="execution-1", invocation_id="invoke-1", tool_name="procurement_mail_document_read",
                intent_id="intent-1", arguments={"ratio": 1e-7}, result={"content": [{"text": "original"}]})


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def endpoint():
    state = SimpleNamespace(calls=[], mode="ack", status=200, accepted=threading.Event(), release=threading.Event(),
                            alter=lambda value: value, before_ack=lambda: None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state.calls.append(body)
            assert self.path == "/internal/hermes/native-observations"
            assert self.headers["Authorization"] == "Bearer synthetic-private-credential"
            state.accepted.set()
            if state.mode == "lose":
                self.close_connection = True
                return
            if state.mode == "wait":
                state.release.wait(5)
            value = json.loads(body)
            identity = json.dumps([value["agent_run_id"], value["invocation_id"]], separators=(",", ":")).encode()
            receipt = dict(observation_id="native:" + hashlib.sha256(identity).hexdigest(),
                           arguments_sha256="sha256:" + "a" * 64, result_sha256="sha256:" + "b" * 64,
                           artifact_sha256="sha256:" + "c" * 64, artifact_ref="oos-native-result:sha256:" + "c" * 64)
            response = state.alter(dict(ok=True, request_sha256=sha(body), receipt=receipt))
            data = json.dumps(response).encode()
            state.before_ack()
            self.send_response(state.status)
            if state.status == 307:
                self.send_header("Location", state.url)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/internal/hermes/native-observations"
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def delivery(endpoint, path, **kwargs):
    from tools.mcp_observation_delivery import NativeObservationDelivery
    return NativeObservationDelivery(endpoint=endpoint.url, token_provider=lambda: "synthetic-private-credential", state_dir=path, **kwargs)


def stored(path):
    with sqlite3.connect(path / "native-observations.sqlite3") as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM native_observations").fetchone())


def test_lost_ack_reopen_replays_exact_bytes_then_clears_body(endpoint, tmp_path):
    state = tmp_path / "delivery"
    endpoint.mode = "lose"
    with pytest.raises(Exception, match="native_observation_delivery_unconfirmed"):
        asyncio.run(delivery(endpoint, state)(observation()))
    row = stored(state)
    assert bytes(row["body"]) == endpoint.calls[0]
    assert row["body_sha256"] == sha(endpoint.calls[0])
    assert row["receipt"] is None
    endpoint.mode = "ack"
    receipt = asyncio.run(delivery(endpoint, state).replay("run-1", "invoke-1"))
    assert endpoint.calls[1] == endpoint.calls[0]
    row = stored(state)
    assert row["body"] is None
    assert json.loads(row["receipt"]) == receipt
    assert asyncio.run(delivery(endpoint, state)(observation())) == receipt
    assert len(endpoint.calls) == 2
    assert b"synthetic-private-credential" not in (state / "native-observations.sqlite3").read_bytes()
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "native-observations.sqlite3").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", [
    lambda ack: {**ack, "request_sha256": "sha256:" + "0" * 64},
    lambda ack: {**ack, "receipt": {**ack["receipt"], "observation_id": "native:foreign"}},
    lambda ack: {**ack, "receipt": {**ack["receipt"], "artifact_ref": "foreign"}},
    lambda ack: {**ack, "receipt": {**ack["receipt"], "arguments_sha256": "bad"}},
    lambda ack: {**ack, "receipt": {**ack["receipt"], "extra": "unknown"}},
    lambda ack: {**ack, "extra": "x" * 65536},
])
def test_invalid_ack_preserves_pending(endpoint, tmp_path, change):
    endpoint.alter = change
    state = tmp_path / "delivery"
    with pytest.raises(Exception, match="native_observation_delivery_unconfirmed"):
        asyncio.run(delivery(endpoint, state)(observation()))
    assert stored(state)["body"] == endpoint.calls[0]
    assert stored(state)["receipt"] is None


@pytest.mark.parametrize("acknowledged", [False, True])
def test_changed_content_or_destination_conflicts_even_after_ack(endpoint, tmp_path, acknowledged):
    from tools.mcp_observation_delivery import NativeObservationDelivery
    state = tmp_path / "delivery"
    client = delivery(endpoint, state)
    if acknowledged:
        asyncio.run(client(observation()))
    else:
        endpoint.mode = "lose"
        with pytest.raises(Exception, match="native_observation_delivery_unconfirmed"):
            asyncio.run(client(observation()))
    with pytest.raises(Exception, match="native_observation_delivery_conflict"):
        asyncio.run(client({**observation(), "result": {"changed": True}}))
    other = NativeObservationDelivery(endpoint=endpoint.url.replace("127.0.0.1", "127.0.0.2"),
                                      token_provider=lambda: "never", state_dir=state)
    with pytest.raises(Exception, match="native_observation_delivery_conflict"):
        asyncio.run(other.replay("run-1", "invoke-1"))
    assert len(endpoint.calls) == 1


def test_cancellation_preserves_durable_request_for_explicit_retry(endpoint, tmp_path):
    state = tmp_path / "delivery"
    endpoint.mode = "wait"

    async def cancelled():
        task = asyncio.create_task(delivery(endpoint, state)(observation()))
        assert await asyncio.to_thread(endpoint.accepted.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled())
    assert stored(state)["body"] == endpoint.calls[0]
    endpoint.mode = "ack"
    endpoint.release.set()
    asyncio.run(delivery(endpoint, state).replay("run-1", "invoke-1"))
    assert endpoint.calls[0] == endpoint.calls[1]


def test_full_image_and_input_snapshot_reach_server_exactly(endpoint, tmp_path):
    value = observation()
    value["result"] = {"content": [{"type": "image", "data": "A" * (6 * 1024 * 1024)}]}
    asyncio.run(delivery(endpoint, tmp_path / "delivery")(value))
    assert json.loads(endpoint.calls[0]) == value


def test_snapshot_is_durable_before_async_token_lookup_and_caller_mutation(endpoint, tmp_path):
    from tools.mcp_observation_delivery import NativeObservationDelivery
    state = tmp_path / "delivery"

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def token():
            entered.set()
            await release.wait()
            return "synthetic-private-credential"

        client = NativeObservationDelivery(endpoint=endpoint.url, token_provider=token, state_dir=state)
        value = observation()
        original = json.loads(json.dumps(value))
        task = asyncio.create_task(client(value))
        await entered.wait()
        assert json.loads(stored(state)["body"]) == original
        assert endpoint.calls == []
        value["arguments"]["ratio"] = 4
        value["result"] = {"changed": True}
        release.set()
        await task
        assert json.loads(endpoint.calls[0]) == original

    asyncio.run(scenario())


def test_real_timeout_keeps_pending_without_automatic_retry(endpoint, tmp_path):
    state = tmp_path / "delivery"
    endpoint.mode = "wait"
    with pytest.raises(Exception, match="native_observation_delivery_unconfirmed"):
        asyncio.run(delivery(endpoint, state, timeout=2)(observation()))
    assert endpoint.accepted.is_set()
    assert len(endpoint.calls) == 1
    assert stored(state)["body"] == endpoint.calls[0]


def test_redirect_is_not_followed_and_proxy_environment_is_ignored(endpoint, tmp_path, monkeypatch):
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]:
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    state = tmp_path / "delivery"
    endpoint.status = 307
    with pytest.raises(Exception, match="native_observation_delivery_unconfirmed"):
        asyncio.run(delivery(endpoint, state)(observation()))
    assert len(endpoint.calls) == 1
    assert stored(state)["body"] is not None
    endpoint.status = 200
    asyncio.run(delivery(endpoint, state).replay("run-1", "invoke-1"))
    assert len(endpoint.calls) == 2


def test_ack_storage_failure_keeps_pending_until_explicit_replay(endpoint, tmp_path):
    state = tmp_path / "delivery"
    client = delivery(endpoint, state)
    database = state / "native-observations.sqlite3"
    endpoint.before_ack = lambda: database.chmod(0o400)
    try:
        with pytest.raises(Exception, match="native_observation_delivery_storage_invalid"):
            asyncio.run(client(observation()))
    finally:
        database.chmod(0o600)
    assert len(endpoint.calls) == 1
    assert stored(state)["body"] == endpoint.calls[0]
    assert stored(state)["receipt"] is None
    endpoint.before_ack = lambda: None
    asyncio.run(delivery(endpoint, state).replay("run-1", "invoke-1"))
    assert endpoint.calls[1] == endpoint.calls[0]
    assert stored(state)["body"] is None


@pytest.mark.parametrize("kind", ["directory_symlink", "directory_mode", "file_symlink", "file_fifo", "file_mode", "corrupt_sqlite"])
def test_invalid_storage_refuses_before_network(endpoint, tmp_path, kind):
    state = tmp_path / "delivery"
    state.mkdir(mode=0o700)
    file = state / "native-observations.sqlite3"
    if kind == "directory_symlink":
        actual = tmp_path / "actual"
        state.rename(actual)
        state.symlink_to(actual, target_is_directory=True)
    elif kind == "directory_mode":
        state.chmod(0o755)
    elif kind == "file_symlink":
        target = tmp_path / "other"
        target.write_bytes(b"")
        file.symlink_to(target)
    elif kind == "file_fifo":
        os.mkfifo(file, 0o600)
    elif kind == "file_mode":
        file.touch(mode=0o644)
    else:
        file.write_bytes(b"not SQLite")
        file.chmod(0o600)
    with pytest.raises(Exception, match="native_observation_delivery_storage_invalid"):
        asyncio.run(delivery(endpoint, state)(observation()))
    assert endpoint.calls == []


def test_pending_body_corruption_is_refused_without_network(endpoint, tmp_path):
    state = tmp_path / "delivery"
    endpoint.mode = "lose"
    with pytest.raises(Exception):
        asyncio.run(delivery(endpoint, state)(observation()))
    with sqlite3.connect(state / "native-observations.sqlite3") as db:
        db.execute("UPDATE native_observations SET body = ?", (b"{}",))
    with pytest.raises(Exception, match="native_observation_delivery_storage_invalid"):
        asyncio.run(delivery(endpoint, state).replay("run-1", "invoke-1"))
    assert len(endpoint.calls) == 1


@pytest.mark.parametrize("url", ["http://localhost/internal/hermes/native-observations", "http://example.com/internal/hermes/native-observations", "https://user:secret@example.com/internal/hermes/native-observations"])
def test_endpoint_is_trusted_explicit_https_or_literal_loopback(tmp_path, url):
    from tools.mcp_observation_delivery import NativeObservationDelivery
    with pytest.raises(Exception, match="native_observation_delivery_config_invalid"):
        NativeObservationDelivery(endpoint=url, token_provider=lambda: "secret", state_dir=tmp_path / "delivery")


def stage_pending(endpoint_url, state, values):
    from tools.mcp_observation_delivery import NativeObservationDelivery, NativeObservationDeliveryError

    def unavailable():
        raise RuntimeError("synthetic credential unavailable")

    client = NativeObservationDelivery(endpoint=endpoint_url, token_provider=unavailable, state_dir=state)

    async def stage():
        for value in values:
            with pytest.raises(NativeObservationDeliveryError, match="delivery_unconfirmed"):
                await client(value)

    asyncio.run(stage())
    return client


def test_pending_restart_discovers_original_binding_for_existing_replay(endpoint, tmp_path):
    from tools.mcp_observation_delivery import NativeObservationDelivery
    state = tmp_path / "delivery"
    stage_pending(endpoint.url, state, [observation()])
    tokens = []
    restarted = NativeObservationDelivery(endpoint=endpoint.url,
        token_provider=lambda: tokens.append(True) or "synthetic-private-credential", state_dir=state)
    before = stored(state)
    page = restarted.pending()
    expected = {key: value for key, value in observation().items() if key not in ("arguments", "result")}
    expected["request_sha256"] = before["body_sha256"]
    assert page == {"observations": [expected], "next_after": None}
    assert stored(state) == before
    assert tokens == [] and endpoint.calls == []
    found = page["observations"][0]
    asyncio.run(restarted.replay(found["agent_run_id"], found["invocation_id"]))
    assert endpoint.calls == [before["body"]]
    assert restarted.pending() == {"observations": [], "next_after": None}
    assert tokens == [True]


def test_pending_lexical_pagination_max_boundary_endpoint_isolation_and_sentinel(endpoint, tmp_path):
    state = tmp_path / "delivery"
    values = [{**observation(), "agent_run_id": f"run-{index // 51}", "invocation_id": f"invoke-{index:03}"}
              for index in range(101)]
    client = stage_pending(endpoint.url, state, list(reversed(values)))
    stage_pending("https://other.example/internal/hermes/native-observations", state,
                  [{**observation(), "agent_run_id": "aaa-other-endpoint"}])
    first = client.pending()
    assert len(first["observations"]) == 100
    assert first["next_after"] == {"agent_run_id": values[99]["agent_run_id"], "invocation_id": values[99]["invocation_id"]}
    second = client.pending(after=first["next_after"], limit=1)
    assert second["next_after"] is None
    all_items = first["observations"] + second["observations"]
    assert [(item["agent_run_id"], item["invocation_id"]) for item in all_items] == [
        (value["agent_run_id"], value["invocation_id"]) for value in values]
    assert client.pending(after={"agent_run_id": "zzz", "invocation_id": "zzz"}) == {
        "observations": [], "next_after": None}
    # Only returned rows are loaded; an unreturned sentinel must not decode its body.
    with sqlite3.connect(state / "native-observations.sqlite3") as db:
        db.execute("UPDATE native_observations SET body=? WHERE agent_run_id=? AND invocation_id=?",
                   (b"{}", values[-1]["agent_run_id"], values[-1]["invocation_id"]))
    assert len(client.pending()["observations"]) == 100
    with pytest.raises(Exception, match="native_observation_delivery_storage_invalid"):
        client.pending(after=first["next_after"])
    assert endpoint.calls == []


@pytest.mark.parametrize("options", [
    {"limit": True}, {"limit": 0}, {"limit": 101}, {"limit": 1.0}, {"limit": "1"},
    {"after": []}, {"after": {}}, {"after": {"agent_run_id": "run-1"}},
    {"after": {"agent_run_id": "run-1", "invocation_id": "invoke-1", "extra": "x"}},
    {"after": {"agent_run_id": "bad\n", "invocation_id": "invoke-1"}},
    {"after": {"agent_run_id": True, "invocation_id": "invoke-1"}},
])
def test_pending_invalid_selectors_refuse_before_storage(endpoint, tmp_path, monkeypatch, options):
    client = delivery(endpoint, tmp_path / "delivery")

    def forbidden():
        pytest.fail("invalid selector opened storage")

    monkeypatch.setattr(client, "_connection", forbidden)
    with pytest.raises(Exception, match="native_observation_delivery_input_invalid"):
        client.pending(**options)
    assert endpoint.calls == []
