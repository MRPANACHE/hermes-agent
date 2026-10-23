"""Live gateway identity for the published source-install Python launcher."""

import importlib
import json

from gateway import status


def _launcher_argv(*args):
    launchers = importlib.import_module("hermes_cli._launchers")
    repo = status.Path(status.__file__).resolve().parents[1]
    return ["/store/bin/python3", "-I", "-c", launchers._launcher_script("hermes", repo, None), *args]


def _record(home, *, pid=4242, start=1000):
    return {
        "pid": pid, "kind": "hermes-gateway", "gateway_state": "running",
        "argv": ["-c", "--profile", "bram", "gateway", "run"],
        "hermes_home": str(home), "start_time": start,
    }


def _live(monkeypatch, argv, *, start=1000):
    monkeypatch.setattr(status, "_pid_exists", lambda pid: pid == 4242)
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: start)
    monkeypatch.setattr(status, "_read_process_cmdline", lambda pid: " ".join(argv))
    monkeypatch.setattr(status, "_read_process_argv", lambda pid: argv)


def test_managed_launcher_liveness_stays_scoped_across_two_homes(tmp_path, monkeypatch):
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    argv = _launcher_argv("--profile", "bram", "gateway", "run")
    _live(monkeypatch, argv)
    assert status.looks_like_gateway_runtime_command_line(" ".join(argv)) is False

    for root in (root_a, root_b, root_a):
        monkeypatch.setenv("HERMES_HOME", str(root))
        home = root / "profiles" / "bram"
        result = status.resolve_gateway_liveness(
            profile_dir=home, runtime=_record(home),
            pid_probe=lambda path: None, health_probe=None, use_cache=False,
        )
        assert (result.running, result.pid, result.source) == (True, 4242, "runtime_status")

    copied = _record(root_a / "profiles" / "bram")
    assert status.get_runtime_status_running_pid(copied, expected_home=root_b / "profiles" / "bram") is None
    assert status.get_runtime_status_running_pid(_record(root_a), expected_home=root_a) is None


def test_managed_launcher_rejects_unrelated_inline_source_and_recycled_pid(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "bram"
    real_argv = _launcher_argv("--profile", "bram", "gateway", "run")
    record = _record(home)

    for argv in (
        ["python3", "-I", "-c", "print('gateway run')", "--profile", "bram", "gateway", "run"],
        [*real_argv[:3], real_argv[3] + "\n# altered", *real_argv[4:]],
        _launcher_argv("--profile", "milo", "gateway", "run"),
        _launcher_argv("--profile", "bram", "gateway", "status"),
    ):
        _live(monkeypatch, argv)
        assert status.get_runtime_status_running_pid(record, expected_home=home) is None

    _live(monkeypatch, real_argv, start=1001)
    assert status.get_runtime_status_running_pid(record, expected_home=home) is None

    _live(monkeypatch, real_argv)
    monkeypatch.setattr(status, "_read_process_argv", lambda pid: None)
    assert status.get_runtime_status_running_pid(record, expected_home=home) is None


def test_managed_launcher_is_seen_through_the_locked_pid_rung(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "bram"
    home.mkdir(parents=True)
    record = _record(home)
    (home / "gateway.pid").write_text(json.dumps(record), encoding="utf-8")
    (home / "gateway.lock").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(status, "is_gateway_runtime_lock_active", lambda lock: True)
    _live(monkeypatch, _launcher_argv("--profile", "bram", "gateway", "run"))

    result = status.resolve_gateway_liveness(profile_dir=home, use_cache=False)
    assert (result.running, result.pid, result.source) == (True, 4242, "pid")

    _live(monkeypatch, _launcher_argv("--profile", "milo", "gateway", "run"))
    assert status.get_running_pid(home / "gateway.pid", cleanup_stale=False) is None
    assert (home / "gateway.pid").exists()


def test_managed_launcher_keeps_its_scoped_lock_live(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "bram"
    record = _record(home)
    monkeypatch.setattr(status, "_process_is_stopped", lambda pid: False)
    _live(monkeypatch, _launcher_argv("--profile", "bram", "gateway", "run"))
    assert status._scoped_lock_record_is_stale(record, 4242) is False

    _live(monkeypatch, ["python3", "-I", "-c", "pass", "gateway", "run"])
    assert status._scoped_lock_record_is_stale(record, 4242) is True


def test_proc_argv_keeps_inline_source_as_one_argument(tmp_path, monkeypatch):
    argv = _launcher_argv("--profile", "bram", "gateway", "run")
    proc = tmp_path / "cmdline"
    proc.write_bytes(b"\x00".join(part.encode() for part in argv) + b"\x00")
    original_path = status.Path
    monkeypatch.setattr(status, "Path", lambda path: proc if path == "/proc/4242/cmdline" else original_path(path))
    assert status._read_process_argv(4242) == argv
