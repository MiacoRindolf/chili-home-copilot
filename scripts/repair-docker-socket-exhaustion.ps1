param(
    [switch]$Repair,
    [switch]$Force,
    [switch]$SkipComposeRestart,
    [int]$WarnBoundSockets = 2000,
    [int]$CriticalDockerBoundSockets = 8000,
    [int]$DockerReadyTimeoutSeconds = 300,
    [string]$ComposeDir = ""
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    if ($ComposeDir) {
        return (Resolve-Path -LiteralPath $ComposeDir).Path
    }
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $PSCommandPath }
    return (Resolve-Path -LiteralPath (Join-Path $scriptDir "..")).Path
}

function Get-BoundTcpSummary {
    $rows = @(Get-NetTCPConnection -State Bound -ErrorAction SilentlyContinue)
    $top = @(
        $rows |
            Group-Object OwningProcess |
            Sort-Object Count -Descending |
            Select-Object -First 12 |
            ForEach-Object {
                $ownerPid = 0
                [void][int]::TryParse([string]$_.Name, [ref]$ownerPid)
                $name = "unknown"
                if ($ownerPid -gt 0) {
                    try {
                        $name = (Get-Process -Id $ownerPid -ErrorAction Stop).ProcessName
                    } catch {
                        $name = "exited"
                    }
                }
                [pscustomobject]@{
                    Count = [int]$_.Count
                    Pid = $ownerPid
                    Process = $name
                }
            }
    )
    $dockerBound = 0
    foreach ($item in $top) {
        if ($item.Process -in @("com.docker.backend", "com.docker.proxy", "vpnkit")) {
            $dockerBound += [int]$item.Count
        }
    }
    [pscustomobject]@{
        TotalBound = [int]$rows.Count
        DockerBound = [int]$dockerBound
        Top = $top
    }
}

function Write-BoundTcpSummary($summary) {
    Write-Host ("Bound TCP sockets: total={0} docker_backend={1}" -f $summary.TotalBound, $summary.DockerBound)
    if ($summary.Top.Count -gt 0) {
        $summary.Top | Format-Table Count, Pid, Process -AutoSize
    }
}

function Test-DockerReady {
    try {
        $version = docker info --format "{{.ServerVersion}}" 2>$null
        return ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($version))
    } catch {
        return $false
    }
}

function Wait-DockerReady([int]$timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-DockerReady) {
            return $true
        }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Stop-ComposeStack([string]$repoRoot) {
    if (-not (Test-DockerReady)) {
        Write-Warning "Docker engine is not ready; skipping compose stop."
        return
    }
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "docker-compose.yml"))) {
        Write-Warning "No docker-compose.yml found at $repoRoot; skipping compose stop."
        return
    }
    Push-Location $repoRoot
    try {
        Write-Host "Stopping Compose stack in $repoRoot ..."
        docker compose stop
    } finally {
        Pop-Location
    }
}

function Start-ComposeStack([string]$repoRoot) {
    if ($SkipComposeRestart) {
        Write-Host "SkipComposeRestart set; leaving Compose services stopped."
        return
    }
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "docker-compose.yml"))) {
        Write-Warning "No docker-compose.yml found at $repoRoot; skipping compose start."
        return
    }
    Push-Location $repoRoot
    try {
        Write-Host "Starting Compose stack in $repoRoot ..."
        docker compose up -d
    } finally {
        Pop-Location
    }
}

function Stop-DockerDesktop {
    $dockerCli = "C:\Program Files\Docker\Docker\DockerCli.exe"
    if (Test-Path -LiteralPath $dockerCli) {
        Write-Host "Shutting down Docker Desktop ..."
        $proc = Start-Process -FilePath $dockerCli -ArgumentList "-Shutdown" -PassThru -WindowStyle Hidden
        if (-not $proc.WaitForExit(90000)) {
            Write-Warning "DockerCli.exe -Shutdown did not exit within 90s; continuing if backend stopped."
            try {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            } catch {}
        }
    } else {
        Write-Warning "DockerCli.exe not found; stopping Docker backend processes directly."
        Get-Process "com.docker.backend", "com.docker.proxy", "vpnkit" -ErrorAction SilentlyContinue |
            Stop-Process -Force
    }

    $deadline = (Get-Date).AddSeconds(90)
    do {
        $backend = @(Get-Process "com.docker.backend" -ErrorAction SilentlyContinue)
        $summary = Get-BoundTcpSummary
        if ($backend.Count -eq 0 -and $summary.DockerBound -lt $WarnBoundSockets) {
            return
        }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
}

function Start-DockerDesktop {
    $desktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path -LiteralPath $desktop)) {
        throw "Docker Desktop executable not found at $desktop"
    }
    Write-Host "Starting Docker Desktop ..."
    Start-Process -FilePath $desktop -WindowStyle Hidden
    if (-not (Wait-DockerReady $DockerReadyTimeoutSeconds)) {
        throw "Docker engine did not become ready within $DockerReadyTimeoutSeconds seconds."
    }
}

$repoRoot = Get-RepoRoot
$before = Get-BoundTcpSummary
Write-BoundTcpSummary $before

$critical = $before.DockerBound -ge $CriticalDockerBoundSockets
$warn = $before.TotalBound -ge $WarnBoundSockets

if (-not $Repair) {
    if ($critical) {
        Write-Warning ("Docker Desktop backend owns {0} bound sockets. Run with -Repair to stop Compose, restart Docker Desktop, and restart Compose." -f $before.DockerBound)
        exit 2
    }
    if ($warn) {
        Write-Warning ("Host has {0} bound sockets. Investigate before running network-heavy jobs." -f $before.TotalBound)
        exit 1
    }
    Write-Host "Socket state is healthy."
    exit 0
}

if (-not $critical -and -not $Force) {
    Write-Host "Docker bound-socket count is below critical threshold. Use -Force to repair anyway."
    exit 0
}

Stop-ComposeStack $repoRoot
Stop-DockerDesktop
Start-DockerDesktop
Start-ComposeStack $repoRoot

$after = Get-BoundTcpSummary
Write-BoundTcpSummary $after

if ($after.DockerBound -ge $CriticalDockerBoundSockets) {
    throw "Docker backend still owns too many bound sockets after repair."
}

Write-Host "Docker socket exhaustion repair completed."
