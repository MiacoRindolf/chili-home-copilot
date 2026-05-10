$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-env-spot-check-out.txt"
"# d-env-spot-check $(Get-Date -Format o)" | Out-File $out -Encoding utf8
$pythonCmd = $null
foreach ($cand in @('python', 'py', 'python3')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $pythonCmd = $cand; break }
}
& $pythonCmd scripts\d-env-spot-check.py 2>&1 | Add-Content $out
"# end" | Add-Content $out
