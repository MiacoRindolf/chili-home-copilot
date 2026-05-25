# COWORK_REVIEW: f-chili-env-pin-pytest-asyncio

**Date:** 2026-05-23
**CC_REPORT:** [`2026-05-23_f-chili-env-pin-pytest-asyncio.md`](../CC_REPORTS/2026-05-23_f-chili-env-pin-pytest-asyncio.md)
**Session duration:** ~9.9 min (after a failed first attempt — see "Dispatch friction" below). Single commit: `f0eb044`. 4 files touched (CLAUDE.md +1, requirements.txt ±1, NEXT_TASK.md +52/-31, CC_REPORT +41). Zero app code.

## TL;DR

The pin fix and CLAUDE.md diagnostic ship cleanly — Option B (widen `<1` → `<2`) is the correct call CC made during plan-gate. **But the `pip install --upgrade` verification step bumped ~16 unpinned deps**, including **pandas 2.x → 3.0.3** and **numpy 2.x → 2.4.6** on the operator's chili-env. The repo's HEAD didn't change for those packages (no commits to requirements.txt for them), but the host's running env is now on a major-version-bumped pandas and numpy. That's a real risk surface for the trading code which has explicit version-sensitivity notes elsewhere in `requirements.txt`. Recommended follow-up: write a small "tighten unbounded deps in requirements.txt" brief, and re-verify the trading test suite passes on the bumped env before the next CC session uses it.

## What's good

**The plan-gate discovery.** Pre-dispatch I'd assumed the existing pin was fine; CC discovered the env was running 1.3.0 — already OUTSIDE the spec's `<1` ceiling. That's a real spec-vs-reality drift the brief didn't anticipate, and CC handled it correctly: chose the smallest fix (widen `<1` → `<2`) rather than the larger restructuring options. The reasoning in plan.request.md ("Option B over A: would force a downgrade … Option B over C: forcing future devs onto 1.x is a bigger scope decision than this docs brief is empowered to make") is exactly the kind of explicit-tradeoff thinking that earns autonomy.

**Verification went end-to-end.** Installed-version probe → `pip install --upgrade` → `pytest --collect-only` → grep both acceptance checks. CC reported the 6/6 collection result with the timing (0.24s). No assertion, only evidence.

**Commit hygiene.** Single commit, 4 files all in scope, no `git add .` sweeping. The commit message explicitly records the spec-vs-reality drift discovery (chili-env hit 1.3.0 during the 2026-05-23 brain-runtime session). Anyone running `git blame` on line 92 six months from now will see exactly why the upper bound moved.

**Stale-comment self-flag.** CC noticed `requirements.txt:74` ("pytest 9 needs pytest-asyncio 1.x") is provably wrong — pytest 8.x runs cleanly with asyncio 1.x — and explicitly DIDN'T touch it because the brief's "Out of scope" line excluded pytest itself. Right discipline; surfacing the stale comment in Open Questions is the correct channel.

## What I'd flag — the pandas/numpy drift

CC_REPORT §40 (Open Question §2) reports that `pip install -r requirements.txt --upgrade` bumped sixteen unrelated packages because they have no upper-bound pin. Looking at the list:

- **pandas 3.0.3** — major version bump from 2.x. The repo has form on this: `yfinance>=1.2.0,<2` was upper-bounded specifically because "silent behavior changes on a provider we can't rescue from" (`requirements.txt` comment, lines 23–26). Pandas is the same shape of risk: it sits under most of the trading code. Pandas 3.0 has deprecated `inplace=True` in several places and changed `dtypes` defaults — both are quietly destructive in code that relied on prior behavior.
- **numpy 2.4.6** — `numpy>=1.24.0,<3` says 2.x was intentional; 2.4 is inside the allowed range. Lower risk but still worth noting alongside pandas.
- **aiohttp 3.13.5, playwright 1.60.0, twilio 9.10.9** — major bumps for surfaces that touch HTTP/browser/SMS code paths.
- **yfinance 1.4.0** — INSIDE the existing `<2` upper bound, fine.

The bump only affected the operator's local chili-env, not HEAD. Production Docker images haven't been rebuilt against the new versions. So nothing is *broken* in prod right now. But:

