$log = 'D:\WeChatGateway\logs\fixed-window-autostart.log'
try {
    $exe = 'D:\Weixin\Weixin.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw "WeChat launcher missing: $exe" }
    $running = Get-CimInstance Win32_Process -Filter "Name='Weixin.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -eq $exe -and $_.CommandLine -match '--scene=desktop' }
    if (-not $running) { Start-Process -FilePath $exe -ArgumentList '--scene=desktop' -WorkingDirectory 'D:\Weixin' }
    $python = 'D:\WeChatGateway\.venv\Scripts\python.exe'
    $fixedWindowScript = 'D:\WeChatGateway\tools\ensure_fixed_wechat_window.py'
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $fixedWindowScript)) {
        throw 'Fixed-window dependency missing'
    }
    & $python $fixedWindowScript 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw "Could not establish fixed WeChat window: $LASTEXITCODE" }
    "SUCCESS $(Get-Date -Format o)" | Out-File -FilePath $log -Append -Encoding utf8
} catch {
    "ERROR $(Get-Date -Format o) $($_.Exception.Message)" | Out-File -FilePath $log -Append -Encoding utf8
    throw
}
