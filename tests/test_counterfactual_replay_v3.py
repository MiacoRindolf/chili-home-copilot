from datetime import datetime, timedelta, timezone

from app.services.trading.momentum_neural.counterfactual_replay import (
    CounterfactualReplayResult,
    CounterfactualTrade,
    ReplayEntryCandidate,
    ReplayTapeTick,
    RossSourceEvent,
    SymbolReplayResult,
    _asr_symbol_aliases_from_text,
    _has_actionable_source_before,
    _has_source_before,
    _iter_tick_vwap_reclaim_burst_candidates,
    _simulate_candidate_trade,
    _trade_tape_to_microbars,
    load_ross_source_events,
    opportunity_label_summary,
    result_to_dict,
    run_counterfactual_replay,
)


def _ts(seconds: int = 0) -> datetime:
    return datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def test_counterfactual_trade_does_not_exit_on_entry_timestamp() -> None:
    candidate = ReplayEntryCandidate(
        symbol="JEM",
        ts=_ts(0),
        reason="ross_breakout_starter_tick",
        entry_price=10.0,
        stop_price=9.5,
        trigger_debug={},
        gate_family="ross_breakout_starter",
        bid=9.99,
        ask=10.0,
        spread_bps=10.0,
    )
    ticks = [
        ReplayTapeTick(ts=_ts(0), bid=11.5, ask=11.55, mid=11.52),
        ReplayTapeTick(ts=_ts(1), bid=10.25, ask=10.3, mid=10.27),
        ReplayTapeTick(ts=_ts(2), bid=11.05, ask=11.1, mid=11.07),
    ]

    trade = _simulate_candidate_trade(
        candidate,
        ticks,
        risk_usd=50.0,
        max_notional_usd=500.0,
        reward_risk=2.0,
        max_hold_seconds=30.0,
    )

    assert trade is not None
    assert trade.exit_ts == _ts(2)
    assert trade.exit_reason == "target"


def test_counterfactual_trade_can_exit_later_same_timestamp_with_trade_sequence() -> None:
    candidate = ReplayEntryCandidate(
        symbol="CANF",
        ts=_ts(0),
        reason="tick_vwap_reclaim_burst",
        entry_price=5.0,
        stop_price=4.9,
        trigger_debug={},
        gate_family="tick_vwap_reclaim_burst",
        bid=4.99,
        ask=5.0,
        spread_bps=20.0,
        sequence=100,
    )
    ticks = [
        ReplayTapeTick(ts=_ts(0), bid=5.3, ask=5.31, mid=5.3, sequence=90),
        ReplayTapeTick(ts=_ts(0), bid=5.21, ask=5.22, mid=5.21, sequence=101),
    ]

    trade = _simulate_candidate_trade(
        candidate,
        ticks,
        risk_usd=50.0,
        max_notional_usd=500.0,
        reward_risk=2.0,
        max_hold_seconds=30.0,
    )

    assert trade is not None
    assert trade.exit_ts == _ts(0)
    assert trade.exit_reason == "target"


def test_counterfactual_momentum_trail_holds_past_first_target() -> None:
    candidate = ReplayEntryCandidate(
        symbol="CANF",
        ts=_ts(0),
        reason="tick_vwap_reclaim_burst",
        entry_price=10.0,
        stop_price=9.0,
        trigger_debug={},
        gate_family="tick_vwap_reclaim_burst",
        bid=9.99,
        ask=10.0,
        spread_bps=10.0,
    )
    ticks = [
        ReplayTapeTick(ts=_ts(1), bid=12.1, ask=12.12, mid=12.1),
        ReplayTapeTick(ts=_ts(2), bid=13.5, ask=13.52, mid=13.5),
        ReplayTapeTick(ts=_ts(3), bid=12.4, ask=12.42, mid=12.4),
    ]

    fixed = _simulate_candidate_trade(
        candidate,
        ticks,
        risk_usd=100.0,
        max_notional_usd=0.0,
        reward_risk=2.0,
        max_hold_seconds=30.0,
    )
    trailed = _simulate_candidate_trade(
        candidate,
        ticks,
        risk_usd=100.0,
        max_notional_usd=0.0,
        reward_risk=2.0,
        max_hold_seconds=30.0,
        exit_model="momentum_trail",
    )

    assert fixed is not None
    assert trailed is not None
    assert fixed.exit_reason == "target"
    assert trailed.exit_reason == "trail_stop"
    assert trailed.exit_price == 12.4
    assert trailed.pnl_r > fixed.pnl_r
    assert trailed.debug["trail_armed"] is True


def test_counterfactual_adaptive_exit_routes_a_plus_vwap_burst_to_runner() -> None:
    candidate = ReplayEntryCandidate(
        symbol="CANF",
        ts=_ts(0),
        reason="tick_vwap_reclaim_burst",
        entry_price=10.0,
        stop_price=9.0,
        trigger_debug={
            "volume_ratio": 3.0,
            "required_volume_ratio": 1.5,
            "spread_cost_of_r": 0.10,
            "max_spread_cost_of_r": 0.35,
            "source_state": "entry_actionable_source",
        },
        gate_family="tick_vwap_reclaim_burst",
        bid=9.99,
        ask=10.0,
        spread_bps=10.0,
    )
    ticks = [
        ReplayTapeTick(ts=_ts(1), bid=12.1, ask=12.12, mid=12.1),
        ReplayTapeTick(ts=_ts(2), bid=13.5, ask=13.52, mid=13.5),
        ReplayTapeTick(ts=_ts(3), bid=12.4, ask=12.42, mid=12.4),
    ]

    trade = _simulate_candidate_trade(
        candidate,
        ticks,
        risk_usd=100.0,
        max_notional_usd=0.0,
        reward_risk=2.0,
        max_hold_seconds=30.0,
        exit_model="adaptive",
    )

    assert trade is not None
    assert trade.exit_reason == "trail_stop"
    assert trade.pnl_r == 2.4
    assert trade.debug["exit_model"] == "momentum_trail"
    assert trade.debug["exit_route"]["quality"] == "a_plus_vwap_reclaim_burst"


def test_counterfactual_adaptive_exit_keeps_starter_target_first() -> None:
    candidate = ReplayEntryCandidate(
        symbol="CANF",
        ts=_ts(0),
        reason="ross_breakout_starter_tick",
        entry_price=10.0,
        stop_price=9.0,
        trigger_debug={
            "volume_ratio": 3.0,
            "required_volume_ratio": 1.5,
            "spread_cost_of_r": 0.10,
            "max_spread_cost_of_r": 0.35,
            "source_state": "entry_actionable_source",
        },
        gate_family="ross_breakout_starter",
        bid=9.99,
        ask=10.0,
        spread_bps=10.0,
    )
    ticks = [
        ReplayTapeTick(ts=_ts(1), bid=12.1, ask=12.12, mid=12.1),
        ReplayTapeTick(ts=_ts(2), bid=13.5, ask=13.52, mid=13.5),
        ReplayTapeTick(ts=_ts(3), bid=12.4, ask=12.42, mid=12.4),
    ]

    trade = _simulate_candidate_trade(
        candidate,
        ticks,
        risk_usd=100.0,
        max_notional_usd=0.0,
        reward_risk=2.0,
        max_hold_seconds=30.0,
        exit_model="adaptive",
    )

    assert trade is not None
    assert trade.exit_reason == "target"
    assert trade.pnl_r == 2.1
    assert trade.debug["exit_model"] == "fixed_target"
    assert trade.debug["exit_route"]["reason"] == "starter_or_scalp_target_first"


def test_counterfactual_adaptive_exit_requires_vwap_volume_evidence_for_runner() -> None:
    candidate = ReplayEntryCandidate(
        symbol="CANF",
        ts=_ts(0),
        reason="tick_vwap_reclaim_burst",
        entry_price=10.0,
        stop_price=9.0,
        trigger_debug={
            "volume_ratio": 1.2,
            "required_volume_ratio": 1.5,
            "spread_cost_of_r": 0.10,
            "max_spread_cost_of_r": 0.35,
            "source_state": "entry_actionable_source",
        },
        gate_family="tick_vwap_reclaim_burst",
        bid=9.99,
        ask=10.0,
        spread_bps=10.0,
    )

    trade = _simulate_candidate_trade(
        candidate,
        [
            ReplayTapeTick(ts=_ts(1), bid=12.1, ask=12.12, mid=12.1),
            ReplayTapeTick(ts=_ts(2), bid=13.5, ask=13.52, mid=13.5),
        ],
        risk_usd=100.0,
        max_notional_usd=0.0,
        reward_risk=2.0,
        max_hold_seconds=30.0,
        exit_model="adaptive",
    )

    assert trade is not None
    assert trade.exit_reason == "target"
    assert trade.debug["exit_model"] == "fixed_target"
    assert trade.debug["exit_route"]["blockers"] == ["volume_below_required"]


def test_counterfactual_zero_notional_cap_means_risk_sized_uncapped() -> None:
    candidate = ReplayEntryCandidate(
        symbol="CANF",
        ts=_ts(0),
        reason="tick_vwap_reclaim_burst",
        entry_price=100.0,
        stop_price=99.0,
        trigger_debug={},
        gate_family="tick_vwap_reclaim_burst",
        bid=99.99,
        ask=100.0,
        spread_bps=1.0,
    )

    trade = _simulate_candidate_trade(
        candidate,
        [ReplayTapeTick(ts=_ts(1), bid=102.0, ask=102.01, mid=102.0)],
        risk_usd=50.0,
        max_notional_usd=0.0,
        reward_risk=2.0,
        max_hold_seconds=30.0,
    )

    assert trade is not None
    assert trade.qty == 50.0
    assert trade.debug["sizing"]["notional_usd"] == 5000.0
    assert trade.debug["sizing"]["capped_by"] is None


def test_counterfactual_cash_fraction_sizing_uses_notional_fraction() -> None:
    candidate = ReplayEntryCandidate(
        symbol="CANF",
        ts=_ts(0),
        reason="tick_vwap_reclaim_burst",
        entry_price=5.0,
        stop_price=4.75,
        trigger_debug={
            "volume_ratio": 3.0,
            "required_volume_ratio": 1.5,
            "spread_cost_of_r": 0.10,
            "max_spread_cost_of_r": 0.35,
            "source_state": "entry_actionable_source",
        },
        gate_family="tick_vwap_reclaim_burst",
        bid=4.99,
        ask=5.0,
        spread_bps=20.0,
    )

    trade = _simulate_candidate_trade(
        candidate,
        [ReplayTapeTick(ts=_ts(1), bid=5.5, ask=5.51, mid=5.5)],
        risk_usd=50.0,
        max_notional_usd=0.0,
        reward_risk=2.0,
        max_hold_seconds=30.0,
        cash_usd=10_000.0,
        cash_fraction=0.15,
    )

    assert trade is not None
    assert trade.qty == 300.0
    assert trade.pnl_usd == 150.0
    assert trade.debug["sizing"]["model"] == "a_grade_cash_fraction_notional"
    assert trade.debug["sizing"]["notional_usd"] == 1500.0
    assert trade.debug["sizing"]["risk_usd"] == 75.0
    assert trade.debug["sizing"]["grade"] == "A+"


def test_counterfactual_cash_fraction_sizing_does_not_lift_non_a_starter() -> None:
    candidate = ReplayEntryCandidate(
        symbol="CANF",
        ts=_ts(0),
        reason="ross_breakout_starter_tick",
        entry_price=5.0,
        stop_price=4.75,
        trigger_debug={},
        gate_family="ross_breakout_starter",
        bid=4.99,
        ask=5.0,
        spread_bps=20.0,
    )

    trade = _simulate_candidate_trade(
        candidate,
        [ReplayTapeTick(ts=_ts(1), bid=5.5, ask=5.51, mid=5.5)],
        risk_usd=50.0,
        max_notional_usd=0.0,
        reward_risk=2.0,
        max_hold_seconds=30.0,
        cash_usd=10_000.0,
        cash_fraction=0.15,
    )

    assert trade is not None
    assert trade.qty == 200.0
    assert trade.pnl_usd == 100.0
    assert trade.debug["sizing"]["model"] == "structural_risk_first"
    assert trade.debug["sizing"]["notional_usd"] == 1000.0


def test_ross_source_before_entry_can_require_certifiable_rows() -> None:
    events = [
        RossSourceEvent(symbol="CANF", ts=_ts(5), source="ross_transcript", certifiable=False),
        RossSourceEvent(symbol="CANF", ts=_ts(10), source="ross_admission", certifiable=True),
    ]

    assert _has_source_before(events, _ts(6), require_certifiable=False)
    assert not _has_source_before(events, _ts(6), require_certifiable=True)
    assert _has_source_before(events, _ts(11), require_certifiable=True)


def test_ross_trade_event_with_certified_visual_review_loads_certifiable_source(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    frame = tmp_path / "frames" / "f0123.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame")
    trade_events.write_text(
        (
            '{"symbol":"CANF","ts":"2026-07-01T13:00:05Z",'
            '"action":"review_certified","visual_evidence_id":"vid-chart"}\n'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"CANF","evidence_id":"vid-chart",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"reviewed_frame_paths":["frames/f0123.jpg"]}]}'
        ),
        encoding="utf-8",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["CANF"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=manifest,
    )

    assert len(events["CANF"]) == 1
    assert events["CANF"][0].certifiable is True
    assert events["CANF"][0].signal["certification_reason"] == "trade_event_visual_evidence_trade_certified"
    assert _has_source_before(events["CANF"], _ts(6), require_certifiable=True)


def test_ross_trade_event_requires_review_certified_action_for_source_proof(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    frame = tmp_path / "frames" / "f0123.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame")
    trade_events.write_text(
        (
            '{"symbol":"CANF","ts":"2026-07-01T13:00:05Z",'
            '"action":"buy","visual_evidence_id":"vid-chart"}\n'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"CANF","evidence_id":"vid-chart",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"reviewed_frame_paths":["frames/f0123.jpg"]}]}'
        ),
        encoding="utf-8",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["CANF"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=manifest,
    )

    assert len(events["CANF"]) == 1
    assert events["CANF"][0].certifiable is False
    assert events["CANF"][0].signal["certification_reason"] == "trade_event_action_not_review_certified"
    assert not _has_source_before(events["CANF"], _ts(6), require_certifiable=True)


def test_ross_trade_event_old_visual_review_schema_stays_noncertifying(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    trade_events.write_text(
        (
            '{"symbol":"CANF","ts":"2026-07-01T13:00:05Z",'
            '"action":"review_certified","visual_evidence_id":"vid-old-schema"}\n'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"CANF","evidence_id":"vid-old-schema",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"reviewed_frame_paths":["frames/f0123.jpg"]}]}'
        ),
        encoding="utf-8",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["CANF"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=manifest,
    )

    assert len(events["CANF"]) == 1
    assert events["CANF"][0].certifiable is False
    assert (
        events["CANF"][0].signal["certification_reason"]
        == "trade_event_visual_evidence_not_source_before_opportunity"
    )
    assert not _has_source_before(events["CANF"], _ts(6), require_certifiable=True)


def test_ross_trade_event_visual_review_lookup_is_symbol_aware_for_shared_video(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    frame = tmp_path / "frames" / "f0280.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame")
    trade_events.write_text(
        (
            '{"symbol":"TC","ts":"2026-07-01T13:00:05Z",'
            '"action":"review_certified","visual_evidence_id":"vid-shared"}\n'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":['
            '{"symbol":"TC","evidence_id":"vid-shared",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"reviewed_frame_paths":["frames/f0280.jpg"]},'
            '{"symbol":"CANF","evidence_id":"vid-shared",'
            '"evidence_type":"post_opportunity_chart_review_context",'
            '"trade_no_trade_certifiable":false,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":false,'
            '"reviewed_frame_paths":["frames/f0529.jpg"]}'
            ']}'
        ),
        encoding="utf-8",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["TC"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=manifest,
    )

    assert len(events["TC"]) == 1
    assert events["TC"][0].certifiable is True
    assert events["TC"][0].signal["certification_reason"] == "trade_event_visual_evidence_trade_certified"
    assert _has_source_before(events["TC"], _ts(6), require_certifiable=True)


def test_ross_trade_event_post_opportunity_visual_review_stays_noncertifying(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    trade_events.write_text(
        (
            '{"symbol":"CANF","ts":"2026-07-01T13:00:05Z",'
            '"action":"review_certified","visual_evidence_id":"vid-post-recap"}\n'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"CANF","evidence_id":"vid-post-recap",'
            '"evidence_type":"post_opportunity_chart_review_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":false,'
            '"reviewed_frame_paths":["frames/f0529.jpg","frames/f0575.jpg"]}]}'
        ),
        encoding="utf-8",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["CANF"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=manifest,
    )

    assert len(events["CANF"]) == 1
    assert events["CANF"][0].certifiable is False
    assert (
        events["CANF"][0].signal["certification_reason"]
        == "trade_event_visual_evidence_not_source_before_opportunity"
    )
    assert not _has_source_before(events["CANF"], _ts(6), require_certifiable=True)


def test_ross_trade_event_visual_review_manifest_accepts_utf8_bom(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    frame = tmp_path / "frames" / "f0123.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame")
    trade_events.write_text(
        (
            '{"symbol":"CANF","ts":"2026-07-01T13:00:05Z",'
            '"action":"review_certified","visual_evidence_id":"vid-chart"}\n'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"CANF","evidence_id":"vid-chart",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"reviewed_frame_paths":["frames/f0123.jpg"]}]}'
        ),
        encoding="utf-8-sig",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["CANF"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=manifest,
    )

    assert events["CANF"][0].certifiable is True


def test_ross_trade_event_source_before_requires_existing_reviewed_frame_files(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    trade_events.write_text(
        (
            '{"symbol":"TC","ts":"2026-07-01T13:00:05Z",'
            '"action":"review_certified","visual_evidence_id":"vid-missing-frame"}\n'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"TC","evidence_id":"vid-missing-frame",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"reviewed_frame_paths":["frames/typo.jpg"]}]}'
        ),
        encoding="utf-8",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["TC"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=manifest,
    )

    assert len(events["TC"]) == 1
    assert events["TC"][0].certifiable is False
    assert (
        events["TC"][0].signal["certification_reason"]
        == "trade_event_visual_evidence_missing_reviewed_frame_files"
    )


