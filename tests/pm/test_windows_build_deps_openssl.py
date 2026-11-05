"""Native Windows case within the unchanged per-file deadline."""
from __future__ import annotations
import pytest
from tests.pm.test_windows_build_deps import (HELPER, _powershell)

@pytest.mark.platforms("windows")
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

