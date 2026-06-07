# Docker disk hygiene

Keeps Docker's per-deploy image churn from filling the host disk, and documents
the Docker Desktop crash-recovery + WSL data location on this Windows box.

## The problem

The deploy flow builds a fresh `chili-app:main-clean-<sha>` image per deploy
(~3/hour, ~1.4–3.8 GB each) with no cleanup. Left unattended these accumulate
(86+ tags observed) and slowly fill the drive that holds Docker's WSL data.

## Automated prune

[`scripts/docker-prune-chili-images.ps1`](../scripts/docker-prune-chili-images.ps1)
runs hourly via the Windows Scheduled Task **`CHILI-Docker-Prune`**.

Policy:
- Keeps the newest `-KeepRecent` (default **15**) `chili-app:main-clean-*` tags as
  rollback targets.
- **Never** removes an image referenced by any container (running *or* stopped),
  and uses `docker rmi` without `-f` as a second safety net (Docker itself
  refuses to delete a container-referenced image).
- Prunes dangling `<none>` images.
- Trims BuildKit cache idle longer than `-KeepCacheHours` (default **12h**) via
  `docker builder prune --filter until=...`. Note: `--max-used-space` is *not*
  used — BuildKit won't evict recently-touched entries under a size cap, so it
  does not reliably bound the store (reclaimed 0 B in testing against a 55 GB
  cache).

Usage:

```powershell
# dry run (default — prints what it would remove)
.\scripts\docker-prune-chili-images.ps1

# execute
.\scripts\docker-prune-chili-images.ps1 -Execute -KeepRecent 15 -KeepCacheHours 12
```

The scheduled task runs as the logged-in user with **Interactive** logon — this
is required so it can reach the Docker named pipe (a SYSTEM-context task cannot).

```powershell
Get-ScheduledTask CHILI-Docker-Prune          # status
Get-Content "$env:LOCALAPPDATA\chili\docker-prune.log" -Tail 10   # run history
```

## Docker WSL data location (relocated D: → E:, 2026-06-07)

Docker's WSL data store was moved off D: (which was filling) onto the larger E:
NVMe. Future image/build-cache growth now lands on E:.

- Data: `E:\CHILI-Docker\docker-desktop-wsl\`
  - `disk\docker_data.vhdx` — images, layers, build cache
  - `main\ext4.vhdx` — `docker-desktop` distro rootfs
- Junction: `%LOCALAPPDATA%\Docker\wsl` → `E:\CHILI-Docker\docker-desktop-wsl`
- WSL registry: `HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss\{...}\BasePath`
  → `\\?\E:\CHILI-Docker\docker-desktop-wsl\main`

Postgres data lives separately at `E:\postgres`.

## Docker Desktop crash recovery (recurring on this host)

**Symptom:** dialog *"An unexpected error occurred … remove `<sock>`: The file
cannot be accessed by the system. (The filename, directory name, or volume label
syntax is incorrect.)"*

**Cause:** force-killing `com.docker.backend` orphans AF_UNIX socket files that
Docker can't remove on the next boot, so each affected service crashes in turn:
- `%LOCALAPPDATA%\Docker\run\dockerInference`, `…\userAnalyticsOtlpHttp.sock` (Inference manager)
- `%LOCALAPPDATA%\docker-secrets-engine\engine.sock` (Secrets engine)

**Fix:**
1. Fully kill all `docker*` / `com.docker.*` processes.
2. Rename the affected dir(s) so Docker recreates clean ones:
   `ren run run.brokenN`, `ren docker-secrets-engine docker-secrets-engine.brokenN`.
3. Relaunch Docker Desktop.

**Never** click *"Reset to factory defaults"* on the crash dialog — it deletes
**all** images, containers, and volumes. Prefer a graceful Docker Desktop quit
over force-kill to avoid orphaning sockets in the first place.

After a Docker outage, containers do **not** auto-restart — start
`postgres` first (wait for healthy), then the app/worker containers.
