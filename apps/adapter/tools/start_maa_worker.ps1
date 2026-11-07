param([ValidateSet('contacts','watch')][string]$Mode, [string]$RunId, [ValidateRange(1,1000)][int]$MaxPages=100, [ValidateRange(1,100000)][int]$Cycles=1,[string]$ReuseScan='', [ValidateSet('personal','enterprise','public')][string]$Scope='personal',[string]$ResumeScan='',[switch]$AutoReply,[switch]$Continuous)
$ErrorActionPreference='Stop'
if ($RunId -cnotmatch '^[a-z][a-z0-9_-]{1,63}$') { throw 'INVALID_ID' }
$root='D:\WeChatMaa'
if (Test-Path "$root\data\$RunId.json") { throw 'OUTPUT_EXISTS_USE_NEW_RUN_ID' }
$old=Get-ScheduledTask JinjieWeChatGuiGateway -ErrorAction SilentlyContinue
if ($old -and $old.State -ne 'Disabled') { throw 'OLD_GATEWAY_NOT_DISABLED' }
if ((Get-ScheduledTask CodexMaaTrial -ErrorAction SilentlyContinue).State -eq 'Running') { throw 'MAA_TRIAL_ALREADY_RUNNING' }
$name='WeChatMaaWorker'
$existing=Get-ScheduledTask $name -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq 'Running') { throw 'MAA_WORKER_ALREADY_RUNNING' }
if ($existing) { Unregister-ScheduledTask $name -Confirm:$false }
$desktopUser=if ($old) { $old.Principal.UserId } else { 'Administrator' }
$principal=New-ScheduledTaskPrincipal -UserId $desktopUser -LogonType Interactive -RunLevel Highest
$args='"{0}\runner.py" {1} --id {2} --max-pages {3} --cycles {4}' -f $root,$Mode,$RunId,$MaxPages,$Cycles
$args += ' --scope '+$Scope
if (($AutoReply -or $Continuous) -and $Mode -ne 'watch') { throw 'REPLY_ONLY_FOR_WATCH' }
if ($AutoReply) { $args += ' --auto-reply' }
if ($Continuous) { $args += ' --continuous' }
if ($ReuseScan) {
    if ($ReuseScan -cnotmatch '^[a-f0-9]{32}$') { throw 'INVALID_REUSE_SCAN' }
    $args += ' --reuse-scan '+$ReuseScan
}
if ($ResumeScan) {
    if ($Mode -ne 'contacts' -or $ResumeScan -cnotmatch '^[a-f0-9]{32}$') { throw 'INVALID_RESUME_SCAN' }
    $args += ' --resume-scan '+$ResumeScan
}
$action=New-ScheduledTaskAction -Execute "$root\.venv\Scripts\pythonw.exe" -Argument $args -WorkingDirectory $root
$limit=if ($Continuous) { [TimeSpan]::Zero } else { New-TimeSpan -Hours 2 }
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit $limit -MultipleInstances IgnoreNew -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask $name
Write-Output ('STARTED='+$name+' RUN_ID='+$RunId+' MODE='+$Mode)
