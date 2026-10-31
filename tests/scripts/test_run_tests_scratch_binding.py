"""Disposable qualification must bind the real shell/runner before mutations."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.platforms("posix")


def context(tmp_path):
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    repo = root / "stage"
    scripts = repo / "scripts"
    (scripts / "ci").mkdir(parents=True)
    for name in ("run_tests.sh", "run_tests_parallel.py", "_activation.sh", "run_tests_scratch.py"):
        source = SOURCE / "scripts" / name
        if source.exists():
            shutil.copy2(source, scripts / name)
    shutil.copy2(SOURCE / "scripts/ci/list_os_marked_tests.py", scripts / "ci")
    (root / "user/.hermes").mkdir(parents=True)
    (root / "tmp").mkdir(mode=0o700)
    protected = tmp_path / "managed"
    protected.mkdir()
    (protected / "sentinel").write_text("unchanged", encoding="utf-8")
    identity = root.stat()
    body = {"schema": "hermes.test-scratch.v1", "run_root": str(root),
            "device": identity.st_dev, "inode": identity.st_ino,
            "protected_roots": [str(protected)]}
    binding = root / "user/.hermes/runner-binding.json"
    binding.write_text(json.dumps(body), encoding="utf-8")
    binding.chmod(0o600)
    env = {**os.environ, "HOME": str(root / "user"),
           "HERMES_HOME": str(root / "user/.hermes"), "TMPDIR": str(root / "tmp"),
           "HERMES_PYTHON": sys.executable, "PYTHONDONTWRITEBYTECODE": "1",
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    for name in ("__HERMES_ACTIVATED", "__HERMES_TEST_PYTHON", "PYTHONPATH", "PYTHONPYCACHEPREFIX"):
        env.pop(name, None)
    return root, repo, protected, binding, body, env


def invoke(repo, binding, env, *, shell=True, extra=()):
    entry = ["bash", str(repo / "scripts/run_tests.sh")] if shell else [
        sys.executable, "-B", str(repo / "scripts/run_tests_parallel.py")]
    return subprocess.run([*entry, "--scratch-binding", str(binding), *extra],
                          env=env, cwd=repo, capture_output=True, text=True,
                          timeout=60, check=False)


def load_scratch():
    spec = importlib.util.spec_from_file_location("scratch_subject", SOURCE / "scripts/run_tests_scratch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_shell_passes_bound_env_and_cleans_only_own_attempt(tmp_path):
    root, repo, protected, binding, _, env = context(tmp_path)
    case = repo / "test_bound.py"
    marker = root / "observed.json"
    case.write_text(
        "import json, os, tempfile\nfrom pathlib import Path\n"
        "def test_bound(tmp_path):\n"
        "    values = {k: os.environ[k] for k in ('HOME', 'HERMES_HOME', 'TMPDIR')}\n"
        "    values['basetemp'] = str(tmp_path)\n"
        "    values['tempfile'] = tempfile.gettempdir()\n"
        f"    Path({str(marker)!r}).write_text(json.dumps(values))\n"
        "    readonly = tmp_path / 'readonly'\n    readonly.mkdir()\n"
        "    (readonly / 'data').write_text('owned')\n    readonly.chmod(0o500)\n"
        f"    (tmp_path / 'external').symlink_to({str(protected)!r}, target_is_directory=True)\n",
        encoding="utf-8")
    # A stale owned sibling and an external symlink are not the current attempt.
    owned = root / "tmp/pytest/hermes-test-stale"
    owned.mkdir(parents=True)
    (owned / "sentinel").write_text("keep", encoding="utf-8")
    os.utime(owned, (1, 1))
    (root / "tmp/pytest/hermes-test-link").symlink_to(protected, target_is_directory=True)
    protected.chmod(0o500)
    result = invoke(repo, binding, env, extra=(str(case), "-j", "1", "--file-retries", "0"))
    assert result.returncode == 0, result.stdout + result.stderr
    values = json.loads(marker.read_text(encoding="utf-8"))
    assert values["HOME"] == env["HOME"]
    assert values["HERMES_HOME"] == env["HERMES_HOME"]
    for name in ("TMPDIR", "basetemp", "tempfile"):
        assert Path(values[name]).is_relative_to(root / "tmp/pytest")
        assert not Path(values[name]).exists()  # this attempt, not another run
    assert (owned / "sentinel").read_text(encoding="utf-8") == "keep"
    assert (protected / "sentinel").read_text(encoding="utf-8") == "unchanged"
    assert protected.stat().st_mode & 0o777 == 0o500
    assert (root / "tmp/pytest/hermes-test-link").is_symlink()
    assert not list(repo.rglob("__pycache__"))  # no shell precompile before admission


@pytest.mark.parametrize("kind", ["missing", "json", "identity", "protected", "empty_protected",
                                  "root_alias", "binding_link", "tmp_link", "pytest_link", "home",
                                  "hermes_home", "tmp_env", "writable"])
@pytest.mark.parametrize("shell", [True, False])
def test_invalid_binding_refuses_before_write(tmp_path, kind, shell):
    root, repo, protected, binding, body, env = context(tmp_path)
    if kind == "missing":
        binding.unlink()
    elif kind == "json":
        binding.write_text("not json", encoding="utf-8")
    elif kind == "identity":
        body["inode"] += 1
    elif kind == "protected":
        body["protected_roots"] = [str(root)]
    elif kind == "empty_protected":
        body["protected_roots"] = []
    elif kind == "root_alias":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        body["run_root"] = str(alias)
    elif kind == "binding_link":
        target = protected / "binding"
        shutil.move(binding, target)
        binding.symlink_to(target)
    elif kind in ("tmp_link", "pytest_link"):
        target = root / ("tmp" if kind == "tmp_link" else "tmp/pytest")
        if target.exists():
            target.rmdir()
        target.symlink_to(protected, target_is_directory=True)
    elif kind in ("home", "hermes_home", "tmp_env"):
        env[{"home": "HOME", "hermes_home": "HERMES_HOME", "tmp_env": "TMPDIR"}[kind]] = str(protected)
    elif kind == "writable":
        binding.chmod(0o666)
    if kind in ("identity", "protected", "empty_protected", "root_alias"):
        binding.write_text(json.dumps(body), encoding="utf-8")
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    result = invoke(repo, binding, env, shell=shell, extra=("--check-scratch-binding",))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "scratch binding" in result.stderr.lower()
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before
    assert (protected / "sentinel").read_text(encoding="utf-8") == "unchanged"


def test_missing_explicit_binding_does_not_fall_back(tmp_path):
    _, repo, _, _, _, env = context(tmp_path)
    result = subprocess.run(["bash", str(repo / "scripts/run_tests.sh"), "--help"],
                            env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2
    assert "scratch binding" in result.stderr.lower()
    assert not list(repo.rglob("__pycache__"))


def test_cleanup_refuses_late_symlink_and_binding_mutation(tmp_path, monkeypatch):
    root, repo, protected, binding, body, env = context(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    scratch = load_scratch().BoundScratch(binding, repo)
    attempt = Path(scratch.new_attempt())
    attempt.rmdir()
    attempt.symlink_to(protected, target_is_directory=True)
    with pytest.raises(ValueError, match="scratch binding"):
        scratch.cleanup(attempt)
    assert (protected / "sentinel").exists()
    attempt.unlink()
    body["inode"] += 1
    binding.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match="scratch binding"):
        scratch.new_attempt()
    assert not list((root / "tmp/pytest").iterdir())
