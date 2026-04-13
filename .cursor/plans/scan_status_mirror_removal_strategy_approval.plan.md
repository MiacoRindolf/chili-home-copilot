# scan/status — external consumer audit & mirror-removal strategy (approval-ready)

**Implementation status (in-repo):** §6 Release 1 shipped with `compat_mirrors` (default on). **Releases 2–3** are implemented on `main` together: happy path has **no** root mirror keys and the **`compat_mirrors` query is removed** (no escape hatch — external integrators use `brain_runtime` or pin an older revision). **`encode_error`** JSON remains **frozen** per `docs/TRADING_BRAIN_WORK_LEDGER.md`. Top-level **`learning`** unchanged.

Binding context (treated as true for this document): `brain_runtime` is primary for operators; in-repo Brain desk prefers `brain_runtime` on happy path; top-level `work_ledger` / `release` / `scheduler` / `scan` are legacy mirrors; `release` is always `{}`; no `release.git_commit` / SHA gates; top-level `learning` stays for graph / full snapshot / mutex-sensitive uses; this document is **strategy only** — no mirror removal executed here.

---

## 1. Executive verdict

**Is mirror-removal planning-ready now?**  
**Yes, for in-repo proven consumers.** The repo has **no remaining success-path dependency** on flat mirrors in `app/templates/brain.html` (`scanStatusBrainRuntime` uses `brain_runtime` when `encode_error` is absent and `brain_runtime.work_ledger` is an object). **Removal is not execution-ready for production** until **unknown external consumers** are addressed (see §4).

**Main risks**

- **Silent external clients** (dashboards, old curl recipes, forks) that still read `data.work_ledger` et al. at the top level — **not visible in this repo**.
- **Encode-error / degraded path** today still expects **flat mirrors** in `brain.html` fallback branch; any removal must **preserve a defined degraded contract** (either keep flat keys only on `encode_error`, or move degraded data entirely under `brain_runtime`).
- **Tests** currently **require** mirror keys and equality — they **block** removal until rewritten (expected).

**Main wins**

- Smaller payloads, single source of truth under `brain_runtime`, fewer “which key wins?” bugs, clearer operator contract.

**Already proven (repo)**

- Only **two** runtime code paths call `GET /api/trading/scan/status` in templates: `brain.html` (`pollLearningStatus`, `tbnUpdateNodeStatuses`).  
- **Graph overlay** uses **`d.learning` only** (not mirrors).  
- **Operator strip** uses **`scanStatusBrainRuntime` → `brain_runtime`** on success.  
- **`chili_mobile`** has **no** hits for `scan/status` / `brain_runtime` / `work_ledger` in grep.  
- **`scripts/prove_execution_feedback_ledger.py`** only **prints** a human hint pointing at `brain_runtime.work_ledger` (not a parser).  
- **`loadInspectHealthCard`** uses `d.scheduler` from **`/api/trading/inspect/health`**, not `scan/status` — **not** a mirror consumer.

**Still unknown**

- Anything **outside** this repository: personal scripts, notebooks, reverse proxies caching JSON, third-party monitors, old deployment runbooks, browser snippets, MCP tools, **forks**.

---

## 2. Current contract summary

**What `brain_runtime` guarantees (happy path)**  
From `app/routers/trading_sub/ai.py`: `work_ledger`, `release` (always `{}`), `scheduler`, `scan`, `learning_summary` (operator fields including `status_role`, `tickers_processed`), `activity_signals` (four minimal keys), `compatibility_mirror_keys`, `compatibility_mirror_note`. This aggregate is intended as the **primary operator/runtime read path** per router docstring.

**What top-level mirrors currently guarantee**  
The same four objects are **duplicated** at the top level for backward compatibility; `tests/test_scan_status_brain_runtime.py` asserts **`data["work_ledger"] == br["work_ledger"]`** (and likewise `release`, `scheduler`, `scan`).

**Why `learning` stays top-level (this phase)**  
- **Graph overlay** (`tbnUpdateNodeStatuses` in `brain.html`) reads **`d.learning`** for `graph_node_id`, `mesh_*`, cluster/step indices, `secondary_miners_skipped`, etc.  
- **Full reconcile snapshot** consumers need the **complete** object returned by `get_learning_status()` (merged with `status_role` in the payload), not only `learning_summary`.  
- **Mutex / single-flight semantics** are tied to that live status object; **no change** to `learning.running` or shape in this slice (per scope).

