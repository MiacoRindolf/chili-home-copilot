$out = "scripts/dispatch-fix-packed-refs-output.txt"
"# fix corrupted .git/packed-refs $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "packed-refs file size + tail" {
    if (Test-Path .git/packed-refs) {
        $fi = Get-Item .git/packed-refs
        "size: $($fi.Length) bytes"
        "last 10 lines:"
        Get-Content .git/packed-refs -Tail 10
        ""
        "raw bytes of last 100:"
        $bytes = [System.IO.File]::ReadAllBytes(".git/packed-refs")
        $start = [Math]::Max(0, $bytes.Length - 100)
        for ($i = $start; $i -lt $bytes.Length; $i++) {
            $b = $bytes[$i]
            $c = if ($b -ge 32 -and $b -lt 127) { [char]$b } else { "[$b]" }
            Write-Output -NoEnumerate "$c"
        }
    } else { "no packed-refs file" }
}

S "git status (will likely fail)" {
    git status 2>&1 | Select-Object -First 5
}

S "backup packed-refs" {
    Copy-Item .git/packed-refs .git/packed-refs.bak.r32fix -Force
    "backed up to .git/packed-refs.bak.r32fix"
}

S "repair: keep only well-formed lines" {
    $lines = Get-Content .git/packed-refs
    $good = @()
    foreach ($ln in $lines) {
        if ($ln -match '^#') { $good += $ln; continue }
        # ref line should be: <40-char-sha> <refname>
        if ($ln -match '^[0-9a-f]{40} \S+$') { $good += $ln; continue }
        # ^<sha> peeled-tag annotation
        if ($ln -match '^\^[0-9a-f]{40}$') { $good += $ln; continue }
        # else: drop
        "DROPPED malformed: '$ln'"
    }
    $good | Set-Content .git/packed-refs -Encoding ascii -NoNewline
    # ensure trailing newline
    Add-Content .git/packed-refs "" -Encoding ascii
    "repaired packed-refs: $($good.Count) lines kept"
}

S "git status post-repair" {
    git status 2>&1 | Select-Object -First 8
}

S "git fsck" {
    git fsck --no-dangling 2>&1 | Select-Object -First 20
}

S "git rev-parse HEAD" { git rev-parse HEAD 2>&1 }

S "git log --oneline -3" { git log --oneline -3 2>&1 }

Write-Host "packed-refs repair done -- see $out"
