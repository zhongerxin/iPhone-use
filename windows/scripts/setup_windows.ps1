param([switch]$SkipDependencies)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $launcher) { throw 'Install the full Python 3.12 Windows distribution with its Python launcher, then retry.' }
    & $launcher.Source -3.12 -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 was not found or could not create its environment.' }
}
if (-not $SkipDependencies) {
    & $pythonPath -m pip install -r (Join-Path $projectRoot 'requirements-lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed. Check your network and retry setup.' }
}
$downloadRoot = Join-Path $projectRoot 'downloads'
New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
$archivePath = Join-Path $downloadRoot 'WebDriverAgentRunner-Runner-v16.14.0.zip'
$expectedHash = '6F7758F72D348D2C35C3FD134FA76A941BBA0C447DE04B2E9D065682F31FC47F'
if (-not (Test-Path -LiteralPath $archivePath)) {
    Write-Output 'Downloading the official unsigned WebDriverAgent v16.14.0 runner...'
    Invoke-WebRequest -Uri 'https://github.com/appium/WebDriverAgent/releases/download/v16.14.0/WebDriverAgentRunner-Runner.zip' -OutFile $archivePath
}
if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'WDA archive checksum mismatch. Keep the file for diagnosis and download the official archive again.'
}
$unsignedApp = Join-Path $downloadRoot 'wda-v16.14.0\WebDriverAgentRunner-Runner.app'
if (-not (Test-Path -LiteralPath $unsignedApp)) {
    $extractRoot = Join-Path $downloadRoot 'wda-v16.14.0'
    if (Test-Path -LiteralPath $extractRoot) { throw 'Incomplete extraction already exists. Move it aside before retrying.' }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot
}
if (-not (Test-Path -LiteralPath (Join-Path $unsignedApp 'PlugIns\WebDriverAgentRunner.xctest\WebDriverAgentRunner'))) {
    throw 'The downloaded archive does not contain the expected physical-device test runner.'
}
New-Item -ItemType Directory -Path (Join-Path $projectRoot 'runtime') -Force | Out-Null
$preparedApp = Join-Path $projectRoot 'runtime\prepared\WebDriverAgentRunner-Runner.app'
if (-not (Test-Path -LiteralPath $preparedApp)) {
    & $pythonPath (Join-Path $projectRoot 'scripts\prepare_wda.py')
    if ($LASTEXITCODE -ne 0) { throw 'Preparing the unsigned WDA bundle failed.' }
}
& $pythonPath (Join-Path $projectRoot 'scripts\check_mcp.py')
if ($LASTEXITCODE -ne 0) { throw 'MCP startup checks failed.' }
Write-Output 'Setup complete. Connect one unlocked phone and run 02-install-wda.cmd.'
