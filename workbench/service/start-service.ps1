$ErrorActionPreference = 'Stop'
$serviceRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$pythonPath = Join-Path $serviceRoot '.venv\Scripts\python.exe'
$runPath = Join-Path $serviceRoot 'run.py'
$runtimePath = Join-Path $serviceRoot 'runtime'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先创建本服务 .venv 并安装 requirements.txt' }
$listener = Get-NetTCPConnection -LocalPort 5191 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:5191/api/v2/health' -TimeoutSec 3
    if ($health.ok -and $health.service -eq 'ul-android-service') { Write-Output '5191 独立安卓服务已运行'; return }
    throw '5191 已由其他程序占用，不会停止该程序'
}
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$serverProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-u', ('"' + $runPath + '"')) -WorkingDirectory $serviceRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'server.out.log') -RedirectStandardError (Join-Path $runtimePath 'server.err.log')
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:5191/api/v2/health' -TimeoutSec 2
        if ($health.ok -and $health.service -eq 'ul-android-service') {
            Write-Output '5191 独立安卓服务已通过连接检查'
            return
        }
    } catch { }
    $serverProcess.Refresh()
    if ($serverProcess.HasExited) { throw '5191 服务启动失败，请查看 runtime/server.err.log' }
    Start-Sleep -Seconds 1
}
throw '5191 服务尚未通过连接检查，请查看 runtime/server.err.log；未停止任何进程'
