# One-shot: register the IQFeed trade-bridge persistence tasks (Logon + Daily), mirroring the
# depth-bridge tasks. Must run ELEVATED. Writes a result marker the caller reads back.
$marker = 'D:\CHILI-Docker\chili-data\iqfeed_trades\_register_result.txt'
$dir = Split-Path $marker
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
try {
    $vbs = 'D:\dev\chili-home-copilot\scripts\run-hidden.vbs'
    $ps1 = 'D:\dev\chili-home-copilot\scripts\start-iqfeed-trade-bridge.ps1'
    $arg = "`"$vbs`" powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps1`""
    $a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arg
    $p = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
    $s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName 'CHILI-IQFeed-Trade-Bridge-Logon' -Action $a -Principal $p -Settings $s -Trigger (New-ScheduledTaskTrigger -AtLogon) -Force -ErrorAction Stop | Out-Null
    Register-ScheduledTask -TaskName 'CHILI-IQFeed-Trade-Bridge-Daily' -Action $a -Principal $p -Settings $s -Trigger (New-ScheduledTaskTrigger -Daily -At 3:56AM) -Force -ErrorAction Stop | Out-Null
    # start it now (runs the wrapper -> IQConnect-up + bridge)
    Start-ScheduledTask -TaskName 'CHILI-IQFeed-Trade-Bridge-Logon' -ErrorAction SilentlyContinue
    "OK registered+started $(Get-Date -Format o)" | Set-Content -Encoding utf8 $marker
} catch {
    "FAIL $_" | Set-Content -Encoding utf8 $marker
}
