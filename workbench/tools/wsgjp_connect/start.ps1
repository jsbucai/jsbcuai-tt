$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = Join-Path $projectPath 'service\.venv\Scripts\python.exe'
$scriptPath = Join-Path $PSScriptRoot 'server.py'
if (Get-NetTCPConnection -State Listen -LocalPort 5192 -ErrorAction SilentlyContinue) {
    throw 'Port 5192 is already in use. Existing services were not changed.'
}
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Workbench service Python environment was not found.' }
$process = Start-Process -FilePath $pythonPath -ArgumentList ('"' + $scriptPath + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
Write-Output ('Local ERP connection tool PID ' + $process.Id + '; open http://127.0.0.1:5192/')
