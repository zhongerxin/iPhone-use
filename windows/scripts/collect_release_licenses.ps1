param(
    [Parameter(Mandatory=$true)][string]$MetadataPath,
    [Parameter(Mandatory=$true)][string]$GccLicenseRoot,
    [Parameter(Mandatory=$true)][string]$ThreadLicenseRoot
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$noticeRoot = Join-Path $projectRoot 'runtime\release-licenses'
New-Item -ItemType Directory -Path $noticeRoot -Force | Out-Null
$metadata = Get-Content -LiteralPath $MetadataPath -Raw | ConvertFrom-Json
$manifest = @('Native installer dependency notices. Source: corresponding tagged source archive and the upstream repositories below.','')
foreach ($package in $metadata.packages) {
    $packageRoot = Split-Path $package.manifest_path -Parent
    $notices = @(Get-ChildItem -LiteralPath $packageRoot -File | Where-Object { $_.Name -match '^(LICENSE|LICENCE|COPYING|NOTICE|COPYRIGHT)' })
    $identifier = $package.name + '-' + $package.version
    $manifest += ($identifier + ' | ' + $package.license + ' | ' + $package.repository)
    $destination = Join-Path $noticeRoot $identifier
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    if ($notices) {
        foreach ($notice in $notices) { Copy-Item -LiteralPath $notice.FullName -Destination $destination }
    } elseif ($package.name -in @('async-compression','compression-codecs','compression-core')) {
        foreach ($license in @('LICENSE-MIT','LICENSE-APACHE')) {
            Invoke-WebRequest -Uri ('https://raw.githubusercontent.com/Nullus157/async-compression/2aa1b5f8122618004b9bbab6dc679bafca616ff2/'+$license) -OutFile (Join-Path $destination $license)
        }
    } elseif ($package.name -eq 'idevice') {
        Copy-Item -LiteralPath (Join-Path $projectRoot 'licenses\idevice-MIT.txt') -Destination $destination
    } elseif ($package.name -in @('isideload','nab138_icloud_auth','nab138_omnisette')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot 'vendor\LICENSE-MPL-2.0.txt') -Destination $destination
    } elseif ($package.name -eq 'wda-installer') {
        Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $destination
    } else { throw ('No license text found for '+$identifier) }
}
Copy-Item -LiteralPath $GccLicenseRoot -Destination (Join-Path $noticeRoot 'gcc-runtime') -Recurse -Force
Copy-Item -LiteralPath $ThreadLicenseRoot -Destination (Join-Path $noticeRoot 'winpthread') -Recurse -Force
$opensslRoot = Join-Path $noticeRoot 'openssl-3.5.4'
New-Item -ItemType Directory -Path $opensslRoot -Force | Out-Null
Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/openssl/openssl/openssl-3.5.4/LICENSE.txt' -OutFile (Join-Path $opensslRoot 'LICENSE.txt')
$manifest += 'OpenSSL 3.5.4 | Apache-2.0 | https://github.com/openssl/openssl/tree/openssl-3.5.4'
$manifest += 'GCC runtime 16.2.0-3 | GPL-3.0-or-later WITH GCC-exception-3.1 | https://github.com/msys2/MINGW-packages/tree/master/mingw-w64-gcc'
$manifest += 'winpthread 14.0.0.r302.gd7f3c5201-1 | MIT | https://github.com/msys2/MINGW-packages/tree/master/mingw-w64-winpthreads'
Set-Content -LiteralPath (Join-Path $noticeRoot 'DEPENDENCIES.txt') -Value $manifest -Encoding UTF8
Write-Output $noticeRoot
