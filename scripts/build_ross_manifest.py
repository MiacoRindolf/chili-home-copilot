#!/usr/bin/env python
"""Build the Ross ground-truth manifest (chili.ross_ground_truth_manifest.v1).

Deterministic merge of four evidence layers. Layers 1-3 live under the
ross_video_evidence/ tree; layer 4 is the master ledger that sits beside it in
../ross/. Precedence runs highest-first:

  1. curated_windows.json  — hand-curation of the 4 frame-audited AUDIT_REPORT
     files (broker panels = ground truth). WINS on conflict.
  2. */trades_*.json       — transcript-extraction recap files
     (chili.ross_recap_trades.v1 / chili.ross_daily_recap_trades.v1).
     One manifest row per trade; one expected_action="reject" row per
     watchlist no-trade entry.
  3. review_manifest.json  — per-(video, symbol) certifiability booleans.
     Applied LAST: ross_trade_outcome_certifiable=false caps pnl_confidence
     at "narrated"; trade_no_trade_certifiable=false appends a note.
  4. ../ross/ross_master_ledger.json (chili.ross_master_ledger.v1) — the
     187-row cross-video master ledger. LOWEST precedence: a frame-audited
     curated window always wins a conflict, and a layer-2 recap row that
     already covers the same (symbol, date, account, kind) absorbs the ledger
     row rather than being duplicated by it.

Output: <evidence dir>/manifest.json

Path overrides (the evidence tree and the code tree live on different drives on
the operator's box, so NEITHER is hardcoded into a caller's workflow):
  ROSS_EVIDENCE_DIR   — root of the ross_video_evidence tree (layers 1-3 + output)
  ROSS_MASTER_LEDGER  — full path to ross_master_ledger.json (layer 4)
Precedence for both: CLI flag > environment variable > the module default below.
The module defaults are the operator's current on-disk locations; they are the
only two absolute paths in this file.

Rules baked in:
  * NO database access; tape.live_covered / tape.golden_pinned stay null.
  * small vs main account is preserved per row and NEVER summed.
  * usage_constraints is copied VERBATIM from
    tests/fixtures/ross_replay/small_account_challenge_manifest.json.
  * Merge (curated wins, fill missing fields) only when exactly ONE curated
    row and exactly ONE trades-file row share (symbol, date, account, kind).
    Merge whitelist: side, window_et, ross_entry_px, catalyst,
    ross_net_usd+pnl_confidence (only when curated has none); refs are
    appended. ross_exit_px is deliberately NOT merged (recap exit levels are
    often the move's high, not Ross's fill — e.g. VRAX "7.40").
  * The master-ledger layer uses that same 1:1 rule (see merge_master) and the
    same ross_exit_px exclusion, for the same reason: the ledger's exit_px is
    transcript-derived, not a broker fill.
  * 0 is a NULL SENTINEL throughout the master ledger (prices, shares, pnl).
    A 0 pnl_usd is therefore read as ABSENT -> ross_net_usd None and
    expected_action None. It is never read as a scratch, because "he broke
    even" and "he never said" are not distinguishable in that field and
    grading the difference would invent an outcome.
  * generated_at is excluded from drift comparison; if content is otherwise
    unchanged the existing file (and its timestamp) is left untouched.

Usage:
  python scripts/build_ross_manifest.py           # write manifest.json
  python scripts/build_ross_manifest.py --check   # exit 1 if output would change
  python scripts/build_ross_manifest.py --evidence-dir D:/... --master-ledger D:/...
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

EVIDENCE_DIR = r"D:\dev\chili-home-copilot\project_ws\AgentOps\ross_video_evidence"
FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "fixtures", "ross_replay", "small_account_challenge_manifest.json",
)
OUT_PATH = os.path.join(EVIDENCE_DIR, "manifest.json")
SCHEMA = "chili.ross_ground_truth_manifest.v1"

TRADE_KEYS = ("trades", "ross_trades")
WATCH_KEYS = ("watchlist_no_trade", "watchlist_not_traded", "watchlist_passed")

# ---------------------------------------------------------------------------
# Path resolution. Resolved per call (not frozen at import) so that a test or
# an operator can set the environment variable and re-enter build() in the same
# process without reloading the module.
# ---------------------------------------------------------------------------


def evidence_dir():
    """Root of the ross_video_evidence tree (layers 1-3 and the output file)."""
    return os.environ.get("ROSS_EVIDENCE_DIR") or EVIDENCE_DIR


def master_ledger_path():
    """Full path to ross_master_ledger.json (layer 4).

    Default is derived from the evidence dir — the ledger is the sibling
    ``ross/`` directory of ``ross_video_evidence/`` in the operator's tree — so
    that overriding ROSS_EVIDENCE_DIR alone relocates BOTH, and there is no
    second absolute path to keep in sync.
    """
    override = os.environ.get("ROSS_MASTER_LEDGER")
    if override:
        return override
    return os.path.join(os.path.dirname(evidence_dir().rstrip("\\/")),
                        "ross", "ross_master_ledger.json")


def out_path():
    return os.path.join(evidence_dir(), "manifest.json")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _relref(path):
    p = path.replace("\\", "/")
    i = p.find("project_ws/")
    return p[i:] if i >= 0 else p


def _norm_account(txt, default=None):
    if not txt:
        return default
    t = str(txt).lower()
    if "small" in t:
        return "small"
    if "big" in t or "main" in t:
        return "main"
    return default


def _pnl_confidence(pnl_value, conf_text, pnl_note):
    """Map recap-file confidence prose to the manifest enum (deterministic)."""
    if pnl_value is None:
        return None
    note = (pnl_note or "").lower()
    t = (conf_text or "").lower()
    demote_terms = ("derived", "implied", "likely", "approx")
    demoted = any(x in note for x in demote_terms) or any(x in t for x in demote_terms)
    if "verbatim" in t or t.startswith("high"):
        conf = "stated_verbatim"
    elif t.startswith("medium"):
        conf = "narrated"
    else:
        conf = "inferred"
    if demoted and conf == "stated_verbatim":
        conf = "narrated"
    return conf


def _exit_px(trade):
    """Single unambiguous scalar exit price, else None."""
    exits = trade.get("exits")
    if not isinstance(exits, list):
        return None
    vals = []
    for e in exits:
        if not isinstance(e, dict):
            continue
        px = e.get("px_approx")
        if isinstance(px, (int, float)):
            vals.append(px)
        elif isinstance(px, list) and len(px) == 1 and isinstance(px[0], (int, float)):
            vals.append(px[0])
    uniq = sorted(set(vals))
    return uniq[0] if len(uniq) == 1 else None


def _window(manifest_id, video_id, date, symbol, account, ross_action,
            expected_action, side, window_et, entry_px, exit_px, net_usd,
            pnl_confidence, source_kind, refs, catalyst, notes,
            *, stated_entry_et=None, stated_exit_et=None,
            xref_verdict=None, xref_mechanism=None, tape_coverage=None):
    """Build one manifest row.

    The five keyword-only fields are an ADDITIVE extension carried by the
    master-ledger layer (layer 4). They default to None so every layer-1/2/3
    call site is unchanged, and they are emitted on EVERY row (null for the
    older layers) so consumers never have to branch on row shape. Adding them
    changes manifest.json content once; --check will report that drift until
    the manifest is rebuilt.
    """
    return {
        "manifest_id": manifest_id,
        "video_id": video_id,
        "date": date,
        "symbol": symbol,
        "account": account,
        "ross_action": ross_action,
        "expected_action": expected_action,
        "side": side,
        "window_et": window_et,
        "stated_entry_et": stated_entry_et,
        "stated_exit_et": stated_exit_et,
        "ross_entry_px": entry_px,
        "ross_exit_px": exit_px,
        "ross_net_usd": net_usd,
        "pnl_confidence": pnl_confidence,
        "source": {"kind": source_kind, "refs": list(refs)},
        "catalyst": catalyst,
        "notes": notes,
        "xref_verdict": xref_verdict,
        "xref_mechanism": xref_mechanism,
        "tape_coverage": tape_coverage,
        "tape": {"live_covered": None, "golden_pinned": None},
    }


def load_curated():
    doc = _load(os.path.join(evidence_dir(), "curated_windows.json"))
    rows = []
    for w in doc["windows"]:
        notes = w.get("notes")
        prov = w.get("pnl_provenance")
        if prov:
            notes = ((notes + " ") if notes else "") + "[pnl: " + prov + "]"
        rows.append(_window(
            w["manifest_id"], w["video_id"], w["date"], w["symbol"],
            w.get("account"), w["ross_action"], w["expected_action"],
            w.get("side"), w.get("window_et"), w.get("ross_entry_px"),
            w.get("ross_exit_px"), w.get("ross_net_usd"),
            w.get("pnl_confidence"), "frame_audit",
            w.get("source_refs", []), w.get("catalyst"), notes,
        ))
    return rows


def load_trades_files():
    """Yield (trade_rows, reject_rows) from every */trades_*.json."""
    paths = sorted(glob.glob(os.path.join(evidence_dir(), "*", "trades_*.json")))
    trade_rows, reject_rows = [], []
    for path in paths:
        j = _load(path)
        ref = _relref(path)
        video_id = j.get("video_id") or j.get("videoId")
        date = j.get("trading_day") or j.get("date")
        if not video_id or not date:
            raise ValueError("trades file missing video/date: " + path)
        broker = str(j.get("broker") or "")
        default_account = "small" if (
            j.get("challenge_day") is not None or "small" in broker.lower()
        ) else None

        trades = []
        for k in TRADE_KEYS:
            if isinstance(j.get(k), list):
                trades = j[k]
                break
        for i, t in enumerate(trades, start=1):
            symbol = t.get("symbol")
            if not symbol:
                continue
            account = _norm_account(t.get("account"), default_account)
            conf = t.get("confidence")
            conf_pnl = conf.get("pnl") if isinstance(conf, dict) else conf
            pnl = t.get("pnl_claimed")
            pnl_note = t.get("pnl_note") or t.get("day_pnl_note")
            notes_parts = [p for p in (
                t.get("entry_trigger") or t.get("entry_note"),
                t.get("entry_px_note"),
                pnl_note,
            ) if p]
            trade_rows.append(_window(
                "::".join((video_id, symbol, date, "t%d" % i)),
                video_id, date, symbol, account, "trade", "trade",
                t.get("side"), t.get("entry_et"), t.get("entry_px"),
                _exit_px(t), pnl,
                _pnl_confidence(pnl, conf_pnl, pnl_note),
                "transcript_extraction", [ref], t.get("catalyst"),
                "; ".join(notes_parts) or None,
            ))
            big = t.get("big_account_pnl_claimed_separate")
            if isinstance(big, (int, float)):
                trade_rows.append(_window(
                    "::".join((video_id, symbol, date, "t%d-big-acct" % i)),
                    video_id, date, symbol, "main", "trade", "trade",
                    t.get("side"), t.get("entry_et"), None, None, big,
                    "narrated", "transcript_extraction", [ref],
                    t.get("catalyst"),
                    "Separate big-account claim attached to the small-account "
                    "recap trade (big_account_pnl_claimed_separate); never "
                    "summed with the small-account row.",
                ))

        watch = []
        for k in WATCH_KEYS:
            if isinstance(j.get(k), list):
                watch = j[k]
                break
        for e in watch:
            symbol = (e.get("ticker") or e.get("symbol") or "").rstrip("?").strip()
            if not symbol:
                continue  # e.g. caption-garbled unnamed scan row
            notes_parts = [p for p in (
                e.get("reason"), e.get("why_passed"), e.get("note"),
                ("conditional trigger: " + e["conditional_trigger"])
                if e.get("conditional_trigger") else None,
                ("gap ~%s%%" % e["gap_pct_approx"])
                if e.get("gap_pct_approx") is not None else None,
                "symbol unverified (as heard: %s)" % e["as_heard"]
                if e.get("verified") is False and e.get("as_heard") else None,
            ) if p]
            at = e.get("approx_time_et")
            reject_rows.append(_window(
                "::".join((video_id, symbol, date, "watchlist")),
                video_id, date, symbol, None, "no_trade", "reject",
                None, ("~" + at) if at else None, None, None, None, None,
                "transcript_extraction", [ref], None,
                "; ".join(notes_parts) or None,
            ))
    return trade_rows, reject_rows


# ---------------------------------------------------------------------------
# Layer 4 — chili.ross_master_ledger.v1
#
# Everything below is written against the SHAPE OF THE ACTUAL FILE, measured on
# the 187 trade rows of ross_master_ledger.json as read on 2026-09-04:
#   * `_path` tells trade rows from no-trade rows. 137 "trades" + 20 "rows" are
#     trades; 16 "no_trade_references" + 11 "misses_and_no_trades" +
#     3 "ross_no_trade_context" are NOT (they were folded in by the builder's
#     walk_lists heuristic out of 5 different sub-schemas).
#   * `entry_time_et` is narrative prose, not a timestamp: 132 rows start with a
#     clock, 14 mention one only mid-sentence ("premarket, before ~09:00 (clock
#     time not stated)"), 11 have none. Only the anchored form is parsed — a
#     mid-sentence clock is usually the thing the entry is being described
#     RELATIVE to, so reading it as the entry time would be a guess.
#   * 0 is a null sentinel: entry_px 67 zeros, exit_px 103, shares 118,
#     pnl_usd 30.
#   * `account`: main 49 / small 25 / big 16 / absent 97.
#   * `confidence`: inferred 90 / approx 59 / exact 8 / absent 30.
# ---------------------------------------------------------------------------

MASTER_LEDGER_SCHEMA = "chili.ross_master_ledger.v1"

# _path buckets. A row in NO_TRADE_PATHS records that Ross did not take the
# symbol (or missed it), which is a different claim from "Ross traded it".
MASTER_TRADE_PATHS = ("trades", "rows")
MASTER_NO_TRADE_PATHS = (
    "no_trade_references", "misses_and_no_trades", "ross_no_trade_context",
)

# Anchored at the START of the field only — see the note above about
# mid-sentence clocks. Optional leading "~" because the ledger writes both
# "07:32:45" and "~08:35".
_CLOCK_RE = re.compile(r"^\s*~?\s*(\d{1,2}):([0-5]\d)(?::([0-5]\d))?")
# Same anchor, plus a range separator. Measured: 74 entry_time_et fields open
# with a range, and ASCII "-" is the ONLY separator any of them uses
# ("~08:40-09:00", "~09:00-09:20 window"). En/em dash are accepted defensively
# because the surrounding prose in this ledger is hand-written; that branch is
# currently unexercised by the real file.
_RANGE_RE = re.compile(
    r"^\s*~?\s*(\d{1,2}):([0-5]\d)(?::[0-5]\d)?\s*[-–—]\s*~?\s*(\d{1,2}):([0-5]\d)"
)
# A bare ticker. Length ceiling 6 covers every US listing; '.' and '-' cover
# class suffixes (BRK.A / BRK-A). No lowercase: the ledger writes tickers upper.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")
# "FCUV (4.80/break of 5; ...)" — ticker immediately followed by a parenthetical.
_TICKER_PREFIX_RE = re.compile(r"^([A-Z][A-Z0-9.\-]{0,5})\s*\(")

# confidence prose -> manifest pnl_confidence enum. The ledger's vocabulary is
# closed (exact/approx/inferred, measured above); anything else is treated as
# unknown-and-therefore-inferred rather than silently promoted.
MASTER_PNL_CONFIDENCE = {
    "exact": "stated_verbatim",
    "approx": "narrated",
    "inferred": "inferred",
}


def _master_clock(text):
    """Anchored clock at the head of a narrative field, else None.

    Returns the clock verbatim-normalised as "HH:MM" or "HH:MM:SS" (zero-padded
    hour). Refuses anything it cannot read at the head of the string: an
    unparseable field becomes null, never an estimate.
    """
    if text is None:
        return None
    m = _CLOCK_RE.match(str(text))
    if not m:
        return None
    hh = int(m.group(1))
    if hh > 23:
        return None
    if m.group(3) is not None:
        return "%02d:%s:%s" % (hh, m.group(2), m.group(3))
    return "%02d:%s" % (hh, m.group(2))


def _master_clock_hhmm(stated):
    """Minute-resolution form of a value returned by _master_clock()."""
    if not stated:
        return None
    return stated[:5]


def _master_range_end(text):
    """End of an anchored "A-B" range at the head of the field, else None."""
    if text is None:
        return None
    m = _RANGE_RE.match(str(text))
    if not m:
        return None
    if int(m.group(1)) > 23 or int(m.group(3)) > 23:
        return None
    return "%02d:%s" % (int(m.group(3)), m.group(4))


def _master_window_et(entry_text, exit_text, stated_entry, stated_exit):
    """Compose the manifest window_et string from parsed clocks only.

    Requires a parsed ENTRY clock: without one there is nothing to anchor the
    window to, and an exit clock alone would label the window with a time Ross
    was already out. The window end prefers the parsed exit clock over the end
    of an entry-side range, because an entry range only bounds the entry.
    """
    if not stated_entry:
        return None
    start = _master_clock_hhmm(stated_entry)
    end = _master_clock_hhmm(stated_exit) or _master_range_end(entry_text)
    if end and end != start:
        return "~%s-%s" % (start, end)
    return "~%s" % start


def _master_symbol(raw):
    """Return (symbol, note) — never invent a ticker.

    Measured: 183 of the 187 ledger symbols are already bare tickers. The other
    four are 'FCUV (4.80/break of 5; ...)', 'NUWE (09:30 pivot 5.34)',
    'EDBL/LGHL/BIYA' and 'MGRX / REPL / KUST'. A trailing parenthetical is
    stripped because the token before '(' is unambiguously the ticker. A
    slash-joined string is NOT split: choosing one of three would silently
    delete the other two, so it is kept verbatim and flagged, which makes the
    row visible to a human and un-joinable to a tape (the honest outcome).
    """
    s = str(raw or "").strip()
    if not s:
        return None, None
    if _TICKER_RE.match(s):
        return s, None
    m = _TICKER_PREFIX_RE.match(s)
    if m:
        return m.group(1), ("ledger symbol carried a parenthetical annotation; "
                            "verbatim: %s" % s)
    return s, ("ledger symbol is not a single ticker and was NOT split; "
               "verbatim: %s" % s)


def _master_account(raw):
    """Stated account only. Never inferred.

    Unlike layer 2 (which defaults to "small" for challenge-day recap files),
    this layer refuses to guess: 97 of 187 ledger rows state no account, and
    defaulting those to "main" would fabricate the very small-vs-main split
    that must never be summed. Stated "big" collapses to "main" (the ledger
    uses both words for the same non-challenge account). A row that names both
    accounts is ambiguous and yields None.
    """
    if raw is None:
        return None
    t = str(raw).strip().lower()
    if not t:
        return None
    small = "small" in t
    main = ("main" in t) or ("big" in t)
    if small and main:
        return None
    if small:
        return "small"
    if main:
        return "main"
    return None


def _master_px(value):
    """Price scalar, with the ledger's 0 read as ABSENT (see module docstring)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return None if value == 0 else float(value)


