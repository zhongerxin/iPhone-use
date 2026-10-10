param(
    [string]$ToolchainBin = $env:WDA_RUST_BIN,
    [string]$NativeBin = $env:WDA_NATIVE_BIN,
    [string]$OpenSSLDir = $env:OPENSSL_DIR,
    [string]$LibClangDir = $env:LIBCLANG_PATH
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
foreach ($directory in @($NativeBin, $OpenSSLDir, $LibClangDir)) {
    if (-not $directory -or -not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw 'Specify valid NativeBin, OpenSSLDir and LibClangDir paths; see README.md.'
    }
}
if ($ToolchainBin) {
    $env:PATH = "$ToolchainBin;$env:PATH"
    $env:RUSTC = Join-Path $ToolchainBin 'rustc.exe'
}
$env:PATH = "$NativeBin;$env:PATH"
$cargoPath = (Get-Command cargo -ErrorAction Stop).Source
$env:OPENSSL_DIR = $OpenSSLDir
$env:OPENSSL_STATIC = '1'
$env:LIBCLANG_PATH = $LibClangDir
$env:CC = Join-Path $NativeBin 'gcc.exe'
$env:CXX = Join-Path $NativeBin 'g++.exe'
$compatHeader = Join-Path $projectRoot 'installer\mingw-msvc-integer-compat.h'
$env:CC_SHELL_ESCAPED_FLAGS = '1'
$env:CXXFLAGS = ($env:CXXFLAGS + ' -include "' + $compatHeader.Replace('\', '/') + '"').Trim()
Push-Location (Join-Path $projectRoot 'installer')
try {
    $runtimeDirectory = Join-Path $projectRoot 'runtime'
    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
    $buildProcess = Start-Process -FilePath $cargoPath -WorkingDirectory (Get-Location).Path -ArgumentList @('build','--release','--locked') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $runtimeDirectory 'build.stdout.log') -RedirectStandardError (Join-Path $runtimeDirectory 'build.stderr.log')
    if ($buildProcess.ExitCode -ne 0) { throw 'Installer build failed. See runtime/build.stderr.log.' }
} finally { Pop-Location }
$binDirectory = Join-Path $projectRoot 'bin'
New-Item -ItemType Directory -Path $binDirectory -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot 'installer\target\release\wda-installer.exe') -Destination $binDirectory
foreach ($library in @('libgcc_s_seh-1.dll', 'libstdc++-6.dll', 'libwinpthread-1.dll')) {
    $libraryPath = Join-Path $NativeBin $library
    if (Test-Path -LiteralPath $libraryPath) { Copy-Item -LiteralPath $libraryPath -Destination $binDirectory }
}
Write-Output 'Installer and GNU runtime libraries copied to bin/.'
