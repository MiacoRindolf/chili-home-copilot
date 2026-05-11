# watcher-check-truncation.ps1
#
# Canonical truncation-detection check for cowork-watcher-chili.
#
# Replaces the prior line-count comparison heuristic (false-positive prone
# because the bash sandbox mount serves stale views and CC_REPORT-stated
# line counts can themselves be misreads of BEFORE state).
#
# Heuristic:
#   1. Fresh host read via [System.IO.File]::ReadAllText against the
#      absolute Windows path. NOT the bash sandbox mount.
#   2. AST parse via `conda run -n chili-env python -c ...`. If it parses,
#      the file is not truncated. Period.
#   3. First-fail debounce: a single failed parse writes a per-file marker
#      and returns PENDING (NOT TRUNCATED). Only on a second failure
#      >= 60s later does the verdict become TRUNCATED.
#
# Exit codes:
#   0 = no TRUNCATED verdicts (may include PENDING / OK_TRANSIENT / OK_MISSING)
#   1 = at least one TRUNCATED verdict (pause-flag-worthy)
#   2 = ENV_ERROR (conda / python not reachable; inconclusive, do NOT pause)
#
# Usage:
#   .\scripts\watcher-check-truncation.ps1 `
#     -Paths @(
#       "app/services/trading/stop_engine.py",
#       "app/services/trading/bracket_reconciliation_service.py",
#       "app/services/trading/venue/coinbase_spot.py",
#       "app/services/trading/bracket_writer_g2.py"
#     ) `
#     -OutFile "scripts/_watcher_truncation_check.json"

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Paths,

    [Parameter(Mandatory = $false)]
    [string]$OutFile,

    [Parameter(Mandatory = $false)]
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,

    [Parameter(Mandatory = $false)]
    [int]$DebounceSeconds = 60,

    [Parameter(Mandatory = $false)]
    [int]$MarkerStaleHours = 24,

    [Parameter(Mandatory = $false)]
    [string]$MarkerDir
)

$ErrorActionPreference = "Continue"

if (-not $MarkerDir) {
    $MarkerDir = Join-Path $RepoRoot "scripts\_watcher_truncation_pending"
}
if (-not (Test-Path $MarkerDir)) {
    New-Item -ItemType Directory -Path $MarkerDir -Force | Out-Null
}

function Get-MarkerPath([string]$absPath) {
    $base = [System.IO.Path]::GetFileNameWithoutExtension($absPath)
    $sha  = (Get-FileHash -Algorithm SHA1 -InputStream `
                ([System.IO.MemoryStream]::new(
                    [System.Text.Encoding]::UTF8.GetBytes($absPath)))).Hash.Substring(0,8)
    return Join-Path $MarkerDir "${base}_${sha}.json"
}

function Sha256-OfFile([string]$absPath) {
    try {
        return (Get-FileHash -Algorithm SHA256 -Path $absPath).Hash
    } catch {
        return $null
    }
}

function Invoke-AstParse([string]$absPath) {
    # Returns @{ exit_code, stdout, stderr, env_error }
    # Writes a small temp .py script and invokes via `conda run` so we avoid
    # PowerShell/Start-Process whitespace splitting of a `-c` argument.

    $pyScriptPath = [System.IO.Path]::GetTempFileName()
    $pyScriptPath = [System.IO.Path]::ChangeExtension($pyScriptPath, ".py")
    $pyBody = @"
import ast, sys
try:
    with open(r'__TARGET__', 'r', encoding='utf-8') as fh:
        src = fh.read()
    ast.parse(src)
    print('OK')
    sys.exit(0)
except SyntaxError as e:
    print('SYNTAX_ERROR line=' + str(e.lineno) + ' msg=' + str(e.msg), file=sys.stderr)
    sys.exit(11)
except Exception as e:
    print('PARSE_ERROR ' + type(e).__name__ + ': ' + str(e), file=sys.stderr)
    sys.exit(12)
"@
    $pyBody = $pyBody -replace '__TARGET__', $absPath.Replace('\', '\\').Replace("'", "\'")
    [System.IO.File]::WriteAllText($pyScriptPath, $pyBody, [System.Text.UTF8Encoding]::new($false))

    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath "conda" `
            -ArgumentList @("run", "--no-capture-output", "-n", "chili-env", "python", $pyScriptPath) `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutFile `
            -RedirectStandardError $stderrFile
        $stdout = Get-Content $stdoutFile -Raw -ErrorAction SilentlyContinue
        $stderr = Get-Content $stderrFile -Raw -ErrorAction SilentlyContinue
        if ($null -eq $stdout) { $stdout = "" }
        if ($null -eq $stderr) { $stderr = "" }
        $code = $proc.ExitCode
        $envError = $false
        if ($code -ne 0 -and $code -ne 11 -and $code -ne 12) {
            # Any exit code outside our expected set means conda/python
            # itself failed (CondaError, missing env, PATH issue, etc).
            $envError = $true
        }
        return [pscustomobject]@{
            exit_code = $code
            stdout    = $stdout
            stderr    = $stderr
            env_error = $envError
        }
    } catch {
        return [pscustomobject]@{
            exit_code = -1
            stdout    = ""
            stderr    = "EXCEPTION $($_.Exception.Message)"
            env_error = $true
        }
    } finally {
        Remove-Item $stdoutFile -ErrorAction SilentlyContinue
        Remove-Item $stderrFile -ErrorAction SilentlyContinue
        Remove-Item $pyScriptPath -ErrorAction SilentlyContinue
    }
}

