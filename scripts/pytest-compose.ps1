param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$testDbUrl = "postgresql://chili:chili@postgres:5432/chili_test"

$composeArgs = @(
    "exec",
    "-T",
    "-w",
    "/workspace",
    "-e",
    "DATABASE_URL=$testDbUrl",
    "-e",
    "TEST_DATABASE_URL=$testDbUrl",
    "-e",
    "CHILI_PYTEST=1",
    "chili",
    "python",
    "-m",
    "pytest"
)

if ($PytestArgs) {
    $composeArgs += $PytestArgs
}

Push-Location $repoRoot
try {
    & docker compose @composeArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