def _master_net_usd(value):
    """pnl_usd with 0 read as ABSENT.

    A 0 here cannot be told apart from "he never stated it" — the ledger's own
    row notes say so ("pnl ABSENT -> 0 placeholder") — so it is dropped rather
    than booked as a scratch. This is load-bearing: a fabricated 0 would count
    as a real avoided loss in Avoidance and as a real captured 0 in Capture.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return None if value == 0 else float(value)


def _master_expected_action(net_usd):
    """trade when Ross made money, reject when he lost it, None when unknown.

    Matches the layer-1 convention (a curated losing trade such as TC
    -394.89 carries ross_action="trade" with expected_action="reject").
    """
    if net_usd is None:
        return None
    return "trade" if net_usd > 0 else "reject"


def _master_pnl_confidence(conf_text, net_usd):
    """Ledger confidence enum -> manifest enum; None when there is no figure."""
    if net_usd is None:
        return None
    key = str(conf_text or "").strip().lower()
    return MASTER_PNL_CONFIDENCE.get(key, "inferred")


def _master_xref_index(doc):
    """(symbol, date) -> xref row, keeping only unambiguous keys.

    Measured on the 68 xref rows read 2026-09-04: (symbol, date) is unique, so
    every key is currently 1:1. The duplicate bucket exists so a later ledger
    build that breaks that property degrades to "carry nothing and say so"
    instead of silently attaching one of two verdicts.
    """
    by_key = {}
    for row in doc.get("xref") or []:
        if not isinstance(row, dict):
            continue
        symbol, _note = _master_symbol(row.get("symbol"))
        date = row.get("date")
        if not symbol or not date:
            continue
        by_key.setdefault((symbol, date), []).append(row)
    return by_key


def _master_xref_fields(xref_rows):
    """(verdict, mechanism, tape_coverage, note) for a (symbol, date) join."""
    if not xref_rows:
        return None, None, None, None
    if len(xref_rows) > 1:
        return None, None, None, (
            "xref join ambiguous: %d master-ledger xref rows share this "
            "(symbol, date); no verdict carried" % len(xref_rows))
    row = xref_rows[0]
    # Two sub-schemas: the wide rows use "chili_verdict", the narrower
    # crossref_batch12/13 rows use "verdict". Both mean the same thing.
    verdict = row.get("chili_verdict") or row.get("verdict")
    return verdict, row.get("mechanism"), row.get("tape_coverage"), None


def _master_refs(ledger_ref, ledger_dir, row):
    """Evidence refs for one ledger row: the ledger, its batch file, its sources."""
    refs = [ledger_ref]
    src = row.get("_src")
    if src:
        batch = _relref(os.path.join(ledger_dir, str(src)))
        if batch not in refs:
            refs.append(batch)
    for path in row.get("source_files") or []:
        # Measured: of 402 source_files entries, 299 are already
        # project_ws-relative and 103 are absolute D: paths. _relref normalises
        # both to the project_ws-relative form (it slices from "project_ws/"),
        # so no drive letter reaches the manifest.
        p = _relref(str(path))
        if p not in refs:
            refs.append(p)
    return refs


def _master_notes(row, extra):
    """Notes for one ledger row: the ledger's own prose plus our provenance flags.

    Nothing here is summarised or reworded — an operator reading the manifest
    must be able to recover why a field is null without opening the ledger.
    """
    parts = []
    for label, key in (
        ("kind", "kind"),
        ("setup", "setup"),
        ("why Ross took it", "ross_reason"),
        ("detail", "detail"),
        ("why", "why"),
        ("Ross", "ross"),
        ("CHILI", "chili"),
        ("adds", "adds"),
        ("partials", "partials"),
        ("stop", "stop_px"),
        ("note", "note"),
        ("ledger notes", "notes"),
    ):
        value = row.get(key)
        if value in (None, "", 0, []):
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        parts.append("%s: %s" % (label, value))
    parts.extend(x for x in extra if x)
    return "; ".join(parts) or None


def load_master_ledger(path=None):
    """Layer 4. Return manifest rows built from ross_master_ledger.json.

    `path` overrides the resolved ROSS_MASTER_LEDGER / default location; it
    exists so tests can drive this function against a fixture without touching
    the operator's evidence tree.
    """
    ledger = path or master_ledger_path()
    doc = _load(ledger)
    got = doc.get("schema")
    if got != MASTER_LEDGER_SCHEMA:
        raise ValueError(
            "master ledger schema mismatch: expected %s, got %r (%s)"
            % (MASTER_LEDGER_SCHEMA, got, ledger))
    ledger_ref = _relref(ledger)
    ledger_dir = os.path.dirname(ledger)
    xref_index = _master_xref_index(doc)

    rows = []
    seen = {}  # (video_id, symbol, date) -> ordinal, so manifest_ids stay unique
    for i, t in enumerate(doc.get("trades") or []):
        if not isinstance(t, dict):
            raise ValueError("master ledger trades[%d] is not an object" % i)
        symbol, symbol_note = _master_symbol(t.get("symbol"))
        video_id = t.get("video_id")
        date = t.get("date")
        if not symbol:
            continue  # no ticker at all: nothing a replay could ever be keyed on
        if not video_id or not date:
            raise ValueError(
                "master ledger trades[%d] missing video_id/date: %r"
                % (i, {"symbol": symbol, "video_id": video_id, "date": date}))

        path_bucket = t.get("_path")
        is_trade = path_bucket in MASTER_TRADE_PATHS
        if not is_trade and path_bucket not in MASTER_NO_TRADE_PATHS:
            raise ValueError(
                "master ledger trades[%d] has unknown _path %r; classify it "
                "before it can be graded" % (i, path_bucket))

        n = seen.get((video_id, symbol, date), 0) + 1
        seen[(video_id, symbol, date)] = n
        manifest_id = "::".join((video_id, symbol, date, "ml%d" % n))

        flags = []
        if symbol_note:
            flags.append(symbol_note)

        if is_trade:
            net_usd = _master_net_usd(t.get("pnl_usd"))
            expected_action = _master_expected_action(net_usd)
            entry_px = _master_px(t.get("entry_px"))
            exit_px = _master_px(t.get("exit_px"))
            stated_entry = _master_clock(t.get("entry_time_et"))
            stated_exit = _master_clock(t.get("exit_time_et"))
            window_et = _master_window_et(
                t.get("entry_time_et"), t.get("exit_time_et"),
                stated_entry, stated_exit)
            ross_action = "trade"
            side = t.get("side")
            if net_usd is None and t.get("pnl_usd") == 0:
                flags.append("pnl_usd is the ledger's 0 sentinel (absent, not a "
                             "scratch): expected_action left null, ungradeable")
            elif net_usd is None:
                flags.append("no pnl_usd in ledger: expected_action left null")
            if entry_px is None and t.get("entry_px") == 0:
                flags.append("entry_px is the ledger's 0 sentinel (absent)")
            if exit_px is None and t.get("exit_px") == 0:
                flags.append("exit_px is the ledger's 0 sentinel (absent)")
            if t.get("entry_time_et") and not stated_entry:
                flags.append("entry time not parseable as a clock; ledger text "
                             "verbatim: %s" % t["entry_time_et"])
            if t.get("exit_time_et") and not stated_exit:
                flags.append("exit time not parseable as a clock; ledger text "
                             "verbatim: %s" % t["exit_time_et"])
        else:
            # A no-trade / miss record. It states that Ross did NOT take the
            # symbol; it does NOT state whether taking it would have been right.
            # expected_action therefore stays null rather than defaulting to
            # "reject" — several of these are MISSES (e.g. ZYBT 0.60 -> 4.50),
            # and grading a miss as a correct reject would teach the wrong
            # lesson. Layer 2's watchlist rows keep their own "reject" mapping;
            # this layer is not asserting the same thing.
            net_usd = None
            expected_action = None
            entry_px = exit_px = None
            stated_entry = stated_exit = window_et = None
            ross_action = "no_trade"
            side = None
            flags.append("master-ledger no-trade record (_path=%s): Ross did "
                         "not trade it; the ledger does not state whether "
                         "trading it would have been correct, so "
                         "expected_action is null" % path_bucket)

        verdict, mechanism, coverage, xref_note = _master_xref_fields(
            xref_index.get((symbol, date)))
        if xref_note:
            flags.append(xref_note)

        rows.append(_window(
            manifest_id, video_id, date, symbol, _master_account(t.get("account")),
            ross_action, expected_action, side, window_et, entry_px, exit_px,
            net_usd, _master_pnl_confidence(t.get("confidence"), net_usd),
            "master_ledger", _master_refs(ledger_ref, ledger_dir, t),
            # The ledger has no catalyst field. `setup` is a chart setup, not a
            # news catalyst, so it goes to notes and catalyst stays null rather
            # than being filled with the wrong kind of string.
            None,
            _master_notes(t, flags),
            stated_entry_et=stated_entry, stated_exit_et=stated_exit,
            xref_verdict=verdict, xref_mechanism=mechanism,
            tape_coverage=coverage,
        ))
    return rows


def merge(curated, trade_rows, reject_rows):
    """Curated wins; 1:1 same-trade rows from recap files merge into it."""
    def key(r, kind):
        return (r["symbol"], r["date"], r["account"], kind)

    cur_index = {}
    for r in curated:
        kind = "trade" if r["ross_action"] == "trade" else "reject"
        cur_index.setdefault(key(r, kind), []).append(r)

    out = list(curated)
    for rows, kind in ((trade_rows, "trade"), (reject_rows, "reject")):
        by_key = {}
        for r in rows:
            by_key.setdefault(key(r, kind), []).append(r)
        for k, group in by_key.items():
            targets = cur_index.get(k, [])
            if len(targets) == 1 and len(group) == 1:
                cur, src = targets[0], group[0]
                for field in ("side", "window_et", "ross_entry_px", "catalyst"):
                    if cur[field] is None and src[field] is not None:
                        cur[field] = src[field]
                if cur["ross_net_usd"] is None and src["ross_net_usd"] is not None:
                    cur["ross_net_usd"] = src["ross_net_usd"]
                    cur["pnl_confidence"] = src["pnl_confidence"]
                for ref in src["source"]["refs"]:
                    if ref not in cur["source"]["refs"]:
                        cur["source"]["refs"].append(ref)
                extra = "Merged with transcript extraction %s (curated wins on conflict)." % src["manifest_id"]
                cur["notes"] = ((cur["notes"] + " ") if cur["notes"] else "") + extra
            else:
                out.extend(group)
    return out


def _merge_kind(row):
    """Merge bucket for a row: layer 2 and layer 4 both file no_trade under reject."""
    return "trade" if row["ross_action"] == "trade" else "reject"


def _absorb_master(target, src, rule):
    """Fill target's missing fields from a master-ledger row. Target wins."""
    for field in ("side", "window_et", "ross_entry_px", "catalyst",
                  "stated_entry_et", "stated_exit_et",
                  "xref_verdict", "xref_mechanism", "tape_coverage"):
        if target[field] is None and src[field] is not None:
            target[field] = src[field]
    if target["ross_net_usd"] is None and src["ross_net_usd"] is not None:
        # Figure and its confidence move together or not at all.
        target["ross_net_usd"] = src["ross_net_usd"]
        target["pnl_confidence"] = src["pnl_confidence"]
    for ref in src["source"]["refs"]:
        if ref not in target["source"]["refs"]:
            target["source"]["refs"].append(ref)
    # expected_action is deliberately NOT filled from the ledger: it is the
    # graded answer, and layers 1-2 always set it, so filling it here could only
    # ever overwrite an audited verdict with a transcript-derived one.
    # ross_exit_px is deliberately NOT merged, for the reason documented on the
    # layer-2 merge: recap/ledger exit levels are often the move's high rather
    # than Ross's fill.
    note = ("Merged with master-ledger row %s on %s (existing row wins on "
            "conflict; ross_exit_px and expected_action not merged)."
            % (src["manifest_id"], rule))
    target["notes"] = ((target["notes"] + " ") if target["notes"] else "") + note


