$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$serverPath = Join-Path $projectRoot 'server\iphone_mcp.py'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create the project Python environment first.' }
& $pythonPath (Join-Path $projectRoot 'scripts\check_mcp.py')
if ($LASTEXITCODE -ne 0) { throw 'MCP protocol checks failed; configuration was not changed.' }
$codexCommand = Get-Command codex -ErrorAction Stop
$configPath = Join-Path $env:USERPROFILE '.codex\config.toml'
$backupPath = Join-Path $projectRoot 'runtime\codex-config-before-mcp.toml'
if ((Test-Path -LiteralPath $configPath) -and -not (Test-Path -LiteralPath $backupPath)) {
    Copy-Item -LiteralPath $configPath -Destination $backupPath
}
& $codexCommand.Source mcp add iphone-use-windows -- $pythonPath -u $serverPath
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP registration failed.' }
Write-Output 'Registered iphone-use-windows. Start a new Codex session or reload the extension to load these tools.'
