from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.trading.momentum_neural.ross_feed_health import FeedHealth
from app.services.trading.momentum_neural.ross_transcript_bridge import warrior_session_marker_ok
from scripts.verify_ross_event_admission_runtime import _recent_events, evaluate_recent_ross_admissions
from scripts.verify_ross_lane_feed_runtime import check_feed_health
from scripts.verify_ross_transcript_runtime import _host_processes, evaluate_transcript_runtime


READINESS_PROFILES = ("quiet", "prestream", "live")


def readiness_requirements_for_profile(
    profile: str,
    *,
    require_warrior_session: bool = False,
    require_live_event_evidence: bool = False,
) -> dict[str, bool]:
    profile_s = str(profile or "quiet").strip().lower()
    if profile_s not in READINESS_PROFILES:
        profile_s = "quiet"
    return {
        "require_warrior_session": bool(require_warrior_session or profile_s in {"prestream", "live"}),
        "require_live_event_evidence": bool(require_live_event_evidence or profile_s == "live"),
    }


def evaluate_live_window_readiness(
    *,
    feed: FeedHealth,
    admission_ok: bool,
    admission_reason: str,
    admission_detail: dict[str, Any],
    transcript_ok: bool,
    transcript_reason: str,
    transcript_detail: dict[str, Any],
    require_warrior_session: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    details = {
        "feed_reason": feed.reason,
        "feed_severity": feed.severity,
        "feed": feed.details,
        "admission_reason": admission_reason,
        "admission": admission_detail,
        "transcript_reason": transcript_reason,
        "transcript": transcript_detail,
        "require_warrior_session": bool(require_warrior_session),
    }
    if not feed.ok:
        return False, f"ross_live_window_feed_not_ready:{feed.reason}", details
    if not transcript_ok:
        return False, f"ross_live_window_transcript_not_safe:{transcript_reason}", details
    warrior_reason = str(transcript_detail.get("warrior_session_reason") or "")
    if require_warrior_session and warrior_reason != "warrior_session_ok":
        return False, f"ross_live_window_warrior_session_not_ready:{warrior_reason or 'unknown'}", details
    if not admission_ok:
        return False, f"ross_live_window_admission_not_proven:{admission_reason}", details
    if str(feed.severity).lower() != "ok":
        return True, "ross_live_window_ready_with_warnings", details
    return True, "ross_live_window_ready", details


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Ross live-window readiness across feed, transcript, and event admission evidence.")
    parser.add_argument(
        "--profile",
        choices=READINESS_PROFILES,
        default="quiet",
        help="quiet=off-hours guard, prestream=Warrior stream/session required, live=stream plus live Ross admission evidence required.",
    )
    parser.add_argument("--since-minutes", type=float, default=30.0)
    parser.add_argument("--min-ticks", type=int, default=1)
    parser.add_argument(
        "--require-live-event-evidence",
        action="store_true",
        help="Require at least one recent live-source Ross admission; use after Ross/tape events are expected.",
    )
    parser.add_argument(
        "--require-warrior-session",
        action="store_true",
        help="Require the Warrior stream marker to be fresh and visible; use while actively monitoring Ross.",
    )
    parser.add_argument("--marker-path", default=None)
    parser.add_argument("--marker-max-age-seconds", type=float, default=None)
    parser.add_argument("--max-iqfeed-age-hot-s", type=float, default=60.0)
    args = parser.parse_args(argv)
    requirements = readiness_requirements_for_profile(
        args.profile,
        require_warrior_session=args.require_warrior_session,
        require_live_event_evidence=args.require_live_event_evidence,
    )

    feed = check_feed_health(max_iqfeed_age_hot_s=args.max_iqfeed_age_hot_s)
    min_checked = 1 if requirements["require_live_event_evidence"] else 0
    admission_ok, admission_reason, admission_detail = evaluate_recent_ross_admissions(
        _recent_events(since_minutes=args.since_minutes),
        min_ticks=args.min_ticks,
        min_checked=min_checked,
    )
    marker_ok, marker_reason, marker_detail = warrior_session_marker_ok(
        args.marker_path,
        max_age_seconds=args.marker_max_age_seconds,
    )
    transcript_ok, transcript_reason, transcript_detail = evaluate_transcript_runtime(
        marker_ok=marker_ok,
        marker_reason=marker_reason,
        marker_detail=marker_detail,
        processes=_host_processes(),
    )
    ok, reason, details = evaluate_live_window_readiness(
        feed=feed,
        admission_ok=admission_ok,
        admission_reason=admission_reason,
        admission_detail=admission_detail,
        transcript_ok=transcript_ok,
        transcript_reason=transcript_reason,
        transcript_detail=transcript_detail,
        require_warrior_session=requirements["require_warrior_session"],
    )
    print(reason)
    print(f"profile={args.profile}")
    print(f"feed_reason={details['feed_reason']}")
    print(f"feed_severity={details['feed_severity']}")
    print(f"admission_reason={details['admission_reason']}")
    print(f"admission_checked={details['admission'].get('checked')}")
    print(f"admission_min_checked={details['admission'].get('min_checked')}")
    print(f"warrior_session_reason={details['transcript'].get('warrior_session_reason')}")
    print(f"running_daemons={len(details['transcript'].get('running_daemons') or [])}")
    print(f"require_warrior_session={requirements['require_warrior_session']}")
    print(f"require_live_event_evidence={requirements['require_live_event_evidence']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