def merge_master(rows, master_rows):
    """Fold layer 4 into the already-merged layers 1-2. LOWEST precedence.

    Pass 1 is the same 1:1 rule the existing layers use: exactly one existing
    row and exactly one ledger row sharing (symbol, date, account, kind).

    Pass 2 exists because this layer refuses to guess an account (see
    _master_account): 97 of 187 ledger rows leave it unstated, so a ledger row
    for a trade that layer 1 or 2 already carries as account="small" would miss
    pass 1 on the account component alone and be appended as a duplicate ground
    -truth row for the same trade. Pass 2 therefore retries the ledger rows
    whose account is UNSTATED against (symbol, date, kind), still 1:1 on both
    sides, and still only into a row that has not already absorbed a ledger
    row. An unstated account cannot contradict a stated one, so this widens the
    key without ever merging a small-account row into a main-account row.

    Returns a new list; `rows` is mutated in place where a merge landed.
    """
    absorbed = set()  # id() of existing rows that already took a ledger row

    existing_exact = {}
    for r in rows:
        existing_exact.setdefault(
            (r["symbol"], r["date"], r["account"], _merge_kind(r)), []).append(r)

    by_exact = {}
    for m in master_rows:
        by_exact.setdefault(
            (m["symbol"], m["date"], m["account"], _merge_kind(m)), []).append(m)

    leftover = []
    for key, group in by_exact.items():
        targets = [r for r in existing_exact.get(key, []) if id(r) not in absorbed]
        if len(targets) == 1 and len(group) == 1:
            _absorb_master(targets[0], group[0], "(symbol, date, account, kind)")
            absorbed.add(id(targets[0]))
        else:
            leftover.extend(group)

    existing_relaxed = {}
    for r in rows:
        existing_relaxed.setdefault(
            (r["symbol"], r["date"], _merge_kind(r)), []).append(r)

    unresolved, by_relaxed = [], {}
    for m in leftover:
        if m["account"] is None:
            by_relaxed.setdefault(
                (m["symbol"], m["date"], _merge_kind(m)), []).append(m)
        else:
            unresolved.append(m)
    for key, group in by_relaxed.items():
        targets = [r for r in existing_relaxed.get(key, []) if id(r) not in absorbed]
        if len(targets) == 1 and len(group) == 1:
            _absorb_master(
                targets[0], group[0],
                "(symbol, date, kind) with the ledger account unstated")
            absorbed.add(id(targets[0]))
        else:
            unresolved.extend(group)

    return list(rows) + unresolved


