# Fast iteration: stops on first failure, 60s per-test timeout
# Usage: .\scripts\run-tests-quick.ps1 [test_file_or_dir]
param([string]$TestPath = "tests/")

$wrapperArgs = @($TestPath, "-v", "--tb=short", "-rs", "--timeout=60", "-x", "-p", "no:cacheprovider")
& "$PSScriptRoot\run-tests.ps1" @wrapperArgs
