# NEXT_TASK: f-chili-env-pin-pytest-asyncio

STATUS: DONE

**Source:** CC_REPORT §54 + Open Question §5 in `2026-05-23_f-brain-runtime-tab-redesign.md`, ACK'd in the corresponding COWORK_REVIEW.
**Scope:** Mostly a docs / diagnostic note. Minimal env spec change (verify the existing pin is sufficient, possibly tighten it). ~10 minutes.
**Risk:** Low. Only touches developer environment + CLAUDE.md, not runtime code.

## Pre-dispatch finding (read first)

When this brief was originally drafted, CC's CC_REPORT (§54) said `pytest-asyncio` "is not pinned anywhere". Pre-dispatch grep against `requirements.txt` line 92 shows:

```
pytest-asyncio>=0.23.8,<1   # 0.23.8+ for pytest 8.x Package.obj collection compat
```

**The pin already exists and the comment already documents the exact failure mode** CC hit. So this brief is NOT "add a missing pin"; it's:

1. **Verify the existing pin is the minimum sufficient.** CC's local env had a 0.23.3 install (per CC_REPORT §54) — which would have been blocked by the `>=0.23.8` floor in requirements.txt IF CC had reinstalled from the file. The problem was env staleness, not missing pin.
2. **Tighten if useful.** If the version CC's session ended up using is significantly newer than 0.23.8, consider raising the floor to that version (still keeping `<1` upper bound). Or leave the existing `>=0.23.8,<1` as-is — both are defensible.
3. **Add the CLAUDE.md diagnostic line** so the next operator who hits the collection error knows the fix is `pip install -r requirements.txt --upgrade` inside `chili-env`, not a code change.
4. **Verify** that a fresh `conda run -n chili-env pip install -r requirements.txt && conda run -n chili-env pytest --collect-only tests/test_brain_runtime_endpoints.py -q` collects cleanly.

## Tasks

1. **Discover the installed version.** Run:
   ```
   conda run -n chili-env python -c "import pytest_asyncio; print(pytest_asyncio.__version__)"
   ```
   Capture the version. Decide whether to tighten the existing `>=0.23.8` pin to that version.
2. **Update `requirements.txt` line 92** if tightening. If leaving as-is, that's a no-op — record the decision in the CC report's "What shipped" section as "no change needed; pin already correct".
3. **Add to `CLAUDE.md`** under "Environment & runtime", as a new line at the end of that section: "If pytest fails to collect with `'Package' object has no attribute 'obj'`, the env's `pytest-asyncio` is older than the floor in `requirements.txt:92`. Recreate or upgrade: `conda run -n chili-env pip install -r requirements.txt --upgrade`."
4. **Verify.** Run:
   ```
   conda run -n chili-env pip install -r requirements.txt --upgrade
   conda run -n chili-env pytest --collect-only tests/test_brain_runtime_endpoints.py -q
   ```
   First command should be idempotent (or upgrade pytest-asyncio if your local was stale). Second should collect 6 tests cleanly.

## Acceptance

- `grep -n 'pytest-asyncio' requirements.txt` shows the (possibly tightened, possibly unchanged) pin.
- `grep -n "Package' object has no attribute 'obj'" CLAUDE.md` shows the new diagnostic note.
- `pytest --collect-only` works.
- ONE commit, docs+config only, no app code touched.

## Commit message

```
chore(chili-env): document pytest-asyncio recovery diagnostic in CLAUDE.md

[Optionally: + tighten requirements.txt pin from >=0.23.8 to >=<actual-version>.]

Pre-dispatch grep showed requirements.txt:92 already pins pytest-asyncio
>=0.23.8,<1 with a comment about the Package.obj collection bug. The
2026-05-23 brain-runtime-tab-redesign session hit the bug because its
chili-env had a stale 0.23.3 install — the pin was correct, the env was
behind it. Adding a CLAUDE.md diagnostic line so the next operator who
sees the error knows the fix is `pip install -r requirements.txt --upgrade`,
not a code change.
```

## Out of scope

- Migrating to a different test framework or pytest major-version upgrade.
- Pinning anything besides pytest-asyncio. The existing pytest pin (`>=8.2,<9`) and pytest-cov pin (`>=7.0.0,<8`) are already coherent.
- Reproducing the original failure (CC already saw it; closing the prevention loop is enough).
- Creating an `environment.yml`. The repo uses `requirements.txt` as the env spec; that's the convention.

## Rollback

`git revert <commit>` — the change is a one-line CLAUDE.md addition (and optionally a tightened pin in requirements.txt). Reverting is trivial.