def test_ross_trade_event_source_before_requires_reviewed_frames(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    trade_events.write_text(
        (
            '{"symbol":"TC","ts":"2026-07-01T13:00:05Z",'
            '"action":"review_certified","visual_evidence_id":"vid-no-frames"}\n'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"TC","evidence_id":"vid-no-frames",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"reviewed_frame_paths":[]}]}'
        ),
        encoding="utf-8",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["TC"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=manifest,
    )

    assert len(events["TC"]) == 1
    assert events["TC"][0].certifiable is False
    assert (
        events["TC"][0].signal["certification_reason"]
        == "trade_event_visual_evidence_missing_reviewed_frames"
    )


def test_ross_trade_event_source_before_requires_chart_trade_context(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    frame = tmp_path / "frames" / "f0280.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame")
    trade_events.write_text(
        (
            '{"symbol":"TC","ts":"2026-07-01T13:00:05Z",'
            '"action":"review_certified","visual_evidence_id":"vid-scanner"}\n'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"TC","evidence_id":"vid-scanner",'
            '"evidence_type":"scanner_selection_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"reviewed_frame_paths":["frames/f0280.jpg"]}]}'
        ),
        encoding="utf-8",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["TC"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=manifest,
    )

    assert len(events["TC"]) == 1
    assert events["TC"][0].certifiable is False
    assert (
        events["TC"][0].signal["certification_reason"]
        == "trade_event_visual_evidence_not_chart_trade_context"
    )


def test_ross_trade_event_with_scanner_only_visual_review_stays_noncertifying(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    trade_events.write_text(
        (
            '{"symbol":"TC","ts":"2026-07-01T13:00:05Z",'
            '"action":"review_certified","visual_evidence_id":"vid-scanner"}\n'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"TC","evidence_id":"vid-scanner",'
            '"evidence_type":"scanner_selection_context",'
            '"trade_no_trade_certifiable":false,'
            '"ross_trade_outcome_certifiable":false,'
            '"source_before_opportunity_certifiable":false,'
            '"reviewed_frame_paths":["frames/f0280.jpg"]}]}'
        ),
        encoding="utf-8",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["TC"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=manifest,
    )

    assert len(events["TC"]) == 1
    assert events["TC"][0].certifiable is False
    assert (
        events["TC"][0].signal["certification_reason"]
        == "trade_event_visual_evidence_not_source_before_opportunity"
    )
    assert not _has_source_before(events["TC"], _ts(6), require_certifiable=True)


def test_ross_trade_event_explicit_certifiable_requires_visual_review(tmp_path) -> None:
    trade_events = tmp_path / "ross_trade_events.jsonl"
    trade_events.write_text(
        '{"symbol":"CANF","ts":"2026-07-01T13:00:05Z","certifiable":true}\n',
        encoding="utf-8",
    )

    events = load_ross_source_events(
        since=_ts(0),
        until=_ts(30),
        symbols=["CANF"],
        transcript_path=tmp_path / "missing_transcript.jsonl",
        admission_paths=(),
        trade_events_path=trade_events,
        visual_review_manifest_path=tmp_path / "missing_manifest.json",
    )

    assert len(events["CANF"]) == 1
    assert events["CANF"][0].certifiable is False
    assert (
        events["CANF"][0].signal["certification_reason"]
        == "trade_event_explicit_certification_requires_visual_review"
    )
    assert not _has_source_before(events["CANF"], _ts(6), require_certifiable=True)


def test_actionable_source_before_entry_rejects_recap_transcript() -> None:
    events = [
        RossSourceEvent(
            symbol="JEM",
            ts=_ts(5),
            text="I got that trade earlier on CANF and that trade on gem didn't really follow through.",
            source="ross_transcript",
        ),
        RossSourceEvent(
            symbol="JEM",
            ts=_ts(10),
            text="Gem could go to high of day here.",
            source="ross_transcript",
        ),
    ]

    ok_early, reason_early, _ = _has_actionable_source_before(
        events,
        _ts(6),
        require_certifiable=False,
    )
    ok_late, reason_late, _ = _has_actionable_source_before(
        events,
        _ts(11),
        require_certifiable=False,
    )

    assert not ok_early
    assert reason_early == "recap_source_not_entry_actionable"
    assert ok_late
    assert reason_late == "entry_actionable_source"


def test_later_hard_negative_source_cancels_earlier_actionable_source() -> None:
    events = [
        RossSourceEvent(
            symbol="TC",
            ts=_ts(5),
            text="TC could go to high of day here.",
            source="ross_transcript",
        ),
        RossSourceEvent(
            symbol="TC",
            ts=_ts(10),
            text="TC is chopping around and I don't know.",
            source="ross_transcript",
        ),
    ]

    ok, reason, _ = _has_actionable_source_before(events, _ts(11), require_certifiable=False)

    assert not ok
    assert reason == "hard_negative_source_not_entry_actionable"


def test_actionable_source_expires_after_watch_window() -> None:
    events = [
        RossSourceEvent(
            symbol="CANF",
            ts=_ts(0),
            text="CANF holding over the VWAP.",
            source="ross_transcript",
        )
    ]

    ok, reason, _ = _has_actionable_source_before(
        events,
        _ts(20),
        require_certifiable=False,
        max_age_seconds=10,
    )

    assert not ok
    assert reason == "ross_source_watch_expired"


def test_jem_asr_alias_is_constrained_to_requested_symbol() -> None:
    text = "Gem looks like it could go to high of day."

    assert _asr_symbol_aliases_from_text(text, {"JEM"}) == ["JEM"]
    assert _asr_symbol_aliases_from_text(text, {"TC"}) == []
    assert _asr_symbol_aliases_from_text(text, set()) == []


def test_trade_microbars_use_trade_size_as_volume() -> None:
    bars = _trade_tape_to_microbars(
        [
            ReplayTapeTick(ts=_ts(0), bid=5.0, ask=5.0, mid=5.0, size=100),
            ReplayTapeTick(ts=_ts(0), bid=5.1, ask=5.1, mid=5.1, size=250),
            ReplayTapeTick(ts=_ts(16), bid=5.2, ask=5.2, mid=5.2, size=50),
        ],
        bar_seconds=15,
    )

    assert bars is not None
    assert float(bars.iloc[0]["Volume"]) == 350.0
    assert float(bars.iloc[1]["Volume"]) == 50.0


def test_tick_vwap_reclaim_burst_fires_through_prior_trade_level() -> None:
    source = [
        RossSourceEvent(
            symbol="CANF",
            ts=_ts(0),
            text="CANF watch here, breakthrough VWAP, holding over the VWAP on the running up scanner.",
            source="ross_transcript",
            certifiable=True,
        )
    ]
    trade_prices = [
        5.00,
        5.02,
        5.05,
        5.01,
        4.98,
        4.96,
        4.95,
        4.99,
        5.01,
        5.03,
        5.06,
    ]
    trade_ticks = [
        ReplayTapeTick(ts=_ts(i * 2), bid=px, ask=px, mid=px, source="iqfeed_trade_ticks", size=(1000 if i == 10 else 10))
        for i, px in enumerate(trade_prices)
    ]
    quote_ticks = [
        ReplayTapeTick(ts=t.ts, bid=t.mid - 0.001, ask=t.mid + 0.001, mid=t.mid, source="iqfeed_nbbo")
        for t in trade_ticks
    ]

    candidates, reasons = _iter_tick_vwap_reclaim_burst_candidates(
        symbol="CANF",
        quote_ticks=quote_ticks,
        trade_ticks=trade_ticks,
        source_events=source,
    )

    assert candidates, reasons
    assert candidates[0].gate_family == "tick_vwap_reclaim_burst"
    assert candidates[0].reason == "tick_vwap_reclaim_burst"
    assert candidates[0].trigger_debug["pullback_high"] == 5.05
    assert candidates[0].trigger_debug["pullback_low"] == 4.95
    assert candidates[0].trigger_debug["volume_ratio"] > candidates[0].trigger_debug["required_volume_ratio"]


def test_tick_vwap_reclaim_burst_rejects_hard_negative_source_context() -> None:
    source = [
        RossSourceEvent(
            symbol="DXF",
            ts=_ts(18),
            text="DXF is below VWAP pulling back too much.",
            source="ross_transcript",
            certifiable=True,
        )
    ]
    trade_ticks = [
        ReplayTapeTick(ts=_ts(i * 2), bid=px, ask=px, mid=px, source="iqfeed_trade_ticks", size=(1000 if i == 10 else 10))
        for i, px in enumerate([5.00, 5.02, 5.05, 5.01, 4.98, 4.96, 4.95, 4.99, 5.01, 5.03, 5.06])
    ]
    quote_ticks = [
        ReplayTapeTick(ts=t.ts, bid=t.mid - 0.01, ask=t.mid + 0.01, mid=t.mid, source="iqfeed_nbbo")
        for t in trade_ticks
    ]

    candidates, reasons = _iter_tick_vwap_reclaim_burst_candidates(
        symbol="DXF",
        quote_ticks=quote_ticks,
        trade_ticks=trade_ticks,
        source_events=source,
    )

    assert candidates == []
    assert reasons["hard_negative_source_not_entry_actionable"] > 0


def test_tick_burst_can_recertify_after_stale_negative_source() -> None:
    source = [
        RossSourceEvent(
            symbol="CANF",
            ts=_ts(0),
            text="CANF is holding over the VWAP.",
            source="ross_transcript",
            certifiable=False,
        ),
        RossSourceEvent(
            symbol="CANF",
            ts=_ts(10),
            text="I'm going to throw in the towel here, was red on CANF before it bounced back.",
            source="ross_transcript",
            certifiable=False,
        ),
    ]
    prices = [
        5.00,
        5.02,
        5.04,
        5.06,
        5.04,
        5.02,
        5.00,
        5.04,
        5.05,
        5.07,
        5.05,
        5.03,
        5.02,
        5.06,
        5.09,
    ]
    trade_ticks = [
        ReplayTapeTick(
            ts=_ts(i * 2),
            bid=px,
            ask=px,
            mid=px,
            source="iqfeed_trade_ticks",
            size=(10000 if i >= 13 else (1000 if i >= 8 else 10)),
            sequence=i,
        )
        for i, px in enumerate(prices)
    ]
    quote_ticks = [
        ReplayTapeTick(ts=t.ts, bid=t.mid - 0.001, ask=t.mid + 0.001, mid=t.mid, source="iqfeed_nbbo")
        for t in trade_ticks
    ]

    candidates, reasons = _iter_tick_vwap_reclaim_burst_candidates(
        symbol="CANF",
        quote_ticks=quote_ticks,
        trade_ticks=trade_ticks,
        source_events=source,
    )

    assert reasons["hard_negative_source_not_entry_actionable"] > 0
    assert candidates, reasons
    assert candidates[-1].trigger_debug["source_mode"] in {"ross_source", "market_certified"}
    assert candidates[-1].trigger_debug["source_blocker_recertified"] is True
    assert candidates[-1].ts >= _ts(22)


def test_tick_vwap_reclaim_burst_can_fire_from_market_certified_tape_without_source() -> None:
    prices = [
        5.00,
        5.02,
        5.04,
        5.06,
        5.08,
        5.10,
        5.08,
        5.06,
        5.04,
        5.08,
        5.11,
        5.14,
    ]
    trade_ticks: list[ReplayTapeTick] = []
    for i, px in enumerate(prices):
        size = 1000 if i >= 9 else 10
        trade_ticks.append(
            ReplayTapeTick(
                ts=_ts(i * 2),
                bid=px,
                ask=px,
                mid=px,
                source="iqfeed_trade_ticks",
                size=size,
                sequence=i,
            )
        )
    quote_ticks = [
        ReplayTapeTick(ts=t.ts, bid=t.mid - 0.01, ask=t.mid + 0.01, mid=t.mid, source="iqfeed_nbbo")
        for t in trade_ticks
    ]

    candidates, reasons = _iter_tick_vwap_reclaim_burst_candidates(
        symbol="AAPL",
        quote_ticks=quote_ticks,
        trade_ticks=trade_ticks,
        source_events=[],
    )

    assert candidates, reasons
    assert candidates[0].trigger_debug["source_mode"] == "market_certified"
    assert candidates[0].trigger_debug["market_certified"] is True


def test_market_certified_tick_burst_rejects_sub_course_price_without_source() -> None:
    prices = [
        1.50,
        1.52,
        1.54,
        1.56,
        1.58,
        1.60,
        1.59,
        1.58,
        1.575,
        1.60,
        1.615,
        1.63,
    ]
    trade_ticks = [
        ReplayTapeTick(
            ts=_ts(i * 2),
            bid=px,
            ask=px,
            mid=px,
            source="iqfeed_trade_ticks",
            size=(1000 if i >= 9 else 10),
            sequence=i,
        )
        for i, px in enumerate(prices)
    ]
    quote_ticks = [
        ReplayTapeTick(ts=t.ts, bid=t.mid - 0.01, ask=t.mid + 0.01, mid=t.mid, source="iqfeed_nbbo")
        for t in trade_ticks
    ]

    candidates, reasons = _iter_tick_vwap_reclaim_burst_candidates(
        symbol="LHAI",
        quote_ticks=quote_ticks,
        trade_ticks=trade_ticks,
        source_events=[],
    )

    assert candidates == []
    assert reasons["tick_vwap_burst_market_price_below_course_range"] > 0


def test_market_certified_tick_burst_rejects_above_scalp_range_without_source() -> None:
    prices = [
        30.00,
        30.20,
        30.40,
        30.60,
        30.80,
        31.00,
        30.85,
        30.70,
        30.55,
        30.90,
        31.15,
        31.35,
    ]
    trade_ticks = [
        ReplayTapeTick(
            ts=_ts(i * 2),
            bid=px,
            ask=px,
            mid=px,
            source="iqfeed_trade_ticks",
            size=(1000 if i >= 9 else 10),
            sequence=i,
        )
        for i, px in enumerate(prices)
    ]
    quote_ticks = [
        ReplayTapeTick(ts=t.ts, bid=t.mid - 0.01, ask=t.mid + 0.01, mid=t.mid, source="iqfeed_nbbo")
        for t in trade_ticks
    ]

    candidates, reasons = _iter_tick_vwap_reclaim_burst_candidates(
        symbol="AAPL",
        quote_ticks=quote_ticks,
        trade_ticks=trade_ticks,
        source_events=[],
    )

    assert candidates == []
    assert reasons["tick_vwap_burst_market_price_above_scalp_range"] > 0


def test_tick_burst_eval_window_keeps_warmup_session_high_context() -> None:
    early_prices = [8.00, 7.70, 7.30, 6.80, 6.20]
    later_prices = [5.00, 5.02, 5.04, 5.02, 5.00, 5.06, 5.09]
    prices = [*early_prices, *later_prices]
    trade_ticks = [
        ReplayTapeTick(
            ts=_ts(i * 2),
            bid=px,
            ask=px,
            mid=px,
            source="iqfeed_trade_ticks",
            size=(5000 if i >= len(early_prices) + 5 else 100),
            sequence=i,
        )
        for i, px in enumerate(prices)
    ]
    quote_ticks = [
        ReplayTapeTick(ts=t.ts, bid=t.mid - 0.01, ask=t.mid + 0.01, mid=t.mid, source="iqfeed_nbbo")
        for t in trade_ticks
    ]

    candidates, reasons = _iter_tick_vwap_reclaim_burst_candidates(
        symbol="TC",
        quote_ticks=quote_ticks,
        trade_ticks=trade_ticks,
        source_events=[],
        eval_since=_ts(10),
    )

    assert candidates == []
    assert reasons["tick_vwap_burst_waiting_for_ordered_pullback"] > 0


def _symbol_result(
    symbol: str,
    *,
    tape_rows: int = 20,
    certifiable_source: bool = True,
    candidate_count: int = 1,
    trades: list[CounterfactualTrade] | None = None,
) -> SymbolReplayResult:
    source_events = (
        [
            {
                "ts": _ts(0).isoformat(),
                "source": "ross_admission",
                "certifiable": certifiable_source,
                "text": f"{symbol} scanner context",
            }
        ]
        if certifiable_source is not None
        else []
    )
    return SymbolReplayResult(
        symbol=symbol,
        ok=True,
        confidence="tick_quote_complete",
        confidence_reasons=[],
        tape_rows=tape_rows,
        trade_rows=tape_rows,
        micro_bars=4,
        source_events=source_events,
        trades=trades or [],
        candidate_count=candidate_count,
        skipped_reasons={},
        gate_reason_counts={},
        first_candidate={
            "ts": _ts(1).isoformat(),
            "reason": "ross_breakout_starter_tick",
            "gate_family": "ross_breakout_starter",
            "entry_price": 4.0,
            "stop_price": 3.8,
            "spread_bps": 10.0,
            "source_before_entry": True,
        },
    )


def test_counterfactual_opportunity_label_summary_separates_taken_missed_and_blocked() -> None:
    trade = CounterfactualTrade(
        symbol="JEM",
        entry_ts=_ts(1),
        exit_ts=_ts(10),
        entry_price=4.0,
        exit_price=4.4,
        stop_price=3.8,
        target_price=4.4,
        qty=100,
        pnl_usd=40.0,
        pnl_r=2.0,
        reason="vwap_reclaim",
        exit_reason="target",
        gate_family="vwap_reclaim",
        max_favorable_r=2.0,
        max_adverse_r=0.0,
        debug={},
    )
    result = CounterfactualReplayResult(
        since=_ts(0),
        until=_ts(60),
        symbols=["JEM", "CANF", "LHAI"],
        results=[
            _symbol_result("JEM", trades=[trade]),
            _symbol_result("CANF"),
            _symbol_result("LHAI", tape_rows=0),
        ],
    )

    summary = opportunity_label_summary(result)

    assert summary["status_counts"] == {
        "labeled_missed": 1,
        "labeled_taken": 1,
        "no_tape": 1,
    }
    assert summary["label_ready_symbol_count"] == 2
    assert summary["taken_label_count"] == 1
    assert summary["missed_label_count"] == 1
    assert summary["pnl_minmax_label_ready"] is False
    assert summary["rows"][0]["status"] == "labeled_taken"


def test_counterfactual_result_json_includes_opportunity_label_summary() -> None:
    result = CounterfactualReplayResult(
        since=_ts(0),
        until=_ts(60),
        symbols=["CANF"],
        results=[_symbol_result("CANF")],
    )

    payload = result_to_dict(result)

    assert payload["opportunity_label_summary"]["pnl_minmax_label_ready"] is True
    assert payload["opportunity_label_summary"]["status_counts"] == {"labeled_missed": 1}


def test_counterfactual_opportunity_label_requires_certified_source_before_opportunity() -> None:
    late_cert_source = SymbolReplayResult(
        symbol="CANF",
        ok=True,
        confidence="tick_quote_complete",
        confidence_reasons=[],
        tape_rows=10,
        trade_rows=10,
        micro_bars=4,
        source_events=[
            {"ts": _ts(30).isoformat(), "source": "ross_admission", "certifiable": True, "text": "late recap"}
        ],
        trades=[],
        candidate_count=1,
        skipped_reasons={},
        gate_reason_counts={},
        first_candidate={"ts": _ts(10).isoformat(), "reason": "vwap_reclaim"},
    )
    result = CounterfactualReplayResult(
        since=_ts(0),
        until=_ts(60),
        symbols=["CANF"],
        results=[late_cert_source],
    )

    summary = opportunity_label_summary(result)

    assert summary["status_counts"] == {"cert_source_after_opportunity": 1}
    assert summary["label_ready_symbol_count"] == 0
    assert summary["rows"][0]["opportunity_ts"] == _ts(10).isoformat()
    assert summary["rows"][0]["first_certifiable_source_ts"] == _ts(30).isoformat()
    assert summary["rows"][0]["cert_source_lag_seconds"] == 20.0
    assert summary["source_certification_queue"] == [
        {
            "symbol": "CANF",
            "status": "cert_source_after_opportunity",
            "action_required": (
                "find_or_mark_reviewed_chart_context_before_opportunity; "
                "later_certifiable_source_cannot_label_this_opportunity"
            ),
            "opportunity_ts": _ts(10).isoformat(),
            "first_certifiable_source_ts": _ts(30).isoformat(),
            "cert_source_lag_seconds": 20.0,
            "candidate_count": 1,
            "source_event_count": 1,
            "replay_confidence": "tick_quote_complete",
            "replay_confidence_reasons": [],
            "sampled_tape_cap": None,
            "sample_limited": False,
            "top_gate_reasons": [],
            "has_any_certifiable_source": True,
            "review_focus": "review_chart_context_before_opportunity",
            "marker_dry_run_command_template": (
                "python scripts\\mark_ross_trade_event.py CANF --action review_certified "
                f"--ts {_ts(10).isoformat()} --visual-evidence-id EVIDENCE_ID "
                '--note "Reviewed chart-context frames before replay opportunity" --dry-run'
            ),
            "marker_command_template": (
                "python scripts\\mark_ross_trade_event.py CANF --action review_certified "
                f"--ts {_ts(10).isoformat()} --visual-evidence-id EVIDENCE_ID "
                '--note "Reviewed chart-context frames before replay opportunity"'
            ),
            "marker_preflight_ready": True,
            "marker_preflight_blocker": None,
        }
    ]


def test_counterfactual_opportunity_label_reports_missing_certified_source() -> None:
    no_cert_source = SymbolReplayResult(
        symbol="TC",
        ok=True,
        confidence="tick_quote_complete",
        confidence_reasons=[],
        tape_rows=10,
        trade_rows=10,
        micro_bars=4,
        source_events=[
            {"ts": _ts(1).isoformat(), "source": "ross_transcript", "certifiable": False, "text": "watch"}
        ],
        trades=[],
        candidate_count=1,
        skipped_reasons={},
        gate_reason_counts={},
        first_candidate={"ts": _ts(10).isoformat(), "reason": "vwap_reclaim"},
    )
    result = CounterfactualReplayResult(
        since=_ts(0),
        until=_ts(60),
        symbols=["TC"],
        results=[no_cert_source],
    )

    summary = opportunity_label_summary(result)

    assert summary["status_counts"] == {"source_not_certified": 1}
    assert summary["rows"][0]["any_certifiable_source"] is False
    assert summary["rows"][0]["first_certifiable_source_ts"] is None
    assert summary["source_certification_queue"][0]["symbol"] == "TC"
    assert summary["source_certification_queue"][0]["status"] == "source_not_certified"
    assert (
        summary["source_certification_queue"][0]["action_required"]
        == "review_chart_frames_before_opportunity_and_link_certifying_marker; "
        "transcript_or_scanner_only_source_is_not_enough"
    )
    assert "mark_ross_trade_event.py TC" in summary["source_certification_queue"][0]["marker_command_template"]
    assert "--visual-evidence-id EVIDENCE_ID" in summary["source_certification_queue"][0]["marker_command_template"]
    assert (
        summary["source_certification_queue"][0]["marker_dry_run_command_template"]
        == summary["source_certification_queue"][0]["marker_command_template"] + " --dry-run"
    )
    assert summary["source_certification_queue"][0]["marker_preflight_ready"] is True
    assert summary["source_certification_queue"][0]["marker_preflight_blocker"] is None


def test_counterfactual_source_queue_notes_when_gate_candidate_is_absent() -> None:
    no_candidate = SymbolReplayResult(
        symbol="JEM",
        ok=True,
        confidence="tick_quote_complete",
        confidence_reasons=[],
        tape_rows=10,
        trade_rows=10,
        micro_bars=4,
        source_events=[
            {"ts": _ts(1).isoformat(), "source": "ross_transcript", "certifiable": False, "text": "watch"}
        ],
        trades=[],
        candidate_count=0,
        skipped_reasons={},
        gate_reason_counts={},
        first_candidate=None,
    )
    result = CounterfactualReplayResult(
        since=_ts(0),
        until=_ts(60),
        symbols=["JEM"],
        results=[no_candidate],
    )

    summary = opportunity_label_summary(result)

    assert summary["status_counts"] == {"source_not_certified": 1}
    assert summary["source_certification_queue"][0]["action_required"] == (
        "source_review_needed_but_no_current_gate_candidate; "
        "review_chart_frames_and_then_audit_entry_gate_shape"
    )
    assert summary["source_certification_queue"][0]["review_focus"] == "review_source_context_then_entry_gate_shape"
    assert "--ts REVIEWED_SOURCE_TS" in summary["source_certification_queue"][0]["marker_command_template"]
    assert "--dry-run" in summary["source_certification_queue"][0]["marker_dry_run_command_template"]
    assert summary["source_certification_queue"][0]["marker_preflight_ready"] is False
    assert summary["source_certification_queue"][0]["marker_preflight_blocker"] == "missing_opportunity_timestamp"


def test_counterfactual_source_queue_surfaces_sample_cap_and_top_gate_reasons() -> None:
    limited = SymbolReplayResult(
        symbol="JEM",
        ok=True,
        confidence="tick_quote_complete_limited",
        confidence_reasons=["sampled_tape_max_ticks_500", "ross_source_not_certified"],
        tape_rows=500,
        trade_rows=100,
        micro_bars=4,
        source_events=[
            {"ts": _ts(1).isoformat(), "source": "ross_transcript", "certifiable": False, "text": "watch"}
        ],
        trades=[],
        candidate_count=0,
        skipped_reasons={},
        gate_reason_counts={
            "waiting_for_vwap_reclaim": 10,
            "pullback_too_deep": 7,
            "below_explosive_floor_rvol": 2,
        },
        first_candidate=None,
    )
    result = CounterfactualReplayResult(
        since=_ts(0),
        until=_ts(60),
        symbols=["JEM"],
        results=[limited],
    )

    queue_row = opportunity_label_summary(result)["source_certification_queue"][0]

    assert queue_row["action_required"] == (
        "rerun_replay_with_higher_or_uncapped_ticks_before_gate_shape_claim; "
        "sampled_tape_cap_may_hide_later_candidate"
    )
    assert queue_row["replay_confidence"] == "tick_quote_complete_limited"
    assert queue_row["sampled_tape_cap"] == "sampled_tape_max_ticks_500"
    assert queue_row["sample_limited"] is True
    assert queue_row["top_gate_reasons"] == [
        {"reason": "waiting_for_vwap_reclaim", "count": 10},
        {"reason": "pullback_too_deep", "count": 7},
        {"reason": "below_explosive_floor_rvol", "count": 2},
    ]


def test_counterfactual_replay_keeps_symbol_level_error_labels(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay.load_ross_source_events",
        lambda **_kwargs: {},
    )

    def fake_symbol_replay(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay.run_counterfactual_symbol_replay",
        fake_symbol_replay,
    )

    result = run_counterfactual_replay(
        object(),
        symbols=["JEM"],
        since=_ts(0),
        until=_ts(60),
    )

    assert result.results[0].ok is False
    assert result.results[0].confidence == "replay_error"
    assert opportunity_label_summary(result)["status_counts"] == {"replay_error": 1}