**Why `release == {}` is correct**  
Fingerprint was **intentionally removed** (`31ca070`); empty `release` preserves JSON shape without lying about deploy revision.

---

## 3. Consumer inventory table

| Consumer name | File/location | Reads `brain_runtime` or flat mirror | Internal / external | Criticality | Blocks removal? | Migration | Notes |
|---------------|---------------|----------------------------------------|---------------------|-------------|-----------------|-----------|-------|
| **scan/status producer** | `app/routers/trading_sub/ai.py` | Builds both | Internal | Runtime | **Yes** until changed | N/A | Single writer; strategy changes here. |
| **Brain desk — operator strip** | `app/templates/brain.html` `pollLearningStatus` | **`brain_runtime`** via `scanStatusBrainRuntime` on success | Internal | Runtime | **No** (success path) | Done | Flat mirrors only in fallback when `encode_error` or missing aggregate. |
| **Brain desk — graph overlay** | `app/templates/brain.html` `tbnUpdateNodeStatuses` | **Top-level `learning` only** (not mirrors) | Internal | Runtime | **No** for mirror removal | N/A | Out of scope for mirror removal. |
| **Brain desk — scanStatusBrainRuntime fallback** | `app/templates/brain.html` | **Flat mirrors** if `encode_error` or no `brain_runtime.work_ledger` | Internal | Degraded | **Maybe** | Keep degraded contract or redefine | Must be specified in removal PR. |
| **Contract tests** | `tests/test_scan_status_brain_runtime.py` | Asserts mirrors exist and equal `brain_runtime` | Internal | CI | **Yes** until updated | Rewrite assertions | Expected gate. |
| **Ledger / ops hint (human)** | `scripts/prove_execution_feedback_ledger.py` | Doc string only → `brain_runtime.work_ledger` | Internal | Ops | **No** | N/A | Not a JSON parser. |
| **Docs — neural mesh** | `docs/trading-brain-neural-mesh-v2.md` | Describes **`learning`** fields on `scan/status` | Internal | Docs | **No** for mirrors | N/A | Confirms `learning` stays top-level for overlay fields. |
| **Docs — work ledger** | `docs/TRADING_BRAIN_WORK_LEDGER.md` | Describes API; mirror checklist | Internal | Docs | **No** | Update on removal | Already notes mirror removal checklist. |
| **Cursor rules / LC plans** | `.cursor/rules/...`, `.cursor/plans/...` | Meta | Internal | DevEx | **No** | Update text | Keep in sync after API change. |
| **Production / fork / notebook / curl users** | *Not in repo* | **Unknown** | External | Unknown | **Maybe** | Announce + compat window | **Cannot prove from repo.** |

---

## 4. Unknown consumer risk

**Cannot be known from repo inspection**

- Operators or integrations that **`curl` or `jq`** top-level `.work_ledger` / `.scheduler` / `.scan` / `.release`.
- **Forks** of `chili-home-copilot` not searched.
- **Browser bookmarks** to DevTools “copy as cURL” from before `brain_runtime`.
- **Monitoring** or **log pipelines** that parse full JSON and key off flat paths.

**Safe assumptions**

- **In-repo** Brain desk **success path** does not need flat mirrors (proven in `brain.html`).  
- **Mobile app in `chili_mobile/`** shows **no** `scan/status` usage in grep (no proven mobile consumer here).

**Unsafe assumptions**

- “Nobody uses flat keys” in **production**.  
- “We can delete mirrors in one PR with no comms.”

**How to de-risk despite unknowns**

1. **One release** with **documented deprecation** + optional **`compat_mirrors=1`** (or default-on compat for one release then flip — see §6).  
2. **Release notes / operator changelog** with before/after JSON example.  
3. **Staging** soak with `compat_mirrors` off (or mirrors omitted) before prod.  
4. **Rollback**: redeploy previous image or re-enable compat param.  
5. **Observability (optional tiny aid)**: e.g. log **one structured line** per process at debug when a request uses `compat_mirrors=1` — counts real usage (implementation in a **later** slice if approved).

