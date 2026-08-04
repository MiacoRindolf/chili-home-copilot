#!/usr/bin/env python
"""Build the Ross ground-truth manifest (chili.ross_ground_truth_manifest.v1).

Deterministic merge of three evidence layers under
project_ws/AgentOps/ross_video_evidence/:

  1. curated_windows.json  — hand-curation of the 4 frame-audited AUDIT_REPORT
     files (broker panels = ground truth). WINS on conflict.
  2. */trades_*.json       — transcript-extraction recap files
     (chili.ross_recap_trades.v1 / chili.ross_daily_recap_trades.v1).
     One manifest row per trade; one expected_action="reject" row per
     watchlist no-trade entry.
  3. review_manifest.json  — per-(video, symbol) certifiability booleans.
     Applied LAST: ross_trade_outcome_certifiable=false caps pnl_confidence
     at "narrated"; trade_no_trade_certifiable=false appends a note.

Output: project_ws/AgentOps/ross_video_evidence/manifest.json

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
  * generated_at is excluded from drift comparison; if content is otherwise
    unchanged the existing file (and its timestamp) is left untouched.

Usage:
  python scripts/build_ross_manifest.py           # write manifest.json
  python scripts/build_ross_manifest.py --check   # exit 1 if output would change
"""

import argparse
import glob
import json
import os
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
            pnl_confidence, source_kind, refs, catalyst, notes):
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
        "ross_entry_px": entry_px,
        "ross_exit_px": exit_px,
        "ross_net_usd": net_usd,
        "pnl_confidence": pnl_confidence,
        "source": {"kind": source_kind, "refs": list(refs)},
        "catalyst": catalyst,
        "notes": notes,
        "tape": {"live_covered": None, "golden_pinned": None},
    }


def load_curated():
    doc = _load(os.path.join(EVIDENCE_DIR, "curated_windows.json"))
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
    paths = sorted(glob.glob(os.path.join(EVIDENCE_DIR, "*", "trades_*.json")))
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


def apply_review_downgrades(rows):
    reviews = _load(os.path.join(EVIDENCE_DIR, "review_manifest.json"))["reviews"]
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
    args = ap.parse_args(argv)

    new = build()
    existing = None
    if os.path.exists(OUT_PATH):
        try:
            existing = _load(OUT_PATH)
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
        print("Unchanged: %s (%d windows; generated_at kept)" % (OUT_PATH, len(new["windows"])))
        return 0
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)
        f.write("\n")
    trades = sum(1 for w in new["windows"] if w["expected_action"] == "trade")
    print("Wrote %s: %d windows (%d expected trade / %d expected reject)"
          % (OUT_PATH, len(new["windows"]), trades, len(new["windows"]) - trades))
    return 0


if __name__ == "__main__":
    sys.exit(main())
