# Verifies that the GitHub PAT works, WITHOUT actually pushing anything.
#
#   1. Force-recreates scheduler-worker so it picks up the .env values
#   2. Confirms the env vars made it into the container (token redacted)
#   3. Runs `git push --dry-run` from inside the container to confirm
#      GitHub accepts the token's auth + permissions for a dispatch/* push
#   4. Reports pass/fail
#
# Output goes to scripts/verify-github-push-output.txt for Claude to Read.
# Usage: .\scripts\verify-github-push.ps1

$out = "scripts/verify-github-push-output.txt"
$start = Get-Date
"# verify-github-push snapshot $start" | Out-File $out -Encoding utf8

function Append { param([string]$Line) $Line | Add-Content $out }
function Section { param([string]$Title) Append ""; Append "===== $Title =====" }

Write-Host "[1/4] Force-recreating scheduler-worker..." -ForegroundColor Yellow
docker compose up -d --force-recreate scheduler-worker 2>&1 | Out-Null
Start-Sleep -Seconds 15

Section "Container status"
docker compose ps scheduler-worker 2>&1 | Add-Content $out

Section "Env vars visible to scheduler-worker (token redacted)"
docker compose exec -T scheduler-worker bash -c @"
echo CHILI_DISPATCH_GIT_PUSH_ENABLED=`$CHILI_DISPATCH_GIT_PUSH_ENABLED
echo CHILI_DISPATCH_GIT_REMOTE_USER=`$CHILI_DISPATCH_GIT_REMOTE_USER
echo CHILI_DISPATCH_GIT_BASE_BRANCH=`$CHILI_DISPATCH_GIT_BASE_BRANCH
if [ -z "`$CHILI_DISPATCH_GITHUB_TOKEN" ]; then
    echo CHILI_DISPATCH_GITHUB_TOKEN=MISSING
else
    echo CHILI_DISPATCH_GITHUB_TOKEN=`${CHILI_DISPATCH_GITHUB_TOKEN:0:14}...redacted (length=`${#CHILI_DISPATCH_GITHUB_TOKEN})
fi
"@ 2>&1 | Add-Content $out

Section "Git push --dry-run smoke test"
$smokeBranch = "dispatch/_smoke_" + (Get-Date -Format "yyyyMMddHHmmss")
Append "smoke branch: $smokeBranch"
Append ""

# Build the push URL inside the container so the token never lands in
# PowerShell history or stdout. The dry-run does NOT create a remote ref;
# it only verifies authentication + permissions are sufficient.
docker compose exec -T scheduler-worker bash -c @"
set +x
cd /workspace
# Construct the URL using the in-container env vars
PUSH_URL="https://x-access-token:`${CHILI_DISPATCH_GITHUB_TOKEN}@github.com/`${CHILI_DISPATCH_GIT_REMOTE_USER}.git"

# We need a local commit to attempt to push. Build a throwaway branch
# from current HEAD. Don't fetch/checkout — that would touch worktrees.
# Just use --dry-run with the current HEAD as the source.
HEAD_SHA=`$(git rev-parse HEAD)
echo "current HEAD: `$HEAD_SHA"

# Try the dry-run push of HEAD to a brand-new remote branch name.
# Auth failure shows 'remote: ... 401' or 'remote: Invalid username or password'.
# Permission failure shows 'remote: Permission ... denied'.
# Success shows 'New branch ... would be created' (or similar).
echo "running: git push --dry-run --no-verify <redacted-url> HEAD:refs/heads/$smokeBranch"
git push --dry-run --no-verify "`${PUSH_URL}" HEAD:refs/heads/$smokeBranch 2>&1 | sed -E "s|x-access-token:[^@]+@|x-access-token:***@|g"
echo "exit_code=`$?"
"@ 2>&1 | Add-Content $out

Section "Recent runner.py code (verifies push helper is loaded)"
docker compose exec -T scheduler-worker python -c @"
import inspect
from app.services.code_dispatch import runner
src = inspect.getsource(runner)
print('has_commit_and_push:', 'def commit_and_push(' in src)
print('has_redact_url:', 'def _redact_url(' in src)
print('has_git_push_enabled:', 'def _git_push_enabled(' in src)
"@ 2>&1 | Add-Content $out

$elapsed = ((Get-Date) - $start).TotalSeconds
Append ""
Append "===== Done in $([Math]::Round($elapsed,1))s ====="

Write-Host "[4/4] Done. Output: $out" -ForegroundColor Green
Write-Host "Send 'ok' so Claude can read it."
