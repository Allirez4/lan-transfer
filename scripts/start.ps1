param([string]$Config = 'config.json')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$taskPython = Join-Path $projectRoot 'backend/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run scripts/setup.ps1 first.' }
if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) { throw 'Frontend is not built. Run scripts/setup.ps1.' }
& $taskPython -m lan_transfer.cli --config $Config serve
exit $LASTEXITCODE
