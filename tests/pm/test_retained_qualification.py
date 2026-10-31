"""Retained launch must defer before native restore, not hide a repair with PM argv."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest

from hermes_cli._launchers import runtime_command
from hermes_cli import _early_recovery as recovery
from hermes_cli import _early_recovery_zip as zip_recovery
from pm import environments

if sys.platform != "win32":
    import fcntl


class Relaunch(BaseException):
    pass


@pytest.fixture
def retained_fixture(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[2] / "hermes_bootstrap.py", root / "hermes_bootstrap.py")
    owner = tmp_path / "managed"
    disposable = tmp_path / "disposable"
    disposable.mkdir()
    monkeypatch.setenv("HOME", str(disposable))
    monkeypatch.setenv("HERMES_HOME", str(disposable / ".hermes"))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("__HERMES_ACTIVATED", raising=False)
    state = owner / "installs" / environments.install_key(root)
    generation = state / "environments" / "fixture" / "venv"
    packages = environments.site_packages(generation)
    packages.mkdir(parents=True)
    (generation / "pyvenv.cfg").write_text(f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n")
    (state / ".install.lock").write_text("stable-lock\n")
    (state / "facts.json").write_text(json.dumps({"packages": {"venv": {"environment": str(generation)}}}))
    tools = owner / "tools"
    (tools / "python/bin").mkdir(parents=True)
    (tools / "python/bin/python3").symlink_to(sys.executable)
    (tools / "facts.json").write_text(json.dumps({"packages": {"python": {"entry": "python"}}}))
    for path in environments.ACTIVATION_INPUTS:
        target = root / path
        target.parent.mkdir(exist_ok=True)
        target.write_text("fixture input\n")
    environments.record_activation_inputs(state / "inputs", environments.activation_input_mtimes(root), root, test_environment=False)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost",
                 "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost"}).strip()
    git("init", "-q")
    (root / ".git/info/exclude").write_text("install-stamp.json\n__pycache__/\n")
    git("add", ".")
    git("commit", "-qm", "synthetic retained launch fixture")
    commit = git("rev-parse", "HEAD")
    (root / "install-stamp.json").write_text(json.dumps({"commit": commit}))
    from hermes_cli.update_lock import checkout_lock_path
    checkout_lock_path(root).write_text("stable-checkout-lock\n")
    monkeypatch.setattr(sys, "argv", ["fixture", "pm"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "hermes_bootstrap", raising=False)
    monkeypatch.setattr(recovery, "relaunch_after_restore", Mock(side_effect=Relaunch()))
    intent = {"owner_home": str(owner), "source_commit": commit}
    yield root, owner, disposable, state, generation, intent
    active = getattr(sys, "_hermes_retained_runtime", None)
    if active is not None:
        active.close()
    for key in ("_hermes_retained_launch", "_hermes_retained_runtime"):
        monkeypatch.delattr(sys, key, raising=False)
    monkeypatch.delitem(sys.modules, "hermes_bootstrap", raising=False)


def execute(fixture, monkeypatch):
    root, _owner, disposable, _state, _generation, intent = fixture
    # The real generated expression imports the actual bootstrap copied into root.
    monkeypatch.setattr(sys, "_hermes_retained_launch", intent, raising=False)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    command = runtime_command(root, ("pm",), code="retained_helper.append('reached')", home=disposable / ".hermes")
    reached = []
    exec(command[3], {"retained_helper": reached})
    return reached


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def test_retained_native_zip_restore_is_deferred_before_mutation(retained_fixture, monkeypatch):
    root, _owner, _disposable, _state, _generation, _intent = retained_fixture
    (root / "alpha").write_text("NEW")
    (root / "alpha.hermes-update-old").write_text("OLD")
    zip_recovery.write_zip_swap_journal(root, "swapping", [["alpha", True,
        zip_recovery.zip_entry_identity(root / "alpha"), zip_recovery.zip_entry_identity(root / "alpha.hermes-update-old")]], "0123456789ab")
    before = snapshot(root)
    try:
        execute(retained_fixture, monkeypatch)
    except (SystemExit, Relaunch):
        pass
    assert snapshot(root) == before
    recovery.relaunch_after_restore.assert_not_called()


@pytest.mark.platforms("linux", "macos")
def test_healthy_retained_native_launch_loads_borrowed_generation_without_state_writes(retained_fixture, monkeypatch):
    _root, owner, disposable, _state, generation, _intent = retained_fixture
    before = snapshot(owner)
    assert execute(retained_fixture, monkeypatch) == ["reached"]
    assert str(environments.site_packages(generation)) in sys.path
    assert not (disposable / ".hermes/installs").exists()
    assert snapshot(owner) == before


@pytest.mark.parametrize("marker", ["publication.json", "source-completion-pending", ".repair-incomplete"])
def test_retained_pending_state_is_deferred(retained_fixture, monkeypatch, marker):
    _root, owner, _disposable, state, _generation, _intent = retained_fixture
    (state / marker).write_text("unsettled")
    before = snapshot(owner)
    with pytest.raises(SystemExit, match="retained launch deferred"):
        execute(retained_fixture, monkeypatch)
    assert snapshot(owner) == before


@pytest.mark.parametrize("fault", ["stale_input", "missing_environment", "missing_store", "wrong_commit", "missing_lock"])
def test_retained_invalid_binding_is_deferred(retained_fixture, monkeypatch, fault):
    root, owner, _disposable, state, generation, intent = retained_fixture
    if fault == "stale_input":
        target = root / "uv.lock"
        os.utime(target, ns=(1, 1))
    elif fault == "missing_environment":
        shutil.rmtree(generation)
    elif fault == "missing_store":
        (owner / "tools/facts.json").unlink()
    elif fault == "wrong_commit":
        intent["source_commit"] = "0" * 40
    else:
        (state / ".install.lock").unlink()
    before = snapshot(owner)
    with pytest.raises(SystemExit, match="retained launch deferred"):
        execute(retained_fixture, monkeypatch)
    assert snapshot(owner) == before


@pytest.mark.parametrize("which", ["checkout", "runtime"])
@pytest.mark.platforms("linux", "macos")
def test_retained_lock_conflict_never_mutates_existing_locks(retained_fixture, monkeypatch, which):
    from hermes_cli.update_lock import checkout_lock_path
    root, owner, _disposable, state, _generation, _intent = retained_fixture
    path = checkout_lock_path(root) if which == "checkout" else state / ".install.lock"
    before = snapshot(owner)
    with path.open("r") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="retained launch deferred"):
            execute(retained_fixture, monkeypatch)
    assert snapshot(owner) == before
    assert path.read_text().startswith("stable-")


@pytest.mark.platforms("linux", "macos")
def test_generated_retained_api_pins_existing_native_locks_in_surviving_child(retained_fixture, monkeypatch):
    from hermes_cli.update_lock import checkout_lock_path
    root, _owner, disposable, state, _generation, intent = retained_fixture
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(sys, "_hermes_retained_launch", None, raising=False)
    command = runtime_command(root, ("pm",), code="reached.append(True)",
        home=disposable / ".hermes", retained=intent)
    reached = []
    exec(command[3], {"reached": reached})
    assert reached == [True]
    retained = vars(sys)["_hermes_retained_runtime"]
    with subprocess.Popen([sys.executable, "-I", "-c", "import sys; print('ready',flush=True); sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            pass_fds=retained.custody_fds, env={"HOME": str(disposable), "PATH": os.environ["PATH"]}) as child:
        try:
            assert child.stdout is not None and child.stdin is not None
            assert child.stdout.readline().strip() == "ready"
            retained.close()
            for path in (checkout_lock_path(root), state / ".install.lock"):
                with path.open("r") as probe:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if child.stdin is not None:
                child.stdin.close()
            child.wait(timeout=10)
    for path in (checkout_lock_path(root), state / ".install.lock"):
        with path.open("r") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("fault", ["malformed_intent", "production_home", "git_restore", "git_operation", "root_repair"])
def test_retained_native_negative_bindings_fail_before_helper(retained_fixture, monkeypatch, fault):
    root, owner, _disposable, _state, _generation, intent = retained_fixture
    if fault == "malformed_intent": intent["extra"] = True
    elif fault == "production_home": monkeypatch.setenv("HERMES_HOME", str(owner))
    elif fault == "git_restore": recovery.interrupted_pull_marker(root).write_text("unsettled")
    elif fault == "git_operation": (root / ".git/MERGE_HEAD").write_text("1" * 40)
    else: (root / ".update-incomplete").write_text("needs repair")
    before = snapshot(owner)
    with pytest.raises(SystemExit, match="retained launch deferred"):
        execute(retained_fixture, monkeypatch)
    assert snapshot(owner) == before


@pytest.mark.platforms("windows")
def test_retained_windows_is_explicitly_deferred_without_writes(retained_fixture, monkeypatch):
    before = snapshot(retained_fixture[1])
    with pytest.raises(SystemExit, match="retained.*POSIX"):
        execute(retained_fixture, monkeypatch)
    assert snapshot(retained_fixture[1]) == before
