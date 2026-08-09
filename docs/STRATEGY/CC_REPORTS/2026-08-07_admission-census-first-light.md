# 2026-08-07 — First light from the admission census: the admitter rejects everything, and my own instrument hid the reason

**PRs:** [#994](https://github.com/MiacoRindolf/chili-home-copilot/pull/994) (sidecar, → `f4f8075`) · [#995](https://github.com/MiacoRindolf/chili-home-copilot/pull/995) (test isolation, → `c2b37b3`, admin-merged) · [#996](https://github.com/MiacoRindolf/chili-home-copilot/pull/996) (raw-reason sampling, open — CI blocked by a GitHub Actions outage, no run has been assigned to this repo since 08-06 16:22 UTC)

## 1. The headline: the admitter is called, and it rejects everything

The first real census (build `c6bd9e3+`, generation of the evening of 08-06, pid 59236):

```
22:18 UTC   total=400      admitted=0
   ⋮        (cumulative, one snapshot ≈ every 2 min)
23:59 UTC   total=17,008   admitted=0
outcomes:   rejected_captured_paper_admission_rejected: 17,008
```

Two questions died that night:

- **"Is the admitter even called?"** Yes — 17,008 times in ~100 minutes. The eligible→arm gap is **not** event supply.
- **"Is the census itself healthy?"** Yes — the cumulative series grows smoothly and survives process lifetime; the sidecar written by #994 is durable and readable.

One question replaced them: **what is the reason?** And the answer is embarrassing:

## 2. My normalizer hid the reason

Every rejection carries the *fallback* label. The census normalizes the raw reason against `^[a-z][a-z0-9_]{0,127}$` **before counting** — anything with uppercase, spaces, or symbols becomes `captured_paper_admission_rejected`. All 17,008 were non-conforming, so the census reported, in effect, "rejected because rejected."

The shape of the failure is familiar by now: an instrument that measures the wrong side of its own transformation. The suspicion — unproven, and I will not act on it until sampled — is that the raw reasons are viability `blocked_reason` strings (`"Below A-setup quality floor (change none < 10%) — not a live setup"`), which have exactly the non-conforming shape.

**Fix (#996):** sample the raw strings *before* normalization — top-8 distinct, 120-char cap, control characters stripped — and emit them in both the WARNING line and the sidecar (`raw_samples`). A structural test asserts the sampling call site precedes the normalization; if someone reorders them, the test fails rather than the visibility.

## 3. The pollution proof

The sidecar also contained rows with the exact synthetic signature of my own emit test (`rejected_cooldown: 16`) — written at 00:24/00:30 UTC by **Codex's** test runs while re-sealing the sidecar build. #995 (isolating the test tempdir) was merged on the strength of that: the hazard it guards against happened, to the other agent, within hours.

Two of my traps fired the same week they were set:
- the attribute-coverage guard on the census test double caught a silently-missing field for the **third** time;
- the probe crashed on an emoji under the cp1252 console at the exact moment it had bad news to deliver (fixed to ASCII — a diagnostic that dies while reporting is doubly blind).

## 4. Infrastructure: the socket-exhaustion night and the Docker runbook, proven

The overnight replay chain (VTAK scale-grid confirmation, CLRO 07-07 retry, canon phase 2) was destroyed by long-lived-connection drops: postgres healthy, short connections 30/30, but every connection held 20+ minutes died (`server closed the connection unexpectedly`, `PendingRollbackError`, `connection already closed`). This is the documented `com.docker.backend` socket leak; Docker had been up 2+ days.

The operator approved a pre-market restart. What the runbook says and what actually happened:

- `docker desktop restart` **stops but does not start** on this box; `docker desktop start` then lies ("already running" with zero processes).
- The relaunch **crashed**, and the backend log named the cause precisely: `initializing Inference manager: listening on unix://…` — an **orphaned `dockerInference` socket** in `%LOCALAPPDATA%\Docker\run\`. Four `run.broken*` directories from past recoveries sat beside it.
- Renaming `run\` aside and relaunching brought the engine up in ~285 s; postgres passed WAL crash recovery; a **3-minute held connection** survived — the symptom that mattered is gone.
- Codex's tooling relaunched its generation on its own within minutes, on a build that already contained the sidecar.

Recorded sequence: kill docker procs → `wsl --shutdown` → rename `run\` → launch exe → wait ~5 min → wait out WAL recovery.

## 5. Today's regression is upstream of everything above

All nine of today's generations (r126–r134) produced **zero capture events** — not even bootstrap — while their processes run. Yesterday's r111 produced 1,754 in the same window. Whatever broke between r111 and r126 is in the capture path itself, upstream of selection and admission both. Relayed to Codex; it is their iteration loop.

Consequence for reading the census: **no census rows today is expected**, and says nothing about admission. The 17,008-rejection finding stands on yesterday's data.

## 6. Replay canon status

- Phase 1 (8 windows at `fbfa7f2`): **canon four reproduced exactly** (TC +6.72, CLRO −11.37, JLHL +19.20, HYFM +24.46 — including entry/exit counts). LHSW −252.29 (+0.83 vs old), QTTB −34.07 (sizing change, unexplained — do not use as reference), VTAK −748.39 (fill-shape evidence points to scale-grid; confirmation run still pending), CLRO 07-07 timed out.
- The night chain must rerun after close today: VTAK minus-scale-grid (7200 s, fresh dir), CLRO 07-07 (7200 s, fresh dir), phase 2 (14 windows, `--only` list to match the explicit_partial scope). Scripts are ready; three resume-logic traps (timeout/error rows count as "done"; scope mismatch refusals; stale provenance) are all documented in memory.

## 7. Ross intake: the YXT cross-reference

Yesterday's recap video (`xGIa8Vg0PWM` — the $2k→$65k/30-day summary) named **YXT** as the day's trade: +500%, +44% account day. On the same day, YXT was **our lane's #1 eligible** (460 viability scores, `paper_eligible=t`, `live_eligible=t`, several windows with no `blocked_reason`) — and nothing armed. The program's most direct miss-to-evidence pairing to date. YXT (773,106 ticks + 96,144 NBBO) and INLF (539,177 + 65,141) are pinned into the golden archive, pruner-immune. YXT also crossed \$20 intraday — the second Ross-day validation of L12's origin band in three days.

## 8. State

- main: `c2b37b3` (#989–#995 all landed).
- #996 open, CI starved; contains production observability (raw-reason sampling) — prefer a CI verdict before merge.
- Still zero fills, ever. But the distance to the answer has collapsed: from "why no fills?" (a day of forensics) to "read `raw_samples` in the next census line" — one re-seal away.
