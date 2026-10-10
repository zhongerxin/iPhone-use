$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$resultPath = Join-Path $projectRoot 'runtime\install-result.json'
if (-not (Test-Path -LiteralPath $resultPath)) { throw 'No successful WDA installation result. Complete install-wda.cmd first.' }
$installed = Get-Content -LiteralPath $resultPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($installed.ok -ne $true -or $installed.bundle_id -notmatch '^com\.iphoneuse\.windows\.wda\.[A-Za-z0-9]+$') {
    throw 'Invalid successful installation result.'
}
& $pythonPath (Join-Path $projectRoot 'scripts\doctor.py')
if ($LASTEXITCODE -ne 0) { throw 'Device discovery failed.' }
$devices = Get-Content -LiteralPath (Join-Path $projectRoot 'runtime\usbmux-list.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (@($devices).Count -ne 1) { throw 'Expected exactly one connected phone.' }
if (Get-NetTCPConnection -LocalPort 18100 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 18100 is already in use. Verify the existing service; this script will not stop it.'
}
$runtimeRoot = Join-Path $projectRoot 'runtime'
$testProcess = Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -ArgumentList @('-m','pymobiledevice3','developer','dvt','xcuitest',$installed.bundle_id,'--userspace') `
    -RedirectStandardOutput (Join-Path $runtimeRoot 'wda-run.stdout.log') `
    -RedirectStandardError (Join-Path $runtimeRoot 'wda-run.stderr.log')
$forwardProcess = Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -ArgumentList @('-m','pymobiledevice3','usbmux','forward','18100','8100','--host','127.0.0.1') `
    -RedirectStandardOutput (Join-Path $runtimeRoot 'wda-forward.stdout.log') `
    -RedirectStandardError (Join-Path $runtimeRoot 'wda-forward.stderr.log')
@{test_pid=$testProcess.Id;forward_pid=$forwardProcess.Id;bundle_id=$installed.bundle_id;started_at=(Get-Date -Format o)} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeRoot 'wda-processes.json') -Encoding UTF8
Write-Output 'WDA test runner and loopback forwarder started. Verify scripts/wda_probe.py status; process startup alone is not READY.'
