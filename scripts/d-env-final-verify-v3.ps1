$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-env-final-verify-v3-out.txt"
"# d-env-final-verify-v3 $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$pythonCmd = $null
foreach ($cand in @('python', 'py', 'python3')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $pythonCmd = $cand; break }
}

& $pythonCmd scripts\d-env-final-verify-v3.py 2>&1 | Add-Content $out
"# end" | Add-Content $out
