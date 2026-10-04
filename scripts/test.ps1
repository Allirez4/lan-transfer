$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
& '.\backend\.venv\Scripts\python.exe' -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
Push-Location -LiteralPath frontend
try {
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    npm.cmd run lint
    if ($LASTEXITCODE -ne 0) { throw 'Frontend lint failed.' }
} finally { Pop-Location }
