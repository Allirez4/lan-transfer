param([string]$Config = 'config.json')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$desktopPython = Join-Path $projectRoot 'backend/.venv/Scripts/pythonw.exe'
if (-not (Test-Path -LiteralPath $desktopPython)) { throw 'Run scripts/setup.ps1 first.' }
$desktopConfig = [System.IO.Path]::GetFullPath($Config)
Start-Process -FilePath $desktopPython -ArgumentList @('-m', 'lan_transfer.desktop', '--config', ('"' + $desktopConfig + '"')) -WorkingDirectory $projectRoot -WindowStyle Hidden
