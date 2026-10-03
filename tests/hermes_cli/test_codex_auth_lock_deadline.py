"""Codex reads share the deadline of the serialized OAuth refresh transaction."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
from pathlib import Path

import pytest

from hermes_cli import auth


@pytest.fixture
def codex_store(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "codex-profile"))
    auth._save_codex_tokens({"access_token": "inert-old", "refresh_token": "inert-refresh"})
    return auth._auth_file_path()


@pytest.mark.parametrize("configured, expected", [(None, 25.0), (40, 45.0), (2, 15.0)])
def test_initial_read_uses_refresh_transaction_deadline(codex_store, monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("HERMES_CODEX_REFRESH_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("HERMES_CODEX_REFRESH_TIMEOUT_SECONDS", str(configured))
    deadlines = []

    @contextmanager
    def capture_lock(timeout_seconds=auth.AUTH_LOCK_TIMEOUT_SECONDS, **kwargs):
        deadlines.append(timeout_seconds)
        yield

    monkeypatch.setattr(auth, "_auth_store_lock", capture_lock)
    assert auth._read_codex_tokens()["tokens"]["access_token"] == "inert-old"
    assert deadlines == [expected]


@pytest.mark.linux_only
def test_concurrent_resolvers_wait_for_one_slow_refresh(codex_store, monkeypatch):
    # Scale the old read deadline to one second; use actual flock, persistence,
    # and the in-lock reread. The HTTP operation alone is replaced.
    original_lock = auth._auth_store_lock
    monkeypatch.setattr(auth, "AUTH_LOCK_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setenv("HERMES_CODEX_REFRESH_TIMEOUT_SECONDS", "1")

    @contextmanager
    def scaled_lock(timeout_seconds=None, **kwargs):
        with original_lock(timeout_seconds=1.0 if timeout_seconds is None else timeout_seconds, **kwargs):
            yield

    monkeypatch.setattr(auth, "_auth_store_lock", scaled_lock)
    monkeypatch.setattr(auth, "_codex_access_token_is_expiring", lambda token, skew: token == "inert-old")
    refreshing = threading.Event()
    release = threading.Event()
    refresh_calls = []

    def slow_refresh(access_token, refresh_token, *, timeout_seconds):
        refresh_calls.append((access_token, refresh_token))
        refreshing.set()
        assert release.wait(8)
        return {"access_token": "inert-new", "refresh_token": "inert-next"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", slow_refresh)
    timer = None
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            owner = workers.submit(auth.resolve_codex_runtime_credentials)
            assert refreshing.wait(5)
            waiter = workers.submit(auth.resolve_codex_runtime_credentials)
            timer = threading.Timer(2.0, release.set)
            timer.start()
            assert owner.result(timeout=8)["api_key"] == "inert-new"
            assert waiter.result(timeout=8)["api_key"] == "inert-new"
    finally:
        release.set()
        if timer is not None:
            timer.cancel()
            timer.join()
    assert refresh_calls == [("inert-old", "inert-refresh")]
    assert auth._read_codex_tokens()["tokens"]["refresh_token"] == "inert-next"


def test_failed_refresh_preserves_store_and_releases_lock(codex_store, monkeypatch):
    before = codex_store.read_bytes()
    monkeypatch.setattr(auth, "_codex_access_token_is_expiring", lambda token, skew: True)

    def failed_refresh(*args, **kwargs):
        raise auth.AuthError("inert unavailable endpoint", provider="openai-codex", code="codex_refresh_failed", relogin_required=False)

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", failed_refresh)
    with pytest.raises(auth.AuthError, match="inert unavailable endpoint"):
        auth.resolve_codex_runtime_credentials()
    assert codex_store.read_bytes() == before
    assert auth._read_codex_tokens()["tokens"]["refresh_token"] == "inert-refresh"


def test_in_transaction_read_does_not_acquire_another_lock(codex_store, monkeypatch):
    def unexpected_lock(*args, **kwargs):
        pytest.fail("in-transaction read must retain the caller's lock")

    monkeypatch.setattr(auth, "_auth_store_lock", unexpected_lock)
    assert auth._read_codex_tokens(_lock=False)["tokens"]["access_token"] == "inert-old"
