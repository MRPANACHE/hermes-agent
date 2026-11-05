"""Native Windows case within the unchanged per-file deadline."""
from __future__ import annotations
from tests.pm.test_windows_build_deps import (HELPER, _powershell, pytest)

@pytest.mark.platforms("windows")
@pytest.mark.parametrize("rust_homes", ['default'])
def test_initializer_preserves_rust_homes_and_shared_openssl_without_downloads(tmp_path, rust_homes):
    """Real cmd.exe imports the SDK; the warm installer never acquires tools."""
    script = tmp_path / "initialize.ps1"
    script.write_text(r'''
param([string]$Helper, [string]$Root, [string]$Homes)
$ErrorActionPreference = 'Stop'
. $Helper
$vs = Join-Path $Root 'VS with spaces'
$devDir = Join-Path $vs 'Common7\Tools'
New-Item -ItemType Directory -Force $devDir | Out-Null
# The MSVC linker pin comes from VsDevCmd's VCToolsInstallDir, never from PATH lookup.
$msvcBin = Join-Path $vs 'VC\Tools\MSVC\14.44\bin\HostARM64\ARM64'
New-Item -ItemType Directory -Force $msvcBin | Out-Null
[IO.File]::WriteAllText((Join-Path $msvcBin 'link.exe'), 'fixture')
[IO.File]::WriteAllText((Join-Path $devDir 'VsDevCmd.bat'), @"
@echo off
set "PATH=%~dp0;%PATH%"
set "INCLUDE=fixture SDK include"
set "LIB=fixture SDK lib"
set "VSCMD_ARG_HOST_ARCH=arm64"
set "VSCMD_ARG_TGT_ARCH=arm64"
set "VSINSTALLDIR=$vs\"
set "VCToolsInstallDir=$vs\VC\Tools\MSVC\14.44\"
"@)
$rustcPath = Join-Path $Root 'rustc.cmd'
[IO.File]::WriteAllText($rustcPath, "@echo off`r`necho host: aarch64-pc-windows-msvc`r`n")
$vcpkg = Join-Path $Root 'vcpkg checkout'
$openssl = Join-Path $Root 'separate shared openssl'
$prefix = Join-Path $openssl 'installed\arm64-windows-static-md'
New-Item -ItemType Directory -Force (Join-Path $vcpkg 'ports\openssl'), (Join-Path $prefix 'lib'), (Join-Path $prefix 'include\openssl') | Out-Null
[IO.File]::WriteAllText((Join-Path $vcpkg 'vcpkg.exe'), 'fixture')
[IO.File]::WriteAllText((Join-Path $vcpkg 'ports\openssl\portfile.cmake'), 'fixture')
foreach ($relative in @('lib\libcrypto.lib', 'lib\libssl.lib', 'include\openssl\ssl.h')) {
    [IO.File]::WriteAllText((Join-Path $prefix $relative), 'fixture')
}
function Get-HermesArm64VisualStudio { return $vs }
function Get-HermesClang { param([string]$VisualStudio); return (Join-Path $vs 'clang.exe') }
function Get-Command {
    param([string]$Name)
    switch ($Name) {
        'cl.exe' { return [pscustomobject]@{Source = (Join-Path $vs 'cl.exe')} }
        'rustup.exe' { return [pscustomobject]@{Source = (Join-Path $Root 'rustup.exe')} }
        'rustc.exe' { return [pscustomobject]@{Source = $rustcPath} }
        'vcpkg.exe' { return $null }
        default { throw "Unexpected command discovery: $Name" }
    }
}
function Invoke-HermesBuildCommand { throw 'Unexpected installation' }
function Invoke-WebRequest { throw 'Unexpected download' }
Remove-Item Env:VCPKG_ROOT -ErrorAction SilentlyContinue
Remove-Item Env:VCPKG_VISUAL_STUDIO_PATH -ErrorAction SilentlyContinue
$env:VCPKG_INSTALLATION_ROOT = $vcpkg
$env:RUSTUP_TOOLCHAIN = 'caller-selected-toolchain'
if ($Homes -eq 'explicit') {
    $cargoHome = Join-Path $Root 'custom cargo'
    $rustupHome = Join-Path $Root 'custom rustup'
    $env:CARGO_HOME = $cargoHome
    $env:RUSTUP_HOME = $rustupHome
} else {
    Remove-Item Env:CARGO_HOME, Env:RUSTUP_HOME -ErrorAction SilentlyContinue
    $cargoHome = Join-Path $HOME '.cargo'
    $rustupHome = Join-Path $HOME '.rustup'
}
$inheritedPath = $env:PATH
$cargoBin = Join-Path $cargoHome 'bin'
$expectedPath = $devDir + '\;' + $inheritedPath
if ($cargoBin -notin ($expectedPath -split ';')) { $expectedPath = $cargoBin + ';' + $expectedPath }
Initialize-HermesArm64BuildTools -StateRoot $Root -OpenSSLRoot $openssl
# The discovered instance is the one VsDevCmd used; vcpkg must not pick another.
if ($env:VCPKG_VISUAL_STUDIO_PATH -ne $vs) { throw 'vcpkg was not pinned to the discovered Visual Studio' }
if ($env:CARGO_HOME -ne $cargoHome -or $env:RUSTUP_HOME -ne $rustupHome) { throw 'Rust homes lost' }
if ($env:RUSTUP_TOOLCHAIN -ne 'caller-selected-toolchain') { throw 'Caller Rust toolchain lost' }
if ($env:PATH -ne $expectedPath) { throw 'PATH lost or reordered' }
if ($env:OPENSSL_DIR -ne $prefix -or $env:OPENSSL_STATIC -ne '1') { throw 'Wrong OpenSSL tree' }
if ($env:INCLUDE -ne 'fixture SDK include' -or $env:LIB -ne 'fixture SDK lib') { throw 'SDK environment lost' }
if ($env:CC_aarch64_pc_windows_msvc -ne (Join-Path $vs 'clang.exe')) { throw 'Compiler lost' }
if ($env:CARGO_TARGET_AARCH64_PC_WINDOWS_MSVC_LINKER -notlike '*\MSVC\*link.exe') { throw 'MSVC linker not pinned' }
# CI initializes once, desktop again, then its native staging child a third time.
# Re-running VsDevCmd grows PATH until cmd.exe hits its 8191-character limit.
$prepared = @{}
foreach ($name in @('PATH', 'INCLUDE', 'LIB', 'VSCMD_ARG_HOST_ARCH', 'VSCMD_ARG_TGT_ARCH', 'VSINSTALLDIR')) {
    $prepared[$name] = (Get-Item "env:$name").Value
}
foreach ($i in 1..3) {
    Initialize-HermesArm64BuildTools -StateRoot $Root -OpenSSLRoot $openssl
    foreach ($name in $prepared.Keys) {
        if ((Get-Item "env:$name").Value -cne $prepared[$name]) { throw "Repeated setup changed $name" }
    }
}
# Inherited VS markers are not sufficient if the SDK, install or architecture differs.
foreach ($invalid in @('INCLUDE', 'LIB', 'VSCMD_ARG_HOST_ARCH', 'VSCMD_ARG_TGT_ARCH', 'VSINSTALLDIR')) {
    foreach ($name in $prepared.Keys) { Set-Item "env:$name" $prepared[$name] }
    Set-Item "env:$invalid" $(if ($invalid -in @('INCLUDE', 'LIB')) { '' } else { 'other' })
    Initialize-HermesArm64BuildTools -StateRoot $Root -OpenSSLRoot $openssl
    foreach ($name in $prepared.Keys | Where-Object { $_ -ne 'PATH' }) {
        if ((Get-Item "env:$name").Value -cne $prepared[$name]) { throw "Did not repair $invalid" }
    }
}
# A caller's explicit pin wins: the guard must not clobber it.
$env:VCPKG_VISUAL_STUDIO_PATH = 'caller-pinned VS'
Initialize-HermesArm64BuildTools -StateRoot $Root -OpenSSLRoot $openssl
if ($env:VCPKG_VISUAL_STUDIO_PATH -ne 'caller-pinned VS') { throw 'Caller Visual Studio pin was overwritten' }
$env:VSCMD_ARG_TGT_ARCH = 'x64'
[IO.File]::WriteAllText((Join-Path $devDir 'VsDevCmd.bat'), "@exit /b 19`r`n")
$failed = $false
try { Initialize-HermesArm64BuildTools -StateRoot $Root -OpenSSLRoot $openssl } catch {
    if ($_.Exception.Message -notmatch 'Could not initialize') { throw }
    $failed = $true
}
if (-not $failed) { throw 'Failed VS activation was accepted' }
Write-Output 'PASS'
''', encoding="utf-8")
    result = _powershell(script, HELPER, tmp_path, rust_homes)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout

