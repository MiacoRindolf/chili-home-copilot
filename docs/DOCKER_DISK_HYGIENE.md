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
  - `disk\docker_data.vhdx` (126 GB) — images, layers, build cache — **on E: ✓**
  - `main\ext4.vhdx` (160 MB) — `docker-desktop` distro rootfs — **still served from
    `D:\CHILI-Docker\docker-desktop-wsl\main\ext4.vhdx`** (see the failed-migration
    note below; the E: copy exists but is NOT used)
- Junction: `%LOCALAPPDATA%\Docker\wsl` → `E:\CHILI-Docker\docker-desktop-wsl`
- WSL registry: `HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss\{...}\BasePath`
  → `\\?\E:\CHILI-Docker\docker-desktop-wsl\main` (**points to E: but WSL ignores it
  for the rootfs — see below**)

Postgres data lives separately at `E:\postgres`.

### ⚠️ Rootfs migration is INCOMPLETE — the distro still runs off D: (verified 2026-06-07)

The data disk (`docker_data.vhdx`, 126 GB) successfully moved to E: — because its
D: copy was **deleted**, forcing WSL onto E:. The **rootfs** (`main\ext4.vhdx`,
160 MB) did **not** move. It was copied to E: and the registry `BasePath` was
edited, but the original D: file was left in place, so the live `docker-desktop`
VM still attaches **`D:\…\main\ext4.vhdx`** as its writable rootfs at the kernel
level (Restart-Manager lock holder = PID 4 `System`). The E: rootfs copy sits
stale and unopened.

**A reboot does NOT fix this** (the runbook's earlier assumption was wrong).
Verified: `BasePath` was already `\\?\E:\…` at 08:57, the box rebooted at 10:50,
and the post-reboot VM *still* mounted the D: rootfs. Editing `BasePath` + copying
the file + rebooting is insufficient to repoint an existing distro's writable disk.

**How to tell which copy is live:** compare `LastWriteTime` and exclusive-open
lock state of the D: vs E: `main\ext4.vhdx` — the live one is **locked** and
freshly written; the stale one opens cleanly. Do **not** trust the registry value.

### ❌ The "delete D: to force fallback" fix does NOT work — ATTEMPTED and rolled back (2026-06-07)

An earlier revision of this doc proposed: quit Docker + `wsl --shutdown`, refresh
the E: rootfs from D:, **delete D: before restart**, and let WSL fall back to
`BasePath=E:` (the same deletion-forces-fallback that moved the data disk). **This
was executed and it FAILED.** Do not do it.

What actually happened, step by step:
1. Stopped the stack gracefully, `docker desktop stop` + `wsl --shutdown` — all
   three vhdx locks released cleanly.
2. Copied D: rootfs → E: rootfs and **SHA256-verified them identical**.
3. Deleted `D:\CHILI-Docker\docker-desktop-wsl`.
4. Started Docker → the `docker-desktop` distro **failed to boot**, wedging Docker
   Desktop for 5+ minutes. Direct boot test gave the smoking gun:
   ```
   Failed to attach disk '\\?\D:\CHILI-Docker\docker-desktop-wsl\main\ext4.vhdx' to WSL2:
   The system cannot find the path specified.  (ERROR_PATH_NOT_FOUND)
   ```
   WSL attaches the rootfs from an **explicit D: path** and does NOT fall back to
   `BasePath`. Deleting D: just removes the disk the distro needs.
5. **Recovery (rollback):** recreated `D:\…\main\` and copied the identical E: rootfs
   back to `D:\…\main\ext4.vhdx`; the distro then booted (`BOOT_OK`), Docker came up,
   and all containers were restarted Postgres-first. Stack fully restored.

**Why it's pinned to D::** every value under the Lxss key says E:
(`BasePath=\\?\E:\…\main`, `VhdFileName=ext4.vhdx`) and **no registry value mentions
D: at all** — yet WSL still attaches the D: path. The rootfs disk location is held
**outside the editable registry value** (WSL caches the path the distro was
registered with) and survives both `BasePath` edits **and** reboots. A manual
file-move + `BasePath` edit therefore **cannot** relocate the docker-desktop rootfs.

**The only real ways to move the rootfs (both heavy — not worth it for 160 MB):**
- Use Docker Desktop's supported **Settings → Resources → Advanced → "Disk image
  location"** move (it re-registers the distro properly), or
- `wsl --unregister docker-desktop` then re-import onto E: (destroys the distro;
  Docker Desktop recreates it — loses the writable rootfs layer).

**Recommendation: LEAVE the rootfs on D:.** It is only ~160 MB and is the disposable
writable layer (rebuilt from `docker-desktop.iso` each boot); the 126 GB data disk —
the part that actually mattered — is correctly on E:. **Never delete
`D:\CHILI-Docker\docker-desktop-wsl` while Docker can run** — it is a *live* disk, and
removing it wedges the whole stack until you restore it.

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
