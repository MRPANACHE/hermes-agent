"""Stdlib-only retained launch binding, before any native repair can mutate an install.

The existing checkout and install locks stay held for the helper's lifetime.
That pins the selected generation without creating/pruning managed lease files.
Only disposable child state is writable; uncertain custody is a deferred launch.
"""
from __future__ import annotations

import atexit
from contextlib import ExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from pm.environments import (
    ACTIVATION_INPUTS, _recorded_venv, install_key, site_packages,
)


@dataclass
class RetainedRuntime:
    root: Path
    environment: Path
    custody: ExitStack
    custody_fds: tuple[int, ...]
    closed: bool = False

    def close(self) -> None:
        atexit.unregister(self.close)
        self.custody.close()
        self.closed = True


def _source_binding(root: Path, commit: str) -> None:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], env=env,
                                capture_output=True, text=True, timeout=30, check=True)
        return result.stdout.strip()
    if git("rev-parse", "HEAD") != commit or git("status", "--porcelain"):
        raise RuntimeError("retained source is not the exact clean commit")
    if json.loads((root / "install-stamp.json").read_text())["commit"] != commit:
        raise RuntimeError("retained install stamp does not match its source")


def _current_inputs(root: Path, state: Path) -> None:
    stamps = state / "inputs"
    if (stamps / ".project-root").read_text() != str(root) or (stamps / ".test-environment").exists():
        raise RuntimeError("retained input stamps belong to another environment")
    for name in ACTIVATION_INPUTS:
        source, stamp = root / name, stamps / name
        if source.is_file() != stamp.is_file():
            raise RuntimeError("retained dependency input set changed")
        if source.is_file() and source.stat().st_mtime_ns != stamp.stat().st_mtime_ns:
            raise RuntimeError("retained dependency inputs need synchronization")
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError("retained dependency input binding is missing")


def retain_for_launch(project_root: Path, intent: dict) -> RetainedRuntime:
    """Validate under native custody and retain one committed environment, without repair."""
    if not isinstance(intent, dict) or set(intent) != {"owner_home", "source_commit"}:
        raise ValueError("invalid retained launch intent")
    owner, commit = intent["owner_home"], intent["source_commit"]
    if (not isinstance(owner, str) or not Path(owner).is_absolute()
            or str(Path(owner).resolve()) != owner
            or not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit)):
        raise ValueError("invalid exact retained source/home binding")
    root, owner = Path(project_root).resolve(), Path(owner)
    home = Path(os.environ["HERMES_HOME"]).resolve()
    user = Path(os.environ["HOME"]).resolve()
    if home == owner or home.is_relative_to(owner) or owner.is_relative_to(home) or owner.is_relative_to(user):
        raise RuntimeError("retained launch needs an independent disposable home")
    from hermes_cli import update_lock
    from hermes_cli._early_recovery import dependency_repair_markers, require_settled_checkout
    from hermes_cli.runtime_state import recover_publication, runtime_lock
    from hermes_cli._launchers import _store_python

    custody = ExitStack()
    try:
        if update_lock._HELD is not None and not update_lock.holds_checkout_lock(root):
            raise RuntimeError("another checkout already owns process custody")
        holder = update_lock._acquire_checkout(root, read_only=True)
        if holder is not None:
            raise RuntimeError(update_lock.describe_holder(holder))
        custody.callback(update_lock._release_checkout)
        require_settled_checkout(root)
        state = owner / "installs" / install_key(root)
        if state.resolve() != state:
            raise RuntimeError("retained install state is not its canonical source binding")
        runtime_fds = []
        if not custody.enter_context(runtime_lock(root, state=state, read_only=True, timeout=0, custody=runtime_fds)):
            raise RuntimeError("retained dependency writer still owns the install lock")
        recover_publication(root, state=state, read_only=True)
        from hermes_cli.venv_sync import completion_pending_path
        pending = state / completion_pending_path(root).name
        if any(p.exists() or p.is_symlink() for p in (pending, *dependency_repair_markers(root, state))):
            raise RuntimeError("retained source completion or dependency repair is pending")
        _source_binding(root, commit)
        _current_inputs(root, state)
        python = _store_python(owner / "tools")
        if python is None or python.resolve() != Path(sys.executable).resolve():
            raise RuntimeError("retained store interpreter binding is unavailable")
        environment = _recorded_venv(root, state=state)
        if (environment is None or not environment.is_relative_to(state / "environments")
                or not site_packages(environment).is_dir()):
            raise RuntimeError("retained committed dependency generation is unavailable")
        fds = (*update_lock.checkout_lock_fds(root), *runtime_fds)
        retained = RetainedRuntime(root, environment, custody.pop_all(), fds)
        atexit.register(retained.close)
        return retained
    finally:
        custody.close()
