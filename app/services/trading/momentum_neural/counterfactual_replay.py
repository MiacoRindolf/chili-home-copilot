"""Counterfactual market-tape Replay v3 for the Ross momentum lane.

This is intentionally separate from ``live_replay_audit``.  The audit replays
already-recorded CHILI sessions.  This module replays historical market tape
against the current entry gate code, then simulates broker fills locally.

Scope of this first counterfactual layer:
- reads persisted IQFeed/NBBO quote tape and IQFeed trade prints;
- requires a Ross/source event before simulated Ross-lane entry by default;
- calls current side-effect-free CHILI entry gates;
- requires a structural stop before simulating an entry;
- simulates entry at ask and exits at bid via stop, first target, or max-hold;
- reports confidence/data boundaries instead of claiming full live parity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ....config import settings
from ..timeframe_utils import canonical_interval_for_seconds
from .entry_gates import (
    TICK_ARMED_WAIT_REASONS,
    momentum_pullback_trigger,
    vwap_reclaim_confirmation,
)

# SHIM (main-lineage adoption): ``ross_breakout_starter_confirmation`` is a codex-fork
# LIVE entry gate (entry_gates.py:8160 on the fork) that is NOT on main and is
# fork-entangled (depends on ``_round_breakout_level_near``/``compute_all_from_df``
# helpers not present in main's entry_gates). Per the master-fix-plan discipline we do
# NOT graft an unversioned live gate into main's decision path just to run the replay.
# Instead: import the real gate if present, else fall back to a benign no-fire stub so
# the counterfactual replay still exercises every OTHER gate family (pullback,
# vwap_reclaim, tick_scalp). When the gate later lands on main as its own reviewed PR,
# this shim transparently picks it up.
try:  # pragma: no cover - exercised only on the fork lineage
    from .entry_gates import ross_breakout_starter_confirmation  # type: ignore
except ImportError:  # main lineage: gate not present
    def ross_breakout_starter_confirmation(  # type: ignore
        df: Any,
        *,
        entry_interval: str,
        live_price: "float | None" = None,
        symbol: "str | None" = None,
        now: Any = None,
        db: Any = None,
        l2_as_of: Any = None,
    ) -> "tuple[bool, str, dict[str, Any]]":
        # Benign no-fire: the replay simply skips this gate family on main lineage.
        return (
            False,
            "ross_breakout_starter_unavailable_on_main",
            {"entry_interval": entry_interval, "pattern": "ross_breakout_starter"},
        )

from .micro_bars import _resample_micro_bars
from .risk_policy import (
    equity_relative_notional_cap,
    liquidity_capped_notional,
    replay_account_equity,
)
from .replay_provenance import (
    IQFEED_NBBO_TIMESTAMP_BASIS,
    IQFEED_TRADE_TIMESTAMP_BASIS,
    certify_iqfeed_tape_row,
)
from .tick_scalp import (
    ROSS_TICK_SCALP_COURSE_PRICE_FLOOR,
    ROSS_TICK_SCALP_MAX_PRICE,
    evaluate_tick_first_pullback,
)


DEFAULT_ROSS_TRANSCRIPT_PATH = Path(r"D:\CHILI-Docker\chili-data\ross_stream\transcript.jsonl")
DEFAULT_ROSS_ADMISSION_PATHS = (
    Path(r"D:\CHILI-Docker\chili-data\ross_stream\ross_transcript_admission_audit.jsonl"),
    Path(r"D:\CHILI-Docker\chili-data\ross_stream\ross_admission_dry_run.jsonl"),
)
DEFAULT_ROSS_TRADE_EVENTS_PATH = Path(r"D:\CHILI-Docker\chili-data\ross_stream\ross_trade_events.jsonl")
DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH = Path(
    "project_ws/AgentOps/ross_video_evidence/review_manifest.json"
)


@dataclass(frozen=True)
class ReplayTapeTick:
    ts: datetime
    bid: float
    ask: float
    mid: float
    spread_bps: float | None = None
    source: str | None = None
    size: float | None = None
    sequence: int | None = None
    availability_ts: datetime | None = None
    market_reference_ts: datetime | None = None
    provenance_certified: bool = False
    provenance_reasons: tuple[str, ...] = ()
    receive_reference_delta_seconds: float | None = None


@dataclass(frozen=True)
class RossSourceEvent:
    symbol: str
    ts: datetime
    text: str = ""
    source: str = "ross_source"
    signal: dict[str, Any] = field(default_factory=dict)
    certifiable: bool = False


@dataclass(frozen=True)
class ReplayEntryCandidate:
    symbol: str
    ts: datetime
    reason: str
    entry_price: float
    stop_price: float
    trigger_debug: dict[str, Any]
    gate_family: str
    bid: float
    ask: float
    spread_bps: float | None
    sequence: int | None = None

    @property
    def risk_per_share(self) -> float:
        return max(0.0, self.entry_price - self.stop_price)


@dataclass(frozen=True)
class CounterfactualTrade:
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    qty: float
    pnl_usd: float
    pnl_r: float
    reason: str
    exit_reason: str
    gate_family: str
    max_favorable_r: float
    max_adverse_r: float
    debug: dict[str, Any]


@dataclass(frozen=True)
class SymbolReplayResult:
    symbol: str
    ok: bool
    confidence: str
    confidence_reasons: list[str]
    tape_rows: int
    trade_rows: int
    causal_tape_rows: int
    causal_trade_rows: int
    causal_provenance_enforced: bool
    micro_bars: int
    source_events: list[dict[str, Any]]
    trades: list[CounterfactualTrade]
    candidate_count: int
    skipped_reasons: dict[str, int]
    gate_reason_counts: dict[str, int]
    first_candidate: dict[str, Any] | None
    bar_seconds: int = 60
    coverage_ready: bool = False
    coverage_reasons: tuple[str, ...] = ()

    @property
    def pnl_usd(self) -> float:
        return round(sum(t.pnl_usd for t in self.trades), 4)

    @property
    def pnl_r(self) -> float:
        return round(sum(t.pnl_r for t in self.trades), 4)


@dataclass(frozen=True)
class CounterfactualReplayResult:
    since: datetime
    until: datetime
    symbols: list[str]
    results: list[SymbolReplayResult]
    causal_provenance_enforced: bool = False
    requested_exit_model: str = "adaptive"
    exit_engine: str = "simplified_counterfactual"
    production_exit_parity: bool = False
    read_only: bool = True
    boundary: str = (
        "Counterfactual Replay v3 P1 uses persisted IQFeed/NBBO tape and current "
        "entry-gate code with a local simulated broker and Ross/source-before-entry "
        "admission. Certification additionally requires post-publication causal provenance; "
        "legacy rows are diagnostic only. It does not yet execute the full live runner FSM, live risk "
        "evaluator, order idempotency, L2 depth, or broker-specific order lifecycle."
    )

    @property
    def pnl_usd(self) -> float:
        return round(sum(r.pnl_usd for r in self.results), 4)

    @property
    def pnl_r(self) -> float:
        return round(sum(r.pnl_r for r in self.results), 4)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _json_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _safe_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return ()
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ()
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _visual_review_rows_by_evidence_id(path: Path) -> dict[str, dict[str, Any]]:
    manifest = _read_json_dict(path)
    reviews = manifest.get("reviews")
    if not isinstance(reviews, list):
        return {}
    by_evidence_id: dict[str, list[dict[str, Any]]] = {}
    out: dict[str, dict[str, Any]] = {}
    for row in reviews:
        if not isinstance(row, Mapping):
            continue
        evidence_id = str(row.get("evidence_id") or "").strip()
        if not evidence_id:
            continue
        review = dict(row)
        review["_visual_review_manifest_dir"] = str(path.parent)
        by_evidence_id.setdefault(evidence_id, []).append(review)
        sym = _safe_symbol(review.get("symbol"))
        if sym:
            out[f"{evidence_id}::{sym}"] = review
    for evidence_id, rows in by_evidence_id.items():
        if len(rows) == 1:
            out[evidence_id] = rows[0]
    return out


def _reviewed_frame_files_exist(frame_paths: Sequence[str], review: Mapping[str, Any]) -> bool:
    manifest_dir = Path(str(review.get("_visual_review_manifest_dir") or "."))
    for raw_path in frame_paths:
        raw_path = str(raw_path or "").strip()
        if not raw_path:
            continue
        normalized = raw_path.replace("\\", "/")
        path = Path(normalized)
        candidates = [path] if path.is_absolute() else [Path.cwd() / path, manifest_dir / path]
        if any(candidate.exists() for candidate in candidates):
            return True
    return False


def _ross_trade_event_certifiable(
    row: Mapping[str, Any],
    *,
    visual_reviews: Mapping[str, Mapping[str, Any]],
) -> tuple[bool, str]:
    evidence_id = str(
        row.get("visual_evidence_id")
        or row.get("evidence_id")
        or row.get("video_id")
        or row.get("source_video_id")
        or ""
    ).strip()
    if not evidence_id:
        if bool(row.get("certifiable") or row.get("trade_no_trade_certifiable")):
            return False, "trade_event_explicit_certification_requires_visual_review"
        return False, "trade_event_missing_visual_evidence_id"
    action = str(row.get("action") or "").strip().lower()
    if action != "review_certified":
        return False, "trade_event_action_not_review_certified"

    event_symbol = _safe_symbol(row.get("symbol") or row.get("ticker"))
    review = visual_reviews.get(f"{evidence_id}::{event_symbol}") if event_symbol else None
    if not review:
        review = visual_reviews.get(evidence_id)
    if not review:
        return False, "trade_event_visual_evidence_unreviewed"
    if _safe_symbol(review.get("symbol")) and _safe_symbol(review.get("symbol")) != event_symbol:
        return False, "trade_event_visual_evidence_symbol_mismatch"
    if review.get("source_before_opportunity_certifiable") is not True:
        return False, "trade_event_visual_evidence_not_source_before_opportunity"
    frame_paths = [str(p or "").strip() for p in review.get("reviewed_frame_paths") or []]
    if not any(frame_paths):
        return False, "trade_event_visual_evidence_missing_reviewed_frames"
    if not _reviewed_frame_files_exist(frame_paths, review):
        return False, "trade_event_visual_evidence_missing_reviewed_frame_files"
    evidence_type = str(review.get("evidence_type") or "").strip().lower()
    if (
        not evidence_type
        or "scanner" in evidence_type
        or "post_opportunity" in evidence_type
        or ("chart" not in evidence_type and "trade" not in evidence_type)
    ):
        return False, "trade_event_visual_evidence_not_chart_trade_context"
    if bool(review.get("trade_no_trade_certifiable")):
        return True, "trade_event_visual_evidence_trade_certified"
    return False, "trade_event_visual_evidence_noncertifying"


def _signal_from_source_row(row: Mapping[str, Any]) -> dict[str, Any]:
    signal: dict[str, Any] = {}
    for root_key in ("ross_evidence_debug", "ross_universe_debug"):
        root = row.get(root_key)
        if isinstance(root, Mapping):
            signal.update(dict(root))
    for key in (
        "price",
        "last_price",
        "change_pct",
        "daily_change_pct",
        "rvol",
        "rvol_pace",
        "float_shares",
        "dollar_volume",
    ):
        if row.get(key) is not None:
            signal[key] = row.get(key)
    transcript_text = str(row.get("transcript_text") or row.get("text") or "").strip()
    if transcript_text:
        signal["transcript_text"] = transcript_text[:500]
        signal["source"] = "ross_audio_transcript counterfactual_replay"
        signal["scanner_source"] = "ross_audio_transcript"
        signal["signal_type"] = "ross_transcript_mention"
    elif row.get("source"):
        signal["source"] = str(row.get("source"))
    if "ross" not in str(signal.get("source") or "").lower():
        signal["source"] = f"{signal.get('source') or 'counterfactual'} ross"
    return signal


def _asr_symbol_aliases_from_text(text_value: str, wanted: set[str]) -> list[str]:
    """Map known ASR ticker confusions, constrained to requested replay symbols."""

    out: list[str] = []
    if "JEM" in wanted and re.search(r"\bgem\b", text_value, flags=re.IGNORECASE):
        out.append("JEM")
    return out


def load_ross_source_events(
    *,
    since: datetime,
    until: datetime,
    symbols: Sequence[str] | None = None,
    transcript_path: Path = DEFAULT_ROSS_TRANSCRIPT_PATH,
    admission_paths: Sequence[Path] = DEFAULT_ROSS_ADMISSION_PATHS,
    trade_events_path: Path = DEFAULT_ROSS_TRADE_EVENTS_PATH,
    visual_review_manifest_path: Path = DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH,
) -> dict[str, list[RossSourceEvent]]:
    """Load local Ross source rows for comparison and tick-scalp evidence.

    Transcript rows are evidence-light unless paired with admission/debug rows.
    The result is deliberately grouped by symbol; callers decide whether a row is
    strong enough to seed tick-first-pullback evidence.
    """

    wanted = {_safe_symbol(s) for s in symbols or [] if _safe_symbol(s)}
    by_symbol: dict[str, list[RossSourceEvent]] = {}

    def add(event: RossSourceEvent) -> None:
        if wanted and event.symbol not in wanted:
            return
        if event.ts < since or event.ts >= until:
            return
        by_symbol.setdefault(event.symbol, []).append(event)

    for path in admission_paths:
        for row in _read_jsonl(Path(path)):
            sym = _safe_symbol(row.get("symbol") or row.get("ticker"))
            ts = _parse_dt(row.get("transcript_ts") or row.get("audit_ts") or row.get("ts"))
            if not sym or ts is None:
                continue
            reason = str(row.get("ross_evidence_reason") or row.get("ross_universe_reason") or "")
            certifiable = bool(
                row.get("admitted")
                or row.get("would_admit")
                or reason in {"tick_first_pullback_watch", "ross_universe_profile_ok"}
            )
            add(
                RossSourceEvent(
                    symbol=sym,
                    ts=ts,
                    text=str(row.get("transcript_text") or row.get("text") or "")[:500],
                    source=str(row.get("source") or Path(path).name),
                    signal=_signal_from_source_row(row),
                    certifiable=certifiable,
                )
            )

    visual_reviews = _visual_review_rows_by_evidence_id(Path(visual_review_manifest_path))
    for row in _read_jsonl(Path(trade_events_path)):
        sym = _safe_symbol(row.get("symbol") or row.get("ticker"))
        ts = _parse_dt(row.get("ts") or row.get("time") or row.get("at"))
        if not sym or ts is None:
            continue
        certifiable, cert_reason = _ross_trade_event_certifiable(
            row,
            visual_reviews=visual_reviews,
        )
        signal = _signal_from_source_row(
            {
                **dict(row),
                "source": "ross_trade_event",
                "signal_type": "ross_trade_event_marker",
            }
        )
        signal["certification_reason"] = cert_reason
        add(
            RossSourceEvent(
                symbol=sym,
                ts=ts,
                text=str(row.get("note") or row.get("text") or row.get("action") or "")[:500],
                source="ross_trade_event",
                signal=signal,
                certifiable=certifiable,
            )
        )

    for row in _read_jsonl(Path(transcript_path)):
        ts = _parse_dt(row.get("ts"))
        text_s = str(row.get("text") or "")
        if ts is None or not text_s:
            continue
        try:
            from .ross_transcript_bridge import extract_tickers_from_text, has_trading_context

            if not has_trading_context(text_s):
                continue
            syms = extract_tickers_from_text(text_s)
        except Exception:
            syms = []
        syms = list(dict.fromkeys([*syms, *_asr_symbol_aliases_from_text(text_s, wanted)]))
        if "CANF" in {_safe_symbol(s) for s in syms} and "canf" in text_s.lower():
            syms = [s for s in syms if _safe_symbol(s) != "ANF"]
        for sym in syms:
            add(
                RossSourceEvent(
                    symbol=_safe_symbol(sym),
                    ts=ts,
                    text=text_s[:500],
                    source="ross_transcript",
                    signal={
                        "source": "ross_audio_transcript counterfactual_replay",
                        "scanner_source": "ross_audio_transcript",
                        "signal_type": "ross_transcript_mention",
                        "transcript_text": text_s[:500],
                    },
                    certifiable=False,
                )
            )

    for events in by_symbol.values():
        events.sort(key=lambda event: event.ts)
    return by_symbol


def _database_table_has_column(db: Session, table_name: str, column_name: str) -> bool:
    """Read-only schema probe; lightweight fakes assume the current schema."""

    if not hasattr(db, "get_bind"):
        return True
    try:
        return bool(
            db.execute(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = :table_name AND column_name = :column_name"
                    ")"
                ),
                {"table_name": table_name, "column_name": column_name},
            ).scalar()
        )
    except Exception:
        return False


def load_nbbo_tape(
    db: Session,
    symbol: str,
    *,
    since: datetime,
    until: datetime,
    max_ticks: int | None = None,
    require_causal_provenance: bool = False,
) -> list[ReplayTapeTick]:
    """Read persisted NBBO tape for one symbol without mutating DB.

    When ``require_causal_provenance`` is true, legacy rows are rejected and
    the event clock becomes bridge ``available_at`` (a conservative marker
    stamped after persistence/publication), not socket receipt or the earlier
    provider trade-time reference.
    """

    sym = _safe_symbol(symbol)
    has_available_at = _database_table_has_column(
        db,
        "momentum_nbbo_spread_tape",
        "available_at",
    )
    if require_causal_provenance and not has_available_at:
        return []
    available_select = (
        "available_at" if has_available_at else "NULL::timestamptz AS available_at"
    )
    expected_build = str(
        getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "") or ""
    ).strip()
    params = {
        "symbol": sym,
        "since": since,
        "until": until,
        "expected_basis": IQFEED_NBBO_TIMESTAMP_BASIS,
        "expected_build": expected_build,
    }
    clock_column = "available_at" if require_causal_provenance else "observed_at"
    strict_predicate = (
        " AND source = 'iqfeed_l1'"
        " AND provider_event_at IS NULL"
        " AND received_at IS NOT NULL"
        " AND available_at IS NOT NULL"
        " AND timestamp_basis = :expected_basis"
        " AND bridge_version = :expected_build"
        " AND provider_trade_reference_at IS NOT NULL"
        " AND message_type = 'Q'"
        " AND bridge_run_id IS NOT NULL"
        " AND connection_generation > 0"
        if require_causal_provenance
        else ""
    )
    if max_ticks is not None and max_ticks > 0:
        params["limit"] = int(max_ticks)
        total_s = max(1.0, (until - since).total_seconds())
        params["step_s"] = max(1.0, total_s / float(max(1, int(max_ticks))))
        sql = text(
            "WITH buckets AS ("
            "  SELECT generate_series(:since, :until, make_interval(secs => :step_s)) AS bucket"
            ") "
            f"SELECT DISTINCT ON (x.{clock_column}) "
            "       x.id, x.observed_at, x.bid, x.ask, x.mid, x.spread_bps, x.source, "
            "       x.provider_event_at, x.received_at, x.available_at, "
            "       x.timestamp_basis, x.bridge_version, "
            "       x.provider_trade_reference_at, x.message_type, x.bridge_run_id, "
            "       x.connection_generation "
            "FROM buckets b "
            "JOIN LATERAL ("
            "  SELECT id, observed_at, bid, ask, mid, spread_bps, source, "
            f"         provider_event_at, received_at, {available_select}, "
            "         timestamp_basis, bridge_version, "
            "         provider_trade_reference_at, message_type, bridge_run_id, "
            "         connection_generation "
            "  FROM momentum_nbbo_spread_tape "
            "  WHERE symbol = :symbol "
            f"    AND {clock_column} >= b.bucket "
            f"    AND {clock_column} < b.bucket + make_interval(secs => :step_s) "
            f"    AND {clock_column} >= :since AND {clock_column} < :until "
            "    AND bid > 0 AND ask > 0 AND ask >= bid "
            f"    {strict_predicate} "
            f"  ORDER BY {clock_column} ASC, id ASC "
            "  LIMIT 1"
            ") x ON true "
            f"ORDER BY x.{clock_column} ASC, x.id ASC LIMIT :limit"
        )
    else:
        sql = text(
            "SELECT id, observed_at, bid, ask, mid, spread_bps, source, "
            f"       provider_event_at, received_at, {available_select}, "
            "       timestamp_basis, bridge_version, "
            "       provider_trade_reference_at, message_type, bridge_run_id, "
            "       connection_generation "
            "FROM momentum_nbbo_spread_tape "
            f"WHERE symbol = :symbol AND {clock_column} >= :since AND {clock_column} < :until "
            "  AND bid > 0 AND ask > 0 AND ask >= bid "
            f"  {strict_predicate} "
            f"ORDER BY {clock_column} ASC, id ASC"
        )
    rows = db.execute(sql, params).mappings().all()
    ticks: list[ReplayTapeTick] = []
    for row in rows:
        observed_ts = _parse_dt(row.get("observed_at"))
        bid = _float_or_none(row.get("bid"))
        ask = _float_or_none(row.get("ask"))
        if observed_ts is None or bid is None or ask is None or bid <= 0 or ask < bid:
            continue
        provenance = certify_iqfeed_tape_row(
            row,
            expected_timestamp_basis=IQFEED_NBBO_TIMESTAMP_BASIS,
            expected_bridge_build=expected_build,
        )
        if require_causal_provenance and not provenance.certified:
            continue
        replay_ts = provenance.availability_ts if provenance.certified else observed_ts
        if replay_ts is None or replay_ts < since or replay_ts >= until:
            continue
        mid = _float_or_none(row.get("mid"))
        if mid is None or mid <= 0:
            mid = (bid + ask) / 2.0
        ticks.append(
            ReplayTapeTick(
                ts=replay_ts,
                bid=bid,
                ask=ask,
                mid=mid,
                spread_bps=_float_or_none(row.get("spread_bps")),
                source=str(row.get("source") or "") or None,
                sequence=int(row["id"]) if row.get("id") is not None else None,
                availability_ts=provenance.availability_ts,
                market_reference_ts=provenance.market_reference_ts or observed_ts,
                provenance_certified=provenance.certified,
                provenance_reasons=provenance.reasons,
                receive_reference_delta_seconds=(
                    provenance.receive_reference_delta_seconds
                ),
            )
        )
    return sorted(ticks, key=lambda tick: (tick.ts, tick.sequence or 0))


def load_trade_tape(
    db: Session,
    symbol: str,
    *,
    since: datetime,
    until: datetime,
    max_ticks: int | None = None,
    require_causal_provenance: bool = False,
) -> list[ReplayTapeTick]:
    """Read persisted IQFeed trade prints for one symbol without mutating DB.

    Strict mode uses the bridge post-publication clock and drops any row lacking
    the complete Q/run/generation/pinned-build/fresh-reference provenance tuple.
    """

    sym = _safe_symbol(symbol)
    has_available_at = _database_table_has_column(
        db,
        "iqfeed_trade_ticks",
        "available_at",
    )
    if require_causal_provenance and not has_available_at:
        return []
    available_select = (
        "available_at" if has_available_at else "NULL::timestamptz AS available_at"
    )
    expected_build = str(
        getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "") or ""
    ).strip()
    params: dict[str, Any] = {
        "symbol": sym,
        "since": (
            since
            if require_causal_provenance
            else since.astimezone(timezone.utc).replace(tzinfo=None)
        ),
        "until": (
            until
            if require_causal_provenance
            else until.astimezone(timezone.utc).replace(tzinfo=None)
        ),
        "expected_basis": IQFEED_TRADE_TIMESTAMP_BASIS,
        "expected_build": expected_build,
    }
    clock_column = "available_at" if require_causal_provenance else "observed_at"
    strict_predicate = (
        " AND source = 'iqfeed_l1'"
        " AND provider_event_at IS NULL"
        " AND received_at IS NOT NULL"
        " AND available_at IS NOT NULL"
        " AND timestamp_basis = :expected_basis"
        " AND bridge_version = :expected_build"
        " AND provider_trade_reference_at IS NOT NULL"
        " AND message_type = 'Q'"
        " AND bridge_run_id IS NOT NULL"
        " AND connection_generation > 0"
        if require_causal_provenance
        else ""
    )
    if max_ticks is not None and max_ticks > 0:
        params["limit"] = int(max_ticks)
        total_s = max(1.0, (until - since).total_seconds())
        params["step_s"] = max(1.0, total_s / float(max(1, int(max_ticks))))
        sql = text(
            "WITH buckets AS ("
            "  SELECT generate_series(:since, :until, make_interval(secs => :step_s)) AS bucket"
            ") "
            f"SELECT DISTINCT ON (x.{clock_column}) "
            "       x.id, x.observed_at, x.price, x.size, x.bid, x.ask, x.source, "
            "       x.provider_event_at, x.received_at, x.available_at, "
            "       x.timestamp_basis, x.bridge_version, "
            "       x.provider_trade_reference_at, x.message_type, x.bridge_run_id, "
            "       x.connection_generation "
            "FROM buckets b "
            "JOIN LATERAL ("
            "  SELECT id, observed_at, price, size, bid, ask, source, "
            f"         provider_event_at, received_at, {available_select}, "
            "         timestamp_basis, bridge_version, "
            "         provider_trade_reference_at, message_type, bridge_run_id, "
            "         connection_generation "
            "  FROM iqfeed_trade_ticks "
            "  WHERE symbol = :symbol "
            f"    AND {clock_column} >= b.bucket "
            f"    AND {clock_column} < b.bucket + make_interval(secs => :step_s) "
            f"    AND {clock_column} >= :since AND {clock_column} < :until "
            "    AND price > 0 "
            f"    {strict_predicate} "
            f"  ORDER BY {clock_column} ASC, id ASC "
            "  LIMIT 1"
            ") x ON true "
            f"ORDER BY x.{clock_column} ASC, x.id ASC LIMIT :limit"
        )
    else:
        sql = text(
            "SELECT id, observed_at, price, size, bid, ask, source, "
            f"       provider_event_at, received_at, {available_select}, "
            "       timestamp_basis, bridge_version, "
            "       provider_trade_reference_at, message_type, bridge_run_id, "
            "       connection_generation "
            "FROM iqfeed_trade_ticks "
            f"WHERE symbol = :symbol AND {clock_column} >= :since AND {clock_column} < :until "
            "  AND price > 0 "
            f"  {strict_predicate} "
            f"ORDER BY {clock_column} ASC, id ASC"
        )
    rows = db.execute(sql, params).mappings().all()
    ticks: list[ReplayTapeTick] = []
    for row in rows:
        observed_ts = _parse_dt(row.get("observed_at"))
        price = _float_or_none(row.get("price"))
        if observed_ts is None or price is None or price <= 0:
            continue
        provenance = certify_iqfeed_tape_row(
            row,
            expected_timestamp_basis=IQFEED_TRADE_TIMESTAMP_BASIS,
            expected_bridge_build=expected_build,
        )
        if require_causal_provenance and not provenance.certified:
            continue
        replay_ts = provenance.availability_ts if provenance.certified else observed_ts
        if replay_ts is None or replay_ts < since or replay_ts >= until:
            continue
        bid = _float_or_none(row.get("bid"))
        ask = _float_or_none(row.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            bid = price
            ask = price
        ticks.append(
            ReplayTapeTick(
                ts=replay_ts,
                bid=bid,
                ask=ask,
                mid=price,
                spread_bps=None,
                source=str(row.get("source") or "iqfeed_trade_ticks") or "iqfeed_trade_ticks",
                size=_float_or_none(row.get("size")),
                sequence=int(row["id"]) if row.get("id") is not None else None,
                availability_ts=provenance.availability_ts,
                market_reference_ts=provenance.market_reference_ts or observed_ts,
                provenance_certified=provenance.certified,
                provenance_reasons=provenance.reasons,
                receive_reference_delta_seconds=(
                    provenance.receive_reference_delta_seconds
                ),
            )
        )
    return sorted(ticks, key=lambda tick: (tick.ts, tick.sequence or 0))


_REPLAY_BAR_RELEASE_TIMES_ATTR = "replay_bar_release_times_utc"
_REPLAY_REAL_EVENT_BUCKETS_ATTR = "replay_bar_has_strategy_event"


def _tick_market_reference(tick: ReplayTapeTick) -> datetime | None:
    """Return the market-geometry clock for a persisted tape row."""

    return _parse_dt(tick.market_reference_ts) or _parse_dt(tick.ts)


def _tick_strategy_available(tick: ReplayTapeTick) -> datetime | None:
    """Return the earliest conservative strategy-availability clock."""

    return _parse_dt(tick.availability_ts) or _parse_dt(tick.ts)


def _release_clock_by_market_bucket(
    ticks: Sequence[ReplayTapeTick],
    *,
    bar_seconds: int,
) -> dict[pd.Timestamp, pd.Timestamp]:
    """Maximum strategy-availability time for each market-reference bucket.

    Geometry and release are deliberately separate.  A quote received at
    ``15:00:14.900`` belongs to the market bucket ending at ``15:00:15`` even
    if the bridge does not publish it until ``15:00:16``.  The completed bar
    may therefore use the former for OHLC placement but cannot be exposed to a
    strategy before the latter.
    """

    seconds = max(1, int(bar_seconds or 1))
    freq = f"{seconds}s"
    release_by_start: dict[pd.Timestamp, pd.Timestamp] = {}
    for tick in ticks or ():
        market_ts = _tick_market_reference(tick)
        available_ts = _tick_strategy_available(tick)
        if market_ts is None or available_ts is None:
            continue
        bucket = pd.Timestamp(market_ts).tz_convert("UTC").floor(freq)
        available = pd.Timestamp(available_ts).tz_convert("UTC")
        prior = release_by_start.get(bucket)
        if prior is None or available > prior:
            release_by_start[bucket] = available
    return release_by_start


def _merge_release_clocks(
    *clocks: Mapping[pd.Timestamp, pd.Timestamp],
) -> dict[pd.Timestamp, pd.Timestamp]:
    merged: dict[pd.Timestamp, pd.Timestamp] = {}
    for clock in clocks:
        for bucket, available in clock.items():
            prior = merged.get(bucket)
            if prior is None or available > prior:
                merged[bucket] = available
    return merged


def _market_geometry_ordered_ticks(
    ticks: Sequence[ReplayTapeTick],
) -> list[ReplayTapeTick]:
    """Stable deterministic order for repeated market-reference timestamps.

    IQFeed Q rows use Most-Recent-Trade-Time as a bounded containment proxy,
    not an exact quote event timestamp, so many quote updates may share it.
    Availability followed by persisted sequence preserves the observable order
    inside such a tie without moving the row into a different market bucket.
    """

    decorated: list[tuple[datetime, datetime, int, int, ReplayTapeTick]] = []
    for ordinal, tick in enumerate(ticks or ()):
        market_ts = _tick_market_reference(tick)
        available_ts = _tick_strategy_available(tick)
        if market_ts is None or available_ts is None:
            continue
        sequence = tick.sequence if tick.sequence is not None else ordinal
        decorated.append((market_ts, available_ts, int(sequence), ordinal, tick))
    decorated.sort(key=lambda row: row[:4])
    return [row[-1] for row in decorated]


def _label_microbars_at_close(
    frame: pd.DataFrame | None,
    *,
    bar_seconds: int,
    release_at_by_bucket_start: Mapping[pd.Timestamp, pd.Timestamp] | None = None,
    strategy_event_bucket_starts: set[pd.Timestamp] | None = None,
) -> pd.DataFrame | None:
    """Label geometry at market close and attach its causal release clock.

    ``pandas.resample`` and the shared live micro-bar builder label a bucket at
    its *start*.  That is harmless live because the builder only receives tape
    observed so far.  Counterfactual replay builds the whole historical frame
    up front, however, so evaluating a ``13:00:00``-labeled 15-second bar at
    ``13:00:00`` would expose its eventual high/low/close/volume through
    ``13:00:14.999``.  Shift the index to the exclusive right edge so a full
    bucket becomes visible only when it has completed.

    Strict replay also has a second clock: the bridge's post-publication
    ``available_at``.  OHLC/volume membership remains anchored to the market
    reference timestamp, while an aligned release vector records when each bar
    may first enter a decision frame.  Release times are cumulative-maxed in
    market order.  That deliberately withholds a later bucket behind an older
    late-arriving bucket rather than exposing a non-prefix frame or looking
    ahead.  Equal release times are treated atomically by
    :func:`_iter_bar_candidates`.

    Non-datetime frames are returned unchanged and are not certification-grade.
    """

    if frame is None or getattr(frame, "empty", True):
        return frame
    out = frame.copy()
    if isinstance(out.index, pd.DatetimeIndex):
        seconds = max(1, int(bar_seconds or 1))
        starts = pd.DatetimeIndex(pd.to_datetime(out.index, utc=True))
        closes = starts + pd.Timedelta(seconds=seconds)
        releases: list[pd.Timestamp] = []
        prior_release: pd.Timestamp | None = None
        release_map = release_at_by_bucket_start or {}
        for start, close in zip(starts, closes):
            direct = release_map.get(start)
            release = max(close, pd.Timestamp(direct).tz_convert("UTC")) if direct is not None else close
            if prior_release is not None and release < prior_release:
                release = prior_release
            releases.append(release)
            prior_release = release
        out.index = closes
        out.attrs["replay_timestamp_semantics"] = (
            "market_close_geometry_with_strategy_release_clock"
        )
        out.attrs["replay_bar_seconds"] = seconds
        out.attrs[_REPLAY_BAR_RELEASE_TIMES_ATTR] = tuple(
            release.to_pydatetime() for release in releases
        )
        if strategy_event_bucket_starts is not None:
            out.attrs[_REPLAY_REAL_EVENT_BUCKETS_ATTR] = tuple(
                start in strategy_event_bucket_starts for start in starts
            )
        out.attrs["replay_release_clock_cumulative"] = True
    return out


def _bar_release_times(
    bars: pd.DataFrame,
    *,
    require_explicit: bool = False,
) -> tuple[datetime, ...]:
    """Aligned release vector, falling back to the bar index diagnostically."""

    raw = bars.attrs.get(_REPLAY_BAR_RELEASE_TIMES_ATTR)
    if isinstance(raw, (list, tuple)) and len(raw) == len(bars):
        parsed = tuple(_parse_dt(value) for value in raw)
        if all(value is not None for value in parsed):
            return tuple(value for value in parsed if value is not None)
    if require_explicit:
        return ()
    fallback: list[datetime] = []
    for value in bars.index:
        parsed = _parse_dt(value.to_pydatetime() if hasattr(value, "to_pydatetime") else value)
        if parsed is None:
            return ()
        fallback.append(parsed)
    return tuple(fallback)


def _tape_to_microbars(
    ticks: Sequence[ReplayTapeTick],
    *,
    bar_seconds: int,
    trade_ticks: Sequence[ReplayTapeTick] = (),
) -> pd.DataFrame | None:
    ordered_ticks = _market_geometry_ordered_ticks(ticks)
    ordered_volume_ticks = _market_geometry_ordered_ticks(trade_ticks or ticks)
    rows = [
        (market_ts, tick.bid, tick.ask)
        for tick in ordered_ticks
        if (market_ts := _tick_market_reference(tick)) is not None
    ]
    # F1 (capture-g fix) live-vs-replay parity: the live micro build now joins REAL
    # per-bucket volume from the trade tape; replay ticks carry print size, so feed the
    # same volume basis (absent sizes ⇒ None ⇒ NaN volume = UNKNOWN, exactly like live).
    volume_ticks = ordered_volume_ticks
    trows = [
        (market_ts, tick.size)
        for tick in volume_ticks
        if tick.size is not None
        and tick.size > 0
        and (market_ts := _tick_market_reference(tick)) is not None
    ]
    quote_release_clock = _release_clock_by_market_bucket(
        ordered_ticks,
        bar_seconds=bar_seconds,
    )
    release_clock = _merge_release_clocks(
        quote_release_clock,
        _release_clock_by_market_bucket(volume_ticks, bar_seconds=bar_seconds),
    )
    return _label_microbars_at_close(
        _resample_micro_bars(rows, bar_seconds=bar_seconds, trade_rows=trows or None),
        bar_seconds=bar_seconds,
        release_at_by_bucket_start=release_clock,
        strategy_event_bucket_starts=set(quote_release_clock),
    )


def _trade_tape_to_microbars(ticks: Sequence[ReplayTapeTick], *, bar_seconds: int) -> pd.DataFrame | None:
    """Build microbars from actual trade prints, using trade size as volume."""

    try:
        seconds = max(1, int(bar_seconds or 15))
    except (TypeError, ValueError):
        seconds = 15
    records: list[tuple[datetime, float, float]] = []
    ordered_ticks = _market_geometry_ordered_ticks(ticks)
    for tick in ordered_ticks:
        market_ts = _tick_market_reference(tick)
        if market_ts is None:
            continue
        px = _float_or_none(tick.mid)
        if px is None or px <= 0:
            continue
        size = _float_or_none(tick.size)
        records.append((market_ts, px, max(1.0, float(size or 1.0))))
    if len(records) < 2:
        return None
    df = pd.DataFrame(records, columns=["ts", "price", "size"])
    if df.empty or len(df) < 2:
        return None
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    ohlc = df["price"].resample(f"{seconds}s").ohlc().dropna()
    if ohlc.empty:
        return None
    ohlc["volume"] = df["size"].resample(f"{seconds}s").sum().reindex(ohlc.index).fillna(0.0)
    for lower, title in (
        ("open", "Open"),
        ("high", "High"),
        ("low", "Low"),
        ("close", "Close"),
        ("volume", "Volume"),
    ):
        ohlc[title] = ohlc[lower]
    release_clock = _release_clock_by_market_bucket(
        ordered_ticks,
        bar_seconds=seconds,
    )
    return _label_microbars_at_close(
        ohlc,
        bar_seconds=seconds,
        release_at_by_bucket_start=release_clock,
        strategy_event_bucket_starts=set(release_clock),
    )


def _trade_tape_frame(ticks: Sequence[ReplayTapeTick]) -> pd.DataFrame | None:
    records: list[tuple[datetime, float, float, int]] = []
    for ordinal, tick in enumerate(ticks or []):
        ts = tick.ts
        px = _float_or_none(tick.mid)
        if not isinstance(ts, datetime) or px is None or px <= 0:
            continue
        size = _float_or_none(tick.size)
        seq = tick.sequence if tick.sequence is not None else ordinal
        records.append((ts, px, max(1.0, float(size or 1.0)), int(seq)))
    if len(records) < 2:
        return None
    df = pd.DataFrame(records, columns=["ts", "price", "size", "sequence"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values(["ts", "sequence"]).set_index("ts")


def _trade_dollar_volume_asof(
    ticks: Sequence[ReplayTapeTick],
    as_of: datetime,
) -> float | None:
    """Executed dollar volume available no later than ``as_of``."""

    total = sum(
        float(t.mid) * float(t.size)
        for t in ticks
        if (
            t.ts <= as_of
            and t.size is not None
            and t.mid is not None
            and t.mid > 0
        )
    )
    return total or None


def _latest_tick_at_or_before(ticks: Sequence[ReplayTapeTick], ts: datetime) -> ReplayTapeTick | None:
    # Single pass would be faster, but this stays simple and the bar count is small.
    best: ReplayTapeTick | None = None
    for tick in ticks:
        if tick.ts > ts:
            break
        best = tick
    return best


def _structural_stop_from_debug(debug: Mapping[str, Any], entry_price: float) -> float | None:
    for key in ("structural_stop_price", "pullback_low", "stop_price"):
        stop = _float_or_none(debug.get(key))
        if stop is not None and 0 < stop < entry_price:
            return stop
    return None


def _risk_sized_qty(entry: float, stop: float, *, risk_usd: float, max_notional_usd: float) -> tuple[float, dict[str, Any]]:
    dist = max(0.0, float(entry) - float(stop))
    if dist <= 0 or risk_usd <= 0:
        return 0.0, {"reason": "invalid_risk_distance", "risk_per_share": dist}
    qty = float(risk_usd) / dist
    capped_by = None
    if max_notional_usd > 0 and qty * entry > max_notional_usd:
        qty = max_notional_usd / entry
        capped_by = "notional_ceiling"
    if qty <= 0:
        return 0.0, {"reason": "qty_nonpositive", "risk_per_share": dist}
    return qty, {
        "model": "structural_risk_first",
        "risk_per_share": round(dist, 6),
        "risk_usd": round(risk_usd, 2),
        "notional_usd": round(qty * entry, 2),
        "capped_by": capped_by,
    }


def _candidate_quality_profile(candidate: ReplayEntryCandidate) -> dict[str, Any]:
    debug = candidate.trigger_debug if isinstance(candidate.trigger_debug, Mapping) else {}
    family = str(candidate.gate_family or "").strip()
    reason = str(candidate.reason or "").strip()
    explicit_grade = str(
        debug.get("setup_quality")
        or debug.get("quality_grade")
        or debug.get("entry_quality")
        or ""
    ).strip().upper()
    profile: dict[str, Any] = {
        "setup_family": family,
        "setup_reason": reason,
        "grade": None,
        "quality": "target_first",
        "notional_sizing_allowed": False,
    }
    if explicit_grade in {"A", "A+", "APLUS", "A_PLUS"}:
        grade = "A+" if explicit_grade in {"A+", "APLUS", "A_PLUS"} else "A"
        profile.update(
            {
                "grade": grade,
                "quality": "explicit_a_grade",
                "notional_sizing_allowed": True,
                "reason": "explicit_setup_quality",
            }
        )
        return profile

    is_vwap_burst = family in {"tick_vwap_reclaim_burst", "vwap_reclaim"} or reason in {
        "tick_vwap_reclaim_burst",
        "vwap_reclaim",
        "vwap_reclaim_tick_ok",
    }
    if not is_vwap_burst:
        profile["reason"] = "starter_or_scalp_target_first"
        return profile

    volume_ratio = _float_or_none(debug.get("volume_ratio"))
    required_volume_ratio = _float_or_none(debug.get("required_volume_ratio"))
    volume_ok = (
        volume_ratio is not None
        and required_volume_ratio is not None
        and volume_ratio >= max(0.0, required_volume_ratio)
    )
    spread_cost = _float_or_none(debug.get("spread_cost_of_r"))
    max_spread_cost = _float_or_none(debug.get("max_spread_cost_of_r"))
    spread_ok = spread_cost is None or max_spread_cost is None or spread_cost <= max_spread_cost
    source_state = str(debug.get("source_state") or "")
    source_ok = (
        source_state == "entry_actionable_source"
        or bool(debug.get("market_certified"))
        or bool(debug.get("source_blocker_recertified"))
    )
    profile.update(
        {
            "volume_ratio": None if volume_ratio is None else round(volume_ratio, 4),
            "required_volume_ratio": (
                None if required_volume_ratio is None else round(required_volume_ratio, 4)
            ),
            "volume_ok": bool(volume_ok),
            "spread_cost_of_r": None if spread_cost is None else round(spread_cost, 6),
            "max_spread_cost_of_r": None if max_spread_cost is None else round(max_spread_cost, 6),
            "spread_ok": bool(spread_ok),
            "source_state": source_state or None,
            "source_ok": bool(source_ok),
        }
    )
    if volume_ok and spread_ok and source_ok:
        profile.update(
            {
                "grade": "A+",
                "quality": "a_plus_vwap_reclaim_burst",
                "notional_sizing_allowed": True,
                "reason": "runner_earned_by_volume_source_and_spread_r",
            }
        )
        return profile

    blockers: list[str] = []
    if not volume_ok:
        blockers.append("volume_below_required")
    if not spread_ok:
        blockers.append("spread_cost_too_high_vs_r")
    if not source_ok:
        blockers.append("source_not_actionable_or_market_certified")
    profile["reason"] = "target_first_quality_incomplete"
    profile["blockers"] = blockers
    return profile


SIMPLE_TARGET_ARMED_1R_TRAIL = "simple_target_armed_1r_trail"
_SIMPLE_TRAIL_ALIASES = frozenset(
    {
        "momentum_trail",
        "live_runner_trail",
        "runner_trail",
        SIMPLE_TARGET_ARMED_1R_TRAIL,
    }
)


def counterfactual_exit_model_metadata(exit_model: str | None) -> dict[str, Any]:
    """Describe the counterfactual exit engine without claiming FSM parity."""

    requested = str(exit_model or "fixed_target").strip().lower()
    if requested in _SIMPLE_TRAIL_ALIASES:
        effective: str | list[str] = SIMPLE_TARGET_ARMED_1R_TRAIL
    elif requested == "adaptive":
        effective = ["fixed_target", SIMPLE_TARGET_ARMED_1R_TRAIL]
    else:
        effective = requested
    return {
        "requested": requested,
        "effective": effective,
        "engine": "simplified_counterfactual",
        "production_exit_parity": False,
        "legacy_alias": (
            requested
            if requested in {"momentum_trail", "live_runner_trail", "runner_trail"}
            else None
        ),
        "boundary": (
            "This engine is a whole-position fixed target or a target-armed 1R "
            "trail. It does not execute live_runner's partials, breakeven runner, "
            "VWAP/BOS/topping-tail/OFI exits, adaptive green max-hold, or broker "
            "order lifecycle. Use ReplayV3Driver for production-exit decisions."
        ),
    }


def _adaptive_exit_model_for_candidate(candidate: ReplayEntryCandidate) -> tuple[str, dict[str, Any]]:
    """Route exits by setup evidence instead of using one global style.

    A fast VWAP/reclaim burst earns a runner only when the same evidence that
    admitted it is still strong: trade-volume burst over its own required floor,
    spread cost inside the setup's R budget, and either source/actionable context
    or market recertification. Starter/scalp candidates stay target-first; those
    are deliberately tighter de-risk trades until they earn separate runner proof.
    """
    profile = _candidate_quality_profile(candidate)
    route = {
        "router": "adaptive",
        "setup_family": profile.get("setup_family"),
        "setup_reason": profile.get("setup_reason"),
        "selected_exit_model": "fixed_target",
        "production_exit_parity": False,
        "grade": profile.get("grade"),
        "quality": profile.get("quality"),
    }
    if profile.get("quality") == "explicit_a_grade":
        route["reason"] = "explicit_a_grade_target_first"
        return "fixed_target", route
    if profile.get("quality") == "target_first" and profile.get("reason") == "starter_or_scalp_target_first":
        route["reason"] = profile.get("reason")
        return "fixed_target", route

    route.update(
        {
            "volume_ratio": profile.get("volume_ratio"),
            "required_volume_ratio": profile.get("required_volume_ratio"),
            "volume_ok": bool(profile.get("volume_ok")),
            "spread_cost_of_r": profile.get("spread_cost_of_r"),
            "max_spread_cost_of_r": profile.get("max_spread_cost_of_r"),
            "spread_ok": bool(profile.get("spread_ok")),
            "source_state": profile.get("source_state"),
            "source_ok": bool(profile.get("source_ok")),
        }
    )
    if profile.get("quality") == "a_plus_vwap_reclaim_burst":
        route["selected_exit_model"] = SIMPLE_TARGET_ARMED_1R_TRAIL
        route["reason"] = profile.get("reason")
        return SIMPLE_TARGET_ARMED_1R_TRAIL, route

    route["reason"] = profile.get("reason") or "target_first_quality_incomplete"
    route["blockers"] = profile.get("blockers") or []
    return "fixed_target", route


def _resolved_exit_model_for_candidate(
    candidate: ReplayEntryCandidate,
    exit_model: str | None,
) -> tuple[str, dict[str, Any]]:
    model = str(exit_model or "fixed_target").strip().lower()
    if model == "adaptive":
        return _adaptive_exit_model_for_candidate(candidate)
    effective = (
        SIMPLE_TARGET_ARMED_1R_TRAIL if model in _SIMPLE_TRAIL_ALIASES else model
    )
    return effective, {
        "router": "explicit",
        "requested_exit_model": model,
        "selected_exit_model": effective,
        "quality": "operator_forced",
        "production_exit_parity": False,
        "legacy_alias": (
            model
            if model in {"momentum_trail", "live_runner_trail", "runner_trail"}
            else None
        ),
    }


def _simulate_candidate_trade(
    candidate: ReplayEntryCandidate,
    ticks: Sequence[ReplayTapeTick],
    *,
    risk_usd: float,
    max_notional_usd: float,
    reward_risk: float,
    max_hold_seconds: float,
    fixed_qty: float | None = None,
    cash_usd: float | None = None,
    cash_fraction: float | None = None,
    exit_model: str = "fixed_target",
) -> CounterfactualTrade | None:
    fixed = _float_or_none(fixed_qty)
    cash = _float_or_none(cash_usd)
    fraction = _float_or_none(cash_fraction)
    quality_profile = _candidate_quality_profile(candidate)
    if (
        cash is not None
        and cash > 0
        and fraction is not None
        and fraction > 0
        and bool(quality_profile.get("notional_sizing_allowed"))
    ):
        frac = max(0.0, min(1.0, float(fraction)))
        notional = cash * frac
        qty = notional / candidate.entry_price if candidate.entry_price > 0 else 0.0
        risk_per_share = max(0.0, candidate.entry_price - candidate.stop_price)
        sizing = {
            "model": "a_grade_cash_fraction_notional",
            "cash_usd": round(cash, 2),
            "cash_fraction": round(frac, 6),
            "qty": round(qty, 6),
            "risk_per_share": round(risk_per_share, 6),
            "risk_usd": round(qty * risk_per_share, 2),
            "notional_usd": round(notional, 2),
            "quality": quality_profile.get("quality"),
            "grade": quality_profile.get("grade"),
        }
    elif fixed is not None and fixed > 0:
        risk_per_share = max(0.0, candidate.entry_price - candidate.stop_price)
        qty = float(fixed)
        sizing = {
            "model": "fixed_share_counterfactual",
            "qty": round(qty, 6),
            "risk_per_share": round(risk_per_share, 6),
            "risk_usd": round(qty * risk_per_share, 2),
            "notional_usd": round(qty * candidate.entry_price, 2),
        }
    else:
        qty, sizing = _risk_sized_qty(
            candidate.entry_price,
            candidate.stop_price,
            risk_usd=risk_usd,
            max_notional_usd=max_notional_usd,
        )
    if qty <= 0:
        return None
    risk_per_share = candidate.entry_price - candidate.stop_price
    rr = float(reward_risk)
    target_price = (
        candidate.entry_price + rr * risk_per_share
        if math.isfinite(rr) and rr > 0
        else math.inf
    )
    exit_tick: ReplayTapeTick | None = None
    exit_reason = "end_of_tape"
    max_fav = 0.0
    max_adv = 0.0
    max_bid = candidate.bid
    trail_armed = False
    trail_stop = candidate.stop_price
    model, exit_route = _resolved_exit_model_for_candidate(candidate, exit_model)
    momentum_trail = model == SIMPLE_TARGET_ARMED_1R_TRAIL
    hold_seconds = float(max_hold_seconds)
    deadline = (
        candidate.ts + timedelta(seconds=hold_seconds)
        if math.isfinite(hold_seconds) and hold_seconds > 0
        else None
    )
    started = False
    for tick in ticks:
        if tick.ts < candidate.ts:
            continue
        if tick.ts == candidate.ts:
            if candidate.sequence is None or tick.sequence is None or tick.sequence <= candidate.sequence:
                continue
        started = True
        prior_max_bid = max_bid
        max_bid = max(max_bid, tick.bid)
        fav_r = (tick.bid - candidate.entry_price) / risk_per_share if risk_per_share > 0 else 0.0
        adv_r = (tick.bid - candidate.entry_price) / risk_per_share if risk_per_share > 0 else 0.0
        max_fav = max(max_fav, fav_r)
        max_adv = min(max_adv, adv_r)
        if tick.bid <= candidate.stop_price:
            exit_tick = tick
            exit_reason = "stop"
            break
        if momentum_trail and risk_per_share > 0 and tick.bid >= target_price:
            trail_armed = True
            trail_stop = max(trail_stop, candidate.entry_price, max_bid - risk_per_share)
        if momentum_trail and trail_armed:
            if tick.bid > prior_max_bid and hold_seconds > 0 and math.isfinite(hold_seconds):
                deadline = tick.ts + timedelta(seconds=hold_seconds)
            trail_stop = max(trail_stop, candidate.entry_price, max_bid - risk_per_share)
            if tick.bid <= trail_stop:
                exit_tick = tick
                exit_reason = "trail_stop"
                break
        if not momentum_trail and tick.bid >= target_price:
            exit_tick = tick
            exit_reason = "target"
            break
        if deadline is not None and tick.ts >= deadline:
            exit_tick = tick
            exit_reason = "momentum_idle" if momentum_trail and trail_armed else "max_hold"
            break
    if not started:
        return None
    if exit_tick is None:
        exit_tick = ticks[-1]
    pnl = (exit_tick.bid - candidate.entry_price) * qty
    pnl_r = (exit_tick.bid - candidate.entry_price) / risk_per_share if risk_per_share > 0 else 0.0
    debug = dict(candidate.trigger_debug)
    debug["sizing"] = sizing
    debug["entry_spread_bps"] = candidate.spread_bps
    debug["exit_model"] = model
    debug["exit_route"] = exit_route
    debug["production_exit_parity"] = False
    if momentum_trail:
        debug["trail_armed"] = bool(trail_armed)
        debug["trail_stop"] = round(trail_stop, 6)
        debug["trail_distance_r"] = 1.0
    return CounterfactualTrade(
        symbol=candidate.symbol,
        entry_ts=candidate.ts,
        exit_ts=exit_tick.ts,
        entry_price=round(candidate.entry_price, 6),
        exit_price=round(exit_tick.bid, 6),
        stop_price=round(candidate.stop_price, 6),
        target_price=round(target_price, 6),
        qty=round(qty, 6),
        pnl_usd=round(pnl, 4),
        pnl_r=round(pnl_r, 4),
        reason=candidate.reason,
        exit_reason=exit_reason,
        gate_family=candidate.gate_family,
        max_favorable_r=round(max_fav, 4),
        max_adverse_r=round(max_adv, 4),
        debug=debug,
    )


def _source_signal_for_symbol(events: Sequence[RossSourceEvent]) -> dict[str, Any] | None:
    for event in events:
        if event.certifiable and event.signal:
            return dict(event.signal)
    for event in events:
        if event.signal:
            return dict(event.signal)
    return None


def _has_source_before(
    events: Sequence[RossSourceEvent],
    ts: datetime,
    *,
    require_certifiable: bool,
) -> bool:
    for event in events:
        if event.ts > ts:
            break
        if require_certifiable and not event.certifiable:
            continue
        return True
    return False


def _source_event_actionable(event: RossSourceEvent) -> tuple[bool, str, dict[str, Any]]:
    text_l = str(event.text or event.signal.get("transcript_text") or "").lower()
    if not text_l:
        source_l = str(event.source or event.signal.get("source") or "").lower()
        if "audit" in source_l or "dry_run" in source_l:
            return False, "audit_source_not_entry_actionable", {
                "certifiable": bool(event.certifiable),
                "source": event.source,
            }
        return bool(event.certifiable), (
            "certifiable_structured_source" if event.certifiable else "empty_transcript_source"
        ), {"certifiable": bool(event.certifiable)}

    recap_phrases = (
        "trade earlier",
        "earlier on",
        "earlier in the morning",
        "didn't really follow through",
        "break that one down",
        "this was the second stock i traded",
        "i had some trades",
        "i thought was pretty good",
        "thought was pretty good",
        "glad to see it moving higher",
        "continued into after hours",
        "the one yesterday",
        "from yesterday",
    )
    hard_negative = [phrase for phrase in _TICK_VWAP_HARD_NEGATIVE_PHRASES if phrase in text_l]
    recap = [phrase for phrase in recap_phrases if phrase in text_l]
    positive_phrases = _TICK_VWAP_POSITIVE_PHRASES + (
        "above the vwap",
        "could go to high of day",
        "could go up",
        "moving higher",
        "leading gapper",
        "leading game",
        "hits the running up scanner",
    )
    positives = [phrase for phrase in positive_phrases if phrase in text_l]
    debug = {
        "certifiable": bool(event.certifiable),
        "positive_phrases": sorted(set(positives)),
        "hard_negative_phrases": sorted(set(hard_negative)),
        "recap_phrases": sorted(set(recap)),
        "source": event.source,
    }
    if recap:
        return False, "recap_source_not_entry_actionable", debug
    if hard_negative:
        return False, "hard_negative_source_not_entry_actionable", debug
    if not positives:
        return False, "source_not_entry_actionable", debug
    return True, "entry_actionable_source", debug


def _has_actionable_source_before(
    events: Sequence[RossSourceEvent],
    ts: datetime,
    *,
    require_certifiable: bool,
    max_age_seconds: float | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    last_reason = "no_ross_source_before_entry"
    last_debug: dict[str, Any] = {}
    last_actionable: tuple[str, dict[str, Any], datetime] | None = None
    last_blocker: tuple[str, dict[str, Any], datetime] | None = None
    blocking_reasons = {
        "hard_negative_source_not_entry_actionable",
        "recap_source_not_entry_actionable",
        "audit_source_not_entry_actionable",
    }
    max_age = (
        float(max_age_seconds)
        if max_age_seconds is not None
        else float(getattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 1800) or 1800)
    )
    for event in events:
        if event.ts > ts:
            break
        if max_age > 0 and (ts - event.ts).total_seconds() > max_age:
            last_reason = "ross_source_watch_expired"
            last_debug = {
                "source_ts": _json_dt(event.ts),
                "source": event.source,
                "max_age_seconds": max_age,
            }
            continue
        if require_certifiable and not event.certifiable:
            last_reason = "ross_source_not_certified"
            last_debug = {"source_ts": _json_dt(event.ts), "source": event.source}
            continue
        actionable, reason, debug = _source_event_actionable(event)
        last_reason = reason
        last_debug = dict(debug)
        last_debug["source_ts"] = _json_dt(event.ts)
        if actionable:
            last_actionable = (reason, dict(last_debug), event.ts)
        elif reason in blocking_reasons:
            last_blocker = (reason, dict(last_debug), event.ts)
    if last_actionable is not None:
        if last_blocker is not None and last_blocker[2] > last_actionable[2]:
            return False, last_blocker[0], last_blocker[1]
        return True, last_actionable[0], last_actionable[1]
    return False, last_reason, last_debug


def _candidate_from_gate(
    *,
    symbol: str,
    ts: datetime,
    tick: ReplayTapeTick,
    gate_family: str,
    ok: bool,
    reason: str,
    debug: Mapping[str, Any],
    sequence: int | None = None,
) -> tuple[ReplayEntryCandidate | None, str | None]:
    if not ok:
        return None, reason
    stop = _structural_stop_from_debug(debug, tick.ask)
    if stop is None:
        return None, "missing_structural_stop"
    risk_per_share = max(0.0, float(tick.ask) - float(stop))
    debug_out = dict(debug)
    if risk_per_share > 0 and tick.ask > tick.bid > 0:
        spread_abs = float(tick.ask) - float(tick.bid)
        spread_cost_of_r = spread_abs / risk_per_share
        debug_out["spread_cost_of_r"] = round(spread_cost_of_r, 6)
        if gate_family in {"tick_vwap_reclaim_burst", "vwap_reclaim"}:
            max_spread_frac = float(
                getattr(settings, "chili_momentum_spread_cost_reclaim_max_fraction_of_r", 0.35)
                or 0.35
            )
            debug_out["max_spread_cost_of_r"] = round(max_spread_frac, 6)
            if spread_cost_of_r > max_spread_frac:
                return None, "spread_cost_too_high_vs_r"
    return (
        ReplayEntryCandidate(
            symbol=symbol,
            ts=ts,
            reason=reason,
            entry_price=tick.ask,
            stop_price=stop,
            trigger_debug=debug_out,
            gate_family=gate_family,
            bid=tick.bid,
            ask=tick.ask,
            spread_bps=tick.spread_bps,
            sequence=sequence if sequence is not None else tick.sequence,
        ),
        None,
    )


def _call_bar_gate(
    family: str,
    *,
    frame: pd.DataFrame,
    entry_interval: str,
    live_price: float | None,
    symbol: str,
    now: datetime,
) -> tuple[bool, str, dict[str, Any]]:
    """Dispatch one named bar-close gate. Shared by the bar-close pass AND the
    tick-armed re-fire below, so both call sites stay byte-identical."""
    if family == "momentum_pullback":
        # D1 (cf-parity): ``first_pullback_interval=`` was RETIRED by the F2 fix —
        # the trigger reads ``chili_momentum_first_pullback_interval`` from settings
        # itself (that IS live parity; live_runner.py:6450 passes no such kwarg).
        # Passing it raised TypeError on every bar candidate.
        return momentum_pullback_trigger(
            frame,
            entry_interval=entry_interval,
            live_price=live_price,
            symbol=symbol,
            now=now,
            db=None,
            l2_as_of=now,
        )
    if family == "vwap_reclaim":
        return vwap_reclaim_confirmation(
            frame,
            entry_interval=entry_interval,
            live_price=live_price,
            symbol=symbol,
            now=now,
        )
    if family == "ross_breakout_starter":
        return ross_breakout_starter_confirmation(
            frame,
            entry_interval=entry_interval,
            live_price=live_price,
            symbol=symbol,
            now=now,
            db=None,
            l2_as_of=now,
        )
    raise ValueError(f"unknown bar gate family: {family}")


_BAR_GATE_FAMILIES = ("momentum_pullback", "vwap_reclaim", "ross_breakout_starter")


def _iter_bar_candidates(
    *,
    symbol: str,
    ticks: Sequence[ReplayTapeTick],
    bars: pd.DataFrame,
    bar_seconds: int,
    bar_eval_stride: int = 1,
    eval_since: datetime | None = None,
    require_release_clock: bool = False,
) -> tuple[list[ReplayEntryCandidate], dict[str, int]]:
    candidates: list[ReplayEntryCandidate] = []
    reasons: dict[str, int] = {}
    # Entry gates compare interval identities, not just elapsed duration.  Keep
    # replay on the same canonical spelling as the production OHLCV path so a
    # 60-second frame reaches the production ``1m`` first-pullback branch.
    entry_interval = canonical_interval_for_seconds(int(bar_seconds))
    stride = max(1, int(bar_eval_stride or 1))
    # Production persists this marker in the per-symbol live-entry ledger.  The
    # former lightweight replay called only stateless gate functions, allowing a
    # later MACD/VWAP pivot to re-arm a move that production had already benched.
    benched_at_hod: float | None = None
    release_times = _bar_release_times(
        bars,
        require_explicit=require_release_clock,
    )
    if len(release_times) != len(bars):
        return [], {"bar_release_clock_missing": 1}
    event_mask_raw = bars.attrs.get(_REPLAY_REAL_EVENT_BUCKETS_ATTR)
    if isinstance(event_mask_raw, (list, tuple)) and len(event_mask_raw) == len(bars):
        event_mask = tuple(bool(value) for value in event_mask_raw)
    elif require_release_clock:
        return [], {"bar_decision_event_mask_missing": 1}
    else:
        # Hand-built/legacy diagnostic frames have no provenance metadata.
        event_mask = tuple(True for _ in range(len(bars)))

    # Bars withheld by the cumulative release clock can become visible at the
    # same instant.  Production would query the complete newly available
    # prefix, not transient intermediate prefixes that exist only because this
    # DataFrame is iterated row by row.  Evaluate the final row of each equal-
    # release group.  With distinct release times this is byte-for-byte the
    # former 0..N bar sequence.
    release_groups: list[tuple[int, int]] = []
    group_start = 0
    for idx, release in enumerate(release_times):
        if idx + 1 == len(release_times) or release_times[idx + 1] != release:
            release_groups.append((group_start, idx))
            group_start = idx + 1
    decision_group_ends = [
        end
        for start, end in release_groups
        if any(event_mask[start : end + 1])
    ]
    eligible_group_ends = [end for end in decision_group_ends if end >= 9]
    evaluation_indices = eligible_group_ends[::stride]
    next_release_by_index: dict[int, datetime | None] = {}
    for group_pos, idx in enumerate(decision_group_ends):
        next_release_by_index[idx] = (
            release_times[decision_group_ends[group_pos + 1]]
            if group_pos + 1 < len(decision_group_ends)
            else None
        )

    for idx in evaluation_indices:
        # Keep the complete causal session prefix.  Production's lifecycle
        # guards anchor on the session HOD/open/VWAP; a rolling 160-bar slice
        # turns into only 40 minutes at the historical 15-second default and
        # can make a post-spike/backside curl look like a brand-new impulse.
        # The upper bound is still strictly ``idx + 1``: no future bar enters.
        frame = bars.iloc[: idx + 1]
        market_close_raw = frame.index[-1]
        market_close_ts = _parse_dt(
            market_close_raw.to_pydatetime()
            if hasattr(market_close_raw, "to_pydatetime")
            else market_close_raw
        )
        ts = release_times[idx]
        if eval_since is not None and ts < eval_since:
            continue
        tick = _latest_tick_at_or_before(ticks, ts)
        if tick is None:
            continue
        try:
            from .entry_gates import evaluate_sticky_backside_bench

            phase_benched, phase_reason, phase_hod, _phase_debug = (
                evaluate_sticky_backside_bench(
                    frame,
                    benched_at_hod=benched_at_hod,
                    live_price=tick.ask,
                )
            )
            benched_at_hod = float(phase_hod) if phase_benched and phase_hod is not None else None
        except Exception:
            # Match the production helper's fail-open boundary.  The replay is
            # diagnostic when it cannot evaluate the phase latch; individual
            # entry gates still run and surface their own reasons.
            phase_benched = False
            phase_reason = "bench_fail_open"
        fired = False
        for family in _BAR_GATE_FAMILIES:
            ok, reason, debug = _call_bar_gate(
                family,
                frame=frame,
                entry_interval=entry_interval,
                live_price=tick.mid,
                symbol=symbol,
                now=ts,
            )
            debug = debug if isinstance(debug, Mapping) else {}
            fire_tick = tick
            # D2 (cf-parity): live's tick-speed dispatch does NOT wait for the next bar
            # close once a gate is TICK-ARMED (``TICK_ARMED_WAIT_REASONS`` — e.g.
            # ``waiting_for_break`` with ``pullback_high`` set) — live_runner.py:6450
            # onward re-evaluates the SAME completed-bar structure against every
            # subsequent live ask and fires the instant the ask trades through the
            # level (see live_runner.py's tick-break comment + replay_v2.py's mirror
            # of the same mechanic). The old bar-close-only loop here only re-checked
            # once per NEW bar, so a tick fire between bar closes (SVRE 06-30 12:45:25Z
            # pbh 6.89 -> tick fire 6.88-7.00, entirely inside one 15s bar) was silently
            # missed -> zero candidates. Mirror live: walk every quote/trade tick after
            # this bar's close (bounded by the NEXT bar's ts so we don't re-claim a
            # later bar's own structure) and re-fire the SAME gate the instant the ask
            # crosses ``pullback_high``.
            if (
                not ok
                and str(reason) in TICK_ARMED_WAIT_REASONS
                and _float_or_none(debug.get("pullback_high")) is not None
            ):
                level = float(debug["pullback_high"])
                next_bar_ts = next_release_by_index.get(idx)
                for cand_tick in ticks:
                    if cand_tick.ts <= tick.ts:
                        continue
                    if next_bar_ts is not None and cand_tick.ts >= next_bar_ts:
                        break
                    if cand_tick.ask <= level:
                        continue
                    # The level-crossing tick is a NECESSARY but not always SUFFICIENT
                    # condition to fire (the trigger also checks volume/candle/VWAP
                    # state that can lag the raw price cross by a tick or two) — keep
                    # walking subsequent ticks until the gate actually fires, exactly
                    # like live's tick-speed dispatch re-evaluating on every quote.
                    # Only stop scanning once it fires; do NOT bail on the first
                    # (possibly premature) crossing tick that the gate still rejects.
                    ok2, reason2, debug2 = _call_bar_gate(
                        family,
                        frame=frame,
                        entry_interval=entry_interval,
                        live_price=cand_tick.ask,
                        symbol=symbol,
                        now=cand_tick.ts,
                    )
                    if ok2:
                        ok, reason, debug = ok2, reason2, (
                            debug2 if isinstance(debug2, Mapping) else {}
                        )
                        fire_tick = cand_tick
                        break
            candidate, skipped = _candidate_from_gate(
                symbol=symbol,
                # A completed bar cannot cause an order before its release
                # time merely because the latest executable quote is older.
                # Tick-armed fires after release retain their actual tick time.
                ts=max(ts, fire_tick.ts),
                tick=fire_tick,
                gate_family=family,
                ok=bool(ok),
                reason=str(reason),
                debug=debug,
            )
            if candidate is not None:
                # A tick-armed break may fire after the bar-close phase read.
                # Re-evaluate the same sticky production latch at the actual
                # executable ask before admitting it.
                try:
                    phase_benched, phase_reason, phase_hod, phase_debug = (
                        evaluate_sticky_backside_bench(
                            frame,
                            benched_at_hod=benched_at_hod,
                            live_price=fire_tick.ask,
                        )
                    )
                    benched_at_hod = (
                        float(phase_hod)
                        if phase_benched and phase_hod is not None
                        else None
                    )
                except Exception:
                    phase_benched = False
                    phase_reason = "bench_fail_open"
                    phase_debug = {}
                if phase_benched:
                    blocked_reason = f"backside_bench_veto:{phase_reason}"
                    reasons[blocked_reason] = reasons.get(blocked_reason, 0) + 1
                    fired = True
                    break
                candidate.trigger_debug["phase_bench_reason"] = phase_reason
                candidate.trigger_debug["bar_market_close_at"] = _json_dt(
                    market_close_ts
                )
                candidate.trigger_debug["bar_released_at"] = _json_dt(ts)
                if phase_debug:
                    candidate.trigger_debug["phase_bench"] = dict(phase_debug)
                candidates.append(candidate)
                fired = True
                break
            if skipped:
                reasons[skipped] = reasons.get(skipped, 0) + 1
        if fired:
            continue
    if not candidates and not reasons:
        # D2 (cf-parity): "gate_reasons must NEVER be silently empty" — if the bar loop
        # produced neither a candidate nor a single skip/wait reason (e.g. zero bars
        # ever reached line 9, or every gate call raised before returning a mapped
        # reason), that is itself a diagnostic fact the caller needs, not a silent
        # empty-empty result that reads as "nothing happened here."
        reasons["bar_candidates_no_reason_recorded"] = 1
    return candidates, reasons


def _iter_tick_scalp_candidates(
    *,
    symbol: str,
    ticks: Sequence[ReplayTapeTick],
    signal: Mapping[str, Any] | None,
    eval_since: datetime | None = None,
) -> tuple[list[ReplayEntryCandidate], dict[str, int]]:
    if not signal:
        return [], {"tick_scalp_no_source_signal": 1}
    candidates: list[ReplayEntryCandidate] = []
    reasons: dict[str, int] = {}
    state: dict[str, Any] | None = None
    max_hold = float(getattr(settings, "chili_momentum_tick_scalp_max_hold_seconds", 12.0) or 12.0)
    for tick in ticks:
        decision = evaluate_tick_first_pullback(
            symbol=symbol,
            signal=dict(signal),
            state=state,
            bid=tick.bid,
            ask=tick.ask,
            mid=tick.mid,
            now_utc=tick.ts,
            min_pullback_bps=float(
                getattr(settings, "chili_momentum_tick_first_pullback_min_pullback_bps", 35.0) or 35.0
            ),
            max_pullback_bps=float(
                getattr(settings, "chili_momentum_tick_first_pullback_max_pullback_bps", 1800.0) or 1800.0
            ),
            min_reclaim_bps=float(
                getattr(settings, "chili_momentum_tick_first_pullback_min_reclaim_bps", 8.0) or 8.0
            ),
            stop_buffer_bps=float(
                getattr(settings, "chili_momentum_tick_first_pullback_stop_buffer_bps", 12.0) or 12.0
            ),
            max_hold_seconds=max_hold,
        )
        state = decision.state
        debug = dict(decision.debug)
        debug["max_hold_seconds"] = max_hold
        if eval_since is not None and tick.ts < eval_since:
            continue
        candidate, skipped = _candidate_from_gate(
            symbol=symbol,
            ts=tick.ts,
            tick=tick,
            gate_family="tick_first_pullback",
            ok=decision.fire,
            reason=decision.reason,
            debug=debug,
        )
        if candidate is not None:
            candidates.append(candidate)
            break
        if skipped:
            reasons[skipped] = reasons.get(skipped, 0) + 1
    return candidates, reasons


_TICK_VWAP_HARD_NEGATIVE_PHRASES = (
    "pulled back way too much",
    "below vwap pulling back too much",
    "chopping around",
    "offering",
    "throw in the towel",
    "was red",
    "too much dispersed attention",
    "dispersed attention",
    "hard for a stock to become",
    "too cheap",
    "too thickly traded",
    "i don't know",
    "i'm not sure",
    "not sure",
    "don't typically do well",
    "not really in a position",
    "not sure about",
)

_TICK_VWAP_POSITIVE_PHRASES = (
    "breakthrough vwap",
    "holding over the vwap",
    "over the vwap",
    "running up scanner",
    "high of day",
    "hod",
    "watch",
    "looking for",
    "got my",
)


def _active_tick_vwap_source(
    events: Sequence[RossSourceEvent],
    ts: datetime,
    *,
    max_watch_seconds: float,
) -> tuple[bool, str, dict[str, Any]]:
    active = [
        event
        for event in events
        if event.ts <= ts and (ts - event.ts).total_seconds() <= max(0.0, float(max_watch_seconds))
    ]
    if not active:
        return False, "tick_vwap_burst_no_active_source", {"max_watch_seconds": max_watch_seconds}

    latest_positive: RossSourceEvent | None = None
    latest_hard_negative: RossSourceEvent | None = None
    matched_positive: list[str] = []
    matched_negative: list[str] = []
    for event in active:
        text_l = str(event.text or event.signal.get("transcript_text") or "").lower()
        if not text_l:
            continue
        positives = [phrase for phrase in _TICK_VWAP_POSITIVE_PHRASES if phrase in text_l]
        negatives = [phrase for phrase in _TICK_VWAP_HARD_NEGATIVE_PHRASES if phrase in text_l]
        if positives:
            latest_positive = event
            matched_positive.extend(positives)
        if negatives:
            latest_hard_negative = event
            matched_negative.extend(negatives)

    debug = {
        "max_watch_seconds": float(max_watch_seconds),
        "active_source_count": len(active),
        "latest_source_ts": _json_dt(active[-1].ts),
        "latest_positive_source_ts": _json_dt(latest_positive.ts) if latest_positive else None,
        "latest_hard_negative_source_ts": (
            _json_dt(latest_hard_negative.ts) if latest_hard_negative else None
        ),
        "positive_phrases": sorted(set(matched_positive)),
        "hard_negative_phrases": sorted(set(matched_negative)),
    }
    if latest_positive is None:
        return False, "tick_vwap_burst_source_not_actionable", debug
    if latest_hard_negative is not None and latest_hard_negative.ts > latest_positive.ts:
        return False, "tick_vwap_burst_source_hard_negative_after_positive", debug
    return True, "tick_vwap_burst_source_ok", debug


def _tick_vwap_source_windows(
    events: Sequence[RossSourceEvent],
    *,
    max_watch_seconds: float,
) -> list[tuple[datetime, datetime, dict[str, Any]]]:
    windows: list[tuple[datetime, datetime, dict[str, Any]]] = []
    for idx, event in enumerate(events):
        actionable, _, _ = _source_event_actionable(event)
        if not actionable:
            continue
        text_l = str(event.text or event.signal.get("transcript_text") or "").lower()
        if not text_l:
            continue
        positives = [phrase for phrase in _TICK_VWAP_POSITIVE_PHRASES if phrase in text_l]
        if not positives:
            continue
        start = event.ts
        end = start + timedelta(seconds=max(0.0, float(max_watch_seconds)))
        matched_negative: list[str] = []
        for later in events[idx + 1 :]:
            if later.ts <= start:
                continue
            if later.ts >= end:
                break
            later_text = str(later.text or later.signal.get("transcript_text") or "").lower()
            negatives = [phrase for phrase in _TICK_VWAP_HARD_NEGATIVE_PHRASES if phrase in later_text]
            if negatives:
                matched_negative.extend(negatives)
                break
        if end <= start:
            continue
        windows.append(
            (
                start,
                end,
                {
                    "max_watch_seconds": float(max_watch_seconds),
                    "source_ts": _json_dt(event.ts),
                    "source": event.source,
                    "positive_phrases": sorted(set(positives)),
                    "hard_negative_phrases": sorted(set(matched_negative)),
                    "text": event.text[:220],
                },
            )
        )
    return windows


def _market_certified_window(
    df: pd.DataFrame,
    *,
    max_watch_seconds: float,
) -> tuple[datetime, datetime, dict[str, Any]] | None:
    if df is None or df.empty:
        return None
    first_ts = _parse_dt(df.index[0].to_pydatetime() if hasattr(df.index[0], "to_pydatetime") else df.index[0])
    last_ts = _parse_dt(df.index[-1].to_pydatetime() if hasattr(df.index[-1], "to_pydatetime") else df.index[-1])
    if first_ts is None or last_ts is None or last_ts <= first_ts:
        return None
    return (
        first_ts,
        last_ts,
        {
            "source_mode": "market_certified",
            "max_watch_seconds": float(max_watch_seconds),
            "text": "",
        },
    )


def _iter_tick_vwap_reclaim_burst_candidates(
    *,
    symbol: str,
    quote_ticks: Sequence[ReplayTapeTick],
    trade_ticks: Sequence[ReplayTapeTick],
    source_events: Sequence[RossSourceEvent],
    eval_since: datetime | None = None,
) -> tuple[list[ReplayEntryCandidate], dict[str, int]]:
    if not trade_ticks:
        return [], {"tick_vwap_burst_no_trade_prints": 1}
    if not quote_ticks:
        return [], {"tick_vwap_burst_no_nbbo_quotes": 1}

    max_hold = float(getattr(settings, "chili_momentum_tick_scalp_max_hold_seconds", 12.0) or 12.0)
    min_pullback_bps = float(
        getattr(settings, "chili_momentum_tick_first_pullback_min_pullback_bps", 35.0) or 35.0
    )
    max_pullback_bps = float(
        getattr(settings, "chili_momentum_tick_first_pullback_max_pullback_bps", 1800.0) or 1800.0
    )
    min_reclaim_bps = float(
        getattr(settings, "chili_momentum_tick_first_pullback_min_reclaim_bps", 8.0) or 8.0
    )
    stop_buffer_bps = float(
        getattr(settings, "chili_momentum_tick_first_pullback_stop_buffer_bps", 12.0) or 12.0
    )
    volume_mult = float(getattr(settings, "chili_momentum_vwap_reclaim_vol_mult", 1.5) or 1.5)
    max_watch = float(getattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 1800) or 1800)
    burst_window_s = max(1.0, max_hold * 2.0)
    fast_min_pullback_bps = max(
        max(0.0, min_pullback_bps),
        max(0.0, min_pullback_bps) + max(max(0.0, min_pullback_bps), max(0.0, min_reclaim_bps)),
    )
    fast_max_pullback_bps = (
        math.sqrt(max(0.0, min_pullback_bps) * max(0.0, max_pullback_bps))
        if max_pullback_bps > min_pullback_bps > 0
        else max(0.0, max_pullback_bps)
    )
    source_windows = _tick_vwap_source_windows(source_events, max_watch_seconds=max_watch)

    df = _trade_tape_frame(trade_ticks)
    if df is None or df.empty:
        return [], {"tick_vwap_burst_bad_trade_frame": 1}
    if not source_windows:
        course_price_floor = float(ROSS_TICK_SCALP_COURSE_PRICE_FLOOR)
        course_price_ceiling = float(ROSS_TICK_SCALP_MAX_PRICE)
        min_trade_price = _float_or_none(df["price"].min())
        max_trade_price = _float_or_none(df["price"].max())
        if max_trade_price is not None and max_trade_price < course_price_floor:
            return [], {"tick_vwap_burst_market_price_below_course_range": int(len(df))}
        if course_price_ceiling > 0 and min_trade_price is not None and min_trade_price > course_price_ceiling:
            return [], {"tick_vwap_burst_market_price_above_scalp_range": int(len(df))}
    window = f"{burst_window_s}s"
    df["prior_high"] = df["price"].rolling(window, closed="left").max()
    df["prior_low"] = df["price"].rolling(window, closed="left").min()
    df["rolling_volume"] = df["size"].rolling(window, closed="both").sum()
    df["cum_dollar"] = (df["price"] * df["size"]).cumsum()
    df["cum_size"] = df["size"].cumsum()
    df["session_vwap"] = df["cum_dollar"] / df["cum_size"].replace(0.0, pd.NA)
    market_window = _market_certified_window(df, max_watch_seconds=max_watch)
    if market_window is not None:
        source_windows = [*source_windows, market_window]

    candidates: list[ReplayEntryCandidate] = []
    reasons: dict[str, int] = {}
    next_candidate_after: datetime | None = None
    for window_start, window_end, source_debug in source_windows:
        start_pd = pd.Timestamp(window_start)
        end_pd = pd.Timestamp(window_end)
        context_start_pd = start_pd
        if eval_since is not None and pd.Timestamp(eval_since) > start_pd:
            context_start_pd = max(
                start_pd,
                pd.Timestamp(eval_since - timedelta(seconds=burst_window_s)),
            )
        frame = df.loc[(df.index >= context_start_pd) & (df.index <= end_pd)]
        if frame.empty:
            reasons["tick_vwap_burst_no_trade_prints_in_source_window"] = (
                reasons.get("tick_vwap_burst_no_trade_prints_in_source_window", 0) + 1
            )
            continue
        source_mode = str(source_debug.get("source_mode") or "ross_source")
        market_mode = source_mode == "market_certified"
        prior_prices = df.loc[df.index < context_start_pd, "price"].dropna()
        session_high = _float_or_none(prior_prices.max() if not prior_prices.empty else None)
        swing_high: float | None = None
        pullback_low: float | None = None
        for ts_raw, row in frame.iterrows():
            ts = _parse_dt(ts_raw.to_pydatetime() if hasattr(ts_raw, "to_pydatetime") else ts_raw)
            if ts is None:
                continue

            price = _float_or_none(row.get("price"))
            trade_sequence = int(row.get("sequence")) if row.get("sequence") is not None else None
            rolling_volume = _float_or_none(row.get("rolling_volume"))
            vwap = _float_or_none(row.get("session_vwap"))
            if price is None or rolling_volume is None:
                reasons["tick_vwap_burst_waiting_for_prior_window"] = (
                    reasons.get("tick_vwap_burst_waiting_for_prior_window", 0) + 1
                )
                continue
            if price <= 0 or vwap is None or vwap <= 0:
                reasons["tick_vwap_burst_bad_price_context"] = (
                    reasons.get("tick_vwap_burst_bad_price_context", 0) + 1
                )
                continue
            session_high_before = session_high
            if session_high is None or price > session_high:
                session_high = price

            if swing_high is None or swing_high <= 0:
                swing_high = price
                reasons["tick_vwap_burst_waiting_for_ordered_pullback"] = (
                    reasons.get("tick_vwap_burst_waiting_for_ordered_pullback", 0) + 1
                )
                continue

            if price <= swing_high:
                depth_now_bps = ((swing_high - price) / swing_high) * 10_000.0
                if depth_now_bps >= max(0.0, min_pullback_bps):
                    pullback_low = price if pullback_low is None else min(pullback_low, price)
                else:
                    reasons["tick_vwap_burst_pullback_too_shallow"] = (
                        reasons.get("tick_vwap_burst_pullback_too_shallow", 0) + 1
                    )
                continue

            break_level = swing_high
            recent_low = pullback_low
            swing_high = price
            pullback_low = None
            if recent_low is None or recent_low <= 0:
                reasons["tick_vwap_burst_waiting_for_ordered_pullback"] = (
                    reasons.get("tick_vwap_burst_waiting_for_ordered_pullback", 0) + 1
                )
                continue
            if eval_since is not None and ts < eval_since:
                continue
            if next_candidate_after is not None and ts <= next_candidate_after:
                continue

            pullback_depth_bps = ((break_level - recent_low) / break_level) * 10_000.0
            if pullback_depth_bps < fast_min_pullback_bps:
                reasons["tick_vwap_burst_pullback_too_shallow"] = (
                    reasons.get("tick_vwap_burst_pullback_too_shallow", 0) + 1
                )
                continue
            if fast_max_pullback_bps > 0 and pullback_depth_bps > fast_max_pullback_bps:
                reasons["tick_vwap_burst_pullback_too_deep"] = (
                    reasons.get("tick_vwap_burst_pullback_too_deep", 0) + 1
                )
                continue
            reclaim_level = break_level * (1.0 + max(0.0, min_reclaim_bps) / 10_000.0)
            if price < reclaim_level:
                reasons["tick_vwap_burst_waiting_for_level"] = (
                    reasons.get("tick_vwap_burst_waiting_for_level", 0) + 1
                )
                continue
            if price < vwap:
                reasons["tick_vwap_burst_below_trade_vwap"] = (
                    reasons.get("tick_vwap_burst_below_trade_vwap", 0) + 1
                )
                continue
            if session_high_before is not None and price < session_high_before * (
                1.0 + max(0.0, min_reclaim_bps) / 10_000.0
            ):
                reasons["tick_vwap_burst_not_frontside_new_high"] = (
                    reasons.get("tick_vwap_burst_not_frontside_new_high", 0) + 1
                )
                continue

            baseline_start = max(df.index[0], pd.Timestamp(ts - timedelta(seconds=max_watch)))
            prior_volumes = df.loc[(df.index >= baseline_start) & (df.index < pd.Timestamp(ts)), "rolling_volume"].dropna()
            baseline_volume = _float_or_none(prior_volumes.median() if not prior_volumes.empty else None)
            if baseline_volume is None or baseline_volume <= 0:
                reasons["tick_vwap_burst_waiting_for_volume_baseline"] = (
                    reasons.get("tick_vwap_burst_waiting_for_volume_baseline", 0) + 1
                )
                continue
            volume_ratio = rolling_volume / baseline_volume
            market_volume_ratio_floor = max(0.0, volume_mult) * max(0.0, volume_mult)
            watch_windows = max(1.0, max_watch / burst_window_s)
            market_tail_quantile = max(0.5, 1.0 - (1.0 / (watch_windows + 1.0)))
            market_tail_volume = _float_or_none(
                prior_volumes.quantile(market_tail_quantile) if len(prior_volumes) > 1 else None
            )
            market_volume_floor = max(
                baseline_volume * max(0.0, volume_mult),
                market_tail_volume or 0.0,
            )
            market_certified = rolling_volume >= market_volume_floor if market_mode else False
            if volume_ratio < max(0.0, volume_mult):
                reasons["tick_vwap_burst_volume_not_confirmed"] = (
                    reasons.get("tick_vwap_burst_volume_not_confirmed", 0) + 1
                )
                continue
            if market_mode and volume_ratio < market_volume_ratio_floor:
                reasons["tick_vwap_burst_market_volume_ratio_weak"] = (
                    reasons.get("tick_vwap_burst_market_volume_ratio_weak", 0) + 1
                )
                continue
            # Market-only trades need the clean Ross scalp price zone because
            # there is no human/source context to justify edge-case liquidity.
            course_price_floor = float(ROSS_TICK_SCALP_COURSE_PRICE_FLOOR)
            if market_mode and price < course_price_floor:
                reasons["tick_vwap_burst_market_price_below_course_range"] = (
                    reasons.get("tick_vwap_burst_market_price_below_course_range", 0) + 1
                )
                continue
            course_price_ceiling = float(ROSS_TICK_SCALP_MAX_PRICE)
            if market_mode and course_price_ceiling > 0 and price > course_price_ceiling:
                reasons["tick_vwap_burst_market_price_above_scalp_range"] = (
                    reasons.get("tick_vwap_burst_market_price_above_scalp_range", 0) + 1
                )
                continue
            source_ok, source_state_reason, source_state_debug = _has_actionable_source_before(
                source_events,
                ts,
                require_certifiable=False,
                max_age_seconds=max_watch,
            )
            source_blocker_recertified = False
            if (
                not source_ok
                and source_state_reason
                in {"hard_negative_source_not_entry_actionable", "recap_source_not_entry_actionable"}
            ):
                blocker_ts = _parse_dt(source_state_debug.get("source_ts"))
                blocker_age_s = (
                    (ts - blocker_ts).total_seconds()
                    if blocker_ts is not None
                    else None
                )
                source_blocker_recertified = bool(
                    (market_certified or not market_mode)
                    and blocker_age_s is not None
                    and blocker_age_s >= max(0.0, max_hold)
                    and volume_ratio >= max(0.0, volume_mult)
                )
                if not source_blocker_recertified:
                    reasons[source_state_reason] = reasons.get(source_state_reason, 0) + 1
                    continue
            if market_mode and not market_certified:
                reasons["tick_vwap_burst_market_not_certified"] = (
                    reasons.get("tick_vwap_burst_market_not_certified", 0) + 1
                )
                continue

            quote_tick = _latest_tick_at_or_before(quote_ticks, ts)
            if quote_tick is None:
                reasons["tick_vwap_burst_no_quote_at_trade"] = (
                    reasons.get("tick_vwap_burst_no_quote_at_trade", 0) + 1
                )
                continue
            structural_stop = recent_low * (1.0 - max(0.0, stop_buffer_bps) / 10_000.0)
            debug = {
                "pattern": "tick_vwap_reclaim_burst",
                "breakout_level_price": round(break_level, 6),
                "pullback_high": round(break_level, 6),
                "pullback_low": round(recent_low, 6),
                "structural_stop_price": round(structural_stop, 6),
                "reclaim_level": round(reclaim_level, 6),
                "trade_price": round(price, 6),
                "trade_vwap": round(vwap, 6),
                "pullback_depth_bps": round(pullback_depth_bps, 4),
                "min_pullback_bps": min_pullback_bps,
                "fast_min_pullback_bps": round(fast_min_pullback_bps, 4),
                "max_pullback_bps": max_pullback_bps,
                "fast_max_pullback_bps": round(fast_max_pullback_bps, 4),
                "min_reclaim_bps": min_reclaim_bps,
                "stop_buffer_bps": stop_buffer_bps,
                "rolling_trade_volume": round(rolling_volume, 4),
                "baseline_trade_volume_median": round(baseline_volume, 4),
                "volume_ratio": round(volume_ratio, 4),
                "required_volume_ratio": volume_mult,
                "market_volume_ratio_floor": round(market_volume_ratio_floor, 4),
                "market_course_price_floor": course_price_floor,
                "market_course_price_ceiling": course_price_ceiling,
                "market_tail_quantile": round(market_tail_quantile, 6),
                "market_tail_volume": None if market_tail_volume is None else round(market_tail_volume, 4),
                "market_volume_floor": round(market_volume_floor, 4),
                "market_certified": bool(market_certified),
                "burst_window_seconds": burst_window_s,
                "max_hold_seconds": max_hold,
                "source": source_debug,
                "source_mode": source_mode,
                "source_state": source_state_reason,
                "source_state_debug": source_state_debug,
                "source_blocker_recertified": bool(source_blocker_recertified),
                "session_high_before": None if session_high_before is None else round(session_high_before, 6),
                "trade_sequence": trade_sequence,
            }
            candidate, skipped = _candidate_from_gate(
                symbol=symbol,
                ts=ts,
                tick=quote_tick,
                gate_family="tick_vwap_reclaim_burst",
                ok=True,
                reason="tick_vwap_reclaim_burst",
                debug=debug,
                sequence=trade_sequence,
            )
            if candidate is not None:
                candidates.append(candidate)
                next_candidate_after = ts + timedelta(seconds=max_hold)
                continue
            if skipped:
                reasons[skipped] = reasons.get(skipped, 0) + 1
    return candidates, reasons


def _dedupe_candidates(candidates: Iterable[ReplayEntryCandidate]) -> list[ReplayEntryCandidate]:
    ordered = sorted(candidates, key=lambda c: (c.ts, c.symbol, c.reason))
    out: list[ReplayEntryCandidate] = []
    seen_bucket: set[tuple[str, int]] = set()
    for c in ordered:
        bucket = int(c.ts.timestamp() // 5)
        key = (c.symbol, bucket)
        if key in seen_bucket:
            continue
        seen_bucket.add(key)
        out.append(c)
    return out


def _confidence(
    *,
    ticks: Sequence[ReplayTapeTick],
    trade_ticks: Sequence[ReplayTapeTick],
    bars: pd.DataFrame | None,
    source_events: Sequence[RossSourceEvent],
    max_ticks: int | None,
    require_causal_provenance: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not ticks:
        if require_causal_provenance:
            return "no_causal_tape", ["no_causally_certified_nbbo_tape"]
        return "no_tape", ["no_nbbo_tape"]
    sources = {str(t.source or "").lower() for t in ticks}
    if any("iqfeed" in s for s in sources):
        base = "tick_quote_complete"
    else:
        base = "quote_replay_available"
        reasons.append("no_iqfeed_source_marker")
    if bars is None or getattr(bars, "empty", True):
        reasons.append("microbars_unavailable")
    elif len(bars) < 10:
        reasons.append("thin_microbars")
    if not trade_ticks:
        reasons.append("no_trade_print_tape")
    causal_quote_rows = sum(1 for tick in ticks if tick.provenance_certified)
    causal_trade_rows = sum(1 for tick in trade_ticks if tick.provenance_certified)
    if not require_causal_provenance:
        reasons.append("causal_provenance_not_enforced")
    if causal_quote_rows != len(ticks):
        reasons.append(
            f"uncertified_quote_rows:{len(ticks) - causal_quote_rows}/{len(ticks)}"
        )
    if trade_ticks and causal_trade_rows != len(trade_ticks):
        reasons.append(
            f"uncertified_trade_rows:{len(trade_ticks) - causal_trade_rows}/{len(trade_ticks)}"
        )
    if require_causal_provenance and ticks and causal_quote_rows == len(ticks):
        reasons.append("causal_quote_clock_available_at_enforced")
    if require_causal_provenance and trade_ticks and causal_trade_rows == len(trade_ticks):
        reasons.append("causal_trade_clock_available_at_enforced")
    if max_ticks is not None and max_ticks > 0:
        reasons.append(f"sampled_tape_max_ticks_{int(max_ticks)}")
    if not source_events:
        reasons.append("no_ross_source_event")
    elif not any(e.certifiable for e in source_events):
        reasons.append("ross_source_not_certified")
    if reasons:
        return f"{base}_limited", reasons
    return base, []


def run_counterfactual_symbol_replay(
    db: Session,
    symbol: str,
    *,
    since: datetime,
    until: datetime,
    eval_since: datetime | None = None,
    source_events: Sequence[RossSourceEvent] = (),
    bar_seconds: int = 60,
    max_ticks: int | None = None,
    bar_eval_stride: int = 1,
    max_trades: int = 3,
    require_source_before_entry: bool = True,
    require_certifiable_source: bool = False,
    risk_usd: float | None = None,
    max_notional_usd: float | None = None,
    reward_risk: float | None = None,
    max_hold_seconds: float | None = None,
    fixed_qty: float | None = None,
    cash_usd: float | None = None,
    cash_fraction: float | None = None,
    exit_model: str = "adaptive",
    live_admission_mode: bool = True,
    account_equity_usd: float | None = None,
    require_causal_provenance: bool = False,
) -> SymbolReplayResult:
    """Replay one symbol's tape against the current entry-gate code.

    ``live_admission_mode`` (D3, cf-parity, DEFAULT ON) restricts candidate
    generation and entry admission to live's ACTUAL entry-class set and
    catalyst/source discipline instead of the harness's diagnostic-only
    superset:

      * ``tick_first_pullback`` (``_iter_tick_scalp_candidates``) and
        ``tick_vwap_reclaim_burst`` (``_iter_tick_vwap_reclaim_burst_candidates``)
        are NOT wired into ``live_runner.py`` at all — ``evaluate_tick_first_pullback``
        and the tick-VWAP-burst walker are called ONLY from this replay module.
        Live's actual ladder is exactly ``_BAR_GATE_FAMILIES`` (momentum_pullback /
        vwap_reclaim / ross_breakout_starter), now tick-armed (D2). Firing an
        entry off a family live never evaluates is not a counterfactual of live —
        it is a different strategy. Diagnostic/opportunity-labeling callers that
        want the wider net can still pass ``live_admission_mode=False``.
      * the ``market_certified`` synthetic source window (a pure-market-volume
        substitute for an actual Ross/catalyst source, see
        ``_market_certified_window``) is disabled as an admission bypass — live
        never enters on "the tape looks busy" alone; it requires a real source/
        catalyst or a certified viability/arm state. CELZ 2026-06-30: live took
        ONE ORB entry (+$40, entry 3.67); with the synthetic bypass active the
        harness fabricated 3 additional chop entries (2.93/3.15/4.47, all
        losers) that live's admission would have refused outright.
    """
    sym = _safe_symbol(symbol)
    ticks = load_nbbo_tape(
        db,
        sym,
        since=since,
        until=until,
        max_ticks=max_ticks,
        require_causal_provenance=require_causal_provenance,
    )
    trade_ticks = load_trade_tape(
        db,
        sym,
        since=since,
        until=until,
        max_ticks=max_ticks,
        require_causal_provenance=require_causal_provenance,
    )
    trade_bars = _trade_tape_to_microbars(trade_ticks, bar_seconds=bar_seconds) if trade_ticks else None
    quote_bars = (
        _tape_to_microbars(
            ticks,
            bar_seconds=bar_seconds,
            trade_ticks=trade_ticks,
        )
        if ticks
        else None
    )
    # Production entry geometry is NBBO-mid OHLC with executed trade volume;
    # trade-price OHLC is only a diagnostic fallback when quote bars are absent.
    bars = quote_bars if quote_bars is not None and not getattr(quote_bars, "empty", True) else trade_bars
    confidence, confidence_reasons = _confidence(
        ticks=ticks,
        trade_ticks=trade_ticks,
        bars=bars,
        source_events=source_events,
        max_ticks=max_ticks,
        require_causal_provenance=require_causal_provenance,
    )
    confidence_reasons.append(f"bar_seconds:{int(bar_seconds)}")
    confidence_reasons.append(
        "production_entry_cadence_matched"
        if int(bar_seconds) == 60
        else "nonproduction_entry_cadence"
    )
    confidence_reasons.append(
        "bar_geometry_market_reference_release_strategy_available_at"
    )
    confidence_reasons.append("bar_release_prefix_cumulative_fail_closed")
    confidence_reasons.append(
        "provider_watermark_and_continuous_trade_coverage_unproven"
    )
    confidence_reasons.append(
        "nbbo_market_reference_is_trade_time_proxy_not_quote_event_clock"
    )
    eval_start = eval_since if eval_since is not None else since
    if eval_since is not None and eval_since > since:
        confidence_reasons.append(f"warmup_context_since_{_json_dt(since)}")
        confidence_reasons.append(f"entry_eval_since_{_json_dt(eval_since)}")
    if quote_bars is not None and not getattr(quote_bars, "empty", True):
        confidence_reasons.append("nbbo_mid_microbars_with_trade_volume_used")
    elif trade_bars is None or getattr(trade_bars, "empty", True):
        confidence_reasons.append("microbars_unavailable")
    else:
        confidence_reasons.append("trade_price_microbars_used_quote_geometry_unavailable")
    skipped: dict[str, int] = {}
    gate_reasons: dict[str, int] = {}
    candidates: list[ReplayEntryCandidate] = []
    if ticks and bars is not None and not getattr(bars, "empty", True):
        bar_candidates, bar_reasons = _iter_bar_candidates(
            symbol=sym,
            ticks=ticks,
            bars=bars,
            bar_seconds=bar_seconds,
            bar_eval_stride=bar_eval_stride,
            eval_since=eval_start,
            require_release_clock=require_causal_provenance,
        )
        candidates.extend(bar_candidates)
        for reason, count in bar_reasons.items():
            gate_reasons[reason] = gate_reasons.get(reason, 0) + count
    signal = _source_signal_for_symbol(source_events)
    if live_admission_mode:
        # D3 (cf-parity): these two families are harness-only diagnostics with NO live
        # counterpart (see the docstring above) — skip them under live-admission so the
        # candidate set matches live's actual entry-class ladder. Recorded (not silently
        # dropped) so a caller diffing gate_reason_counts sees why they're absent.
        gate_reasons["tick_first_pullback_skipped_live_admission_mode"] = 1
        gate_reasons["tick_vwap_reclaim_burst_skipped_live_admission_mode"] = 1
    else:
        tick_candidates, tick_reasons = _iter_tick_scalp_candidates(
            symbol=sym,
            ticks=ticks,
            signal=signal,
            eval_since=eval_start,
        )
        candidates.extend(tick_candidates)
        for reason, count in tick_reasons.items():
            gate_reasons[reason] = gate_reasons.get(reason, 0) + count
        tick_vwap_candidates, tick_vwap_reasons = _iter_tick_vwap_reclaim_burst_candidates(
            symbol=sym,
            quote_ticks=ticks,
            trade_ticks=trade_ticks,
            source_events=source_events,
            eval_since=eval_start,
        )
        candidates.extend(tick_vwap_candidates)
        for reason, count in tick_vwap_reasons.items():
            gate_reasons[reason] = gate_reasons.get(reason, 0) + count
    candidates = _dedupe_candidates(candidates)

    risk = float(
        risk_usd
        if risk_usd is not None
        else getattr(settings, "chili_momentum_risk_max_loss_per_trade_usd", 50.0)
    )
    notional = float(max_notional_usd if max_notional_usd is not None else 0.0)
    cash_fraction_value = float(
        cash_fraction
        if cash_fraction is not None
        else getattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    )
    # D4 (cf-parity): wire LIVE's actual notional-ceiling chain instead of the flat,
    # caller-passed ``max_notional_usd`` the harness used to size against unconditionally
    # (proof: SVRE/CELZ-class replays sized ~$15.4k notional on a ~$13k account). Live's
    # chain (live_runner.py, risk_policy.py) is:
    #   fixed/flat cap -> equity_relative_notional_cap (equity x
    #   chili_momentum_risk_notional_fraction_of_equity, default 15%) ->
    #   liquidity_capped_notional (participation fraction of the name's $-volume).
    # ``account_equity_usd`` (default the mission's ~$13k account) is injected through
    # the SAME ``replay_account_equity`` ContextVar seam Replay v3 P2 already uses
    # (risk_policy.py) — zero broker I/O, pure and side-effect-free. There is no
    # historical raw buying-power table, so a fixed injected equity is the honest
    # basis; it is reported in ``confidence_reasons`` and per-trade debug rather than
    # silently assumed. Dollar-volume for the liquidity leg is approximated from the
    # REPLAYED trade tape itself (price*size), since ``universe.snapshot_dollar_volumes``
    # is a live-only data source with no as-of history to query.
    equity_basis = float(account_equity_usd) if account_equity_usd is not None else 13_000.0
    equity_capped: float | None = None
    if live_admission_mode and fixed_qty is None and cash_usd is None:
        # The flat fallback only matters when equity is unavailable (never true here —
        # ``replay_account_equity`` always injects one); a caller-passed
        # ``max_notional_usd`` still layers in as an extra flat ceiling ABOVE the
        # equity-relative fraction, exactly like live's frozen-policy flat cap.
        flat_fallback = notional if notional > 0 else max(equity_basis * 10.0, 1.0)
        with replay_account_equity(lambda *_a, **_k: equity_basis):
            equity_capped = equity_relative_notional_cap(flat_fallback)
        confidence_reasons.append(
            f"live_sizing_equity_usd:{round(equity_basis, 2)}"
            f"_equity_cap:{round(equity_capped, 2)}"
            "_liquidity_cap:computed_asof_each_entry"
        )
    if max_notional_usd is None and fixed_qty is None and cash_usd is None and not live_admission_mode:
        confidence_reasons.append("counterfactual_notional_uncapped_no_broker_state")
    if cash_usd is not None:
        confidence_reasons.append(
            f"counterfactual_a_grade_cash_fraction_sizing:{round(float(cash_fraction_value), 6)}"
        )
    if str(exit_model or "").strip().lower() == "adaptive":
        confidence_reasons.append("adaptive_exit_routing")
    rr = float(
        reward_risk
        if reward_risk is not None
        else getattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0)
    )
    hold = float(
        max_hold_seconds
        if max_hold_seconds is not None
        else getattr(settings, "chili_momentum_risk_max_hold_seconds", 900)
    )
    trades: list[CounterfactualTrade] = []
    simulation_ticks = trade_ticks or ticks
    cursor_ts = eval_start
    for candidate in candidates:
        if len(trades) >= max(0, int(max_trades)):
            break
        if candidate.ts < cursor_ts:
            continue
        source_ok, source_reason, source_debug = _has_actionable_source_before(
            source_events,
            candidate.ts,
            require_certifiable=require_certifiable_source,
        )
        # D3 (cf-parity): under live-admission the ``market_certified`` synthetic
        # source (pure market-volume, no actual catalyst/source) no longer bypasses
        # the source-before-entry requirement — see the function docstring.
        market_certified = (
            False
            if live_admission_mode
            else bool(candidate.trigger_debug.get("market_certified"))
        )
        source_recertified = (
            False
            if live_admission_mode
            else bool(candidate.trigger_debug.get("source_blocker_recertified"))
        )
        if require_source_before_entry and not source_ok and not market_certified and not source_recertified:
            skipped[source_reason] = skipped.get(source_reason, 0) + 1
            continue
        candidate_debug = dict(candidate.trigger_debug)
        candidate_debug["entry_source_debug"] = source_debug
        candidate_notional = notional
        candidate_liquidity_cap: float | None = None
        candidate_tape_dollar_volume: float | None = None
        if (
            live_admission_mode
            and fixed_qty is None
            and cash_usd is None
            and equity_capped is not None
        ):
            # Liquidity is an as-of-entry fact.  Summing the whole replay window
            # let post-entry prints enlarge an earlier position (direct future
            # leakage).  Release only prints available by this candidate.
            candidate_tape_dollar_volume = _trade_dollar_volume_asof(
                trade_ticks,
                candidate.ts,
            )
            candidate_liquidity_cap = liquidity_capped_notional(
                equity_capped,
                candidate_tape_dollar_volume,
            )
            candidate_notional = candidate_liquidity_cap
            candidate_debug["liquidity_dollar_volume_asof_entry"] = (
                round(candidate_tape_dollar_volume, 4)
                if candidate_tape_dollar_volume is not None
                else None
            )
            candidate_debug["live_notional_cap_usd"] = candidate_liquidity_cap
        candidate = ReplayEntryCandidate(
            symbol=candidate.symbol,
            ts=candidate.ts,
            reason=candidate.reason,
            entry_price=candidate.entry_price,
            stop_price=candidate.stop_price,
            trigger_debug=candidate_debug,
            gate_family=candidate.gate_family,
            bid=candidate.bid,
            ask=candidate.ask,
            spread_bps=candidate.spread_bps,
            sequence=candidate.sequence,
        )
        trade = _simulate_candidate_trade(
            candidate,
            simulation_ticks,
            risk_usd=risk,
            max_notional_usd=candidate_notional,
            reward_risk=rr,
            max_hold_seconds=(
                _float_or_none(candidate.trigger_debug.get("max_hold_seconds")) or hold
            ),
            fixed_qty=fixed_qty,
            cash_usd=cash_usd,
            cash_fraction=cash_fraction_value,
            exit_model=exit_model,
        )
        if trade is None:
            skipped["simulate_no_trade"] = skipped.get("simulate_no_trade", 0) + 1
            continue
        # D4 (cf-parity): print the per-trade notional so a validation run can see the
        # live-parity dollar sizing directly on the trade, not just in the aggregate
        # confidence_reasons string.
        trade.debug["live_notional_cap_usd"] = candidate_liquidity_cap
        trade.debug["liquidity_dollar_volume_asof_entry"] = (
            round(candidate_tape_dollar_volume, 4)
            if candidate_tape_dollar_volume is not None
            else None
        )
        trade.debug["account_equity_basis_usd"] = equity_basis if live_admission_mode else None
        trades.append(trade)
        cursor_ts = trade.exit_ts

    first = None
    if candidates:
        c = candidates[0]
        first = {
            "ts": _json_dt(c.ts),
            "reason": c.reason,
            "gate_family": c.gate_family,
            "entry_price": round(c.entry_price, 6),
            "stop_price": round(c.stop_price, 6),
            "spread_bps": c.spread_bps,
            "sequence": c.sequence,
            "source_before_entry": _has_actionable_source_before(
                source_events,
                c.ts,
                require_certifiable=require_certifiable_source,
            )[0],
        }
    src_rows = [
        {
            "ts": _json_dt(event.ts),
            "source": event.source,
            "certifiable": event.certifiable,
            "text": event.text[:220],
        }
        for event in source_events[:8]
    ]
    return SymbolReplayResult(
        symbol=sym,
        ok=True,
        confidence=confidence,
        confidence_reasons=confidence_reasons,
        tape_rows=len(ticks),
        trade_rows=len(trade_ticks),
        causal_tape_rows=sum(1 for tick in ticks if tick.provenance_certified),
        causal_trade_rows=sum(1 for tick in trade_ticks if tick.provenance_certified),
        causal_provenance_enforced=bool(require_causal_provenance),
        micro_bars=0 if bars is None else int(len(bars)),
        source_events=src_rows,
        trades=trades,
        candidate_count=len(candidates),
        skipped_reasons=skipped,
        gate_reason_counts=dict(sorted(gate_reasons.items(), key=lambda kv: kv[1], reverse=True)[:25]),
        first_candidate=first,
        bar_seconds=int(bar_seconds),
        coverage_ready=False,
        coverage_reasons=(
            (
                "sampled_tape_not_certifiable"
                if max_ticks is not None and max_ticks > 0
                else "continuous_warmup_entry_hold_exit_coverage_not_yet_proven"
            ),
            "provider_watermark_and_bounded_lateness_not_proven",
            "nbbo_quote_event_clock_unavailable",
        ),
    )


def run_counterfactual_replay(
    db: Session,
    *,
    symbols: Sequence[str],
    since: datetime,
    until: datetime,
    eval_since: datetime | None = None,
    bar_seconds: int = 60,
    max_ticks: int | None = None,
    bar_eval_stride: int = 1,
    max_trades_per_symbol: int = 3,
    require_source_before_entry: bool = True,
    require_certifiable_source: bool = False,
    risk_usd: float | None = None,
    max_notional_usd: float | None = None,
    reward_risk: float | None = None,
    max_hold_seconds: float | None = None,
    fixed_qty: float | None = None,
    cash_usd: float | None = None,
    cash_fraction: float | None = None,
    exit_model: str = "adaptive",
    live_admission_mode: bool = True,
    account_equity_usd: float | None = None,
    require_causal_provenance: bool = False,
) -> CounterfactualReplayResult:
    syms = sorted({_safe_symbol(s) for s in symbols if _safe_symbol(s)})
    source_by_symbol = load_ross_source_events(since=since, until=until, symbols=syms)
    results: list[SymbolReplayResult] = []
    for sym in syms:
        try:
            results.append(
                run_counterfactual_symbol_replay(
                    db,
                    sym,
                    since=since,
                    until=until,
                    eval_since=eval_since,
                    source_events=tuple(source_by_symbol.get(sym, ())),
                    bar_seconds=bar_seconds,
                    max_ticks=max_ticks,
                    bar_eval_stride=bar_eval_stride,
                    max_trades=max_trades_per_symbol,
                    require_source_before_entry=require_source_before_entry,
                    require_certifiable_source=require_certifiable_source,
                    risk_usd=risk_usd,
                    max_notional_usd=max_notional_usd,
                    reward_risk=reward_risk,
                    max_hold_seconds=max_hold_seconds,
                    fixed_qty=fixed_qty,
                    cash_usd=cash_usd,
                    cash_fraction=cash_fraction,
                    exit_model=exit_model,
                    live_admission_mode=live_admission_mode,
                    account_equity_usd=account_equity_usd,
                    require_causal_provenance=require_causal_provenance,
                )
            )
        except Exception as exc:
            if isinstance(exc, SQLAlchemyError):
                try:
                    db.rollback()
                except Exception:
                    pass
            results.append(
                SymbolReplayResult(
                    symbol=sym,
                    ok=False,
                    confidence="replay_error",
                    confidence_reasons=[type(exc).__name__, str(exc)[:400]],
                    tape_rows=0,
                    trade_rows=0,
                    causal_tape_rows=0,
                    causal_trade_rows=0,
                    causal_provenance_enforced=bool(require_causal_provenance),
                    micro_bars=0,
                    source_events=[],
                    trades=[],
                    candidate_count=0,
                    skipped_reasons={"replay_error": 1},
                    gate_reason_counts={},
                    first_candidate=None,
                )
            )
    return CounterfactualReplayResult(
        since=since,
        until=until,
        symbols=syms,
        results=results,
        causal_provenance_enforced=bool(require_causal_provenance),
        requested_exit_model=str(exit_model or "adaptive").strip().lower(),
    )


def opportunity_label_summary(result: CounterfactualReplayResult) -> dict[str, Any]:
    """Summarize whether counterfactual replay produced opportunity labels.

    The labels are intentionally conservative: a symbol is label-ready only when
    replay has causally certified post-publication tape, a certifiable Ross/source
    event, and at least one entry candidate. This is a market-path opportunity label, not proof of live
    runner parity or broker PnL min/max by itself.
    """

    statuses: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    source_certification_queue: list[dict[str, Any]] = []
    label_ready = 0
    taken_labels = 0
    missed_labels = 0
    for row in result.results:
        opportunity_ts = row.trades[0].entry_ts if row.trades else None
        if opportunity_ts is None and row.first_candidate:
            opportunity_ts = _parse_dt(row.first_candidate.get("ts"))
        has_any_cert_source = any(bool(src.get("certifiable")) for src in row.source_events)
        has_cert_source = False
        first_cert_source_ts: datetime | None = None
        for src in row.source_events:
            if not bool(src.get("certifiable")):
                continue
            src_ts = _parse_dt(src.get("ts"))
            if src_ts is not None and (first_cert_source_ts is None or src_ts < first_cert_source_ts):
                first_cert_source_ts = src_ts
            if opportunity_ts is None or (src_ts is not None and src_ts <= opportunity_ts):
                has_cert_source = True
        cert_source_lag_seconds = None
        if opportunity_ts is not None and first_cert_source_ts is not None:
            cert_source_lag_seconds = round((first_cert_source_ts - opportunity_ts).total_seconds(), 3)
        causal_provenance_ready = bool(
            row.causal_provenance_enforced
            and row.tape_rows > 0
            and row.trade_rows > 0
            and row.causal_tape_rows == row.tape_rows
            and row.causal_trade_rows == row.trade_rows
        )
        if row.tape_rows <= 0:
            if not row.ok:
                status = "replay_error"
            elif row.causal_provenance_enforced:
                status = "causal_provenance_unavailable"
            else:
                status = "no_tape"
        elif not causal_provenance_ready:
            status = "causal_provenance_unavailable"
        elif not row.coverage_ready:
            status = "replay_coverage_unproven"
        elif not row.source_events:
            status = "no_source_event"
        elif not has_cert_source:
            status = "cert_source_after_opportunity" if has_any_cert_source else "source_not_certified"
        elif row.candidate_count <= 0:
            status = "no_entry_candidate"
        elif row.trades:
            status = "labeled_taken"
        else:
            status = "labeled_missed"
        statuses[status] = statuses.get(status, 0) + 1
        ready = status in {"labeled_taken", "labeled_missed"}
        if ready:
            label_ready += 1
        if status == "labeled_taken":
            taken_labels += len(row.trades)
        if status == "labeled_missed":
            missed_labels += 1
        rows.append(
            {
                "symbol": row.symbol,
                "status": status,
                "label_ready": ready,
                "certifiable_source": has_cert_source,
                "any_certifiable_source": has_any_cert_source,
                "opportunity_ts": _json_dt(opportunity_ts),
                "first_certifiable_source_ts": _json_dt(first_cert_source_ts),
                "cert_source_lag_seconds": cert_source_lag_seconds,
                "tape_rows": row.tape_rows,
                "trade_rows": row.trade_rows,
                "causal_tape_rows": row.causal_tape_rows,
                "causal_trade_rows": row.causal_trade_rows,
                "causal_provenance_enforced": row.causal_provenance_enforced,
                "causal_provenance_ready": causal_provenance_ready,
                "coverage_ready": row.coverage_ready,
                "coverage_reasons": list(row.coverage_reasons),
                "bar_seconds": row.bar_seconds,
                "candidate_count": row.candidate_count,
                "trade_count": len(row.trades),
                "pnl_usd": row.pnl_usd,
                "confidence": row.confidence,
                "confidence_reasons": row.confidence_reasons,
            }
        )
        if status in {
            "causal_provenance_unavailable",
            "replay_coverage_unproven",
            "no_source_event",
            "source_not_certified",
            "cert_source_after_opportunity",
        }:
            gate_reason_counts = dict(row.gate_reason_counts or {})
            top_gate_reasons = [
                {"reason": reason, "count": count}
                for reason, count in sorted(
                    gate_reason_counts.items(),
                    key=lambda item: (-int(item[1] or 0), str(item[0])),
                )[:5]
            ]
            sampled_tape_cap = next(
                (
                    str(reason)
                    for reason in row.confidence_reasons
                    if str(reason).startswith("sampled_tape_max_ticks_")
                ),
                None,
            )
            marker_ts_text = _json_dt(opportunity_ts) if opportunity_ts is not None else "REVIEWED_SOURCE_TS"
            marker_command_template = (
                "python scripts\\mark_ross_trade_event.py "
                f"{row.symbol} --action review_certified --ts {marker_ts_text} "
                "--visual-evidence-id EVIDENCE_ID "
                "--note \"Reviewed chart-context frames before replay opportunity\""
                if marker_ts_text
                else None
            )
            marker_dry_run_command_template = (
                f"{marker_command_template} --dry-run" if marker_command_template else None
            )
            if status == "causal_provenance_unavailable":
                action_required = (
                    "capture_or_replay_only_complete_causal_q_rows_with_post_publication_clock_run_generation_and_pinned_build; "
                    "legacy_tape_is_diagnostic_only"
                )
            elif status == "replay_coverage_unproven":
                action_required = (
                    "prove_uncapped_continuous_warmup_entry_hold_exit_tape_coverage_before_labeling"
                )
            elif row.candidate_count <= 0 and sampled_tape_cap is not None:
                action_required = (
                    "rerun_replay_with_higher_or_uncapped_ticks_before_gate_shape_claim; "
                    "sampled_tape_cap_may_hide_later_candidate"
                )
            elif row.candidate_count <= 0:
                action_required = (
                    "source_review_needed_but_no_current_gate_candidate; "
                    "review_chart_frames_and_then_audit_entry_gate_shape"
                )
            elif status == "cert_source_after_opportunity":
                action_required = (
                    "find_or_mark_reviewed_chart_context_before_opportunity; "
                    "later_certifiable_source_cannot_label_this_opportunity"
                )
            elif status == "source_not_certified":
                action_required = (
                    "review_chart_frames_before_opportunity_and_link_certifying_marker; "
                    "transcript_or_scanner_only_source_is_not_enough"
                )
            else:
                action_required = (
                    "locate_ross_source_video_or_transcript_before_opportunity_then_review_chart_frames"
                )
            source_certification_queue.append(
                {
                    "symbol": row.symbol,
                    "status": status,
                    "action_required": action_required,
                    "opportunity_ts": _json_dt(opportunity_ts),
                    "first_certifiable_source_ts": _json_dt(first_cert_source_ts),
                    "cert_source_lag_seconds": cert_source_lag_seconds,
                    "candidate_count": row.candidate_count,
                    "source_event_count": len(row.source_events),
                    "causal_provenance_ready": causal_provenance_ready,
                    "coverage_ready": row.coverage_ready,
                    "coverage_reasons": list(row.coverage_reasons),
                    "replay_confidence": row.confidence,
                    "replay_confidence_reasons": list(row.confidence_reasons),
                    "sampled_tape_cap": sampled_tape_cap,
                    "sample_limited": sampled_tape_cap is not None,
                    "top_gate_reasons": top_gate_reasons,
                    "has_any_certifiable_source": has_any_cert_source,
                    "review_focus": (
                        "review_chart_context_before_opportunity"
                        if opportunity_ts is not None
                        else "review_source_context_then_entry_gate_shape"
                    ),
                    "marker_dry_run_command_template": marker_dry_run_command_template,
                    "marker_command_template": marker_command_template,
                }
            )
    return {
        "symbol_count": len(result.results),
        "label_ready_symbol_count": label_ready,
        "taken_label_count": taken_labels,
        "missed_label_count": missed_labels,
        "status_counts": dict(sorted(statuses.items())),
        "pnl_minmax_label_ready": bool(label_ready == len(result.results) and len(result.results) > 0),
        "claim_boundary": (
            "Counterfactual opportunity labels require causally certified post-publication tape, "
            "uncapped continuous warmup/entry/hold/exit coverage, certifiable Ross/source evidence, "
            "and at least one current-gate entry candidate. They support market-path missed/taken "
            "analysis but still need live-session linkage before certifying live PnL min/max."
        ),
        "source_certification_queue": source_certification_queue,
        "rows": rows,
    }


def result_to_dict(result: CounterfactualReplayResult) -> dict[str, Any]:
    label_summary = opportunity_label_summary(result)
    exit_model = counterfactual_exit_model_metadata(result.requested_exit_model)
    exit_model["engine"] = result.exit_engine
    exit_model["production_exit_parity"] = bool(result.production_exit_parity)
    return {
        "ok": True,
        "read_only": result.read_only,
        "causal_provenance_enforced": result.causal_provenance_enforced,
        "boundary": result.boundary,
        "exit_model": exit_model,
        "opportunity_label_summary": label_summary,
        "since": _json_dt(result.since),
        "until": _json_dt(result.until),
        "symbols": result.symbols,
        "total_pnl_usd": result.pnl_usd,
        "total_pnl_r": result.pnl_r,
        "results": [
            {
                "symbol": r.symbol,
                "ok": r.ok,
                "confidence": r.confidence,
                "confidence_reasons": r.confidence_reasons,
                "tape_rows": r.tape_rows,
                "trade_rows": r.trade_rows,
                "causal_tape_rows": r.causal_tape_rows,
                "causal_trade_rows": r.causal_trade_rows,
                "causal_provenance_enforced": r.causal_provenance_enforced,
                "bar_seconds": r.bar_seconds,
                "coverage_ready": r.coverage_ready,
                "coverage_reasons": list(r.coverage_reasons),
                "micro_bars": r.micro_bars,
                "source_events": r.source_events,
                "candidate_count": r.candidate_count,
                "first_candidate": r.first_candidate,
                "pnl_usd": r.pnl_usd,
                "pnl_r": r.pnl_r,
                "trades": [
                    {
                        "entry_ts": _json_dt(t.entry_ts),
                        "exit_ts": _json_dt(t.exit_ts),
                        "entry_price": t.entry_price,
                        "exit_price": t.exit_price,
                        "stop_price": t.stop_price,
                        "target_price": t.target_price,
                        "qty": t.qty,
                        "notional_usd": round(t.qty * t.entry_price, 2),
                        "pnl_usd": t.pnl_usd,
                        "pnl_r": t.pnl_r,
                        "reason": t.reason,
                        "exit_reason": t.exit_reason,
                        "gate_family": t.gate_family,
                        # D5 (cf-parity): explicit attribution aliases. ``reason`` /
                        # ``gate_family`` are the dataclass's own fields (kept for
                        # existing consumers); ``entry_reason`` / ``trigger_class`` are
                        # the mission's requested names, GUARANTEED non-empty (never a
                        # blank string reads as '?' in a report table).
                        "entry_reason": str(t.reason or "unknown_entry_reason"),
                        "trigger_class": str(t.gate_family or "unknown_gate_family"),
                        "max_favorable_r": t.max_favorable_r,
                        "max_adverse_r": t.max_adverse_r,
                        "debug": t.debug,
                    }
                    for t in r.trades
                ],
                "skipped_reasons": r.skipped_reasons,
                "gate_reason_counts": r.gate_reason_counts,
            }
            for r in result.results
        ],
    }
