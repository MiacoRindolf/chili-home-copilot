"""The replay bench's fail-closed invariants — PURE, importable, no DB, no network.

Every function here exists because a specific replay result was WRONG and looked
right. A replay that measures silence, reads a contaminated sink, downsamples the
behaviour under test out of existence, or fills against a mock configured
differently from the one the baseline used still prints a clean number with a
plus sign in front of it. That is the most expensive failure mode we have: it
looks like a finding.

So the rule is FAIL CLOSED. These raise ``AssertionError`` naming the incident
rather than returning a bool a caller can forget to check, and the driver calls
the startup subset (1, 2, 4, 5, 9) before it loads a single tick.

Deliberately dependency-free (stdlib only) so a scorer, a plan builder, a test,
or the driver itself can import it without pulling in ``app.config`` / a
DATABASE_URL. ``verify_tree`` shells out to git only when its injectable
``head_reader`` is not supplied.

Runnable: pytest tests/test_replay_harness_invariants.py -v
"""
from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# 1. DENSITY
# ─────────────────────────────────────────────────────────────────────────────

# Question kinds whose ANSWER IS THE DOWNSAMPLED-AWAY DETAIL. An exit ladder, an
# order-flow read and a human-ledger bench all live in the seconds between the
# prints a stride throws away.
DENSE_QUESTION_MARKERS: tuple[str, ...] = ("exit", "flow", "bench")

# 1 keeps every print; 2 is the documented ceiling that still resolved the same
# fills in the 08-28 re-measure. Anything coarser is a different tape.
DENSE_STRIDE_MAX = 2


