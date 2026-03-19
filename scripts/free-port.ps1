# Stop every process listening on a TCP port (fixes WinError 10048 on Windows).
# If kill fails (e.g. access denied), re-launches as Administrator (UAC prompt).
# PID 4 (System) cannot be killed - surfaces HTTP.sys / Hyper-V guidance.
param(
    [int]$Port = 8000
)

function Test-PortInExcludedTcpRange {
    param([int]$PortToCheck)
    try {
        $lines = netsh interface ipv4 show excludedportrange protocol=tcp 2>$null
        foreach ($line in $lines) {
            if ($line -match '^\s*(\d+)\s+(\d+)\s*$') {
                $start = [int]$Matches[1]; $end = [int]$Matches[2]
                if ($PortToCheck -ge $start -and $PortToCheck -le $end) {
                    return @{ InRange = $true; Start = $start; End = $end }
                }
            }
        }
    } catch { }
    return @{ InRange = $false; Start = 0; End = 0 }
}

function Write-HttpSysHint {
    param([int]$PortNum)
    Write-Host "Port $PortNum is held by System (PID 4) - usually HTTP.sys URL reservation, not a normal app." -ForegroundColor Red
    Write-Host "  Inspect: netsh http show urlacl" -ForegroundColor Yellow
    Write-Host "  If you see http://+:$PortNum/ or similar, remove (run as Admin):" -ForegroundColor Yellow
    Write-Host "  netsh http delete urlacl url=http://+:$PortNum/" -ForegroundColor Gray
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not running as Administrator. A separate elevated PowerShell window will open (UAC) - watch THAT window for kill results." -ForegroundColor Yellow
    $args = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Port $Port"
    $proc = Start-Process powershell -Verb RunAs -ArgumentList $args -Wait -PassThru
    $code = $proc.ExitCode
    if ($code -eq 0) {
        Write-Host "Elevated free-port finished: SUCCESS (exit 0). Port $Port should be free." -ForegroundColor Green
    } else {
        Write-Host "Elevated free-port finished: FAILED (exit $code). Port $Port may still be in use." -ForegroundColor Red
        Write-Host "Next: .\scripts\diagnose-port-8000.ps1 -Port $Port" -ForegroundColor Cyan
    }
    exit $code
}

$pids = @(
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
) | Where-Object { $_ -and $_ -ne 0 }

if (-not $pids) {
    $ex = Test-PortInExcludedTcpRange -PortToCheck $Port
    if ($ex.InRange) {
        Write-Host "Port $Port has no LISTENER but sits in Windows excluded range $($ex.Start)-$($ex.End) - bind will likely fail with 10048." -ForegroundColor Red
        Write-Host "  Reboot may reshuffle ranges, or use another port. Run: .\scripts\diagnose-port-8000.ps1" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "Port $Port is free (no LISTENING process found)." -ForegroundColor Green
    exit 0
}

$hadPid4 = $false
foreach ($procId in $pids) {
    if ($procId -eq 4) {
        Write-HttpSysHint -PortNum $Port
        $hadPid4 = $true
        continue
    }

    $name = $null
    try {
        $p = Get-Process -Id $procId -ErrorAction Stop
        $name = $p.ProcessName
        Write-Host "Stopping PID $procId ($name) on port $Port (process tree)..." -ForegroundColor Yellow
    } catch {
        Write-Host "PID $procId not visible to Get-Process (other session?); trying taskkill /F /T anyway..." -ForegroundColor Yellow
    }

    cmd /c "taskkill /F /T /PID $procId 2>nul"
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
}

if ($hadPid4) {
    Start-Sleep -Seconds 1
    $still4 = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.OwningProcess -eq 4 }
    if ($still4) {
        Write-Host "Port $Port still owned by PID 4. Fix HTTP.sys urlacl or change port." -ForegroundColor Red
        exit 1
    }
}

Start-Sleep -Seconds 2
$still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($still) {
    $stillPids = $still | Select-Object -ExpandProperty OwningProcess -Unique
    Write-Host "Port $Port still in use after kill attempts." -ForegroundColor Red
    foreach ($sp in $stillPids) {
        if ($sp -eq 4) {
            Write-HttpSysHint -PortNum $Port
        } else {
            try {
                $p2 = Get-Process -Id $sp -ErrorAction Stop
                Write-Host "  Still listening: PID $sp ($($p2.ProcessName))" -ForegroundColor Yellow
            } catch {
                Write-Host "  Still listening: PID $sp (process not found in this session - try Admin taskkill, or WSL/Docker may own the bind)." -ForegroundColor Yellow
                Write-Host "  If using WSL/Docker: wsl --shutdown  or stop containers publishing :$Port" -ForegroundColor Gray
            }
        }
    }
    $ex = Test-PortInExcludedTcpRange -PortToCheck $Port
    if ($ex.InRange) {
        Write-Host "Port $Port is inside Hyper-V / dynamic excluded range $($ex.Start)-$($ex.End). Reboot or use another port." -ForegroundColor Yellow
    }
    Write-Host "Run: .\scripts\diagnose-port-8000.ps1" -ForegroundColor Cyan
    exit 1
}
Write-Host "Port $Port is now free." -ForegroundColor Green
exit 0
