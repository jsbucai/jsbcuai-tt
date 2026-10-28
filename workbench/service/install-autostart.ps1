# Install only this user's independent 5191 background service.
$ErrorActionPreference = 'Stop'
$taskName = 'UL_Android_5191'
$serviceRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$watchPath = Join-Path $serviceRoot 'watch-service.ps1'
$shellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$userName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $watchPath + '"'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Arguments -ne $arguments)) {
    throw 'Task name belongs to a different configuration; it was not changed.'
}
$action = New-ScheduledTaskAction -Execute $shellPath -Argument $arguments -WorkingDirectory $serviceRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userName
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'UL Android LAN API 5191. Start at user logon and recover an exited service; never stop other services.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'UL_Android_5191 installed and started for the current Windows user.'