def apply_review_downgrades(rows):
    reviews = _load(os.path.join(evidence_dir(), "review_manifest.json"))["reviews"]
    cert = {}
    for r in reviews:
        k = (r["evidence_id"], r["symbol"])
        prev = cert.get(k, {"tnc": True, "rtoc": True})
        cert[k] = {
            "tnc": prev["tnc"] and bool(r.get("trade_no_trade_certifiable", True)),
            "rtoc": prev["rtoc"] and bool(r.get("ross_trade_outcome_certifiable", True)),
        }
    for row in rows:
        c = cert.get((row["video_id"], row["symbol"]))
        if not c:
            continue
        add = []
        if not c["rtoc"] and row["pnl_confidence"] in ("frame_verified", "stated_verbatim"):
            row["pnl_confidence"] = "narrated"
            add.append("pnl_confidence downgraded: review_manifest marks ross_trade_outcome not certifiable for this (video, symbol)")
        if not c["tnc"]:
            add.append("review_manifest: trade/no-trade decision not certifiable from the reviewed frames")
        if add:
            row["notes"] = ((row["notes"] + " ") if row["notes"] else "") + "[review: " + "; ".join(add) + "]"
    return rows


def build():
    fixture = _load(FIXTURE_PATH)
    curated = load_curated()
    trade_rows, reject_rows = load_trades_files()
    rows = merge(curated, trade_rows, reject_rows)
    try:
        master_rows = load_master_ledger()
    except FileNotFoundError as exc:
        # Fail loudly: silently emitting a 3-layer manifest would look like
        # ordinary drift to --check and quietly shrink the bench's ground truth.
        raise FileNotFoundError(
            "master ledger not found at %s — set ROSS_MASTER_LEDGER (or "
            "ROSS_EVIDENCE_DIR, from which it is derived) to the evidence tree"
            % master_ledger_path()) from exc
    rows = merge_master(rows, master_rows)
    # Review downgrades run AFTER the ledger merge so ledger rows are subject to
    # the same per-(video, symbol) certifiability caps as every other layer.
    rows = apply_review_downgrades(rows)
    ids = [r["manifest_id"] for r in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError("duplicate manifest_ids: %s" % dupes)
    rows.sort(key=lambda r: (r["date"], r["video_id"], r["symbol"], r["manifest_id"]))
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evidence_role": "after_fact_grading_only",
        "usage_constraints": fixture["usage_constraints"],
        "windows": rows,
    }


