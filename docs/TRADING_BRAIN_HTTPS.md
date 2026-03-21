# Trading Brain UI + HTTPS (mixed content)

If a **Trading Brain** frontend runs on **`https://localhost:5001`** (or any HTTPS origin) but the CHILI API is **`http://localhost:8000`**, the browser **blocks** those requests:

> Mixed Content: The page was loaded over HTTPS, but requested an insecure XMLHttpRequest endpoint `http://localhost:8000/...`

There is no server-side CORS fix for this — the request never leaves the browser.

## Fix (pick one)

### 1. Serve CHILI over HTTPS on port 8000 (recommended)

Use the same scheme as the page. **From the repo root:**

```powershell
.\scripts\start-https.ps1
```

This serves CHILI with mkcert certificates on **port 8000** by default (`conda run -n chili-env uvicorn ...`).

Point the Trading Brain frontend at **`https://localhost:8000`** (not `http://`).

Another port (e.g. Hyper-V excluded range):

```powershell
$env:CHILI_PORT = "8010"
.\scripts\start-https.ps1
```

### 2. Proxy API through the dev server (Vite / webpack)

Configure the dev server so the browser only talks to **one origin** (e.g. `https://localhost:5001`), and the dev server proxies `/api` to CHILI. Then use **relative** URLs like `/api/trading/brain/status` in the frontend (no hard-coded `http://localhost:8000` in JS).

### 3. Run the Trading UI over HTTP in development

Only for local testing: serve the UI on **`http://`** so it can call **`http://localhost:8000`** without mixed-content blocking. Do not use this pattern for production.

## API endpoint

`GET /api/trading/brain/status` returns:

- `learning` — `get_learning_status()` shape  
- `worker` — same JSON as `GET /api/trading/brain/worker/status`

Use this if your UI expects a single “brain status” poll instead of two separate calls.

## Troubleshooting: `PR_END_OF_FILE_ERROR` (Firefox) on `https://localhost:8000`

That error means the browser is doing a **TLS (HTTPS) handshake**, but the process on that port is **plain HTTP** (no certificate), so the connection drops — often seen as **`PR_END_OF_FILE_ERROR`**.

| How you started CHILI | Use this URL |
|----------------------|--------------|
| **`.\scripts\start-dev.ps1`** (HTTP) | **`http://localhost:8000/brain`** — **not** `https://` |
| **`.\scripts\start-https.ps1`** (HTTPS) | **`https://localhost:8000/brain`** (or the port you set with `CHILI_PORT`) |

**Fix:** Either switch the address bar to **`http://`** for the HTTP dev server, or stop it and run **`.\scripts\start-https.ps1`**.

If you use **HTTPS** and **`localhost` still fails** in Firefox, try **`https://127.0.0.1:8000/...`** or ensure your mkcert cert lists **`localhost`** and **`127.0.0.1`** (and IPv6 **`::1`** if you use `localhost` and the browser resolves to IPv6).
