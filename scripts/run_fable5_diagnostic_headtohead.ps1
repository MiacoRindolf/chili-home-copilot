param(
    [switch]$Execute,
    [decimal]$MaxBudgetUsd = 5.00
)

$ErrorActionPreference = "Stop"
$ExpectedPromptSha256 = "C279126FB23319A30F9B440645062A54BFF98ACAF8B6E88252B83A476B307EEC"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutputRoot = Join-Path $RepoRoot "project_ws\AgentOps\fable5_diagnostic_headtohead"
$PromptPack = Join-Path $OutputRoot "prompt_pack.md"
$ResponsePath = Join-Path $OutputRoot "fable5_response.json"
$TranscriptPath = Join-Path $OutputRoot "fable5_transcript.jsonl"
$RawProviderPath = Join-Path $OutputRoot "fable5_provider_result.json"
$ProviderErrorPath = Join-Path $OutputRoot "fable5_provider_stderr.log"
$ChiliResults = Join-Path $RepoRoot "project_ws\AgentOps\fable5_class_diagnostic_blinded_eighth_run.json"
$Evaluator = Join-Path $RepoRoot "scripts\autopilot_fable5_diagnostic_headtohead.py"
$ClaudeProjects = Join-Path $env:USERPROFILE ".claude\projects"

if (-not $Execute) {
    Write-Output "READY_ONLY: no model call was made."
    Write-Output "This collector makes one authenticated premium claude-fable-5 request."
    Write-Output "Run again with -Execute only after explicit approval."
    exit 0
}

foreach ($required in @($PromptPack, $ChiliResults, $Evaluator)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required frozen input is missing: $required"
    }
}
$actualPromptSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $PromptPack).Hash
if ($actualPromptSha256 -ne $ExpectedPromptSha256) {
    throw "Frozen prompt pack hash mismatch: expected $ExpectedPromptSha256, got $actualPromptSha256"
}
$claude = Get-Command claude -ErrorAction Stop
$auth = (& $claude.Source auth status --json | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $auth.loggedIn -ne $true -or $auth.apiProvider -ne "firstParty") {
    throw "Claude first-party authentication is not ready."
}

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$sessionId = [guid]::NewGuid().ToString()
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) "chili-fable5-headtohead-$sessionId"
New-Item -ItemType Directory -Path $sandbox | Out-Null
$prompt = Get-Content -Raw -LiteralPath $PromptPack

Push-Location $sandbox
try {
    $providerLines = $prompt | & $claude.Source `
        --print `
        --model claude-fable-5 `
        --effort max `
        --safe-mode `
        --tools "" `
        --permission-mode plan `
        --disable-slash-commands `
        --no-chrome `
        --session-id $sessionId `
        --output-format json `
        --max-budget-usd $MaxBudgetUsd `
        2> $ProviderErrorPath
    $providerExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($providerExitCode -ne 0) {
    throw "Fable 5 provider call failed with exit code $providerExitCode. See $ProviderErrorPath"
}
$providerText = $providerLines -join "`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($RawProviderPath, $providerText, $utf8NoBom)
$providerResult = $providerText | ConvertFrom-Json
$response = [string]$providerResult.result
if ([string]::IsNullOrWhiteSpace($response)) {
    throw "Provider output did not contain a non-empty result."
}
[System.IO.File]::WriteAllText($ResponsePath, $response, $utf8NoBom)

Start-Sleep -Milliseconds 750
$transcriptMatches = @(
    Get-ChildItem -LiteralPath $ClaudeProjects -Recurse -File -Filter "$sessionId.jsonl" -ErrorAction Stop
)
if ($transcriptMatches.Count -ne 1) {
    throw "Expected one provider-native transcript for session $sessionId; found $($transcriptMatches.Count)."
}
Copy-Item -LiteralPath $transcriptMatches[0].FullName -Destination $TranscriptPath -Force

& python $Evaluator `
    --prompt-pack $PromptPack `
    --response $ResponsePath `
    --transcript $TranscriptPath `
    --chili-results $ChiliResults `
    --no-write
if ($LASTEXITCODE -ne 0) {
    throw "Head-to-head provenance or scoring preflight failed."
}
& python $Evaluator `
    --prompt-pack $PromptPack `
    --response $ResponsePath `
    --transcript $TranscriptPath `
    --chili-results $ChiliResults
if ($LASTEXITCODE -ne 0) {
    throw "Head-to-head result publication failed."
}

Write-Output "AUTHENTICATED_FABLE5_HEADTOHEAD_COMPLETE"
Write-Output "Session: $sessionId"
Write-Output "Report: $(Join-Path $OutputRoot 'HEADTOHEAD_REPORT.md')"
