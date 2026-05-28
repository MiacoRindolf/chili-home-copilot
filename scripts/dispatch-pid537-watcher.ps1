$ErrorActionPreference = "Continue"
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repo
$out = "$PSScriptRoot\dispatch-pid537-watcher-out.txt"
"# $(Get-Date -Format o) -- pid537 watcher start" | Out-File $out -Encoding utf8
if (-not $env:DATABASE_URL) { $env:DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili" }

$o = & conda run -n chili-env python "$repo\scripts\d-pid537-watcher.py" 2>&1
$rc = $LASTEXITCODE
"  exit_code=$rc" | Add-Content $out
"--- output ---" | Add-Content $out
$o | Out-String | Add-Content $out
"# $(Get-Date -Format o) -- end" | Add-Content $out

$o | ForEach-Object { Write-Output $_ }
"DISPATCH_PID537_WATCHER_DONE exit=$rc"
exit $rc