def assert_dense_stride(stride: Any, question: str, *, max_stride: int = DENSE_STRIDE_MAX) -> None:
    """A stride-N tape cannot answer a question about what happens between prints.

    ``question`` is the operator's declared question for the run. An EMPTY
    question asserts nothing (the pre-existing A/B callers declare none, so they
    stay byte-identical); declaring an exit/flow/bench question arms the floor.
    """
    # INCIDENT 2026-08-28 downsampling artifact: the same symbol/window replayed
    # +$193.92 at TICK_STRIDE=1 and -$4.66 at TICK_STRIDE=10 — the published
    # "exit churn" finding was the stride, not the strategy.
    q = str(question or "").strip().lower()
    if not q:
        return
    if not any(m in q for m in DENSE_QUESTION_MARKERS):
        return
    try:
        n = int(stride)
    except (TypeError, ValueError):
        raise AssertionError(
            f"assert_dense_stride: stride {stride!r} is not an integer; an "
            f"exit/flow/bench question ({question!r}) needs a declared dense stride"
        ) from None
    if n < 1 or n > int(max_stride):
        raise AssertionError(
            f"assert_dense_stride: TICK_STRIDE={n} cannot answer {question!r}. "
            f"Exit/flow/bench questions require stride 1..{int(max_stride)} — measured "
            "2026-08-28: the same window replayed +$193.92 at stride 1 and -$4.66 at "
            "stride 10, so the 'exit churn' finding was a downsampling artifact. "
            "Re-run with TICK_STRIDE=1 (or declare a different question)."
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. CLEAN SINK
# ─────────────────────────────────────────────────────────────────────────────

KEEP_SINK_ENV = "REPLAY_KEEP_SINK"


def assert_clean_sink(env: Mapping[str, str]) -> None:
    """The sink must be reset for this run. ``REPLAY_KEEP_SINK`` is not a shortcut."""
    # INCIDENT 2026-08-29 sink contamination: a REUSED sink moved a MIMI baseline
    # from +60.60 to +46.59 with NO code change, and nearly rejected the correct
    # 0.6R-rung experiment (#1240) on a ghost kill inherited from a prior run.
    raw = str((env or {}).get(KEEP_SINK_ENV, "") or "").strip()
    if raw and raw != "0":
        raise AssertionError(
            f"assert_clean_sink: {KEEP_SINK_ENV}={raw!r} keeps the previous run's "
            "sessions, lockouts and viability rows in the sink. Measured 2026-08-29: a "
            "reused sink moved a baseline +60.60 -> +46.59 with no code change. Unset it "
            "(a deliberate multi-run accumulation study must not go through this driver)."
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. INTERLEAVING
# ─────────────────────────────────────────────────────────────────────────────

def interleave(cases: Sequence[Any], arms: Sequence[Any]) -> list[tuple[Any, Any]]:
    """Build a CASE-MAJOR plan: every arm of one window runs before the next window."""
    # INCIDENT: the live source DB drifts under a 120-hour frame warm-up, so an
    # all-A-then-all-B plan measures the DRIFT, not the arm.
    if not cases:
        raise AssertionError("interleave: no cases")
    if not arms:
        raise AssertionError("interleave: no arms")
    if len(set(map(str, arms))) != len(arms):
        raise AssertionError(f"interleave: duplicate arms {list(arms)!r}")
    return [(c, a) for c in cases for a in arms]


def assert_interleaved(plan: Sequence[tuple[Any, Any]]) -> None:
    """Every case's arms must be CONTIGUOUS and COMPLETE — never all-A then all-B."""
    # INCIDENT: the source tape and the 5-day frame warm-up move between the first
    # and last run of a batch; blocking by arm hands that drift to one arm only.
    if not plan:
        raise AssertionError("assert_interleaved: empty plan")
    order: list[Any] = []
    runs: dict[Any, list[Any]] = {}
    for case, arm in plan:
        if case not in runs:
            order.append(case)
            runs[case] = []
        runs[case].append(arm)

    # Contiguity: a case may not reappear after another case has started.
    seen: list[Any] = []
    for case, _arm in plan:
        if seen and seen[-1] != case and case in seen:
            raise AssertionError(
                f"assert_interleaved: case {case!r} is SPLIT — its runs are not "
                "contiguous, which is the all-A-then-all-B shape. The live source DB "
                "drifts under the 120-hour frame warm-up, so arm-blocked plans measure "
                "the drift. Use interleave(cases, arms)."
            )
        if not seen or seen[-1] != case:
            seen.append(case)

    arms_first = list(runs[order[0]])
    for case in order:
        got = runs[case]
        if len(set(map(str, got))) != len(got):
            raise AssertionError(f"assert_interleaved: case {case!r} repeats an arm: {got!r}")
        if set(map(str, got)) != set(map(str, arms_first)):
            raise AssertionError(
                f"assert_interleaved: case {case!r} runs {got!r} but {order[0]!r} runs "
                f"{arms_first!r} — every window must run every arm."
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. AS-OF READS (AST)
# ─────────────────────────────────────────────────────────────────────────────

# The sim clock. Every as-of slice and every tape-feature helper must reach it.
SIM_CLOCK_TOKEN = "_utcnow"

# Wall-clock calls that silently re-introduce look-ahead / trailing-now reads.
WALL_CLOCK_TOKENS: tuple[str, ...] = ("datetime.now(", "datetime.utcnow(", "time.time(")

# The re-point sites that anchor the tape-feature helpers on the sim clock. Each
# is a monkeypatch the driver installs; losing one silently degrades a whole
# gate family to "reads empty" without changing a single printed number's shape.
REQUIRED_SIM_CLOCK_ANCHORS: tuple[str, ...] = (
    "schedule_window_now",        # else sched_mult 0.0 => entry placement skipped
    "signed_tape_accel_features",  # else the buyers-confirm tape read finds NOTHING
    "_utcnow_for_bars",           # else every bar reads as long-complete (08-19 YJ fix)
    # else the spread-distribution derate reads the WALL clock over a sink the NBBO
    # mirror pre-loaded in full: measured p50=210.0 n=60 where the as-of answer is
    # p50=20.0 n=36, and the gate's verdict then depends on the CALENDAR DATE the
    # bench runs (>20 days after the tape it reads nothing and fails open).
    "name_spread_percentiles",
)


def _find_function(tree: ast.AST, name: str, *, cls: Optional[str] = None) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if cls is not None:
            if not (isinstance(node, ast.ClassDef) and node.name == cls):
                continue
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                    return sub  # type: ignore[return-value]
            raise AssertionError(f"no method {cls}.{name}")
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name!r}")


def _reaches_sim_clock(tree: ast.AST, value: ast.AST) -> bool:
    """True when ``value`` reads the sim clock directly, or names a function that does
    (the driver assigns a nested ``def`` for the tape-feature re-point)."""
    if SIM_CLOCK_TOKEN in ast.unparse(value):
        return True
    if isinstance(value, ast.Name):
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == value.id
                and SIM_CLOCK_TOKEN in ast.unparse(node)
            ):
                return True
    return False


def assert_as_of_reads(driver_src: str) -> None:
    """The as-of provider must slice on ``<= sim-now``, and the tape helpers must be
    anchored to the sim clock — never the wall clock."""
    # INCIDENT: with a trailing real-now() read the mirrored tape looks EMPTY, so the
    # buyers-confirm gate and the forming-bar volume normalization fail closed and the
    # replay silently re-runs the exact bug live was already cured of.
    tree = ast.parse(driver_src)
    call = _find_function(tree, "__call__", cls="AsOfProvider")
    body = ast.unparse(call)

    anchored: set[str] = set()
    for node in ast.walk(call):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and SIM_CLOCK_TOKEN in ast.unparse(node.value):
                anchored.add(tgt.id)
    if not anchored:
        raise AssertionError(
            "assert_as_of_reads: AsOfProvider.__call__ binds no name from the sim "
            f"clock ({SIM_CLOCK_TOKEN}) — an as-of slice against the wall clock reads "
            "the mirrored tape as empty."
        )

    sliced = False
    for node in ast.walk(call):
        if isinstance(node, ast.Compare) and any(isinstance(o, ast.LtE) for o in node.ops):
            for comp in node.comparators:
                if isinstance(comp, ast.Name) and comp.id in anchored:
                    sliced = True
    if not sliced:
        raise AssertionError(
            "assert_as_of_reads: AsOfProvider.__call__ has no `<= sim-now` slice — "
            "an unbounded read LOOKS AHEAD past the replayed instant."
        )

    for tok in WALL_CLOCK_TOKENS:
        if tok in body:
            raise AssertionError(
                f"assert_as_of_reads: AsOfProvider.__call__ calls {tok!r} — the as-of "
                "slice must read the SIM clock, not the wall clock."
            )

    for anchor in REQUIRED_SIM_CLOCK_ANCHORS:
        ok = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Attribute)
                        and tgt.attr == anchor
                        and _reaches_sim_clock(tree, node.value)
                    ):
                        ok = True
        if not ok:
            raise AssertionError(
                f"assert_as_of_reads: {anchor!r} is not re-pointed at the sim clock "
                f"({SIM_CLOCK_TOKEN}). Without it that gate family reads the mirrored "
                "tape as empty and the replay measures silence."
            )


