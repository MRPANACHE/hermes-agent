"""Native PowerShell prerequisite selection without downloading build tools."""
from pathlib import Path
import json
import os
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.platforms("windows")
HELPER = Path(__file__).resolve().parents[2] / "scripts" / "windows-build-deps.ps1"
ENTRYPOINT = HELPER.parent / "build" / "windows-deps.ps1"


def _powershell(script, *args, env=None):
    env = dict(os.environ) if env is None else env
    env.setdefault("SystemRoot", r"C:\Windows")
    env.setdefault("ComSpec", str(Path(env["SystemRoot"]) / "System32/cmd.exe"))
    env.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    env.setdefault("SystemDrive", Path(env["SystemRoot"]).drive)
    shell = shutil.which("powershell") or str(
        Path(env["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=180,
        check=False,
    )


def _github_environment(path):
    lines = iter(path.read_text(encoding="utf-8").splitlines())
    values = {}
    for line in lines:
        name, delimiter = line.split("<<", 1)
        value = []
        for line in lines:
            if line == delimiter:
                break
            value.append(line)
        else:
            raise AssertionError(f"unterminated GitHub environment value: {name}")
        values[name.upper()] = "\n".join(value)
    return values


def test_entrypoint_exports_child_environment_and_only_github_deltas(tmp_path):
    """The file protocol preserves Unicode, multiline SDK values and PATH order."""
    entry = tmp_path / "scripts" / "build" / ENTRYPOINT.name
    entry.parent.mkdir(parents=True)
    shutil.copyfile(ENTRYPOINT, entry)
    # Replace only the installer boundary; the wrapper runs as a real child.
    (entry.parent.parent / HELPER.name).write_text(r'''
function Initialize-HermesArm64BuildTools {
    param([string]$StateRoot, [string]$OpenSSLRoot)
    if (-not $StateRoot -or -not $OpenSSLRoot) { throw 'missing build roots' }
    if ($env:FAIL_BUILD_SETUP) { throw 'fixture installer failed' }
    $env:INCLUDE = 'sdk include ' + [char]0x03bb + "`nsecond line=with equals"
    $env:LIB = Join-Path $StateRoot 'sdk lib'
    $env:WindowsSdkDir = Join-Path $StateRoot 'SDK'
    $env:OPENSSL_DIR = Join-Path $OpenSSLRoot 'installed\arm64-windows-static-md'
    $env:OPENSSL_STATIC = '1'
    $env:CC_aarch64_pc_windows_msvc = Join-Path $StateRoot 'clang.exe'
    $env:PATH = (Join-Path $StateRoot 'cargo bin') + ';' + (Join-Path $StateRoot 'compiler bin') + ';' + $env:PATH
    $env:GITHUB_FIXTURE = 'must not be published'
    $env:RUNNER_FIXTURE = 'must not be published'
    $env:NODE_OPTIONS = 'must not be published'
}
''', encoding="utf-8")
    env = dict(os.environ)
    # The runner job env may already carry the toolchain variables the fixture sets (a prior
    # workflow step exported them); the delta filter then rightly skips them. This test is about
    # the protocol, so start from a parent that does not have them.
    for name in ("INCLUDE", "LIB", "WINDOWSSDKDIR", "OPENSSL_DIR", "OPENSSL_STATIC",
                 "CC_AARCH64_PC_WINDOWS_MSVC", "GITHUB_FIXTURE", "RUNNER_FIXTURE", "NODE_OPTIONS"):
        for key in [k for k in env if k.upper() == name]:
            del env[key]
    env.update(CARGO_HOME=str(tmp_path / "caller cargo"), RUSTUP_HOME=str(tmp_path / "caller rustup"),
               RUSTUP_TOOLCHAIN="caller-toolchain", UNCHANGED_BUILD_SENTINEL="inherited")
    state = tmp_path / "state with spaces"
    openssl = tmp_path / "common openssl"
    result_file = tmp_path / "environment.json"
    github_env = tmp_path / "github-env"
    github_path = tmp_path / "github-path"
    github_env.write_text("EARLIER_STEP<<end\nkeep\nend\n", encoding="utf-8")
    earlier_path = str(tmp_path / "earlier bin")
    github_path.write_text(earlier_path + "\n", encoding="utf-8")
    result = _powershell(entry, "-StateRoot", state, "-OpenSSLRoot", openssl,
                         "-EnvironmentFile", result_file, "-GithubEnv", github_env,
                         "-GithubPath", github_path, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    child = {key.upper(): value for key, value in json.loads(result_file.read_text(encoding="utf-8")).items()}
    assert child["INCLUDE"] == "sdk include λ\nsecond line=with equals"
    assert child["RUSTUP_TOOLCHAIN"] == env["RUSTUP_TOOLCHAIN"]
    published = _github_environment(github_env)
    assert published["EARLIER_STEP"] == "keep"
    for name in ("CARGO_HOME", "RUSTUP_HOME", "LIB", "INCLUDE", "WINDOWSSDKDIR",
                 "OPENSSL_DIR", "OPENSSL_STATIC", "CC_AARCH64_PC_WINDOWS_MSVC"):
        assert published[name] == child[name]
    for name in ("CARGO_HOME", "RUSTUP_HOME", "UNCHANGED_BUILD_SENTINEL"):
        assert child[name] == env[name]
    assert "UNCHANGED_BUILD_SENTINEL" not in published
    assert not any(name.startswith(("GITHUB_", "RUNNER_")) for name in published)
    assert "NODE_OPTIONS" not in published
    assert "PATH" not in published
    # Reproduce the runner's per-line prepend; inherited PATH is left intact.
    path_lines = github_path.read_text(encoding="utf-8").splitlines()
    assert path_lines[0] == earlier_path
    inherited_path = next(value for key, value in env.items() if key.upper() == "PATH")
    next_path = inherited_path
    for item in path_lines[1:]:
        next_path = item + ";" + next_path
    assert next_path == child["PATH"]

    # Without GITHUB_PATH the complete PATH belongs in the env-file instead.
    env_only = tmp_path / "env-only"
    result = _powershell(entry, "-StateRoot", state, "-OpenSSLRoot", openssl,
                         "-GithubEnv", env_only, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _github_environment(env_only)["PATH"] == child["PATH"]

    failed_file = tmp_path / "failed.json"
    result = _powershell(entry, "-StateRoot", state, "-OpenSSLRoot", openssl,
                         "-EnvironmentFile", failed_file, env={**env, "FAIL_BUILD_SETUP": "1"})
    assert result.returncode != 0
    assert "fixture installer failed" in result.stderr
    assert not failed_file.exists()






