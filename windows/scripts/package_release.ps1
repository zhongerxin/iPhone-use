param(
    [Parameter(Mandatory=$true)][ValidatePattern('^windows-v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?$')][string]$Tag,
    [Parameter(Mandatory=$true)][string]$LicenseDirectory
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$repositoryRoot = Split-Path $projectRoot -Parent
$buildRoot = Join-Path $projectRoot ('runtime\release-' + $Tag)
if (Test-Path -LiteralPath $buildRoot) { throw 'Release staging directory already exists; use a new tag or move the previous staging directory aside.' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'bin\wda-installer.exe'))) { throw 'Build the installer first.' }
if (-not (Test-Path -LiteralPath $LicenseDirectory -PathType Container)) { throw 'Collect native dependency licenses before packaging.' }
if (git -C $repositoryRoot status --porcelain --untracked-files=no) { throw 'Commit tracked source changes before packaging.' }
foreach ($binary in @('wda-installer.exe','libgcc_s_seh-1.dll','libstdc++-6.dll','libwinpthread-1.dll')) {
    $bytes = [System.IO.File]::ReadAllBytes((Join-Path $projectRoot ('bin\'+$binary)))
    $ascii = [System.Text.Encoding]::ASCII.GetString($bytes)
    $unicode = [System.Text.Encoding]::Unicode.GetString($bytes)
    if ($ascii -match '(?i)[a-z]:[/\\]Users[/\\]' -or $unicode -match '(?i)[a-z]:[/\\]Users[/\\]') {
        throw 'A release binary contains a local user profile path. Rebuild Rust and native libraries with portable paths before packaging.'
    }
}
$packageRoot = Join-Path $buildRoot ('iphone-use-' + $Tag)
New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
$sourceFiles = @(git -C $repositoryRoot ls-files windows)
$sourceFiles += @('LICENSE','THIRD_PARTY_NOTICES.md','server/wda_client.py','server/wda_controller.py','server/wda_image.py','server/wda_text.py')
foreach ($relativeFile in $sourceFiles) {
    if ($relativeFile -match '/(runtime|downloads|bin|\.venv|target)/|\.(p12|p8|pem|key|mobileprovision|ipa|exe|dll|png|jpg|jpeg|log)$') { throw ('Unexpected artifact in tracked source: '+$relativeFile) }
    $destination = Join-Path $packageRoot $relativeFile
    New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $repositoryRoot $relativeFile) -Destination $destination
}
$packageBin = Join-Path $packageRoot 'windows\bin'
New-Item -ItemType Directory -Path $packageBin -Force | Out-Null
foreach ($binary in @('wda-installer.exe','libgcc_s_seh-1.dll','libstdc++-6.dll','libwinpthread-1.dll')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot ('bin\'+$binary)) -Destination $packageBin
}
Copy-Item -LiteralPath $LicenseDirectory -Destination (Join-Path $packageRoot 'windows\bin\licenses') -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot 'RELEASE-START.zh-CN.md') -Destination (Join-Path $packageRoot 'START-HERE.zh-CN.md')
$revision = (git -C $repositoryRoot rev-parse HEAD).Trim()
Set-Content -LiteralPath (Join-Path $packageRoot 'SOURCE-REVISION.txt') -Value $revision -Encoding ASCII
$zipPath = Join-Path $buildRoot ('iphone-use-'+$Tag+'-win-x64.zip')
Compress-Archive -LiteralPath $packageRoot -DestinationPath $zipPath -CompressionLevel Optimal
$sourceZip = Join-Path $buildRoot ('iphone-use-'+$Tag+'-source.zip')
git -C $repositoryRoot archive --format=zip --output=$sourceZip HEAD
if ($LASTEXITCODE -ne 0) { throw 'Source archive failed.' }
$hashLines = @()
foreach ($artifact in @($zipPath,$sourceZip)) {
    $hashLines += ((Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLower() + '  ' + (Split-Path $artifact -Leaf))
}
Set-Content -LiteralPath (Join-Path $buildRoot 'SHA256SUMS.txt') -Value $hashLines -Encoding ASCII
Write-Output $buildRoot