---

## 5. Strategy options

### A. Hard removal in one release

| | |
|--|--|
| **Pros** | Simplest code; smallest long-term surface. |
| **Cons** | **Highest** breakage risk for unknown externals; hard rollback = redeploy. |
| **Burden** | Medium (router + tests + docs + brain fallback path). |
| **Consumer risk** | **High** |
| **Recommendation** | **Not first** — only after compat window or proven zero external use. |

### B. Query-param or settings-gated mirrors (`?compat_mirrors=1`)

| | |
|--|--|
| **Pros** | **Controlled** rollback; can measure usage; default can move from `true` → `false`. |
| **Cons** | Permanent “wart” unless removed after sunset; must document param forever or version API. |
| **Burden** | Medium–high (router branching, tests for both modes). |
| **Consumer risk** | **Low** if default keeps mirrors one release, then default drops mirrors with param escape hatch. |
| **Recommendation** | **Strong candidate** as **transitional** mechanism. |

### C. Deprecation period (release notes + one version warning), then removal

| | |
|--|--|
| **Pros** | Social/process safety; pairs well with B. |
| **Cons** | Slower; relies on humans reading notes. |
| **Burden** | Low process cost; same code work as A/B eventually. |
| **Consumer risk** | Medium without B; **Low** with B. |
| **Recommendation** | **Always do** the comms part; **pair with B** for safety. |

### D. Keep mirrors indefinitely, document as legacy

| | |
|--|--|
| **Pros** | Zero breakage. |
| **Cons** | **Perpetual duplication**; invites new bugs; contradicts “primary path is `brain_runtime`.” |
| **Burden** | None. |
| **Consumer risk** | None. |
| **Recommendation** | **Unacceptable** as a **final** state — acceptable only as a **short** stabilization pause. |

---

## 6. Recommended path (opinionated)

**Combine C + B, then A.**

1. **Release 1 (deprecation + telemetry-ready):**  
   - Document in **release notes** and `TRADING_BRAIN_WORK_LEDGER.md`: flat keys **deprecated**; migrate to `brain_runtime`.  
   - Implement **`?compat_mirrors=1`** (or header — query param is simpler for curl) where **when absent or `0`**, happy-path response **omits** top-level `work_ledger`, `release`, `scheduler`, `scan` **OR** keep them but add **`brain_runtime.deprecated_top_level_mirrors: true`** — *pick one explicit behavior in the implementation slice* (prefer **omit** when defaulting to new world, with param **forcing** mirrors back).  
   - **Default for Release 1:** still emit mirrors (`compat` default **on**) to avoid surprise — *or* default off if you only ship to controlled staging first. **Production recommendation:** **default mirrors ON for exactly one release** with loud docs, then **default OFF** with `compat_mirrors=1` escape hatch for Release 2.

2. **Release 2:** Default **no** flat mirrors; `compat_mirrors=1` restores current shape for stragglers. Monitor/support window (e.g. 4–8 weeks operator-defined).

3. **Release 3 (hard removal A):** Remove param and mirror code paths; **encode-error** response defined to still return **useful** `brain_runtime` fragments only (no duplicate top-level).

**Why not hard A first?** Unknown externals make **silent500s or empty UI** in invisible places too likely.

**Why not D forever?** Duplication undermines the last several phases of work and invites drift.

---

## 7. Implementation blueprint for the next removal slice (no code in this doc)

**Goal:** Implement **gated or omitted** top-level mirrors per §6 **without** touching top-level `learning`.

**Likely files**

