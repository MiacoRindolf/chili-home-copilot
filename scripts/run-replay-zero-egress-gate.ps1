param(
    [string]$ProjectName = "chili-replay-zero-egress-$PID"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot "docker-compose.replay-zero-egress.yml"

Push-Location $repoRoot
try {
    docker compose `
        --project-name $ProjectName `
        --file $composeFile `
        up `
        --abort-on-container-exit `
        --exit-code-from replay-zero-egress
    if ($LASTEXITCODE -ne 0) {
        throw "Replay zero-egress gate failed with exit code $LASTEXITCODE"
    }
}
finally {
    docker compose `
        --project-name $ProjectName `
        --file $composeFile `
        down `
        --volumes `
        --remove-orphans
    Pop-Location
}