1. The operator's local `pytest`/`docker compose` runs are now executing against pandas 3.0.3, numpy 2.4.6, etc. Trading-code tests that previously passed could silently fail on numerical edges (rounding, NaN propagation, dtype inference).
2. The next CC session that runs `pip install -r requirements.txt --upgrade` will pull in the same bumps in its env. The same risk compounds.
3. Production Docker images will eventually be rebuilt against `requirements.txt`. When they are, all of the above bumps land in the running brain-worker and chili containers.

**Recommended follow-up (write this as the next QUEUED brief):**

- **`f-chili-env-tighten-unbounded-deps`** — add upper-bound pins to pandas (`<3`), aiohttp (`<4`), playwright (`<2`), twilio (`<10`), authlib (`<2`), and any others CC's upgrade output revealed. Goal: make `pip install --upgrade` truly idempotent against the current `requirements.txt`, not silently destructive.
- **Before that ships, run the trading-test suite** on the operator's bumped env to surface any pandas 3.0 / numpy 2.4 regressions early. `conda run -n chili-env pytest tests/test_*opportunity* tests/test_*pattern* tests/test_*fast* -x --tb=short`. If the suite is clean, the bumps are probably safe; if anything fails on a dtype/inplace/loc semantics, that's the brief becoming urgent.

## Answers to CC's open questions

**Q1 — Rewrite `requirements.txt:74` stale comment.** Yes, but tiny. The line says "pytest 9 needs pytest-asyncio 1.x; stay on 8.x until that's evaluated", which today's evidence (pytest 8.4.2 + asyncio 1.3.0 running clean) contradicts. Fix: change to something like `# pytest-asyncio 1.x is required for pytest 8.4+ collection compat; pytest 9 evaluation deferred independently`. Fold into the upper-bounds brief above; not a separate brief.

**Q2 — pandas/numpy/etc. unpinned bump.** Answered in detail above — this is the most important deliverable from this session, even though the session's scope was nominally about pytest-asyncio. Write the tightening brief.

**Q3 — Diagnostic line referencing 74 vs 92.** Leave it pointing at line 92 (the actual pin). Once Q1 is shipped, line 74 won't be misleading anymore. Don't pre-bake a "see also" that will be obsolete in a week.

## Dispatch friction (worth recording)

Two attempts to ship this. Attempt 400 failed in 2.8s because the session JSON's `prompt` contained the literal text `pip install -r requirements.txt --upgrade` inside backticks, and claude.exe parsed `--upgrade` as a top-level CLI flag, throwing `error: unknown option '--upgrade'`. Attempt 401 (this session) cut the prompt down to "follow the brief in NEXT_TASK.md" and skipped inlining flag-bearing shell commands.

**Lesson:** JSON session prompts should NEVER inline shell commands containing CLI flags inside backticks/code-fences. The flag tokens get re-parsed by the shell that invokes claude.exe even when wrapped in markdown formatting. Two future-proofing options:
1. Always make the session prompt a pointer ("execute the brief in NEXT_TASK.md") rather than a self-contained instruction set.
2. Or strip `--` flags from the prompt and use plain language ("upgrade-install the requirements file").

Recording this here so the next Cowork knows; the queued briefs all follow pattern (1) going forward.

## Recommended next moves

1. **Operator:** Decide on the `f-chili-env-tighten-unbounded-deps` brief. If yes, I'll write it. If you want to verify the trading suite passes on the bumped env first, run the pytest oneliner from the §"recommended follow-up" section above and tell me what fell over.
2. **Or skip ahead to the global a11y reduced-motion pass** brief (already in QUEUED) — that's independent of the env-pin work and unblocked.
3. **Or hold for tomorrow's Phase 5B day-2 soak probe** — that re-promotion is the standing item.

## Verdict

Ship. The pin widening is correct; the CLAUDE.md diagnostic is the right shape; the spec-vs-reality drift was discovered and resolved with sound reasoning. The unintended consequence (pandas/numpy bumps) is worth a follow-up brief but doesn't undermine this commit.

— Cowork (interactive)
