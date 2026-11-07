$ErrorActionPreference='Stop'
$root='D:\WeChatMaa'
if (-not (Test-Path "$root\private\admin-agent.json")) { throw 'MISSING_PROTECTED_MACHINE_CONFIG' }
& icacls.exe "$root\private" /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'BUILTIN\Administrators:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'PRIVATE_CONFIG_ACL_FAILED' }
$name='WeChatMaaAdminAgent'
if (Get-ScheduledTask $name -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask $name -Confirm:$false
}
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$action=New-ScheduledTaskAction -Execute "$root\.venv\Scripts\pythonw.exe" -Argument "`"$root\admin_agent.py`"" -WorkingDirectory $root
$trigger=New-ScheduledTaskTrigger -AtStartup
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask $name -Principal $principal -Action $action -Trigger $trigger -Settings $settings | Out-Null
Start-ScheduledTask $name
Write-Output 'ADMIN_AGENT_STARTED; telemetry only, no automatic chat replies'
