$ErrorActionPreference='Stop'
Stop-ScheduledTask WeChatMaaWorker -ErrorAction SilentlyContinue
$workers=@(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'pythonw.exe' -and $_.CommandLine -match 'WeChatMaa\\runner\.py'
})
foreach ($process in $workers) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Output 'MAA_WORKER_STOPPED; old gateway remains disabled'
