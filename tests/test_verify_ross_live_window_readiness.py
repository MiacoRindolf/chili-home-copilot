from __future__ import annotations

from app.services.trading.momentum_neural.ross_feed_health import FeedHealth
from scripts.verify_ross_live_window_readiness import evaluate_live_window_readiness, readiness_requirements_for_profile


def _feed(*, ok: bool = True, severity: str = "ok", reason: str = "ross_lane_feed_runtime_ok") -> FeedHealth:
    return FeedHealth(
        ok=ok,
        severity=severity,
        reason=reason,
        details={"clock": {"in_hot_window": False}},
    )


def _transcript(*, marker_reason: str = "warrior_session_marker_not_ok") -> tuple[bool, str, dict]:
    return True, "ross_transcript_ingestion_guarded", {"warrior_session_reason": marker_reason, "running_daemons": []}


def _admission(*, ok: bool = True, reason: str = "ross_event_admission_runtime_ok", checked: int = 0) -> tuple[bool, str, dict]:
    return ok, reason, {"checked": checked, "bad": [], "min_checked": 0}


def test_live_window_readiness_allows_quiet_period_without_stream_or_admissions() -> None:
    transcript_ok, transcript_reason, transcript_detail = _transcript()
    admission_ok, admission_reason, admission_detail = _admission()

    ok, reason, detail = evaluate_live_window_readiness(
        feed=_feed(),
        admission_ok=admission_ok,
        admission_reason=admission_reason,
        admission_detail=admission_detail,
        transcript_ok=transcript_ok,
        transcript_reason=transcript_reason,
        transcript_detail=transcript_detail,
    )

    assert ok is True
    assert reason == "ross_live_window_ready"
    assert detail["admission"]["checked"] == 0


def test_readiness_profiles_expand_required_evidence() -> None:
    assert readiness_requirements_for_profile("quiet") == {
        "require_warrior_session": False,
        "require_live_event_evidence": False,
    }
    assert readiness_requirements_for_profile("prestream") == {
        "require_warrior_session": True,
        "require_live_event_evidence": False,
    }
    assert readiness_requirements_for_profile("live") == {
        "require_warrior_session": True,
        "require_live_event_evidence": True,
    }
    assert readiness_requirements_for_profile("quiet", require_warrior_session=True) == {
        "require_warrior_session": True,
        "require_live_event_evidence": False,
    }


def test_live_window_readiness_can_require_warrior_stream_evidence() -> None:
    transcript_ok, transcript_reason, transcript_detail = _transcript(marker_reason="warrior_session_marker_not_ok")
    admission_ok, admission_reason, admission_detail = _admission()

    ok, reason, _detail = evaluate_live_window_readiness(
        feed=_feed(),
        admission_ok=admission_ok,
        admission_reason=admission_reason,
        admission_detail=admission_detail,
        transcript_ok=transcript_ok,
        transcript_reason=transcript_reason,
        transcript_detail=transcript_detail,
        require_warrior_session=True,
    )

    assert ok is False
    assert reason == "ross_live_window_warrior_session_not_ready:warrior_session_marker_not_ok"


def test_live_window_readiness_can_require_event_admission_evidence() -> None:
    transcript_ok, transcript_reason, transcript_detail = _transcript(marker_reason="warrior_session_ok")

    ok, reason, _detail = evaluate_live_window_readiness(
        feed=_feed(),
        admission_ok=False,
        admission_reason="ross_event_admission_no_recent_live_events",
        admission_detail={"checked": 0, "bad": [], "min_checked": 1},
        transcript_ok=transcript_ok,
        transcript_reason=transcript_reason,
        transcript_detail=transcript_detail,
        require_warrior_session=True,
    )

    assert ok is False
    assert reason == "ross_live_window_admission_not_proven:ross_event_admission_no_recent_live_events"


def test_live_window_readiness_blocks_feed_error() -> None:
    transcript_ok, transcript_reason, transcript_detail = _transcript(marker_reason="warrior_session_ok")
    admission_ok, admission_reason, admission_detail = _admission(checked=1)

    ok, reason, _detail = evaluate_live_window_readiness(
        feed=_feed(ok=False, severity="error", reason="iqfeed_l1_stale_during_hot_live_window"),
        admission_ok=admission_ok,
        admission_reason=admission_reason,
        admission_detail=admission_detail,
        transcript_ok=transcript_ok,
        transcript_reason=transcript_reason,
        transcript_detail=transcript_detail,
        require_warrior_session=True,
    )

    assert ok is False
    assert reason == "ross_live_window_feed_not_ready:iqfeed_l1_stale_during_hot_live_window"


def test_live_window_readiness_surfaces_feed_warning_without_failing() -> None:
    transcript_ok, transcript_reason, transcript_detail = _transcript()
    admission_ok, admission_reason, admission_detail = _admission()

    ok, reason, detail = evaluate_live_window_readiness(
        feed=_feed(severity="warn", reason="iqfeed_l1_stale_outside_hot_window"),
        admission_ok=admission_ok,
        admission_reason=admission_reason,
        admission_detail=admission_detail,
        transcript_ok=transcript_ok,
        transcript_reason=transcript_reason,
        transcript_detail=transcript_detail,
    )

    assert ok is True
    assert reason == "ross_live_window_ready_with_warnings"
    assert detail["feed_severity"] == "warn"
