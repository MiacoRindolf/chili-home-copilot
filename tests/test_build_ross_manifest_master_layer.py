"""Tests for layer 4 of the Ross ground-truth manifest builder.

Layer 4 folds project_ws/AgentOps/ross/ross_master_ledger.json
(chili.ross_master_ledger.v1) into scripts/build_ross_manifest.py at the LOWEST
precedence. The ledger is dirty by construction — narrative time fields, 0 used
as a null sentinel, three account vocabularies, and 30 no-trade records merged
in from 5 sub-schemas — so most of what is asserted here is the builder's
refusal to guess.

Everything below runs against fixtures written into tmp_path. Nothing here
reads the operator's evidence tree (except one explicitly skipped shape test),
touches a database, or writes to the real manifest.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import build_ross_manifest as builder


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _trade(**overrides):
    """A well-formed master-ledger trade row (the `_path="rows"` sub-schema)."""
    row = {
        "video_id": "vid1",
        "date": "2026-07-09",
        "symbol": "VRAX",
        "_path": "rows",
        "_src": "ledger_batch05_2026-07-09_to_2026-07-14.json",
        "side": "long",
        "entry_time_et": "07:32:45",
        "exit_time_et": "07:33:00",
        "entry_px": 6.3,
        "exit_px": 6.8,
        "shares": 0,
        "pnl_usd": 2262.35,
        "confidence": "approx",
    }
    row.update(overrides)
    return row


def _write_ledger(tmp_path: Path, trades, xref=None, name="ross_master_ledger.json",
                  schema=None):
    doc = {
        "schema": schema if schema is not None else builder.MASTER_LEDGER_SCHEMA,
        "trades": list(trades),
        "xref": list(xref or []),
    }
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _load_one(tmp_path: Path, **overrides):
    """Load a single-row ledger and return the one manifest row it produced."""
    path = _write_ledger(tmp_path, [_trade(**overrides)])
    rows = builder.load_master_ledger(path)
    assert len(rows) == 1
    return rows[0]


def _existing(**overrides):
    """A layer-1/layer-2 style manifest row to merge a ledger row into."""
    row = builder._window(
        "vid1::VRAX::2026-07-09::t1", "vid1", "2026-07-09", "VRAX", "small",
        "trade", "trade", None, None, None, None, None, None,
        "frame_audit", ["project_ws/AgentOps/ross_video_evidence/AUDIT_REPORT.md"],
        None, None,
    )
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# the 0-as-null-sentinel rule (load-bearing: a fabricated 0 would score)
# ---------------------------------------------------------------------------


def test_zero_pnl_is_absent_not_a_scratch(tmp_path):
    row = _load_one(tmp_path, pnl_usd=0)
    assert row["ross_net_usd"] is None
    assert row["expected_action"] is None
    assert row["pnl_confidence"] is None
    assert "0 sentinel" in row["notes"]


def test_missing_pnl_is_also_ungradeable(tmp_path):
    trade = _trade()
    del trade["pnl_usd"]
    path = _write_ledger(tmp_path, [trade])
    row = builder.load_master_ledger(path)[0]
    assert row["ross_net_usd"] is None
    assert row["expected_action"] is None


@pytest.mark.parametrize(
    ("pnl", "expected_action", "net"),
    [
        (2262.35, "trade", 2262.35),
        (-31000.0, "reject", -31000.0),
        (0, None, None),
    ],
)
def test_expected_action_comes_from_the_sign_of_pnl(tmp_path, pnl, expected_action, net):
    # Matches the layer-1 convention: a curated LOSING trade (TC -394.89) is
    # ross_action="trade" with expected_action="reject".
    row = _load_one(tmp_path, pnl_usd=pnl)
    assert row["expected_action"] == expected_action
    assert row["ross_net_usd"] == net
    assert row["ross_action"] == "trade"


@pytest.mark.parametrize(
    ("field", "manifest_key"),
    [("entry_px", "ross_entry_px"), ("exit_px", "ross_exit_px")],
)
def test_zero_price_is_the_null_sentinel(tmp_path, field, manifest_key):
    row = _load_one(tmp_path, **{field: 0})
    assert row[manifest_key] is None
    assert "0 sentinel" in row["notes"]


# ---------------------------------------------------------------------------
# pnl_confidence vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ledger_confidence", "manifest_confidence"),
    [
        ("exact", "stated_verbatim"),
        ("approx", "narrated"),
        ("inferred", "inferred"),
    ],
)
def test_pnl_confidence_maps_the_ledger_vocabulary(tmp_path, ledger_confidence,
                                                   manifest_confidence):
    row = _load_one(tmp_path, confidence=ledger_confidence)
    assert row["pnl_confidence"] == manifest_confidence


def test_unknown_confidence_word_is_never_promoted(tmp_path):
    # An unrecognised word must not become stated_verbatim by accident.
    row = _load_one(tmp_path, confidence="pretty sure")
    assert row["pnl_confidence"] == "inferred"


def test_confidence_is_null_when_there_is_no_figure(tmp_path):
    row = _load_one(tmp_path, confidence="exact", pnl_usd=0)
    assert row["pnl_confidence"] is None


# ---------------------------------------------------------------------------
# account: stated only, never inferred
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stated", "expected"),
    [
        ("small", "small"),
        ("main", "main"),
        ("big", "main"),          # ledger uses big/main for the same account
        (None, None),             # 97 of 187 ledger rows state nothing
        ("", None),
        ("small+main", None),     # names both -> cannot be one account
    ],
)
def test_account_is_stated_only(tmp_path, stated, expected):
    trade = _trade()
    if stated is None:
        trade.pop("account", None)
    else:
        trade["account"] = stated
    path = _write_ledger(tmp_path, [trade])
    assert builder.load_master_ledger(path)[0]["account"] == expected


def test_account_is_not_inferred_from_the_source_batch(tmp_path):
    # Layer 2 defaults challenge-day recap files to "small". Layer 4 must not:
    # inventing the small/main split would corrupt the never-summed rule.
    row = _load_one(tmp_path, _src="ledger_batch05_small_account_challenge.json")
    assert row["account"] is None


# ---------------------------------------------------------------------------
# time parsing: strict, anchored, never a guess
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("07:32:45", "07:32:45"),
        ("~08:35 (break of the 17.70 high)", "08:35"),
        ("9:05 (inferred)", "09:05"),
        ("~06:00-07:15 (headline 06:00; first fill time not stated)", "06:00"),
        # mid-sentence clock: the entry is described RELATIVE to it, so reading
        # it as the entry time would be a guess
        ("premarket, before ~09:00 (clock time not stated)", None),
        ("unknown (bounded ~09:06-09:40 ET)", None),
        # no clock at all
        ("not stated ('ADBB hit the scanner just as VCIG started to pull back')", None),
        # not a real clock
        ("25:00", None),
        ("09:60", None),
        (None, None),
    ],
)
def test_stated_clock_is_anchored_and_strict(text, expected):
    assert builder._master_clock(text) == expected


def test_unparseable_time_is_null_and_the_prose_is_preserved(tmp_path):
    prose = "premarket, before ~09:00 (clock time not stated)"
    row = _load_one(tmp_path, entry_time_et=prose)
    assert row["stated_entry_et"] is None
    assert row["window_et"] is None
    assert prose in row["notes"]


def test_window_et_prefers_the_exit_clock_over_an_entry_range_end(tmp_path):
    # An entry-side range only bounds the ENTRY; the exit clock bounds the window.
    row = _load_one(
        tmp_path,
        entry_time_et="~06:00-07:15 (multiple scalps)",
        exit_time_et="09:30 (flat before the bell)",
    )
    assert row["stated_entry_et"] == "06:00"
    assert row["stated_exit_et"] == "09:30"
    assert row["window_et"] == "~06:00-09:30"


def test_window_et_falls_back_to_the_entry_range_end(tmp_path):
    row = _load_one(
        tmp_path,
        entry_time_et="~06:00-07:15 (multiple scalps)",
        exit_time_et="not stated",
    )
    assert row["window_et"] == "~06:00-07:15"


def test_window_et_requires_an_entry_clock(tmp_path):
    # An exit clock alone would label the window with a time Ross was already out.
    row = _load_one(tmp_path, entry_time_et="premarket (not stated)",
                    exit_time_et="09:30")
    assert row["stated_exit_et"] == "09:30"
    assert row["window_et"] is None


def test_window_et_collapses_to_a_point_when_entry_equals_exit(tmp_path):
    row = _load_one(tmp_path, entry_time_et="08:00:10", exit_time_et="08:00:55")
    assert row["window_et"] == "~08:00"


# ---------------------------------------------------------------------------
# no-trade / miss records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path_bucket", list(builder.MASTER_NO_TRADE_PATHS))
def test_no_trade_records_are_separated_by_path_and_left_ungraded(tmp_path, path_bucket):
    row = builder.load_master_ledger(_write_ledger(tmp_path, [{
        "video_id": "vid9",
        "date": "2026-07-20",
        "symbol": "ZYBT",
        "_path": path_bucket,
        "_src": "ledger_batch07.json",
        "note": "leading gapper, halted up; not traded",
    }]))[0]
    assert row["ross_action"] == "no_trade"
    # Not "reject": several of these are MISSES, and grading a miss as a correct
    # reject would teach the opposite of the lesson.
    assert row["expected_action"] is None
    assert row["ross_net_usd"] is None
    assert row["ross_entry_px"] is None
    assert row["window_et"] is None
    assert path_bucket in row["notes"]


def test_no_trade_row_keeps_its_prose(tmp_path):
    row = builder.load_master_ledger(_write_ledger(tmp_path, [{
        "video_id": "vid9", "date": "2026-08-17", "symbol": "UCL",
        "_path": "ross_no_trade_context", "_src": "crossref_batch12.json",
        "ross": "explicit NO-TRADE ('too heavy')",
        "chili": "float-vetoed 25.9M > 20M ceiling",
    }]))[0]
    assert "too heavy" in row["notes"]
    assert "float-vetoed" in row["notes"]


def test_unknown_path_bucket_raises_rather_than_being_guessed(tmp_path):
    path = _write_ledger(tmp_path, [_trade(_path="some_new_sub_schema")])
    with pytest.raises(ValueError, match="unknown _path"):
        builder.load_master_ledger(path)


# ---------------------------------------------------------------------------
# schema guard and identity
# ---------------------------------------------------------------------------


def test_schema_mismatch_raises(tmp_path):
    path = _write_ledger(tmp_path, [_trade()], schema="chili.ross_master_ledger.v2")
    with pytest.raises(ValueError, match="schema mismatch"):
        builder.load_master_ledger(path)


def test_manifest_ids_stay_unique_when_a_symbol_day_repeats(tmp_path):
    # Measured: 35 (video_id, symbol, date) triples repeat in the real ledger,
    # up to 8 times (HYFM 2026-08-03).
    path = _write_ledger(tmp_path, [_trade(pnl_usd=1.0), _trade(pnl_usd=2.0),
                                    _trade(pnl_usd=3.0)])
    ids = [r["manifest_id"] for r in builder.load_master_ledger(path)]
    assert ids == ["vid1::VRAX::2026-07-09::ml1",
                   "vid1::VRAX::2026-07-09::ml2",
                   "vid1::VRAX::2026-07-09::ml3"]


def test_missing_video_or_date_raises(tmp_path):
    trade = _trade()
    del trade["date"]
    with pytest.raises(ValueError, match="missing video_id/date"):
        builder.load_master_ledger(_write_ledger(tmp_path, [trade]))


# ---------------------------------------------------------------------------
# symbol handling
# ---------------------------------------------------------------------------


def test_parenthetical_annotation_is_stripped_from_the_ticker(tmp_path):
    raw = "FCUV (4.80/break of 5; break of 8; post-11.50 halt re-entry)"
    row = _load_one(tmp_path, symbol=raw)
    assert row["symbol"] == "FCUV"
    assert raw in row["notes"]


def test_multi_symbol_string_is_kept_verbatim_and_flagged(tmp_path):
    # Splitting "EDBL/LGHL/BIYA" would silently delete two of the three tickers.
    row = _load_one(tmp_path, symbol="EDBL/LGHL/BIYA")
    assert row["symbol"] == "EDBL/LGHL/BIYA"
    assert "NOT split" in row["notes"]


def test_row_without_a_symbol_is_dropped(tmp_path):
    path = _write_ledger(tmp_path, [_trade(symbol=""), _trade()])
    assert len(builder.load_master_ledger(path)) == 1


# ---------------------------------------------------------------------------
# xref join
# ---------------------------------------------------------------------------


def _xref(**overrides):
    row = {
        "video_id": "vid1",
        "date": "2026-07-09",
        "symbol": "VRAX",
        "chili_verdict": "never_armed",
        "mechanism": "never_armed: the symbol never reached the viability board",
        "tape_coverage": "recorded_nbbo_only",
    }
    row.update(overrides)
    return row


def test_xref_join_carries_verdict_mechanism_and_coverage(tmp_path):
    path = _write_ledger(tmp_path, [_trade()], [_xref()])
    row = builder.load_master_ledger(path)[0]
    assert row["xref_verdict"] == "never_armed"
    assert row["xref_mechanism"].startswith("never_armed:")
    assert row["tape_coverage"] == "recorded_nbbo_only"


def test_xref_alternate_subschema_uses_the_verdict_key(tmp_path):
    # crossref_batch12/13 rows write "verdict" where the wider rows write
    # "chili_verdict"; both mean the same thing.
    alt = _xref(chili_verdict=None, verdict="armed_no_entry")
    alt.pop("chili_verdict")
    path = _write_ledger(tmp_path, [_trade()], [alt])
    assert builder.load_master_ledger(path)[0]["xref_verdict"] == "armed_no_entry"


def test_xref_join_is_on_symbol_and_date_not_video(tmp_path):
    # The same symbol-day is discussed across videos; the join key is the day.
    path = _write_ledger(tmp_path, [_trade(video_id="other_video")], [_xref()])
    assert builder.load_master_ledger(path)[0]["xref_verdict"] == "never_armed"


def test_no_xref_match_leaves_the_fields_null(tmp_path):
    path = _write_ledger(tmp_path, [_trade()], [_xref(symbol="OTHER")])
    row = builder.load_master_ledger(path)[0]
    assert row["xref_verdict"] is None
    assert row["xref_mechanism"] is None
    assert row["tape_coverage"] is None


def test_ambiguous_xref_join_carries_nothing_and_says_so(tmp_path):
    # (symbol, date) is 1:1 in the ledger as read on 2026-09-04; if a later
    # build breaks that, carrying one of two verdicts silently would be worse
    # than carrying none.
    path = _write_ledger(
        tmp_path, [_trade()],
        [_xref(), _xref(chili_verdict="armed_no_entry")],
    )
    row = builder.load_master_ledger(path)[0]
    assert row["xref_verdict"] is None
    assert "xref join ambiguous" in row["notes"]


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_source_kind_and_refs(tmp_path):
    ledger = _write_ledger(tmp_path, [_trade(source_files=[
        "project_ws/AgentOps/ross_video_evidence/vid1/trades_2026-07-09.json",
    ])])
    row = builder.load_master_ledger(ledger)[0]
    assert row["source"]["kind"] == "master_ledger"
    refs = row["source"]["refs"]
    assert any(r.endswith("ross_master_ledger.json") for r in refs)
    assert any("ledger_batch05_2026-07-09_to_2026-07-14.json" in r for r in refs)
    assert ("project_ws/AgentOps/ross_video_evidence/vid1/trades_2026-07-09.json"
            in refs)


def test_catalyst_is_null_because_the_ledger_has_no_catalyst_field(tmp_path):
    # `setup` is a chart setup, not a news catalyst; it belongs in notes.
    row = _load_one(tmp_path, setup="high-of-day break at 8")
    assert row["catalyst"] is None
    assert "high-of-day break at 8" in row["notes"]


# ---------------------------------------------------------------------------
# merge_master — lowest precedence
# ---------------------------------------------------------------------------


def test_merge_master_fills_gaps_and_the_existing_row_wins(tmp_path):
    existing = _existing(side=None, window_et=None, ross_entry_px=None,
                         ross_net_usd=None, ross_exit_px=99.0,
                         expected_action="reject")
    master = builder.load_master_ledger(
        _write_ledger(tmp_path, [_trade(account="small")], [_xref()]))
    out = builder.merge_master([existing], master)

    assert len(out) == 1, "the ledger row must be absorbed, not appended"
    assert existing["side"] == "long"
    assert existing["window_et"] == "~07:32-07:33"
    assert existing["ross_entry_px"] == 6.3
    assert existing["ross_net_usd"] == 2262.35
    assert existing["pnl_confidence"] == "narrated"
    assert existing["xref_verdict"] == "never_armed"
    assert existing["tape_coverage"] == "recorded_nbbo_only"
    assert existing["stated_entry_et"] == "07:32:45"
    # existing values are never overwritten
    assert existing["expected_action"] == "reject"
    assert existing["ross_exit_px"] == 99.0, (
        "ledger exit levels are transcript-derived, not broker fills; the "
        "layer-2 merge excludes exit_px for the same reason")
    assert any("ross_master_ledger.json" in r for r in existing["source"]["refs"])
    assert "Merged with master-ledger row" in existing["notes"]


def test_merge_master_does_not_overwrite_a_stated_field(tmp_path):
    existing = _existing(side="short", ross_entry_px=5.0, ross_net_usd=-100.0,
                         pnl_confidence="frame_verified")
    master = builder.load_master_ledger(
        _write_ledger(tmp_path, [_trade(account="small")]))
    builder.merge_master([existing], master)
    assert existing["side"] == "short"
    assert existing["ross_entry_px"] == 5.0
    assert existing["ross_net_usd"] == -100.0
    assert existing["pnl_confidence"] == "frame_verified"


def test_merge_master_is_one_to_one_only(tmp_path):
    # Two ledger rows for the same key: ambiguous, so neither is merged.
    existing = _existing()
    master = builder.load_master_ledger(_write_ledger(
        tmp_path, [_trade(account="small", pnl_usd=1.0),
                   _trade(account="small", pnl_usd=2.0)]))
    out = builder.merge_master([existing], master)
    assert len(out) == 3
    assert existing["ross_net_usd"] is None
    assert existing["notes"] is None


def test_merge_master_needs_a_single_existing_target(tmp_path):
    a = _existing(manifest_id="vid1::VRAX::2026-07-09::t1")
    b = _existing(manifest_id="vid1::VRAX::2026-07-09::t2")
    master = builder.load_master_ledger(
        _write_ledger(tmp_path, [_trade(account="small")]))
    out = builder.merge_master([a, b], master)
    assert len(out) == 3
    assert a["ross_net_usd"] is None and b["ross_net_usd"] is None


def test_merge_master_relaxed_pass_when_the_ledger_account_is_unstated(tmp_path):
    # The ledger leaves 97 of 187 accounts unstated; without this pass those
    # rows would be appended as duplicate ground truth for a trade layer 1 or 2
    # already carries.
    existing = _existing(account="small")
    trade = _trade()
    trade.pop("account", None)
    out = builder.merge_master(
        [existing], builder.load_master_ledger(_write_ledger(tmp_path, [trade])))
    assert len(out) == 1
    assert existing["ross_net_usd"] == 2262.35
    assert "account unstated" in existing["notes"]


def test_relaxed_pass_never_crosses_two_stated_accounts(tmp_path):
    existing = _existing(account="small")
    master = builder.load_master_ledger(
        _write_ledger(tmp_path, [_trade(account="main")]))
    out = builder.merge_master([existing], master)
    assert len(out) == 2, "a stated main-account row must not merge into small"
    assert existing["ross_net_usd"] is None


def test_relaxed_pass_needs_a_single_existing_target(tmp_path):
    a = _existing(manifest_id="a", account="small")
    b = _existing(manifest_id="b", account="main")
    trade = _trade()
    trade.pop("account", None)
    out = builder.merge_master(
        [a, b], builder.load_master_ledger(_write_ledger(tmp_path, [trade])))
    assert len(out) == 3
    assert a["ross_net_usd"] is None and b["ross_net_usd"] is None


def test_one_existing_row_absorbs_at_most_one_ledger_row(tmp_path):
    existing = _existing(account="small")
    trade_unstated = _trade(pnl_usd=500.0)
    trade_unstated.pop("account", None)
    master = builder.load_master_ledger(_write_ledger(
        tmp_path, [_trade(account="small"), trade_unstated]))
    out = builder.merge_master([existing], master)
    assert len(out) == 2, "the second ledger row must be appended, not folded in"
    assert existing["ross_net_usd"] == 2262.35


def test_no_trade_ledger_rows_merge_into_the_reject_bucket(tmp_path):
    # Layer 2 files watchlist no-trade rows under ross_action="no_trade"; the
    # merge kind must match so they do not duplicate each other.
    existing = _existing(ross_action="no_trade", expected_action="reject",
                         account=None, symbol="ZYBT", date="2026-07-20")
    master = builder.load_master_ledger(_write_ledger(tmp_path, [{
        "video_id": "vid9", "date": "2026-07-20", "symbol": "ZYBT",
        "_path": "no_trade_references", "_src": "b.json", "note": "not traded",
    }]))
    out = builder.merge_master([existing], master)
    assert len(out) == 1
    assert existing["expected_action"] == "reject"


# ---------------------------------------------------------------------------
# path resolution / env overrides
# ---------------------------------------------------------------------------


def test_env_override_selects_the_ledger(tmp_path, monkeypatch):
    path = _write_ledger(tmp_path, [_trade()], name="elsewhere.json")
    monkeypatch.setenv("ROSS_MASTER_LEDGER", path)
    assert builder.master_ledger_path() == path
    assert len(builder.load_master_ledger()) == 1


def test_ledger_path_is_derived_from_the_evidence_dir(monkeypatch):
    # One override relocates both trees: the ledger is the sibling ross/ dir of
    # ross_video_evidence/. Nothing about the D:/E: split is hardcoded here.
    monkeypatch.delenv("ROSS_MASTER_LEDGER", raising=False)
    monkeypatch.setenv("ROSS_EVIDENCE_DIR", os.path.join("X:", "ev", "ross_video_evidence"))
    assert builder.master_ledger_path() == os.path.join(
        "X:", "ev", "ross", "ross_master_ledger.json")


def test_explicit_ledger_override_beats_the_evidence_dir(tmp_path, monkeypatch):
    path = _write_ledger(tmp_path, [_trade()], name="pinned.json")
    monkeypatch.setenv("ROSS_EVIDENCE_DIR", str(tmp_path / "ev" / "ross_video_evidence"))
    monkeypatch.setenv("ROSS_MASTER_LEDGER", path)
    assert builder.master_ledger_path() == path


def test_out_path_follows_the_evidence_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSS_EVIDENCE_DIR", str(tmp_path))
    assert builder.out_path() == str(tmp_path / "manifest.json")


def test_evidence_dir_default_is_used_when_unset(monkeypatch):
    monkeypatch.delenv("ROSS_EVIDENCE_DIR", raising=False)
    assert builder.evidence_dir() == builder.EVIDENCE_DIR


# ---------------------------------------------------------------------------
# every row carries the layer-4 field set, whatever layer produced it
# ---------------------------------------------------------------------------


def test_layer4_fields_exist_on_rows_from_every_layer():
    # Consumers must never branch on row shape; the older layers emit nulls.
    row = builder._window("id", "vid", "2026-07-09", "VRAX", None, "trade",
                          "trade", None, None, None, None, None, None,
                          "frame_audit", [], None, None)
    for key in ("stated_entry_et", "stated_exit_et", "xref_verdict",
                "xref_mechanism", "tape_coverage"):
        assert key in row and row[key] is None


# ---------------------------------------------------------------------------
# the real ledger, when the operator's evidence tree is mounted
# ---------------------------------------------------------------------------


_REAL_LEDGER = builder.master_ledger_path()


@pytest.mark.skipif(not os.path.exists(_REAL_LEDGER),
                    reason="operator evidence tree not mounted on this host")
def test_real_ledger_matches_the_shape_this_layer_was_written_against():
    """Guards the counts every comment in the layer cites (read 2026-09-04).

    If the ledger is rebuilt and these move, the comments in
    scripts/build_ross_manifest.py are stale and must be re-measured — that is
    the point of this test, not the numbers themselves.
    """
    rows = builder.load_master_ledger(_REAL_LEDGER)
    assert len(rows) == 187
    assert sum(1 for r in rows if r["ross_action"] == "trade") == 157
    assert sum(1 for r in rows if r["ross_action"] == "no_trade") == 30
    # 132 rows lead with a clock; the other 25 are prose or absent.
    assert sum(1 for r in rows if r["stated_entry_et"]) == 132
    # 30 trade rows carry the 0 pnl sentinel; with the 30 no-trade rows that is
    # 60 rows this layer refuses to grade.
    assert sum(1 for r in rows if r["expected_action"] is None) == 60
    # account: 25 small, 65 main (49 "main" + 16 "big"), 97 unstated.
    accounts = [r["account"] for r in rows]
    assert accounts.count("small") == 25
    assert accounts.count("main") == 65
    assert accounts.count(None) == 97
    ids = [r["manifest_id"] for r in rows]
    assert len(set(ids)) == len(ids)
