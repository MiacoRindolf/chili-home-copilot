# Start Brain Worker Script
# This script starts the brain worker if it's not already running
# Designed to be run by Windows Task Scheduler on system startup

$projectPath = "c:\dev\chili-home-copilot"
$statusFile = "$projectPath\data\brain_worker_status.json"
$logFile = "$projectPath\brain_worker_startup.log"
$pythonPath = "python"  # Adjust if using a specific Python installation

Set-Location $projectPath

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logFile -Value "=== Startup check at $timestamp ==="

# Check if worker is already running
$isRunning = $false
if (Test-Path $statusFile) {
    try {
        $status = Get-Content $statusFile | ConvertFrom-Json
        if ($status.pid) {
            $proc = Get-Process -Id $status.pid -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -like "*python*") {
                $isRunning = $true
                Add-Content -Path $logFile -Value "Worker already running (PID: $($status.pid))"
            }
        }
    } catch {
        Add-Content -Path $logFile -Value "Error checking status: $_"
    }
}

if (-not $isRunning) {
    Add-Content -Path $logFile -Value "Starting brain worker..."
    
    # Remove stale signal files
    Remove-Item "$projectPath\data\brain_worker_stop" -ErrorAction SilentlyContinue
    Remove-Item "$projectPath\data\brain_worker_pause" -ErrorAction SilentlyContinue
    
    # Start the worker
    try {
        $proc = Start-Process -FilePath $pythonPath `
            -ArgumentList "scripts/brain_worker.py", "--interval", "30" `
            -WorkingDirectory $projectPath `
            -PassThru `
            -WindowStyle Hidden
        
        Add-Content -Path $logFile -Value "Worker started (PID: $($proc.Id))"
    } catch {
        Add-Content -Path $logFile -Value "Error starting worker: $_"
    }
} else {
    Add-Content -Path $logFile -Value "Worker already running, skipping start"
}
