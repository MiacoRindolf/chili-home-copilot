$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-env-where-out.txt"
"# d-env-where $(Get-Date -Format o)" | Out-File $out -Encoding utf8
& python scripts\d-env-where.py 2>&1 | Add-Content $out
"# end" | Add-Content $out
