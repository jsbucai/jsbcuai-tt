# Run under the Windows Task Scheduler, independently of a terminal or Codex.
$ErrorActionPreference = 'Stop'
$serviceRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$runtimePath = Join-Path $serviceRoot 'runtime'
$logPath = Join-Path $runtimePath 'watch-service.log'
$mutex = New-Object System.Threading.Mutex($false, 'Local\UL.AndroidService.5191.Supervisor')
$ownsMutex = $false
try {
    try { $ownsMutex = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { return }
    New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
    while ($true) {
        try {
            & (Join-Path $serviceRoot 'start-service.ps1') | Out-Null
            $listener = Get-NetTCPConnection -LocalPort 5191 -State Listen -ErrorAction Stop | Select-Object -First 1
            # Observe only. Never terminate a service or interrupt printing.
            Wait-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        } catch {
            if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1MB) {
                Move-Item -LiteralPath $logPath -Destination ($logPath + '.1') -Force
            }
            Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' ' + $_.Exception.Message) -Encoding UTF8
        }
        Start-Sleep -Seconds 5
    }
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
