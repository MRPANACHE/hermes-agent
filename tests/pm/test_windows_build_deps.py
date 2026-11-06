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




def test_openssl_installs_once_and_rejects_damaged_shared_install(tmp_path):
    script = tmp_path / "check.ps1"
    script.write_text(r'''
param([string]$Helper, [string]$Root)
$ErrorActionPreference = 'Stop'
. $Helper
$prefix = Join-Path $Root 'installed\arm64-windows-static-md'
function Invoke-HermesBuildCommand {
    param([string]$Command, [string[]]$Arguments)
    if ($Arguments[0] -ne 'install' -or $Arguments[1] -ne 'openssl:arm64-windows-static-md') {
        throw 'incorrect installation request'
    }
    if ($Arguments -notcontains '--classic') { throw 'manifest mode was not disabled' }
    $script:calls += 1
    if ($script:calls -gt 1) { return } # vcpkg trusts its installed database on subsequent requests.
    New-Item -ItemType Directory -Force (Join-Path $prefix 'lib'), (Join-Path $prefix 'include\openssl') | Out-Null
    foreach ($relative in @('lib\libcrypto.lib', 'lib\libssl.lib', 'include\openssl\ssl.h')) {
        [IO.File]::WriteAllText((Join-Path $prefix $relative), 'fixture')
    }
}
$calls = 0
$first = Install-HermesArm64OpenSSL -Vcpkg 'fixture-vcpkg' -Root $Root
$second = Install-HermesArm64OpenSSL -Vcpkg 'fixture-vcpkg' -Root $Root
if ($calls -ne 1 -or $first -ne $prefix -or $second -ne $prefix) { throw 'warm setup installed twice' }
Remove-Item (Join-Path $prefix 'include\openssl\ssl.h')
$rejected = $false
try { Install-HermesArm64OpenSSL -Vcpkg 'fixture-vcpkg' -Root $Root } catch {
    if ($_.Exception.Message -notmatch 'installation is damaged') { throw }
    $rejected = $true
}
if (-not $rejected -or $calls -ne 2) { throw 'damaged install was accepted' }
Write-Output 'PASS'
''', encoding="utf-8")
    # Real PowerShell child: routed through _powershell like the rest of this
    # file. Its 180s budget exists because a real powershell.exe on a cold
    # CI runner can exceed 30s (330ff28d44); these inline copies kept the old
    # 30s timeout and timed out on cold ARM64 runners.
    result = _powershell(script, HELPER, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_native_build_command_preserves_failures_and_spaces(tmp_path):
    script = tmp_path / "native.ps1"
    script.write_text(r'''
param([string]$Helper, [string]$Root)
$ErrorActionPreference = 'Stop'
. $Helper
$command = Join-Path $Root 'child with spaces.cmd'
[IO.File]::WriteAllText($command, "@echo off`r`necho native-progress 1>&2`r`nexit /b 19`r`n")
$failed = $false
try { Invoke-HermesBuildCommand $command @() } catch {
    if ($_.Exception.Message -notmatch 'exit code 19') { throw }
    $failed = $true
}
if (-not $failed -or $ErrorActionPreference -ne 'Stop') { throw 'failure or shell preference was lost' }
[IO.File]::WriteAllText($command, "@echo off`r`necho native-progress 1>&2`r`nexit /b 0`r`n")
Invoke-HermesBuildCommand $command @()
$failed = $false
try { Invoke-HermesBuildCommand (Join-Path $Root 'absent.exe') @() } catch { $failed = $true }
if (-not $failed) { throw 'missing executable was accepted after a successful command' }
$a = Join-Path $Root 'cmd'
$b = Join-Path $Root 'bin'
New-Item -ItemType Directory -Force $a, $b | Out-Null
Set-Content -Path (Join-Path $a 'git.exe') -Value 'first' -Encoding ascii
Set-Content -Path (Join-Path $b 'git.exe') -Value 'second' -Encoding ascii
$env:PATH = "$a;$b;$env:PATH"
$resolved = @(Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1)[0].Source
if ($resolved -ne (Join-Path $a 'git.exe')) { throw "resolved every git.exe: $resolved" }
# The call operator must receive that one path, not both paths joined by a space.
$executable = $resolved
if ($executable -isnot [string] -or $executable.Contains(' ')) { throw "joined path leaked: $executable" }
Write-Output 'PASS'
''', encoding="utf-8")
    # Same as above: the shared helper's cold-runner budget, not a stale 30s copy.
    result = _powershell(script, HELPER, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