# Clear stale markers older than MarkerStaleHours
$staleCutoff = (Get-Date).AddHours(-1 * $MarkerStaleHours)
Get-ChildItem -Path $MarkerDir -Filter "*.json" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.LastWriteTime -lt $staleCutoff) {
        Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
    }
}

$results = @()
$anyTruncated = $false
$anyEnvError = $false

foreach ($rel in $Paths) {
    $rel = $rel -replace '/', '\'
    $abs = if ([System.IO.Path]::IsPathRooted($rel)) { $rel } else { Join-Path $RepoRoot $rel }

    $entry = [ordered]@{
        path           = $rel
        abs_path       = $abs
        verdict        = $null
        lines          = $null
        bytes          = $null
        sha256         = $null
        parse_exit     = $null
        parse_stderr   = $null
        marker_present = $false
        first_fail_at  = $null
        seconds_since_first_fail = $null
        notes          = ""
    }

    if (-not [System.IO.File]::Exists($abs)) {
        $entry.verdict = "OK_MISSING"
        $entry.notes = "File does not exist on host filesystem."
        $results += [pscustomobject]$entry
        continue
    }

    # Fresh host read via .NET (NOT bash mount)
    try {
        $bytes = [System.IO.File]::ReadAllBytes($abs)
        $text  = [System.Text.Encoding]::UTF8.GetString($bytes)
        $entry.bytes  = $bytes.Length
        # Count lines without loading via Get-Content (which can lag mount)
        $entry.lines  = ([regex]::Matches($text, "`n")).Count + 1
        $entry.sha256 = Sha256-OfFile $abs
    } catch {
        $entry.verdict = "READ_ERROR"
        $entry.notes = "Failed to read file: $($_.Exception.Message)"
        $results += [pscustomobject]$entry
        continue
    }

    $parse = Invoke-AstParse $abs
    $entry.parse_exit   = $parse.exit_code
    $stderrRaw = if ($null -ne $parse.stderr) { $parse.stderr } else { "" }
    $entry.parse_stderr = $stderrRaw.Trim()

    if ($parse.env_error) {
        $entry.verdict = "ENV_ERROR"
        $entry.notes = "Conda/python environment unreachable; check inconclusive."
        $anyEnvError = $true
        $results += [pscustomobject]$entry
        continue
    }

    $markerPath = Get-MarkerPath $abs

    if ($parse.exit_code -eq 0) {
        # Clean parse. If we had a pending marker, this is OK_TRANSIENT.
        if (Test-Path $markerPath) {
            $entry.marker_present = $true
            try {
                $marker = Get-Content $markerPath -Raw | ConvertFrom-Json
                $entry.first_fail_at = $marker.first_fail_at
            } catch { }
            Remove-Item $markerPath -Force -ErrorAction SilentlyContinue
            $entry.verdict = "OK_TRANSIENT"
            $entry.notes = "Prior parse failed; this parse succeeded - transient I/O, marker cleared."
        } else {
            $entry.verdict = "OK"
        }
        $results += [pscustomobject]$entry
        continue
    }

    # Parse failed. Decide PENDING vs TRUNCATED based on debounce marker.
    if (Test-Path $markerPath) {
        $entry.marker_present = $true
        try {
            $marker = Get-Content $markerPath -Raw | ConvertFrom-Json
            $firstFail = [datetime]::Parse($marker.first_fail_at).ToUniversalTime()
            $now = (Get-Date).ToUniversalTime()
            $elapsed = [int]($now - $firstFail).TotalSeconds
            $entry.first_fail_at = $marker.first_fail_at
            $entry.seconds_since_first_fail = $elapsed

            if ($elapsed -ge $DebounceSeconds) {
                # Second failure after debounce window. Real truncation.
                $entry.verdict = "TRUNCATED"
                $entry.notes = "AST parse failed twice (debounce >= ${DebounceSeconds}s)."
                $anyTruncated = $true
                Remove-Item $markerPath -Force -ErrorAction SilentlyContinue
            } else {
                # Marker exists but debounce not elapsed yet. Stay PENDING.
                $entry.verdict = "PENDING"
                $entry.notes = "AST parse failed; debounce window not yet elapsed (${elapsed}s of ${DebounceSeconds}s)."
            }
        } catch {
            # Marker corrupt — treat as first fail again.
            $entry.verdict = "PENDING"
            $entry.notes = "AST parse failed; prior marker corrupt, resetting."
            $now = (Get-Date).ToUniversalTime().ToString("o")
            $entry.first_fail_at = $now
            $payload = @{
                path          = $rel
                abs_path      = $abs
                first_fail_at = $now
                sha256        = $entry.sha256
                bytes         = $entry.bytes
                lines         = $entry.lines
                parse_stderr  = $entry.parse_stderr
            } | ConvertTo-Json
            Set-Content -Path $markerPath -Value $payload -Encoding UTF8
        }
    } else {
        # First failure. Write marker, return PENDING.
        $now = (Get-Date).ToUniversalTime().ToString("o")
        $entry.verdict = "PENDING"
        $entry.first_fail_at = $now
        $entry.seconds_since_first_fail = 0
        $entry.notes = "AST parse failed; first failure, debounce armed (re-check in ${DebounceSeconds}s)."
        $payload = @{
            path          = $rel
            abs_path      = $abs
            first_fail_at = $now
            sha256        = $entry.sha256
            bytes         = $entry.bytes
            lines         = $entry.lines
            parse_stderr  = $entry.parse_stderr
        } | ConvertTo-Json
        Set-Content -Path $markerPath -Value $payload -Encoding UTF8
    }

    $results += [pscustomobject]$entry
}

$summary = [ordered]@{
    schema_version   = 1
    host_time        = (Get-Date).ToUniversalTime().ToString("o")
    repo_root        = $RepoRoot
    debounce_seconds = $DebounceSeconds
    any_truncated    = $anyTruncated
    any_env_error    = $anyEnvError
    results          = $results
}

$json = $summary | ConvertTo-Json -Depth 6

if ($OutFile) {
    $outAbs = if ([System.IO.Path]::IsPathRooted($OutFile)) { $OutFile } else { Join-Path $RepoRoot $OutFile }
    [System.IO.File]::WriteAllText($outAbs, $json, [System.Text.UTF8Encoding]::new($false))
}

Write-Output $json

if ($anyTruncated) {
    exit 1
} elseif ($anyEnvError) {
    exit 2
} else {
    exit 0
}
