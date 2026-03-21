# External Brain UI (Chill on :3000 / API on :3333)

## Investigation summary

| Question | Finding |
|----------|---------|
| **Is Chill in this repo?** | **No.** There is no Vite/React app, no `3333` port config, and no Chill source under `chili-home-copilot`. Only CHILI (FastAPI + Jinja) lives here. |
| **What is :3333?** | A **separate process** you (or another tool) started. It is not part of CHILI. When it is **not running**, the browser shows **connection refused** / timeouts for that host. |
| **Should :3333 be “closed”?** | You do **not** have to run anything on 3333. If you are not developing that server, **ignore it** or stop the process that binds 3333. Use CHILI on **8000** instead. |
| **Why doesn’t the cookie “just work”?** | Browsers treat **`https://localhost:8000`** and **`http://localhost:3000`** as **different origins** (scheme and port differ). The `chili_device_token` cookie set when you use CHILI is **not** sent** when JavaScript on :3000 calls :8000. |

## What CHILI provides

CHILI serves:

- Built-in Brain UI: **`https://localhost:8000/brain`** (uses `/api/brain/...` and `/api/trading/...`).
- Compatibility routes for external SPAs (same paths many Chill builds use):

| Method / path | Role |
|---------------|------|
| `GET` / `POST` `/api/v1/brain-next-cycle` | Writes `data/brain_worker_wake` |
| `GET` `/api/v1/brain-status` | Learning + worker snapshot (no DB for core fields) |
| `GET` `/api/v1/brain-worker-stats` | Worker JSON + queue counts |
| `GET` `/api/v1/brain-logs` | Recent learning events (needs auth like other trading APIs) |
| `GET` `/api/v1/brain-pending-items` | Pending scan-pattern queue |
| `WS` `/ws` | Minimal WebSocket (accept + ack) |

## How to point Chill at CHILI

### Option A — Shared secret (simplest for localhost)

1. In CHILI **`.env`** add a long random string:

   ```env
   BRAIN_V1_WAKE_SECRET=your-long-random-string-here
   ```

2. Restart CHILI (`scripts/start-https.ps1` or your uvicorn command).

3. In the **Chill** project (wherever it lives on your machine), set the API base URL to **`https://localhost:8000`** and configure HTTP client to send on every `brain-next-cycle` request:

   ```http
   X-Chili-Brain-Wake-Secret: your-long-random-string-here
   ```

   Exact env var name in Chill depends on that repo (often `VITE_API_URL` + a custom header in fetch/axios).

4. For **self-signed HTTPS**, the Chill dev tooling may need `secure: false` on a proxy or you must trust the cert in the OS/browser.

**Security:** Treat `BRAIN_V1_WAKE_SECRET` like a password. Do not commit it. On a LAN/server, prefer VPN or reverse proxy instead of a long-lived secret.

### Option B — Same origin (no secret, cookie works)

Serve the Chill build **from CHILI** (static mount) or put **one** reverse proxy in front so the browser only ever sees **`https://localhost:8000`**. Then `chili_device_token` is sent on API calls.

### Option C — Vite dev proxy (Chill repo only)

If Chill uses Vite, a `server.proxy` from `/api` → `https://localhost:8000` still **does not** attach a cookie that was issued for :8000 to requests that the **browser** sends to :3000. You still need **Option A** or **B** unless Chill logs in through the proxy in a way that sets a cookie for :3000.

## Port :3333

If something still listens on **3333**, identify it:

```powershell
Get-NetTCPConnection -LocalPort 3333 -ErrorAction SilentlyContinue | Format-Table OwningProcess, State
Get-Process -Id <OwningProcess>
```

If you do not need that app, stop it. Point Chill at **8000** (CHILI) instead.
