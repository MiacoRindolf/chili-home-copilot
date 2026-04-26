# Delete leftover code_dispatch test scratch files (untracked).
$paths = @(
    "app/services/code_dispatch/test2.py",
    "app/services/code_dispatch/test3.py",
    "app/services/code_dispatch/test_write.txt"
)
foreach ($p in $paths) {
    if (Test-Path $p) {
        Remove-Item $p -Force
        Write-Host "removed $p"
    } else {
        Write-Host "missing $p (already gone)"
    }
}
