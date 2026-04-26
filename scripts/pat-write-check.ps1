# Direct API write test: attempt to create + delete a refs/heads/dispatch/_smoke
# branch using the PAT. This bypasses git CLI entirely so we know whether the
# token has Contents:Write on the repo.
#
# Output -> scripts/pat-write-check-output.txt
# Usage: .\scripts\pat-write-check.ps1

$out = "scripts/pat-write-check-output.txt"
"# pat-write-check $(Get-Date)" | Out-File $out -Encoding utf8

docker compose exec -T scheduler-worker python -c "
import os, json, urllib.request
tok = os.environ['CHILI_DISPATCH_GITHUB_TOKEN']
repo = os.environ['CHILI_DISPATCH_GIT_REMOTE_USER']
hdr = {'Authorization': f'token {tok}', 'Accept': 'application/vnd.github+json', 'User-Agent': 'chili-pat-check'}

def call(method, url, body=None):
    req = urllib.request.Request(url, headers=hdr, method=method)
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        data = json.dumps(body).encode()
    else:
        data = None
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as r:
            return r.status, r.read().decode('utf-8', errors='replace')[:400]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')[:400]
    except Exception as e:
        return -1, f'{type(e).__name__}: {e}'

# 1. Get HEAD of default branch.
print('--- 1. read default branch HEAD ---')
status, body = call('GET', f'https://api.github.com/repos/{repo}/git/ref/heads/main')
print('GET refs/heads/main:', status)
print(body[:300])
sha = None
try:
    sha = json.loads(body)['object']['sha']
    print('main SHA:', sha)
except Exception as e:
    print('could not extract SHA:', e)

if sha:
    # 2. Try to create a new ref pointing at main HEAD.
    print()
    print('--- 2. create ref dispatch/_smoke (PAT write test) ---')
    status, body = call('POST', f'https://api.github.com/repos/{repo}/git/refs',
                        {'ref': 'refs/heads/dispatch/_smoke', 'sha': sha})
    print('POST git/refs:', status)
    print(body[:400])

    # 3. If the create succeeded, delete it so we leave the repo clean.
    if status in (200, 201):
        print()
        print('--- 3. cleanup: delete dispatch/_smoke ---')
        status, body = call('DELETE', f'https://api.github.com/repos/{repo}/git/refs/heads/dispatch/_smoke')
        print('DELETE git/refs:', status)
        print(body[:200])
" 2>&1 | Add-Content $out

Write-Host "Done -> $out" -ForegroundColor Green
Write-Host "Send 'ok'."
