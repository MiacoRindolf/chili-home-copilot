# Check that the Context domain card is in /api/brain/domains.
# Uses curl.exe which ships with Windows 10/11 and handles self-signed
# certs via -k flag.
$json = & curl.exe -sk "https://localhost:8000/api/brain/domains"
if (-not $json) {
    Write-Output "REQUEST FAILED: empty response from chili"
    exit 1
}
try {
    $d = $json | ConvertFrom-Json
} catch {
    Write-Output "JSON parse error: $_"
    Write-Output "raw response (first 500 chars):"
    Write-Output $json.Substring(0, [Math]::Min(500, $json.Length))
    exit 1
}
Write-Output "domain ids:"
$d.domains | ForEach-Object { Write-Output "  - $($_.id) ($($_.label)) status=$($_.status) phase=$($_.phase)" }
$ctx = $d.domains | Where-Object { $_.id -eq 'context' } | Select-Object -First 1
Write-Output ""
if ($ctx) {
    Write-Output "Context card found:"
    $ctx | ConvertTo-Json -Depth 4
} else {
    Write-Output "ERROR: Context card MISSING from /api/brain/domains response"
}
