from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.services.trading.momentum_neural import ross_transcript_bridge as bridge


def _snapshot_row(symbol: str, *, price: float, change: float, volume: float, prev_volume: float):
    return {
        "ticker": symbol,
        "todaysChangePerc": change,
        "lastTrade": {"p": price},
        "day": {"c": price, "h": price * 1.05, "l": price * 0.55, "v": volume},
        "min": {"c": price, "av": volume},
        "prevDay": {"c": price / (1.0 + change / 100.0), "v": prev_volume},
    }


def test_extract_tickers_handles_spelled_symbols_and_filters_terms() -> None:
    text = "Watching C-A-N-F over VWAP, JEM attempt, DXST high day, MACD looks fine."

    assert bridge.extract_tickers_from_text(text) == ["CANF", "JEM", "DXST"]


def test_extract_tickers_filters_common_non_market_acronyms() -> None:
    text = "QR code on the show, the US sanctioned them, the LLC filed papers, and GDP was cut."

    assert bridge.extract_tickers_from_text(text) == []


def test_recent_transcript_mentions_ignores_non_trading_audio(tmp_path) -> None:
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    path = tmp_path / "transcript.jsonl"
    rows = [
        {
            "ts": (now - timedelta(seconds=20)).isoformat(),
            "text": "The GP clinic was open and the doctor was available",
        },
        {
            "ts": (now - timedelta(seconds=10)).isoformat(),
            "text": "QR code and the US audience is watching our show",
        },
        {
            "ts": (now - timedelta(seconds=8)).isoformat(),
            "text": "I had a long conversation with ABC earlier",
        },
        {
            "ts": (now - timedelta(seconds=5)).isoformat(),
            "text": "Watching CANF over VWAP for the first pullback",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    mentions = bridge.recent_transcript_mentions(
        path,
        now_utc=now,
        lookback_seconds=60,
        max_symbols=4,
    )

    assert [m.symbol for m in mentions] == ["CANF"]


def test_recent_transcript_mentions_accepts_ross_interest_watch_context(tmp_path) -> None:
    now = datetime(2026, 7, 2, 11, 43, tzinfo=timezone.utc)
    path = tmp_path / "transcript.jsonl"
    rows = [
        {
            "ts": (now - timedelta(seconds=10)).isoformat(),
            "text": "Ross is interested in CWD.",
        },
        {
            "ts": (now - timedelta(seconds=5)).isoformat(),
            "text": "CWD is interesting to me over 1.55.",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    mentions = bridge.recent_transcript_mentions(
        path,
        now_utc=now,
        lookback_seconds=60,
        max_symbols=4,
    )

    assert [m.symbol for m in mentions] == ["CWD"]
    assert "interesting to me over" in mentions[0].text


def test_recent_transcript_mentions_ignores_recap_not_live_trade(tmp_path) -> None:
    now = datetime(2026, 7, 1, 21, 52, tzinfo=timezone.utc)
    path = tmp_path / "transcript.jsonl"
    rows = [
        {
            "ts": (now - timedelta(seconds=5)).isoformat(),
            "text": (
                "C-A-N-F, you son of a gun. This was the second stock I traded, "
                "we'll break that one down in a second."
            ),
        },
        {
            "ts": (now - timedelta(seconds=2)).isoformat(),
            "text": "Watching CANF over VWAP for a first pullback scalp.",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    mentions = bridge.recent_transcript_mentions(
        path,
        now_utc=now,
        lookback_seconds=60,
        max_symbols=4,
    )

    assert [m.symbol for m in mentions] == ["CANF"]
    assert "Watching CANF" in mentions[0].text


def test_recent_transcript_mentions_rejects_only_recap_rows(tmp_path) -> None:
    now = datetime(2026, 7, 1, 21, 52, tzinfo=timezone.utc)
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": now.isoformat(),
                "text": (
                    "C-A-N-F, you son of a gun. This was the second stock I traded, "
                    "we'll break that one down in a second."
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    mentions = bridge.recent_transcript_mentions(
        path,
        now_utc=now,
        lookback_seconds=60,
        max_symbols=4,
    )

    assert mentions == []


def test_recent_transcript_mentions_rejects_scanner_recap_with_past_tense(tmp_path) -> None:
    now = datetime(2026, 7, 1, 21, 54, tzinfo=timezone.utc)
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": now.isoformat(),
                "text": (
                    "CANF. So CANF hits the running up scanner at 503 right there. "
                    "It's got 24 million shares of volume with a 2 million share float, "
                    "16 times relative volume today which I thought was pretty good."
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    mentions = bridge.recent_transcript_mentions(
        path,
        now_utc=now,
        lookback_seconds=60,
        max_symbols=4,
    )

    assert mentions == []


def test_recent_transcript_mentions_rejects_ambiguous_ticker_correction_rows(tmp_path) -> None:
    now = datetime(2026, 7, 1, 21, 52, tzinfo=timezone.utc)
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": now.isoformat(),
                "text": (
                    "This is the ANF, no this is DX, what was it? "
                    "Now I forgot which ticker it was. DXST, all right so DXST. S-S-T."
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    mentions = bridge.recent_transcript_mentions(
        path,
        now_utc=now,
        lookback_seconds=60,
        max_symbols=8,
    )

    assert mentions == []


def test_recent_transcript_mentions_rejects_recap_and_old_headline_rows(tmp_path) -> None:
    now = datetime(2026, 7, 1, 21, 52, tzinfo=timezone.utc)
    path = tmp_path / "transcript.jsonl"
    rows = [
        {
            "ts": (now - timedelta(seconds=9)).isoformat(),
            "text": "DXST at the scanner at 9.17 AM, it was almost immediately up 85%.",
        },
        {
            "ts": (now - timedelta(seconds=8)).isoformat(),
            "text": "I did miss this IPO BSP. Here's kind of a good example of how IPOs work.",
        },
        {
            "ts": (now - timedelta(seconds=7)).isoformat(),
            "text": "Let's check out that BSP from earlier. You can see the volume really went down.",
        },
        {
            "ts": (now - timedelta(seconds=6)).isoformat(),
            "text": "They were a little early on the BSP drop, but that 39 level might be important.",
        },
        {
            "ts": (now - timedelta(seconds=5)).isoformat(),
            "text": "That high volume candle on BSP when it opened was at 3188.",
        },
        {
            "ts": (now - timedelta(seconds=4)).isoformat(),
            "text": "Now the dollar volume on BSP is kind of...",
        },
        {
            "ts": (now - timedelta(seconds=3)).isoformat(),
            "text": "Members on BB Blackberry, volume down tick, old headlines from the FDA.",
        },
        {
            "ts": (now - timedelta(seconds=2)).isoformat(),
            "text": "EHGO on the 5 minute is a big drop, but the chart is intact for later.",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    mentions = bridge.recent_transcript_mentions(
        path,
        now_utc=now,
        lookback_seconds=60,
        max_symbols=8,
    )

    assert mentions == []


def test_recent_transcript_mentions_rejects_non_trading_shout_and_media_rows(tmp_path) -> None:
    now = datetime(2026, 7, 1, 21, 52, tzinfo=timezone.utc)
    path = tmp_path / "transcript.jsonl"
    rows = [
        {
            "ts": (now - timedelta(seconds=4)).isoformat(),
            "text": "When it stops it would be a noise like BEE! That was amazing!",
        },
        {
            "ts": (now - timedelta(seconds=2)).isoformat(),
            "text": "Havana has a McDonald's and a Walmart and a CBS surrounded by fentanyl addicts.",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    mentions = bridge.recent_transcript_mentions(
        path,
        now_utc=now,
        lookback_seconds=60,
        max_symbols=8,
    )

    assert mentions == []


def test_warrior_session_marker_requires_fresh_stream_marker(tmp_path) -> None:
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    marker = tmp_path / "warrior_session_ok.json"
    marker.write_text(
        json.dumps({"ok": True, "ts": now.isoformat(), "video_count": 1, "url": "https://chatroom.warriortrading.com/dashboard"}),
        encoding="utf-8",
    )

    ok, reason, detail = bridge.warrior_session_marker_ok(
        marker,
        now_utc=now + timedelta(seconds=5),
        max_age_seconds=30,
    )

    assert ok is True
    assert reason == "warrior_session_ok"
    assert detail["age_s"] == 5


def test_warrior_session_marker_rejects_missing_and_stale(tmp_path) -> None:
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    missing = tmp_path / "missing.json"
    ok, reason, _detail = bridge.warrior_session_marker_ok(missing, now_utc=now)
    assert ok is False
    assert reason == "warrior_session_marker_missing"

    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps({"ok": True, "ts": (now - timedelta(seconds=90)).isoformat(), "video_count": 1}),
        encoding="utf-8",
    )
    ok, reason, _detail = bridge.warrior_session_marker_ok(stale, now_utc=now, max_age_seconds=30)
    assert ok is False
    assert reason == "warrior_session_marker_stale"


def test_warrior_session_marker_preserves_explicit_negative_reason(tmp_path) -> None:
    marker = tmp_path / "warrior_session_ok.json"
    marker.write_text(
        '{"ok":false,"reason":"warrior_disclaimer_blocking","ts":"2026-07-01T12:00:00+00:00"}',
        encoding="utf-8",
    )

    ok, reason, detail = bridge.warrior_session_marker_ok(marker, now_utc=datetime(2026, 7, 1, 12, 0, 5, tzinfo=timezone.utc))

    assert ok is False
    assert reason == "warrior_disclaimer_blocking"
    assert detail["marker"]["reason"] == "warrior_disclaimer_blocking"


def test_run_bridge_once_requires_warrior_session_marker(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bridge.settings, "chili_momentum_ross_transcript_bridge_enabled", True, raising=False)
    monkeypatch.setattr(bridge.settings, "chili_momentum_ross_transcript_require_warrior_session_ok", True, raising=False)
    monkeypatch.setattr(
        bridge.settings,
        "chili_momentum_ross_transcript_warrior_session_ok_path",
        str(tmp_path / "missing_marker.json"),
        raising=False,
    )
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({"ts": now.isoformat(), "text": "JEM starter attempt"}) + "\n", encoding="utf-8")

    summary = bridge.run_ross_transcript_bridge_once(
        object(),
        transcript_path=str(path),
        now_utc=now,
        lookback_seconds=60,
        runner=lambda *args, **kwargs: {"ok": True},
    )

    assert summary["skipped"] == "warrior_session_marker_missing"
    assert summary["scored"] == 0


def test_run_bridge_once_allows_fresh_warrior_session_marker(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bridge.settings, "chili_momentum_ross_transcript_bridge_enabled", True, raising=False)
    monkeypatch.setattr(bridge.settings, "chili_momentum_ross_transcript_require_warrior_session_ok", True, raising=False)
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    marker = tmp_path / "warrior_session_ok.json"
    marker.write_text(json.dumps({"ok": True, "ts": now.isoformat(), "video_count": 1}), encoding="utf-8")
    monkeypatch.setattr(
        bridge.settings,
        "chili_momentum_ross_transcript_warrior_session_ok_path",
        str(marker),
        raising=False,
    )
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({"ts": now.isoformat(), "text": "JEM starter attempt"}) + "\n", encoding="utf-8")
    calls = []

    def runner(db, *, meta, **kwargs):
        calls.append(meta)
        return {"ok": True}

    summary = bridge.run_ross_transcript_bridge_once(
        object(),
        transcript_path=str(path),
        now_utc=now,
        lookback_seconds=60,
        snapshot_provider=lambda: [_snapshot_row("JEM", price=8.82, change=123.17, volume=21_000_000, prev_volume=700_000)],
        runner=runner,
    )

    assert summary["scored"] == 1
    assert calls[0]["tickers"] == ["JEM"]


def test_recent_transcript_mentions_uses_recent_latest_distinct_rows(tmp_path) -> None:
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    path = tmp_path / "transcript.jsonl"
    rows = [
        {"ts": (now - timedelta(minutes=5)).isoformat(), "text": "Old JEM"},
        {"ts": (now - timedelta(seconds=20)).isoformat(), "text": "JEM attempt here"},
        {"ts": (now - timedelta(seconds=5)).isoformat(), "text": "CANF over 80"},
        {"ts": (now - timedelta(seconds=3)).isoformat(), "text": "GDP of Venezuela got cut after Maduro took over"},
        {"ts": (now - timedelta(seconds=1)).isoformat(), "text": "JEM still watching"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    mentions = bridge.recent_transcript_mentions(
        path,
        now_utc=now,
        lookback_seconds=60,
        max_symbols=4,
    )

    assert [m.symbol for m in mentions] == ["JEM", "CANF"]
    assert mentions[0].text == "JEM still watching"


def test_build_signal_map_marks_transcript_focus_without_single_symbol_field() -> None:
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    mention = bridge.TranscriptMention(symbol="JEM", ts=now, text="JEM starter attempt")
    snapshot = [
        _snapshot_row("JEM", price=8.82, change=123.17, volume=21_000_000, prev_volume=700_000),
        _snapshot_row("CANF", price=5.75, change=80.0, volume=11_000_000, prev_volume=1_200_000),
        _snapshot_row("DXST", price=4.23, change=42.0, volume=8_000_000, prev_volume=900_000),
    ]

    focus, signals = bridge.build_ross_transcript_signal_map(
        [mention],
        snapshot=snapshot,
        now_utc=now,
    )

    assert focus == ["JEM"]
    assert set(signals).issuperset({"JEM", "CANF", "DXST"})
    assert signals["JEM"]["signal_type"] == "ross_transcript_mention"
    assert "ross" in signals["JEM"]["source"]
    assert signals["JEM"]["daily_change_pct"] == 123.17
    assert signals["JEM"]["price"] == 8.82
    assert signals["JEM"]["dollar_volume"] > 100_000_000


def test_build_signal_map_repairs_missing_leading_letter_asr_when_market_proves_symbol() -> None:
    now = datetime(2026, 7, 2, 12, 23, tzinfo=timezone.utc)
    mention = bridge.TranscriptMention(
        symbol="LRO",
        ts=now,
        text="The LRO, I got a starter in the small account looking for the squeeze through five",
    )
    snapshot = [
        _snapshot_row("CLRO", price=4.95, change=118.0, volume=24_000_000, prev_volume=800_000),
    ]

    focus, signals = bridge.build_ross_transcript_signal_map(
        [mention],
        snapshot=snapshot,
        now_utc=now,
    )

    assert focus == ["CLRO"]
    assert signals["CLRO"]["signal_type"] == "ross_transcript_mention"
    assert signals["CLRO"]["transcript_text"].startswith("The LRO")
    assert signals["CLRO"]["playbook_hint"] == "ross_breakout_starter_or_first_pullback"


def test_build_signal_map_does_not_repair_near_symbol_without_missing_leading_letter_shape() -> None:
    now = datetime(2026, 7, 2, 12, 23, tzinfo=timezone.utc)
    mention = bridge.TranscriptMention(
        symbol="DXTS",
        ts=now,
        text="DXTS starter attempt through high of day",
    )
    snapshot = [
        _snapshot_row("DXF", price=3.41, change=64.0, volume=14_000_000, prev_volume=900_000),
    ]

    focus, signals = bridge.build_ross_transcript_signal_map(
        [mention],
        snapshot=snapshot,
        now_utc=now,
    )

    assert focus == []
    assert "DXF" in signals
    assert signals["DXF"]["signal_type"] == "ross_field_snapshot"


def test_symbol_resolution_warnings_surface_near_market_symbol_without_remapping() -> None:
    warnings = bridge.symbol_resolution_warnings(
        ["DXTS"],
        resolved_signals={},
        snapshot=[
            _snapshot_row("DXF", price=3.41, change=64.0, volume=14_000_000, prev_volume=900_000),
            _snapshot_row("CANF", price=5.75, change=80.0, volume=11_000_000, prev_volume=1_200_000),
        ],
    )

    assert warnings == [
        {
            "mentioned_symbol": "DXTS",
            "reason": "mentioned_symbol_unresolved_near_market_symbol",
            "near_symbols": [
                {
                    "symbol": "DXF",
                    "edit_distance": 2,
                    "price": 3.41,
                    "change_pct": 64.0,
                    "source": "snapshot",
                }
            ],
        }
    ]


def test_symbol_resolution_warnings_do_not_flag_resolved_exact_symbol() -> None:
    warnings = bridge.symbol_resolution_warnings(
        ["DXTS"],
        resolved_signals={"DXTS": {"symbol": "DXTS"}},
        snapshot=[
            _snapshot_row("DXF", price=3.41, change=64.0, volume=14_000_000, prev_volume=900_000),
        ],
    )

    assert warnings == []


def test_run_bridge_once_reports_unresolved_near_symbol_without_admitting(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    marker = tmp_path / "warrior_session_ok.json"
    marker.write_text(json.dumps({"ok": True, "ts": now.isoformat(), "video_count": 1}), encoding="utf-8")
    monkeypatch.setattr(
        bridge.settings,
        "chili_momentum_ross_transcript_warrior_session_ok_path",
        str(marker),
        raising=False,
    )
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps({"ts": now.isoformat(), "text": "DXTS starter attempt through high of day"}) + "\n",
        encoding="utf-8",
    )
    calls = []

    def runner(db, *, meta, **kwargs):
        calls.append(meta)
        return {"ok": True}

    summary = bridge.run_ross_transcript_bridge_once(
        object(),
        transcript_path=str(path),
        now_utc=now,
        lookback_seconds=60,
        snapshot_provider=lambda: [
            _snapshot_row("DXF", price=3.41, change=64.0, volume=14_000_000, prev_volume=900_000),
        ],
        runner=runner,
    )

    assert summary["skipped"] == "mentions_without_market_pillars"
    assert summary["scored"] == 0
    assert calls == []
    assert summary["symbol_resolution_warnings"][0]["mentioned_symbol"] == "DXTS"
    assert summary["symbol_resolution_warnings"][0]["near_symbols"][0]["symbol"] == "DXF"


def test_run_bridge_once_near_symbol_warning_never_calls_admission(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bridge.settings, "chili_momentum_ross_event_admission_enabled", True, raising=False)
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    marker = tmp_path / "warrior_session_ok.json"
    marker.write_text(json.dumps({"ok": True, "ts": now.isoformat(), "video_count": 1}), encoding="utf-8")
    monkeypatch.setattr(
        bridge.settings,
        "chili_momentum_ross_transcript_warrior_session_ok_path",
        str(marker),
        raising=False,
    )
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps({"ts": now.isoformat(), "text": "DXTS starter attempt through high of day"}) + "\n",
        encoding="utf-8",
    )
    runner_calls = []
    admission_calls = []

    summary = bridge.run_ross_transcript_bridge_once(
        object(),
        transcript_path=str(path),
        now_utc=now,
        lookback_seconds=60,
        snapshot_provider=lambda: [
            _snapshot_row("DXF", price=3.41, change=64.0, volume=14_000_000, prev_volume=900_000),
        ],
        runner=lambda *args, **kwargs: runner_calls.append(kwargs) or {"ok": True},
        admitter=lambda *args, **kwargs: admission_calls.append(kwargs) or {"ok": True, "admitted": True},
    )

    assert summary["skipped"] == "mentions_without_market_pillars"
    assert summary["scored"] == 0
    assert runner_calls == []
    assert admission_calls == []
    warning = summary["symbol_resolution_warnings"][0]
    assert warning["mentioned_symbol"] == "DXTS"
    assert warning["reason"] == "mentioned_symbol_unresolved_near_market_symbol"
    assert warning["near_symbols"][0]["symbol"] == "DXF"


def test_run_bridge_once_calls_pipeline_for_recent_mentions(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    marker = tmp_path / "warrior_session_ok.json"
    marker.write_text(json.dumps({"ok": True, "ts": now.isoformat(), "video_count": 1}), encoding="utf-8")
    monkeypatch.setattr(
        bridge.settings,
        "chili_momentum_ross_transcript_warrior_session_ok_path",
        str(marker),
        raising=False,
    )
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps({"ts": now.isoformat(), "text": "JEM attempt breakout"}) + "\n",
        encoding="utf-8",
    )
    calls = []

    def runner(db, *, meta, **kwargs):
        calls.append(meta)
        return {"ok": True}

    summary = bridge.run_ross_transcript_bridge_once(
        object(),
        transcript_path=str(path),
        now_utc=now,
        lookback_seconds=60,
        snapshot_provider=lambda: [
            _snapshot_row("JEM", price=8.82, change=123.17, volume=21_000_000, prev_volume=700_000),
            _snapshot_row("CANF", price=5.75, change=80.0, volume=11_000_000, prev_volume=1_200_000),
        ],
        runner=runner,
    )

    assert summary["scored"] == 1
    assert summary["symbols"] == ["JEM"]
    assert calls[0]["tickers"] == ["JEM"]
    assert "CANF" in calls[0]["ross_signals"]


def test_run_bridge_once_forwards_focus_symbol_to_admission(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bridge.settings, "chili_momentum_ross_event_admission_enabled", True, raising=False)
    now = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)
    marker = tmp_path / "warrior_session_ok.json"
    marker.write_text(json.dumps({"ok": True, "ts": now.isoformat(), "video_count": 1}), encoding="utf-8")
    monkeypatch.setattr(
        bridge.settings,
        "chili_momentum_ross_transcript_warrior_session_ok_path",
        str(marker),
        raising=False,
    )
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps({"ts": now.isoformat(), "text": "JEM starter attempt"}) + "\n",
        encoding="utf-8",
    )
    admissions = []

    def runner(db, *, meta, **kwargs):
        return {"ok": True, "symbols": meta["tickers"]}

    def admitter(db, **kwargs):
        admissions.append(kwargs)
        return {"ok": True, "admitted": True, "symbol": kwargs["symbol"]}

    summary = bridge.run_ross_transcript_bridge_once(
        object(),
        transcript_path=str(path),
        now_utc=now,
        lookback_seconds=60,
        snapshot_provider=lambda: [
            _snapshot_row("JEM", price=8.82, change=123.17, volume=21_000_000, prev_volume=700_000),
        ],
        runner=runner,
        admitter=admitter,
    )

    assert summary["admitted"] == 1
    assert admissions[0]["symbol"] == "JEM"
    assert admissions[0]["source"] == "ross_transcript"
    assert admissions[0]["refresh_viability"] is False
    assert admissions[0]["signal"]["signal_type"] == "ross_transcript_mention"