- `app/routers/trading_sub/ai.py` — `api_scan_status`: branch on `Request` query `compat_mirrors`; build `payload` with or without top-level mirror keys; adjust `encode_error` JSON similarly; keep **`learning` last** when mirrors present; if mirrors omitted, key order becomes `ok`, `brain_runtime`, `prescreen`, `learning` (and update tests).  
- `tests/test_scan_status_brain_runtime.py` — split or parametrize: **with** `compat_mirrors=1` assert legacy shape; **without** assert mirrors absent and `brain_runtime` unchanged.  
- `app/templates/brain.html` — already falls back to flat keys only when aggregate missing; if API omits mirrors on success, **no change** needed on success path; verify fallback still sane if `encode_error` shape changes.  
- `docs/TRADING_BRAIN_WORK_LEDGER.md` — param semantics, default, sunset.  
- `.cursor/rules/chili-scan-status-deploy-validation.mdc` — Phase 0 checklist: mirror keys may be absent; `release` still `{}` inside `brain_runtime`.  
- `.cursor/plans/scan_status_mirror_removal_readiness.plan.md` — mark phases done.

**API behavior (to specify in freeze)**

- **Happy path:** `brain_runtime` full; `learning` full; `prescreen` unchanged.  
- **Mirrors:** omitted unless `compat_mirrors=1` (exact name frozen at implementation).  
- **`encode_error`:** either (a) keep minimal flat keys for last-resort clients, or (b) only `brain_runtime` partial + `learning` empty — **pick (a) or (b)** in freeze; (a) is lower risk for unknown parsers.

**Tests**

- Default new-world response: no top-level mirror keys.  
- Compat: mirrors equal `brain_runtime` slices.  
- Graph: still parse `learning`.  
- `release == {}` inside `brain_runtime` always.

**`learning`**

- **No** move under `brain_runtime`; **no** field removals.

---

## 8. Validation / rollout plan

| Stage | Action |
|-------|--------|
| **Local** | `pytest tests/test_scan_status_brain_runtime.py` + any new compat tests; manual Brain desk: strip, graph overlay, reconcile details. |
| **CI** | Full trading test subset if touching router imports. |
| **Staging** | Deploy with new default; run curl with/without `compat_mirrors`; compare JSON size and keys. |
| **Prod** | Release notes; optional **1-release** default mirrors ON if team wants extra cushion; then flip default. |
| **Monitor** | Support tickets / logs; if using optional debug counter for `compat_mirrors=1`, watch count. |
| **Rollback** | Redeploy prior image **or** set default to restore mirrors / force compat param server-side via config (if you add server-side override). |

**Rollback triggers:** external integration breakage reports; unexpected empty operator UI on non-Brain clients.

---

## 9. Explicit deferrals

- **Top-level `learning` removal or nesting** under `brain_runtime.learning_full` — **separate** program.  
- **Graph payload redesign** — out of scope.  
- **Mutex / `learning.running` replacement** — out of scope.  
- **Broad API versioning** — only add what’s needed for `compat_mirrors` semantics.  
- **Proving or migrating unknown external consumers** — cannot be completed from repo; handled via **compat window + comms**.

---

## Phase 0 — current-state check (evidence summary)

| # | Claim | Evidence |
|---|--------|----------|
| 1 | `brain_runtime` primary & sufficient for in-repo operator display | `ai.py` docstring; `learning_summary` + `activity_signals`; `brain.html` `pollLearningStatus` uses `scanStatusBrainRuntime` → aggregate on success. |
| 2 | Brain desk does not depend on flat mirrors on success | `brain.html` `useAggregate = !encErr && wlBr != null && typeof wlBr === 'object'` then reads only `br.*`. |
| 3 | Top-level mirrors still exist and equal `brain_runtime` | `ai.py` payload assigns same `*_st` to both; tests assert equality. |
| 4 | `learning` top-level for graph / full snapshot | `tbnUpdateNodeStatuses` uses `d.learning`; `pollLearningStatus` merges `learning_summary` onto `d.learning` for operator fields. |
| 5 | `release == {}` | Tests `assert br.get("release") == {}` and top-level same. |
| 6 | Tests / rules / docs reflect contract | `test_scan_status_brain_runtime.py`, `chili-scan-status-deploy-validation.mdc`, `TRADING_BRAIN_WORK_LEDGER.md`, `lc_shrink_validation_reset.plan.md`, `scan_status_mirror_removal_readiness.plan.md`. |

**Mismatch call-out:** None found between **stated** contract and **in-repo** success-path behavior. **Only** gap is **unprovable external** mirror consumers — treated as **risk**, not contradiction.

---

*This file is the approval-ready strategy. Implementation belongs in a separate frozen slice after sign-off.*
