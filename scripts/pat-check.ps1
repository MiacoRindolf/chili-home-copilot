# Inspects what the GitHub PAT actually has access to. Uses Python's urllib
# inside the scheduler-worker container so we don't need curl.
#
# Output -> scripts/pat-check-output.txt for Claude to Read directly.
# Usage: .\scripts\pat-check.ps1

$out = "scripts/pat-check-output.txt"
$start = Get-Date
"# pat-check snapshot $start" | Out-File $out -Encoding utf8

function Append { param([string]$Line) $Line | Add-Content $out }
function Section { param([string]$Title) Append ""; Append "===== $Title =====" }

Section "Env vars in container (token redacted)"
docker compose exec -T scheduler-worker bash -c "echo CHILI_DISPATCH_GIT_REMOTE_USER=`$CHILI_DISPATCH_GIT_REMOTE_USER; if [ -z `"`$CHILI_DISPATCH_GITHUB_TOKEN`" ]; then echo TOKEN=MISSING; else echo TOKEN=`${CHILI_DISPATCH_GITHUB_TOKEN:0:14}...redacted; fi" 2>&1 | Add-Content $out

Section "GitHub API: who is this token?"
docker compose exec -T scheduler-worker python -c "
import os, urllib.request, json
tok = os.environ.get('CHILI_DISPATCH_GITHUB_TOKEN', '')
if not tok:
    print('TOKEN MISSING in env')
    raise SystemExit(0)
req = urllib.request.Request(
    'https://api.github.com/user',
    headers={'Authorization': f'token {tok}', 'Accept': 'application/vnd.github+json', 'User-Agent': 'chili-pat-check'},
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    print('login:', body.get('login'))
    print('id:', body.get('id'))
    print('type:', body.get('type'))
    print('html_url:', body.get('html_url'))
except urllib.error.HTTPError as e:
    print('HTTP', e.code, e.read().decode('utf-8', errors='replace')[:500])
except Exception as e:
    print('ERROR:', type(e).__name__, e)
" 2>&1 | Add-Content $out

Section "GitHub API: repo info + token's effective permissions"
docker compose exec -T scheduler-worker python -c "
import os, urllib.request, json
tok = os.environ.get('CHILI_DISPATCH_GITHUB_TOKEN', '')
remote_user = os.environ.get('CHILI_DISPATCH_GIT_REMOTE_USER', '')
if not tok or not remote_user:
    print('missing env')
    raise SystemExit(0)
url = f'https://api.github.com/repos/{remote_user}'
print('GET', url)
req = urllib.request.Request(url, headers={'Authorization': f'token {tok}', 'Accept': 'application/vnd.github+json', 'User-Agent': 'chili-pat-check'})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    print('full_name:', body.get('full_name'))
    print('private:', body.get('private'))
    print('default_branch:', body.get('default_branch'))
    print('permissions:', body.get('permissions'))
    print('archived:', body.get('archived'))
    print('disabled:', body.get('disabled'))
except urllib.error.HTTPError as e:
    print('HTTP', e.code, e.read().decode('utf-8', errors='replace')[:500])
except Exception as e:
    print('ERROR:', type(e).__name__, e)
" 2>&1 | Add-Content $out

Section "GitHub API: list installations / token scopes if discoverable"
docker compose exec -T scheduler-worker python -c "
import os, urllib.request, json
tok = os.environ.get('CHILI_DISPATCH_GITHUB_TOKEN', '')
if not tok:
    raise SystemExit(0)
# Try the metadata endpoint that returns rate-limit + token info
req = urllib.request.Request('https://api.github.com/rate_limit', headers={'Authorization': f'token {tok}', 'Accept': 'application/vnd.github+json', 'User-Agent': 'chili-pat-check'})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        # Headers tell us the token type and scopes
        for h in ['x-oauth-scopes', 'x-accepted-oauth-scopes', 'x-github-token-expiration', 'x-ratelimit-resource']:
            v = r.headers.get(h)
            if v: print(f'{h}: {v}')
        body = json.loads(r.read())
        core = body.get('rate', {}) or body.get('resources', {}).get('core', {})
        print('rate_limit_core:', core)
except urllib.error.HTTPError as e:
    print('HTTP', e.code, e.read().decode('utf-8', errors='replace')[:300])
except Exception as e:
    print('ERROR:', type(e).__name__, e)
" 2>&1 | Add-Content $out

$elapsed = ((Get-Date) - $start).TotalSeconds
Append ""
Append "===== Done in $([Math]::Round($elapsed,1))s ====="

Write-Host "Done -> $out" -ForegroundColor Green
Write-Host "Send 'ok' so Claude can read it."