def _comparable(doc):
    d = dict(doc)
    d["generated_at"] = None
    return json.dumps(d, sort_keys=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if manifest.json would change (drift detection); writes nothing")
    ap.add_argument("--evidence-dir", default=None,
                    help="override ROSS_EVIDENCE_DIR (root of ross_video_evidence/)")
    ap.add_argument("--master-ledger", default=None,
                    help="override ROSS_MASTER_LEDGER (path to ross_master_ledger.json)")
    args = ap.parse_args(argv)

    # CLI beats env beats default. Applied by writing the env vars so there is a
    # SINGLE resolution path (evidence_dir()/master_ledger_path()) rather than
    # threading two optional paths through every loader.
    if args.evidence_dir:
        os.environ["ROSS_EVIDENCE_DIR"] = args.evidence_dir
    if args.master_ledger:
        os.environ["ROSS_MASTER_LEDGER"] = args.master_ledger

    out = out_path()
    new = build()
    existing = None
    if os.path.exists(out):
        try:
            existing = _load(out)
        except (ValueError, OSError):
            existing = None
    same = existing is not None and _comparable(existing) == _comparable(new)

    if args.check:
        if same:
            print("OK: manifest.json is up to date (%d windows)" % len(new["windows"]))
            return 0
        print("DRIFT: manifest.json would change (existing=%s windows, new=%d windows)"
              % ("none" if existing is None else len(existing.get("windows", [])),
                 len(new["windows"])))
        return 1

    if same:
        print("Unchanged: %s (%d windows; generated_at kept)" % (out, len(new["windows"])))
        return 0
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)
        f.write("\n")
    trades = sum(1 for w in new["windows"] if w["expected_action"] == "trade")
    rejects = sum(1 for w in new["windows"] if w["expected_action"] == "reject")
    ungraded = len(new["windows"]) - trades - rejects
    ledger = sum(1 for w in new["windows"] if w["source"]["kind"] == "master_ledger")
    print("Wrote %s: %d windows (%d expected trade / %d expected reject / "
          "%d ungradeable) — %d standalone master_ledger rows"
          % (out, len(new["windows"]), trades, rejects, ungraded, ledger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
