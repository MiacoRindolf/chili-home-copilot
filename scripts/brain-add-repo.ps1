# Register any kind of project with the brain.
#
# Detects the input shape automatically:
#   * Local Windows path:   C:\dev\some-project   (must be under C:\dev\)
#   * Container path:       /host_dev/foo   /workspace   /workspace_managed/...
#   * GitHub HTTPS URL:     https://github.com/USER/REPO
#   * GitHub SSH URL:       git@github.com:USER/REPO.git
#   * GitHub shorthand:     USER/REPO
#   * Bare name:            chili-home-copilot   (looks up existing row)
#
# Usage:
#   .\scripts\brain-add-repo.ps1 "C:\dev\some-other-project"
#   .\scripts\brain-add-repo.ps1 "https://github.com/MiacoRindolf/another-repo"
#   .\scripts\brain-add-repo.ps1 "MiacoRindolf/another-repo"
#
# Output goes to scripts/brain-add-repo-output.txt for Claude to Read.

param(
    [Parameter(Mandatory=$true)][string]$Input,
    [switch]$NoClone
)

$out = "scripts/brain-add-repo-output.txt"
$start = Get-Date
"# brain-add-repo $start" | Out-File $out -Encoding utf8
"input: $Input" | Add-Content $out
"no_clone: $NoClone" | Add-Content $out
"" | Add-Content $out

# Easiest path: hit the API via psql + python in the postgres container is
# overkill; just run the resolver directly inside scheduler-worker so we
# don't depend on the web service being reachable.
$pyScript = @"
import json, sys, os
from app.db import SessionLocal
from app.services.code_brain import repo_resolver

raw = os.environ.get('REPO_INPUT', '')
allow_clone = os.environ.get('REPO_ALLOW_CLONE', '1') == '1'

with SessionLocal() as db:
    try:
        result = repo_resolver.resolve_or_register(db, raw, allow_clone=allow_clone)
    except Exception as e:
        print(json.dumps({'ok': False, 'error': f'{type(e).__name__}: {e}'}))
        sys.exit(1)
    repo = result.repo
    print(json.dumps({
        'ok': True,
        'repo': {
            'id': int(repo.id),
            'name': repo.name,
            'host_path': getattr(repo, 'host_path', None),
            'container_path': getattr(repo, 'container_path', None),
            'path': getattr(repo, 'path', None),
        },
        'parsed_kind': result.parsed.kind.value,
        'created': result.created,
        'cloned': result.cloned,
        'git_initialized': result.git_initialized,
        'notes': result.notes,
    }, indent=2))
"@

# Pass input via env var so the input string never goes through shell
# quoting hell (paths with spaces, special chars, etc.).
$env:REPO_INPUT = $Input
$env:REPO_ALLOW_CLONE = if ($NoClone) { "0" } else { "1" }

"===== resolver output =====" | Add-Content $out
docker compose exec -T -e REPO_INPUT=$Input -e REPO_ALLOW_CLONE=$env:REPO_ALLOW_CLONE scheduler-worker python -c $pyScript 2>&1 | Add-Content $out

"" | Add-Content $out
"===== current code_repos table =====" | Add-Content $out
docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, name, host_path, container_path, path FROM code_repos ORDER BY id;" 2>&1 | Add-Content $out

$elapsed = ((Get-Date) - $start).TotalSeconds
"" | Add-Content $out
"===== Done in $([Math]::Round($elapsed,1))s =====" | Add-Content $out

Write-Host "Done -> $out" -ForegroundColor Green
Write-Host "Send 'ok' so Claude can read it."
