# WSL2 memory cap (host-level defense)

**Origin:** F-leak-1, subtask 2.
**Host config file (NOT in repo):** `%USERPROFILE%\.wslconfig`

## Why this exists

WSL2's `vmmem` process can grow to consume up to **50% of host RAM by default** — that's why container memory limits don't fully protect the host. When a container inside WSL leaks (e.g., chili at 99.94% of its 3 GiB cap, restart-cycling 7× in 16h), `vmmem` keeps the leaked memory committed and the host's other applications run out of RAM. The operator's symptom: "can't open apps, browser unresponsive" approximately every 12h.

The `.wslconfig` cap is **defense-in-depth**: even if a container leaks internally, the host always has at least `(host_total - cap)` of RAM available for Windows.

## Current values

Probed on 2026-05-03 (host: 64 GiB total RAM):

```ini
[wsl2]
memory=32GB
processors=6
swap=4GB
swapFile=C:\\Users\\rindo\\.wsl-swap.vhdx
```

| Setting | Value | Why |
|---|---|---|
| `memory` | 32 GB | ~50% of host RAM — gives Windows guaranteed 32 GiB headroom |
| `processors` | 6 | Caps CPU cores WSL2 can use; prevents brain-worker (109% CPU) from monopolising the host scheduler |
| `swap` | 4 GB | Lets WSL hit swap before OOM; smoother degradation |
| `swapFile` | `C:\Users\rindo\.wsl-swap.vhdx` | Explicit path so the swap file is locatable |

## Apply the change

```powershell
# 1. Shut down WSL (this stops Docker Desktop's WSL backend)
wsl --shutdown

# 2. Restart Docker Desktop (containers auto-restart per restart policy)
# Either via the Docker Desktop tray, or:
Get-Process "Docker Desktop" -ErrorAction SilentlyContinue | Stop-Process
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"

# 3. Wait ~60s for containers to come back, then verify:
Get-Counter '\Memory\Available MBytes'
docker stats --no-stream
```

After applying:
- `Get-Counter '\Memory\Available MBytes'` should show materially more host memory (was: 34 GB available; expect: 40+ GB after the cap takes effect).
- `docker stats` should show no container exceeding the per-container limit (the cap doesn't change container-level limits, just the WSL2 envelope).

## Adjust the cap

If the operator wants more aggressive protection (more host headroom):
- Edit `memory=` lower (e.g., `24GB` for 64-GiB host = 37%).
- Re-apply via `wsl --shutdown` + Docker Desktop restart.

If containers start hitting their own limits more often, the host-side `memory=` is too low — bump it back up.

## Roll back

Either delete `%USERPROFILE%\.wslconfig` or set `memory=` to a much larger value (e.g., `60GB`), then `wsl --shutdown` + Docker restart. Cap removed.

## Verification observable

The dispatch-stats-trend script (`scripts/dispatch-stats-trend.ps1`) shows per-container memory deltas and the host's free memory pressure. After applying the cap, run it after ~30 min to confirm stable behavior.

## Related runbooks

- `docs/RUNBOOKS/fix31-deletion.md` — prior leak-class incident.
- `docs/STRATEGY/CC_REPORTS/2026-05-03_f-leak-1.md` — the diagnosis that produced this runbook.