# ─────────────────────────────────────────────────────────────────────────────
# 5. TIE-STABLE TAPE SQL (AST)
# ─────────────────────────────────────────────────────────────────────────────

# Recorded tape/book relations the driver reads. Equal ``observed_at`` values are
# COMMON (a burst prints many rows inside one millisecond).
TAPE_TABLES: tuple[str, ...] = (
    "iqfeed_trade_ticks",
    "momentum_nbbo_spread_tape",
    "iqfeed_depth_snapshots",
)

TIE_STABLE_TAIL = "order by observed_at asc, id asc"


def _sql_constants(driver_src: str) -> list[tuple[int, str]]:
    tree = ast.parse(driver_src)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((int(getattr(node, "lineno", 0)), node.value))
    return out


def assert_tie_stable_sql(driver_src: str) -> None:
    """Every tape SELECT must end ``ORDER BY observed_at ASC, id ASC``."""
    # INCIDENT: rows sharing an ``observed_at`` previously came back in PHYSICAL SCAN
    # ORDER, so the same window mirrored in a different row order and produced
    # divergent fills — an A/B delta that was really a heap-layout delta.
    offenders: list[tuple[int, str]] = []
    for lineno, raw in _sql_constants(driver_src):
        flat = " ".join(raw.split()).lower()
        if "select" not in flat:
            continue
        if not any(f"from {t}" in flat for t in TAPE_TABLES):
            continue
        if not flat.rstrip().endswith(TIE_STABLE_TAIL):
            offenders.append((lineno, " ".join(raw.split())[:140]))
    if offenders:
        raise AssertionError(
            "assert_tie_stable_sql: tape SELECT(s) are not tie-stable — equal "
            f"observed_at values fall back to physical scan order. Append "
            f"'ORDER BY observed_at ASC, id ASC':\n"
            + "\n".join(f"  line {ln}: {sql}" for ln, sql in offenders)
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. TREE VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def _git_head(build_dir: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(build_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"verify_tree: cannot read HEAD of {build_dir!r}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _git_resolve(build_dir: str, ref: str) -> Optional[str]:
    """The commit a REF names, resolved inside the build tree. None if unresolvable.

    Without this, ``verify_tree`` compares a 40-hex HEAD against whatever string the
    caller passed, so a symbolic ref can never match: ``--ref origin/main`` failed with
    "is at HEAD 'c5e627fdf…', not 'origin/main'" while the tree was EXACTLY at
    origin/main. That forced callers to pass a raw sha, which defeats naming a ref at
    all and quietly invites the very staleness this guard exists to catch — a caller who
    hardcodes yesterday's sha passes the check every time.

    Unresolvable is NOT an error here: it falls through to the literal comparison, so a
    caller passing a sha still works and a typo'd ref still fails loudly.
    """
    proc = subprocess.run(
        ["git", "-C", str(build_dir), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True, check=False,
    )
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and out else None


def verify_tree(
    build_dir: str,
    ref: str,
    sentinel_file: str,
    sentinel: str,
    *,
    head_reader: Callable[[str], str] = _git_head,
    ref_resolver: Optional[Callable[[str, str], Optional[str]]] = None,
) -> str:
    """The tree that RAN must be the tree we think ran: HEAD == ref AND the sentinel
    string is actually present in ``sentinel_file``."""
    # INCIDENT: a stale local branch (the fix was never checked out in the build tree)
    # produced a confident "the fix works" result for code that was not in the run.
    head = str(head_reader(str(build_dir)) or "").strip()
    want = str(ref or "").strip()
    if not want:
        raise AssertionError("verify_tree: no ref given")
    # Resolve the ref FIRST, so a symbolic name (origin/main, a branch, a tag) is compared
    # as the commit it points at rather than as a string a 40-hex HEAD can never equal.
    resolved = _git_resolve(str(build_dir), want) if ref_resolver is None else ref_resolver(str(build_dir), want)
    want_sha = resolved or want
    if not (head == want_sha or head.startswith(want_sha) or want_sha.startswith(head)):
        shown = f"{want!r}" if resolved is None else f"{want!r} ({want_sha})"
        raise AssertionError(
            f"verify_tree: {build_dir!r} is at HEAD {head!r}, not {shown} — a stale "
            "build tree once produced a fake 'fix works' result for code that never ran."
        )
    path = Path(build_dir) / sentinel_file
    if not path.exists():
        raise AssertionError(f"verify_tree: sentinel file {str(path)!r} does not exist")
    if str(sentinel) not in path.read_text(encoding="utf-8", errors="replace"):
        raise AssertionError(
            f"verify_tree: sentinel {sentinel!r} is ABSENT from {sentinel_file!r} — HEAD "
            "matches but the working tree does not carry the change under test."
        )
    return head


# ─────────────────────────────────────────────────────────────────────────────
# 7. COLD-START TAGGING
# ─────────────────────────────────────────────────────────────────────────────

# Events that COMMIT the lane. A commitment taken before the runner has state is
# taken on inherited/warm-up state, not on the window under test.
DECISIVE_EVENT_MARKERS: tuple[str, ...] = (
    "entry_candidate", "entry_submitted", "entry_filled",
    "exit_filled", "partial_exit", "bailout", "stopped",
    "benched", "blocked", "veto",
)


def _as_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def cold_start_tags(
    events: Iterable[Mapping[str, Any]],
    runner_started_ts: Any,
    min_ticks: int,
    min_uptime_s: float,
) -> list[dict[str, Any]]:
    """Tag decisive events that fired before the runner had ``min_ticks`` ticks or
    ``min_uptime_s`` seconds of uptime. Returns tags; it does NOT raise."""
    # INCIDENT 2026-09-02 (#1287 A/B): a cold-start seed armed on the FIRST grid step
    # against a frame HOD inherited from the warm-up and blocked a +1.515R entry — the
    # only delta in an otherwise identical 4-window A/B.
    t0 = _as_dt(runner_started_ts)
    tags: list[dict[str, Any]] = []
    for idx, ev in enumerate(events or []):
        et = str((ev or {}).get("event_type") or "")
        if not any(m in et for m in DECISIVE_EVENT_MARKERS):
            continue
        ts = _as_dt((ev or {}).get("ts"))
        uptime = (ts - t0).total_seconds() if (ts is not None and t0 is not None) else None
        raw_ticks = (ev or {}).get("ticks_seen")
        ticks = int(raw_ticks) if isinstance(raw_ticks, (int, float)) else None
        reasons: list[str] = []
        if uptime is not None and uptime < float(min_uptime_s):
            reasons.append(f"uptime_{uptime:.1f}s_lt_{float(min_uptime_s):g}s")
        if ticks is not None and ticks < int(min_ticks):
            reasons.append(f"ticks_{ticks}_lt_{int(min_ticks)}")
        if reasons:
            tags.append({
                "index": idx,
                "event_type": et,
                "ts": ts.isoformat() if ts is not None else None,
                "uptime_s": (round(uptime, 3) if uptime is not None else None),
                "ticks_seen": ticks,
                "reasons": reasons,
            })
    return tags


# ─────────────────────────────────────────────────────────────────────────────
# 8. ADDITIVE-COUNT CHECK
# ─────────────────────────────────────────────────────────────────────────────

def additive_count_check(
    previous: Mapping[str, int],
    current: Mapping[str, int],
    *,
    tolerance: int = 0,
) -> None:
    """Two identical runs on a CLEAN sink produce IDENTICAL counts. Growth means the
    previous run's rows survived and this run's numbers are additive on top of them."""
    # INCIDENT 2026-08-29: post-run counts that grew run-over-run were the visible
    # surface of the sink contamination that moved a baseline +60.60 -> +46.59.
    grew = []
    for key, cur in (current or {}).items():
        prev = (previous or {}).get(key)
        if prev is None:
            continue
        if int(cur) > int(prev) + int(tolerance):
            grew.append((key, int(prev), int(cur)))
    if grew:
        raise AssertionError(
            "additive_count_check: post-run counts are ADDITIVE versus the previous run "
            "— the sink was not clean, so this run's result sits on top of the last one:\n"
            + "\n".join(f"  {k}: {p} -> {c} (+{c - p})" for k, p, c in grew)
        )


# ─────────────────────────────────────────────────────────────────────────────
# 9. MOCK PARITY
# ─────────────────────────────────────────────────────────────────────────────

# The validated $0.05-fidelity parity-fixture config (replay_parity.py:219). Any
# deviation is a DIFFERENT fill model, so its PnL is not comparable to a baseline.
REQUIRED_MOCK_CONFIG: dict[str, Any] = {
    "resting_limit_fills": True,
    "volume_cap_enabled": True,
    "fill_mode": "conservative",
    "freshness_mode": "wall",
}


def _mock_attr(mock: Any, name: str) -> Any:
    for candidate in (name, f"_{name}"):
        if hasattr(mock, candidate):
            return getattr(mock, candidate)
    raise AssertionError(f"assert_mock_parity: mock exposes no {name!r}")


def assert_mock_parity(mock: Any) -> None:
    """The mock broker must carry the validated parity-fixture fill config."""
    # INCIDENT: resting_limit_fills=False caused exit-ladder submit SPAM, and a
    # non-conservative / uncapped mock over-credits fills the recorded tape could not
    # have supplied — either way the PnL is not comparable to any baseline.
    wrong = []
    for key, want in REQUIRED_MOCK_CONFIG.items():
        got = _mock_attr(mock, key)
        norm = str(got).strip().lower() if isinstance(got, str) else got
        if norm != want:
            wrong.append((key, want, got))
    if wrong:
        raise AssertionError(
            "assert_mock_parity: mock broker is NOT the validated parity config "
            "($0.05 fidelity, replay_parity.py:219) — its PnL is not comparable:\n"
            + "\n".join(f"  {k}: want {w!r}, got {g!r}" for k, w, g in wrong)
        )


__all__ = [
    "assert_dense_stride", "assert_clean_sink", "interleave", "assert_interleaved",
    "assert_as_of_reads", "assert_tie_stable_sql", "verify_tree", "cold_start_tags",
    "additive_count_check", "assert_mock_parity",
    "DENSE_QUESTION_MARKERS", "DENSE_STRIDE_MAX", "KEEP_SINK_ENV",
    "REQUIRED_SIM_CLOCK_ANCHORS", "TAPE_TABLES", "TIE_STABLE_TAIL",
    "DECISIVE_EVENT_MARKERS", "REQUIRED_MOCK_CONFIG",
]

# ── invariant 12: monkeypatch wrappers forward EVERY argument (2026-09-05) ──────────────
def simclock_default_wrapper(fn, clock, *, key):
    """Wrap ``fn`` so that keyword ``key`` defaults to ``clock()`` when the caller passes
    None (or omits it) -- and forward EVERY other positional/keyword argument untouched.

    MEASURED 2026-09-05 (harness gate 14): the driver's previous wrapper around
    ``entry_gates.signed_tape_accel_features`` re-declared the signature as
    ``(symbol, *, db, window_s, as_of)`` and so rejected ``settings_obj`` (added by #1024,
    2026-08-11). ``tape_confirms_hold`` passes ``settings_obj`` on every call and swallows
    the TypeError into its fail-closed branch => ``(False, tape_hold_error)`` in EVERY
    replay, while the identical read offline confirmed (JWEL 2026-08-10 11:31:47Z: accel
    +6,899, tick_rate 137 >= floor 53). A wrapper must never own the wrapped signature."""
    import functools

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        if kwargs.get(key) is None:
            kwargs[key] = clock()
        return fn(*args, **kwargs)

    _wrapped._simclock_wrapped = fn  # type: ignore[attr-defined]
    _wrapped._simclock_key = key  # type: ignore[attr-defined]
    return _wrapped

