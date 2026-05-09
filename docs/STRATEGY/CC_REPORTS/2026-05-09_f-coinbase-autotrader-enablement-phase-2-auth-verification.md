# CC_REPORT: f-coinbase-autotrader-enablement (Phase 2: auth verification)

**STATUS: BLOCKED at Item 1.** Credentials missing. Items 2-5 NOT
attempted per the brief's "if unsure" §1: "Credentials missing:
STOP, surface env var names. No auth probe without credentials."

Zero code changes shipped. Zero broker calls made. Zero orders
placed.

## Item-by-item results

### Item 1 — Credentials probe — **FAILED**

**Required env vars** (per `coinbase_service.py:54` +
`app/config.py:158-159`):

```
COINBASE_API_KEY     -> resolves to settings.coinbase_api_key
COINBASE_API_SECRET  -> resolves to settings.coinbase_api_secret
```

**Probe result** (no values logged):

```
$ grep -iE '^COINBASE_API_KEY|^COINBASE_API_SECRET' .env
(no output — vars not present)

$ python -c "from app.config import settings; print(bool(settings.coinbase_api_key), bool(settings.coinbase_api_secret))"
False False
```

`.env` contains a single comment-line referencing the
`f-fastpath-rotator-coinbase-fixes-bundle` brief but **no API key or
secret entries**. The fast-path rotator (which uses Coinbase REST
read-only) doesn't require auth, so its bundle didn't add
credentials. Phase 2 (auth-protected order placement) does.

### Items 2-5 — NOT ATTEMPTED

Per the brief's sequencing step 6 ("STOP and surface if items 1-4
don't all pass"):

* Item 2 (multi-process `is_connected()` across containers) — would
  return False in every container because the underlying SDK
  initialization at `coinbase_service.py:71-76` short-circuits when
  `_credentials_configured()` returns False.
* Item 3 (`get_portfolio()` $2.2k cash check) — requires auth.
* Item 4 (`get_positions()` snapshot) — requires auth.
* Item 5 (paper-test order) — explicitly gated on items 1-4
  passing, AND requires explicit operator confirmation per Auto
  Mode's destructive-action policy. Not attempted.

### Item 6 — CC report — **THIS DOCUMENT**

## Operator action required (Phase 2.5 — credentials setup)

Add the two API credentials to `.env`. Suggested form:

```dotenv
# Coinbase Advanced Trade API credentials.
# Generate at https://portal.cdp.coinbase.com/access/api .
# Required permissions: View, Trade. Restrict IP if possible.
# Read by app/services/coinbase_service.py:54 via app/config.py:158-159.
COINBASE_API_KEY=organizations/<org-uuid>/apiKeys/<key-uuid>
COINBASE_API_SECRET=-----BEGIN EC PRIVATE KEY-----\n<key body>\n-----END EC PRIVATE KEY-----\n
```

**Important quirks**:

1. The Coinbase Advanced Trade API key format is a long
   `organizations/.../apiKeys/...` string, NOT a short legacy
   key.
2. The secret is a multi-line PEM-formatted EC private key. The
   `coinbase_service.py:75` does
   `secret = settings.coinbase_api_secret.replace("\\n", "\n")`
   to handle the dotenv-style `\n` escapes; you can paste the key
   with `\n` literals inside the value.
3. After adding, **all four worker containers need
   `docker compose up -d --force-recreate`** to pick up the
   variables: `chili`, `autotrader-worker`, `scheduler-worker`,
   `broker-sync-worker`.
4. The `f-fastpath-rotator-coinbase-fixes-bundle` line in `.env`
   is just a comment marker; it doesn't conflict.

## After credentials are added — re-run Phase 2

Operator should:

1. Add credentials to `.env`.
2. Force-recreate the four worker containers.
3. Re-queue this brief OR run a quick manual probe yourself:
   ```bash
   docker exec chili-home-copilot-chili-1 python -c \
     "from app.services import coinbase_service as cb; \
      print('connected:', cb.is_connected()); \
      print('portfolio:', cb.get_portfolio())"
   ```
4. If the manual probe shows `connected=True` and a portfolio with
   the funded $2.2k cash, queue Phase 2 redux as a new
   NEXT_TASK. CC then runs items 2-5 (multi-process check,
   portfolio, positions, paper-test) and produces a fuller report.

## Constraints honored

* ✅ **No code changes.** No edits to `coinbase_service.py`,
  `coinbase_spot.py`, or any other source file.
* ✅ **No broker calls made.** The probe used direct settings-
  attribute access (no SDK round-trip).
* ✅ **No values logged.** Credential probe checked presence (bool
  cast) only; values never read into log output or terminal
  output.
* ✅ **No paper-test attempted.** Item 5 is gated on items 1-4 +
  operator confirmation; neither precondition met.
* ✅ **RH path untouched.**

## Recommendation

Phase 2 is unblocked-but-paused at the credentials gate. The
re-run cost after credentials are added is small (the four read-
only items + one paper-test, all in a single CC session ≤ 30 min).

Operator's next move:

1. **Add credentials to `.env`** per the format above.
2. **Force-recreate worker containers.**
3. **Queue Phase 2 redux** by promoting this brief back to PENDING
   (or by writing a simple `f-coinbase-phase-2-redux` brief that
   says "credentials now present; re-run items 2-5"). Either path
   works.

## Rollback plan

N/A — no state changed. The report is the only artifact.
