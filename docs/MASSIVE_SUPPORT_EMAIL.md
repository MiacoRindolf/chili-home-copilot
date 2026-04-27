# Draft email to Massive support

Send to whatever address Massive lists for support (likely `support@massive.com`,
or via their dashboard's contact form). Subject and body below — adapt as needed.

---

**Subject:** API access blocked at edge from my residential IP — please review

Hi Massive support team,

I'm a paying premium customer (Default key, ID `6b6c0116-4ef5-49b6-8ca6-5e722421548a`)
and my access to `api.massive.com` appears to have been blocked at the edge
starting around **2026-04-19 13:00 UTC**.

**Symptoms:**
- TCP connections to `api.massive.com:443` from my residential IP `24.253.120.94`
  return `Connection refused` immediately at the kernel level (not an HTTP 4xx,
  not a TLS handshake failure — the listener actively rejects the SYN).
- DNS resolves correctly (rotates across `198.44.194.x` edge IPs), and ICMP ping
  to those IPs succeeds (~80ms RTT). The block is specifically on TCP :443.
- Behavior is per-edge-node: about half the IPs in the rotation refuse, the other
  half accept. So I see intermittent partial successes when DNS happens to land
  on an unblocked node, but most calls fail.
- The same key and same hostname work fine from a different network — `curl
  https://api.massive.com/v2/aggs/ticker/AAPL/prev?apiKey=Xd1l...` returns HTTP
  200 with proper data when I run it through a VPN.
- Your dashboard at `massive.com/dashboard` shows my account healthy, premium
  active, key valid, "System ok".

**What probably triggered it:**
My setup is an automated trading research project that calls Massive for OHLCV
and quotes. On April 18-19 I had a volume spike — went from ~100 batch requests
per day to ~600-900. I think your abuse system caught the spike and added my IP
to a denylist. I've since identified the bug in my client (no circuit breaker on
connection failures, so the failed requests kept retrying and amplified the
apparent volume) and just deployed a fix that backs off after consecutive
failures. So if you unblock me, the abusive retry pattern won't recur.

**What I'm asking:**
Please review the block on `24.253.120.94` and remove it if appropriate. Also
happy to hear what rate-limit thresholds I should respect going forward — my use
case is personal research, not commercial scraping, and I'd like to stay well
under whatever line I crossed.

Happy to provide more diagnostic info if useful.

Thanks,
Rindolf
[email if you want to include it]

---

## Verification commands the support team can run

If they ask for proof that the block is real, these commands reproduce it:

```bash
# From a non-blocked network: succeeds with HTTP 200
curl -sS "https://api.massive.com/v2/aggs/ticker/AAPL/prev?apiKey=$KEY"

# From the blocked IP 24.253.120.94: TCP refused at L4
timeout 5 bash -c '</dev/tcp/api.massive.com/443'  # exit 1, "Connection refused"
```

## Notes for me (not for the email)

- Date/time of break: 2026-04-19 13:02:57 UTC (last successful "Learning cycle (Massive)" event)
- Public IP: 24.253.120.94 (Cox Communications residential)
- Account ID: 6b6c0116-4ef5-49b6-8ca6-5e722421548a
- Key prefix: Xd1lRJ... (32 chars total)
- Volume spike around inflection: Apr 18 → 105 brain_batch_jobs (ok); Apr 19 → 589; Apr 20 → 901+41 timeouts
- Same denylist appears to also hit api.polygon.io (same key, same upstream — Massive looks Polygon-shaped)
- Circuit breaker now in place: app/services/massive_client.py, threshold 5 consecutive connection failures, cooldown 900s
- Trade-write anomaly guards now in place: app/models/trading.py rejects entry_price/quantity <= 0 at the model layer
