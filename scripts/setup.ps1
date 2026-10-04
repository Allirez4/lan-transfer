$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath 'backend/.venv/Scripts/python.exe')) {
    py -3 -m venv backend/.venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 or newer and its Windows launcher first.' }
}
$taskPython = Join-Path $projectRoot 'backend/.venv/Scripts/python.exe'
& $taskPython -c "import sys; assert sys.version_info >= (3,12), 'Python 3.12 or newer is required'"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or newer is required.' }
& $taskPython -m pip install -r backend/requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
& $taskPython -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw 'Application installation failed.' }
Push-Location -LiteralPath frontend
try {
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
Write-Host 'Setup complete. Double-click Open LAN Transfer.vbs for the laptop GUI, or run .\scripts\start.ps1 for the console server.'
