# Read-only .env structural diagnostic. Prints positions + hashes only,
# never the values themselves.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-env-diagnose-out.txt"
"# d-env-diagnose $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$envPath = ".env"
$bytes = [System.IO.File]::ReadAllBytes($envPath)
$content = [System.Text.Encoding]::UTF8.GetString($bytes)

"# total byte length: $($bytes.Length)" | Add-Content $out

# Leading BOM?
$hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
"# leading BOM: $hasBom" | Add-Content $out

# Count newlines (LF or CRLF)
$lfCount = ([regex]::Matches($content, "`n")).Count
$crCount = ([regex]::Matches($content, "`r")).Count
"# LF count: $lfCount   CR count: $crCount" | Add-Content $out

# Position pivots (NOT printing values)
$knownKeyValue = "organizations/83a21581-e0e9-4b74-a4ce-47ee2264a9f2/apiKeys/ae81841f-b08f-42fd-8b7a-13ac15abf837"
$pivots = @{
    'COINBASE_API_KEY=' = $content.IndexOf('COINBASE_API_KEY=')
    'KNOWN_KEY_VALUE_anchor' = $content.IndexOf($knownKeyValue)
    'COINBASE_API_SECRET=' = $content.IndexOf('COINBASE_API_SECRET=')
    'BEGIN_EC_PRIVATE_KEY' = $content.IndexOf('-----BEGIN EC PRIVATE KEY-----')
    'END_EC_PRIVATE_KEY' = $content.IndexOf('-----END EC PRIVATE KEY-----')
    'CHILI_COINBASE_AUTOTRADER_LIVE=' = $content.IndexOf('CHILI_COINBASE_AUTOTRADER_LIVE=')
    'DATABASE_URL=' = $content.IndexOf('DATABASE_URL=')
}

"# pivot positions (-1 = not found):" | Add-Content $out
foreach ($k in $pivots.Keys) {
    "  $k -> $($pivots[$k])" | Add-Content $out
}

# Char immediately before each KEY position (to see if it's mid-line or start-of-line)
"# char before each pivot position (LF=newline-good, other=mid-line-bad):" | Add-Content $out
foreach ($k in @('COINBASE_API_KEY=', 'COINBASE_API_SECRET=', 'CHILI_COINBASE_AUTOTRADER_LIVE=', 'DATABASE_URL=')) {
    $pos = $pivots[$k]
    if ($pos -gt 0) {
        $prevChar = [int]$content[$pos - 1]
        $prevDesc = if ($prevChar -eq 10) { 'LF (good)' } elseif ($prevChar -eq 13) { 'CR' } else { "char_code=$prevChar (mid-line)" }
        "  $k preceded by: $prevDesc" | Add-Content $out
    }
}

# Hash of KEY value region (if anchor found, check if expected key value is intact)
$keyAnchorPos = $pivots['KNOWN_KEY_VALUE_anchor']
if ($keyAnchorPos -ge 0) {
    "# KNOWN_KEY_VALUE found at $keyAnchorPos -- key value is intact in file" | Add-Content $out
} else {
    "# KNOWN_KEY_VALUE NOT found -- key value has been damaged or lost" | Add-Content $out
}

# Length of secret value (between SECRET= and END_EC marker)
$secStart = $pivots['COINBASE_API_SECRET=']
$secEnd = $pivots['END_EC_PRIVATE_KEY']
if ($secStart -ge 0 -and $secEnd -gt $secStart) {
    $secValLen = $secEnd - ($secStart + 'COINBASE_API_SECRET='.Length) + '-----END EC PRIVATE KEY-----'.Length
    "# secret value byte length (incl END marker): $secValLen" | Add-Content $out
}

# What's in the first 60 bytes (decoded, escaped to be safe)?
if ($bytes.Length -ge 60) {
    $first60 = [System.Text.Encoding]::UTF8.GetString($bytes[0..59])
    $first60Esc = $first60.Replace("`r", '\r').Replace("`n", '\n')
    # Mask any potential value content (anything after first =)
    $masked = $first60Esc -replace '=.*', '=<MASKED>'
    "# first 60 bytes (values masked): $masked" | Add-Content $out
}

# What's in the last 60 bytes (decoded, escaped, masked)?
if ($bytes.Length -ge 60) {
    $last60 = [System.Text.Encoding]::UTF8.GetString($bytes[($bytes.Length - 60)..($bytes.Length - 1)])
    $last60Esc = $last60.Replace("`r", '\r').Replace("`n", '\n')
    $masked = $last60Esc -replace '=[^\\\n]+', '=<MASKED>'
    "# last 60 bytes (values masked): $masked" | Add-Content $out
}

"# end" | Add-Content $out
