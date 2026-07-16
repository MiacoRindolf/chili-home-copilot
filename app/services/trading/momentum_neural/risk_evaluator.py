"""Evaluate momentum automation sessions against config policy + governance (Phase 6)."""

from __future__ import annotations

import contextlib
import contextvars
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import (
    BrokerSymbolActionClaim,
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationSession,
)
from ..execution_family_registry import (
    asset_class_of_execution_family,
    is_documented_execution_family,
    is_momentum_automation_implemented,
    normalize_execution_family,
    resolve_execution_family_for_symbol,
)
from ..governance import get_kill_switch_status, is_kill_switch_active
from .market_profile import is_coinbase_spot_symbol
from .live_fsm import (
    LIVE_POSITION_HOLDING_STATES,
    LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY,
    LIVE_WATCHING_PREFILL_STATES,
    STATE_LIVE_PENDING_ENTRY,
)
from .paper_fsm import LIVE_INTENT_STATES, PAPER_CONCURRENT_STATES
from .replay_errors import (
    ReplayInputContractError,
    ReplayScannerSnapshotUnavailableError,
)
from .risk_policy import (
    MomentumAutomationRiskPolicy,
    POLICY_VERSION,
    _et_day_bounds_utc,
    adaptive_max_spread_bps,
    adaptive_watch_fanout,
    effective_position_cap,
    equity_relative_daily_loss_cap,
    resolve_effective_risk_policy,
)

# Count toward concurrency limits (pre-runner + paper/live runner actives until terminal).
_CONCURRENT_STATES = (
    frozenset(PAPER_CONCURRENT_STATES) | frozenset(LIVE_INTENT_STATES) | frozenset(LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY)
)

_ROSS_RISK_SNAPSHOT_TS = 0.0
_ROSS_RISK_SNAPSHOT_ROWS: dict[str, dict[str, Any]] = {}

# ReplayV3 installs this only around one real FSM tick.  The default remains
# ``None`` so production keeps using Massive through the existing path.  The
# provider is intentionally query-shaped: its caller must state the exact
# symbol, provider arguments, and resolved Ross profile that affected admission.
_REPLAY_SCANNER_SNAPSHOT_PROVIDER: contextvars.ContextVar[
    Optional[Callable[..., Mapping[str, Any]]]
] = contextvars.ContextVar(
    "_chili_replay_scanner_snapshot_provider",
    default=None,
)
_CAPTURED_LIVE_SCANNER_SNAPSHOT_REQUIRED: contextvars.ContextVar[bool] = (
    contextvars.ContextVar(
        "_chili_captured_live_scanner_snapshot_required",
        default=False,
    )
)


@contextlib.contextmanager
def replay_scanner_snapshot_provider(
    provider: Optional[Callable[..., Mapping[str, Any]]],
) -> Iterator[None]:
    """Bind one receipt-backed scanner provider for a replay FSM tick."""

    token = _REPLAY_SCANNER_SNAPSHOT_PROVIDER.set(provider)
    try:
        yield
    finally:
        _REPLAY_SCANNER_SNAPSHOT_PROVIDER.reset(token)


def _replay_scanner_snapshot_provider_bound() -> bool:
    """Return whether the current context owns a recorded scanner seam."""

    return _REPLAY_SCANNER_SNAPSHOT_PROVIDER.get() is not None


@contextlib.contextmanager
def captured_live_scanner_snapshot_scope() -> Iterator[None]:
    """Require the already-installed live Massive capture sink for this tick.

    The PAPER host freezes ``_SIM_NOW`` for causal parity, but its scanner input
    is still a live provider read that must be durably captured before return.
    This explicit mode prevents that clock from being mistaken for an offline
    replay and prevents a bound capture scope from silently using a warm global
    snapshot without the capture sink.
    """

    token = _CAPTURED_LIVE_SCANNER_SNAPSHOT_REQUIRED.set(True)
    try:
        yield
    finally:
        _CAPTURED_LIVE_SCANNER_SNAPSHOT_REQUIRED.reset(token)


def _captured_live_scanner_snapshot_required() -> bool:
    return _CAPTURED_LIVE_SCANNER_SNAPSHOT_REQUIRED.get() is True


def _plain_scanner_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy an immutable captured projection into the legacy dict shape."""

    def _copy(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): _copy(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [_copy(child) for child in item]
        return item

    return {str(key): _copy(child) for key, child in value.items()}


def _utcnow() -> datetime:
    # REPLAY v3 P2: route through the SAME sim-clock chokepoint the live runner uses
    # (``live_runner._SIM_NOW``) so the eligibility recency-grace age, the anchor age, and the
    # viability-freshness age are all computed against the SIM clock under replay — otherwise
    # they read the real wall clock and a replayed (historical) snapshot looks hours-stale, the
    # anchor ages out of the grace window, and the grace can never fire (the entry-instant
    # TOCTOU could never be reproduced). PROD: ``_SIM_NOW`` is None (only the replay harness
    # sets it) ⇒ this returns ``datetime.utcnow()`` on the identical path — BYTE-IDENTICAL.
    # Lazy import avoids the import cycle (live_runner imports this module at top level).
    try:
        from .live_runner import _SIM_NOW

        v = _SIM_NOW.get()
        if v is not None:
            return v
    except Exception:
        pass
    return datetime.utcnow()


def _normalize_decision_as_of_utc(value: datetime | None = None) -> datetime:
    """Return one timezone-aware UTC decision frontier.

    ``_utcnow`` is replay-aware and preserves the production wall-clock path when no
    replay clock is bound.  Normalizing once prevents one evaluation from observing
    slightly different daily ledgers across its loss/giveback/green-to-red checks.
    """

    resolved = value if value is not None else _utcnow()
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _check(
    cid: str,
    ok: bool,
    *,
    severity: str,
    message: str,
    detail: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {"id": cid, "ok": ok, "severity": severity, "message": message, "detail": detail or {}}


def _ross_lane_universe_required(*, mode: str, execution_family: str, symbol: str) -> bool:
    """True when live equity admission is using the Ross equity lane."""
    if str(mode or "").lower().strip() != "live":
        return False
    if not bool(getattr(settings, "chili_momentum_ross_equity_universe_required", True)):
        return False
    sym = str(symbol or "").strip().upper()
    if not sym or "-USD" in sym:
        return False
    try:
        return asset_class_of_execution_family(normalize_execution_family(execution_family)) == "equity"
    except Exception:
        return False


def _ross_signal_from_viability(via: MomentumSymbolViability | None, symbol: str) -> dict[str, Any] | None:
    if via is None:
        return None
    try:
        from .tick_scalp import ross_signal_for_symbol

        return ross_signal_for_symbol(via.execution_readiness_json, symbol)
    except Exception:
        return None


def _ross_risk_snapshot_rows(
    symbol: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Fresh provider snapshot map for filling missing Ross universe fields.

    ``massive_client.get_full_market_snapshot`` owns the one authoritative TTL.
    Keeping another TTL here could re-age a provider row for a second full
    window, so these globals are last-success observability only and are never
    consumed as a decision fallback.
    """
    global _ROSS_RISK_SNAPSHOT_TS, _ROSS_RISK_SNAPSHOT_ROWS
    from .live_runner import _SIM_NOW

    provider = _REPLAY_SCANNER_SNAPSHOT_PROVIDER.get()
    captured_live = _captured_live_scanner_snapshot_required()
    if provider is not None:
        if captured_live:
            raise ReplayScannerSnapshotUnavailableError(
                "scanner snapshot authority is ambiguous"
            )
        sym = str(symbol or "").strip().upper()
        if not sym or "-USD" in sym or "/" in sym:
            raise ReplayScannerSnapshotUnavailableError(
                "replay scanner_snapshot query requires one canonical equity symbol"
            )
        from .universe import EQUITY_ROSS_SMALLCAP

        profile = EQUITY_ROSS_SMALLCAP
        try:
            row = provider(
                sym,
                include_otc=False,
                max_age_seconds=profile.snapshot_max_age_seconds,
                profile_id=profile.profile_id,
                asset_class=profile.asset_class,
                price_min=profile.price_min,
                price_max=profile.price_max,
                min_dollar_volume=profile.min_dollar_volume,
                min_change_pct=profile.min_change_pct,
            )
        except ReplayInputContractError:
            raise
        except Exception as exc:
            raise ReplayScannerSnapshotUnavailableError(
                "replay scanner_snapshot receipt could not satisfy the exact query"
            ) from exc
        if not isinstance(row, Mapping):
            raise ReplayScannerSnapshotUnavailableError(
                "replay scanner_snapshot receipt returned a malformed projection"
            )
        plain = _plain_scanner_mapping(row)
        if str(plain.get("ticker") or "").strip().upper() != sym:
            raise ReplayScannerSnapshotUnavailableError(
                "replay scanner_snapshot receipt returned the wrong symbol"
            )
        # Never populate or consult the process-global observability cache in
        # replay.  This exact row lives only for the bound FSM tick.
        return {sym: plain}
    if _SIM_NOW.get() is not None and not captured_live:
        raise ReplayScannerSnapshotUnavailableError(
            "replay scanner_snapshot input is unavailable: "
            "recorded Ross universe snapshot not bound"
        )
    try:
        from ...massive_client import (
            MassiveFullSnapshotCaptureError,
            get_full_market_snapshot,
        )
        from .universe import EQUITY_ROSS_SMALLCAP

        snapshot = get_full_market_snapshot(
            max_age_seconds=EQUITY_ROSS_SMALLCAP.snapshot_max_age_seconds
        ) or []
    except MassiveFullSnapshotCaptureError as exc:
        raise ReplayScannerSnapshotUnavailableError(
            "captured scanner_snapshot read was not durably receipted"
        ) from exc
    except Exception:
        _ROSS_RISK_SNAPSHOT_ROWS = {}
        _ROSS_RISK_SNAPSHOT_TS = 0.0
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for row in snapshot:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker:
            rows[ticker] = row
    _ROSS_RISK_SNAPSHOT_ROWS = rows
    _ROSS_RISK_SNAPSHOT_TS = 0.0
    return rows


def _ross_lane_universe_check(symbol: str, via: MomentumSymbolViability | None) -> tuple[bool, str, dict[str, Any]]:
    """Hard Ross equity instrument-class check for final live admission."""
    # A viability row is mutable process/DB state, not a receipt-bound replay input.
    # Guard before reading it: a complete persisted Ross signal can otherwise pass
    # ``ross_smallcap_profile_evidence`` without ever reaching the snapshot fallback
    # (and therefore bypass the replay guard in ``_ross_risk_snapshot_rows``).
    from .live_runner import _SIM_NOW

    replay_active = _SIM_NOW.get() is not None
    captured_live = _captured_live_scanner_snapshot_required()
    sealed_scanner_active = (
        replay_active
        or captured_live
        or _replay_scanner_snapshot_provider_bound()
    )
    if (
        replay_active
        and not captured_live
        and not _replay_scanner_snapshot_provider_bound()
    ):
        raise ReplayScannerSnapshotUnavailableError(
            "replay scanner_snapshot input is unavailable: "
            "recorded Ross universe snapshot not bound"
        )
    try:
        from .universe import ross_smallcap_profile_evidence

        if sealed_scanner_active:
            # A captured scanner receipt is the authority for this family.  Do
            # not read mutable viability JSON first and accidentally pass from
            # a warm/current DB row without consuming the recorded provider fact.
            normalized = str(symbol or "").strip().upper()
            snapshot_row = _ross_risk_snapshot_rows(normalized).get(normalized)
            ok, reason, detail = ross_smallcap_profile_evidence(
                symbol,
                signal=None,
                snapshot_row=snapshot_row,
            )
            detail = dict(detail or {})
            detail["snapshot_backfill_used"] = True
            detail["snapshot_authority"] = "sealed_replay_receipt"
            return bool(ok), str(reason or ""), detail

        signal = _ross_signal_from_viability(via, symbol)
        ok, reason, detail = ross_smallcap_profile_evidence(symbol, signal=signal)
        if ok or reason not in {
            "ross_universe_missing_price",
            "ross_universe_missing_dollar_volume",
            "ross_universe_missing_change_pct",
        }:
            return bool(ok), str(reason or ""), dict(detail or {})
        snapshot_row = _ross_risk_snapshot_rows().get(
            str(symbol or "").strip().upper()
        )
        if snapshot_row:
            ok, reason, detail = ross_smallcap_profile_evidence(
                symbol,
                signal=signal,
                snapshot_row=snapshot_row,
            )
            detail = dict(detail or {})
            detail["snapshot_backfill_used"] = True
        return bool(ok), str(reason or ""), dict(detail or {})
    except ReplayInputContractError:
        raise
    except Exception as exc:
        return False, "ross_universe_risk_check_error", {"error": str(exc)[:160]}


def _ross_lane_universe_message(ok: bool, reason: str | None) -> str:
    if ok:
        return "Ross equity universe proof present."
    r = str(reason or "").strip()
    if r == "ross_universe_price_above_profile":
        return "Ross equity lane blocks broad/mega-cap equity candidate."
    if r == "ross_universe_price_below_profile":
        return "Ross equity lane blocks sub-dollar/non-profile equity candidate."
    if r in {"ross_universe_change_below_profile", "ross_universe_dollar_volume_below_profile"}:
        return "Ross equity lane blocks faded/thin small-cap candidate below profile."
    if r.startswith("ross_universe_missing_"):
        return "Ross equity lane blocks candidate with incomplete Ross universe proof."
    return "Ross equity lane blocks non-profile equity candidate."


_ALPACA_PAPER_RISK_FAMILIES = ("alpaca_spot", "alpaca_short")


def _positive_finite_number(value: Any) -> float | None:
    """Return a strict positive finite number; booleans are not numeric evidence."""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0.0 else None


def _raise_unknown_alpaca_risk(
    reason: str,
    *,
    session_id: int | None = None,
) -> None:
    suffix = f":session={int(session_id)}" if session_id is not None else ""
    raise RuntimeError(f"alpaca_risk_ledger_unavailable:{reason}{suffix}")


def _real_capital_execution_family_clause() -> Any:
    """Conservatively retain a corrupt legacy NULL family in the real ledger.

    The current schema is NOT NULL, but SQL ``NOT IN`` silently drops NULL.  If a
    manual import or partially-migrated database ever violates the invariant, an
    unknown family must consume the historical real-capital budget rather than
    disappear from every concurrency and loss query.
    """
    return or_(
        TradingAutomationSession.execution_family.is_(None),
        TradingAutomationSession.execution_family.notin_(
            _ALPACA_PAPER_RISK_FAMILIES
        ),
    )


def _alpaca_unresolved_entry_claims(db: Session) -> list[BrokerSymbolActionClaim]:
    """Read the durable paper-account entry ledger; failures propagate closed."""
    from .alpaca_orphan_claims import alpaca_account_scope

    return (
        db.query(BrokerSymbolActionClaim)
        .filter(
            BrokerSymbolActionClaim.account_scope == alpaca_account_scope(),
            BrokerSymbolActionClaim.action == "entry",
            BrokerSymbolActionClaim.phase != "resolved",
        )
        .all()
    )


def _alpaca_claim_is_pure_pre_http(claim: BrokerSymbolActionClaim) -> bool:
    """True only for a zero-risk arm reservation with no frozen transport."""
    metadata = claim.metadata_json if isinstance(claim.metadata_json, dict) else {}
    return bool(
        claim.phase == "claimed"
        and claim.client_order_id is None
        and claim.broker_order_id is None
        and metadata.get("order_request") is None
        and metadata.get("reserved_risk_usd") is None
    )


def _alpaca_claims_by_owner(
    claims: Iterable[BrokerSymbolActionClaim],
) -> dict[int, list[BrokerSymbolActionClaim]]:
    owners: dict[int, list[BrokerSymbolActionClaim]] = {}
    for claim in claims:
        if claim.owner_session_id is None:
            continue
        owners.setdefault(int(claim.owner_session_id), []).append(claim)
    return owners


def _scope_account_risk_query(query: Any, execution_family: str | None) -> Any:
    """Keep paper and real-capital concurrency/risk ledgers independent.

    The no-family compatibility view remains the historical real-capital ledger.
    Alpaca long and short instead share one paper-account budget.
    """
    family = normalize_execution_family(execution_family) if execution_family else None
    if family in _ALPACA_PAPER_RISK_FAMILIES:
        return query.filter(
            TradingAutomationSession.execution_family.in_(
                _ALPACA_PAPER_RISK_FAMILIES
            )
        )
    return query.filter(_real_capital_execution_family_clause())


def alpaca_paper_arm_resource_capacity(
    db: Session,
    *,
    user_id: int | None = None,
    exclude_session_id: int | None = None,
) -> dict[str, Any]:
    """Resolve the resource-only capacity for zero-risk Alpaca paper watchers.

    Arming and confirming a watcher creates no broker exposure.  Financial
    concurrency belongs to the final adaptive account reservation, after the
    causal decision packet exists.  The only arm-time concurrency constraint is
    therefore the already-documented watcher processing ceiling, derived from
    the current live-eligible field.  Alpaca long/short share one paper account
    and one runner resource pool, so the count is deliberately account-wide
    rather than user-scoped.

    This helper performs no broker/network read and invents no position, dollar,
    or per-symbol cap.  Query failures propagate so callers can fail closed on
    unavailable resource coverage instead of silently treating it as zero use.
    """

    try:
        max_age_seconds = float(
            getattr(
                settings,
                "chili_momentum_risk_viability_max_age_seconds",
                600.0,
            )
            or 600.0
        )
    except (TypeError, ValueError):
        max_age_seconds = 600.0
    max_age_seconds = max(0.0, max_age_seconds)
    cutoff = _utcnow() - timedelta(seconds=max_age_seconds)
    field_size = int(
        db.query(MomentumSymbolViability.symbol)
        .filter(
            MomentumSymbolViability.scope == "symbol",
            MomentumSymbolViability.live_eligible.is_(True),
            MomentumSymbolViability.freshness_ts >= cutoff,
        )
        .distinct()
        .count()
    )

    watcher_q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state.in_(LIVE_WATCHING_PREFILL_STATES),
        TradingAutomationSession.execution_family.in_(
            _ALPACA_PAPER_RISK_FAMILIES
        ),
    )
    if exclude_session_id is not None:
        watcher_q = watcher_q.filter(
            TradingAutomationSession.id != int(exclude_session_id)
        )
    watching = int(watcher_q.count())
    capacity = int(adaptive_watch_fanout(field_size))
    headroom = max(0, capacity - watching)
    return {
        "schema_version": "chili.alpaca-paper-arm-resource-capacity.v1",
        "account_scope": "alpaca:paper",
        "risk_usd": 0.0,
        "field_size": field_size,
        "watching": watching,
        "capacity": capacity,
        "headroom": headroom,
        "available": headroom > 0,
        "excluded_session_id": (
            int(exclude_session_id) if exclude_session_id is not None else None
        ),
        "requested_user_id": int(user_id) if user_id is not None else None,
        "provenance": {
            "authority": "resource_only_watch_fanout",
            "formula": "max(0, adaptive_watch_fanout(field_size) - watching)",
            "field_source": "fresh_distinct_live_eligible_symbols",
            "watcher_source": "alpaca_account_prefill_sessions",
            "financial_authority": "final_adaptive_reservation",
        },
    }


def aggregate_open_risk_usd(
    db: Session,
    *,
    user_id: int,
    execution_family: str | None = None,
) -> tuple[float, list[dict[str, Any]]]:
    """Sum of entry-to-stop $ at-risk across OPEN live equity momentum positions.

    The 2026-06-11 lesson: three 'independent' losses (CPSH/SNDG/INDP) were ONE
    correlated regime trade trebled — per-trade risk caps don't see the pile-up.
    At-risk counts only what can still be LOST below entry (a breakeven/locked
    stop contributes 0), so winners being managed don't block new entries.
    Returns (total_usd, per-position breakdown)."""
    total = 0.0
    rows: list[dict[str, Any]] = []
    family = normalize_execution_family(execution_family) if execution_family else None
    alpaca_scope = family in _ALPACA_PAPER_RISK_FAMILIES
    claims_by_owner: dict[int, list[BrokerSymbolActionClaim]] = {}
    if alpaca_scope:
        claims_by_owner = _alpaca_claims_by_owner(
            _alpaca_unresolved_entry_claims(db)
        )
    risk_states = tuple(LIVE_POSITION_HOLDING_STATES)
    if alpaca_scope:
        # A fill can commit before the outer FSM transaction advances out of
        # pending-entry. Position evidence wins over that stale state. A truly
        # unfilled pending order remains in the separate in-flight ledger.
        risk_states = (*risk_states, STATE_LIVE_PENDING_ENTRY)
    held_q = db.query(TradingAutomationSession).filter(
                TradingAutomationSession.mode == "live",
                TradingAutomationSession.state.in_(risk_states),
                ~TradingAutomationSession.symbol.like("%-USD"),
            )
    if alpaca_scope:
        # Alpaca long/short share one paper account. Their correlated open
        # risk must consume that account's own budget, not disappear from it.
        held_q = held_q.filter(
            TradingAutomationSession.execution_family.in_(_ALPACA_PAPER_RISK_FAMILIES)
        )
    else:
        # Paper exposure must never consume a real-capital account's budget.
        held_q = held_q.filter(
            TradingAutomationSession.user_id == int(user_id),
            _real_capital_execution_family_clause(),
        )
    # Unknown ledger state is not proof of zero exposure. Propagate read failure
    # so the broker-submit boundary fails closed.
    held = held_q.all()
    for sess in held:
        try:
            snap = sess.risk_snapshot_json or {}
            le = snap.get("momentum_live_execution") if isinstance(snap, dict) else None
            holding_state = sess.state in LIVE_POSITION_HOLDING_STATES
            if not isinstance(le, dict):
                if alpaca_scope and holding_state:
                    _raise_unknown_alpaca_risk(
                        "held_live_execution_missing",
                        session_id=sess.id,
                    )
                if alpaca_scope and not claims_by_owner.get(int(sess.id)):
                    _raise_unknown_alpaca_risk(
                        "pending_entry_evidence_missing",
                        session_id=sess.id,
                    )
                continue
            pos = le.get("position")
            if pos is None:
                if alpaca_scope and holding_state:
                    _raise_unknown_alpaca_risk(
                        "held_position_missing",
                        session_id=sess.id,
                    )
                if (
                    alpaca_scope
                    and le.get("entry_submitted") is not True
                    and not claims_by_owner.get(int(sess.id))
                ):
                    _raise_unknown_alpaca_risk(
                        "pending_entry_evidence_missing",
                        session_id=sess.id,
                    )
                continue
            if not isinstance(pos, dict):
                if alpaca_scope:
                    _raise_unknown_alpaca_risk(
                        "position_malformed",
                        session_id=sess.id,
                    )
                continue
            if alpaca_scope:
                qty = _positive_finite_number(pos.get("quantity"))
                entry = _positive_finite_number(pos.get("avg_entry_price"))
                stop = _positive_finite_number(pos.get("stop_price"))
                if qty is None or entry is None or stop is None:
                    _raise_unknown_alpaca_risk(
                        "position_risk_fields_invalid",
                        session_id=sess.id,
                    )
            else:
                qty = abs(float(pos.get("quantity") or 0.0))
                entry = float(pos.get("avg_entry_price") or 0.0)
                stop = float(pos.get("stop_price") or 0.0)
                if qty <= 0 or entry <= 0 or stop <= 0:
                    continue
            side_long = le.get("side_long") is not False and (
                str(sess.execution_family or "") != "alpaca_short"
            )
            at_risk = (
                max(0.0, entry - stop)
                if side_long
                else max(0.0, stop - entry)
            ) * qty
            if alpaca_scope and not math.isfinite(at_risk):
                _raise_unknown_alpaca_risk(
                    "position_risk_nonfinite",
                    session_id=sess.id,
                )
            if at_risk > 0:
                total += at_risk
                rows.append({"symbol": sess.symbol, "session_id": sess.id,
                             "execution_family": sess.execution_family,
                             "at_risk_usd": round(at_risk, 2)})
        except (TypeError, ValueError):
            if alpaca_scope:
                _raise_unknown_alpaca_risk(
                    "position_risk_unreadable",
                    session_id=sess.id,
                )
            continue
    return total, rows


def count_concurrent_automation_sessions(
    db: Session,
    *,
    user_id: int,
    mode: Optional[str] = None,
    exclude_session_id: Optional[int] = None,
    execution_family: str | None = None,
) -> int:
    """Active pre-runner sessions only (cancelled/archived/expired excluded by state set).

    Alpaca paper sessions (both long and short families, fake money against
    the paper endpoint) are EXCLUDED: every real arm spawns a twin, so counting
    them halves the lane's real capacity (2026-06-12 — 10 "live" slots were
    only ~5 real names on IPO morning). Twins are bounded 1:1 by the real arms.
    """
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.user_id == user_id,
        TradingAutomationSession.state.in_(_CONCURRENT_STATES),
    )
    q = _scope_account_risk_query(q, execution_family)
    if mode in ("paper", "live"):
        q = q.filter(TradingAutomationSession.mode == mode)
    if exclude_session_id is not None:
        q = q.filter(TradingAutomationSession.id != int(exclude_session_id))
    return int(q.count())


def count_open_positions(
    db: Session,
    *,
    user_id: int,
    mode: str = "live",
    crypto_only: Optional[bool] = None,
    execution_family: str | None = None,
) -> int:
    """HELD positions only (``LIVE_POSITION_HOLDING_STATES`` = entered / scaling_out
    / trailing / bailout — the states that hold capital + a live stop). The
    decouple_watching position cap charges THESE; pre-fill watchers are $0-risk and
    are governed by the watch-fanout cap instead. Alpaca twins excluded (1:1 bounded
    by real arms; never consume a real position slot). ``crypto_only`` filters to /
    out ``-USD`` for the crypto super-bucket + per-lane checks."""
    family = normalize_execution_family(execution_family) if execution_family else None
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == mode,
        TradingAutomationSession.state.in_(LIVE_POSITION_HOLDING_STATES),
    )
    if family not in _ALPACA_PAPER_RISK_FAMILIES:
        q = q.filter(TradingAutomationSession.user_id == int(user_id))
    q = _scope_account_risk_query(q, execution_family)
    if crypto_only is True:
        q = q.filter(TradingAutomationSession.symbol.like("%-USD"))
    elif crypto_only is False:
        q = q.filter(~TradingAutomationSession.symbol.like("%-USD"))
    return int(q.count())


def count_inflight_entry_orders(
    db: Session,
    *,
    user_id: int,
    crypto_only: Optional[bool] = None,
    exclude_session_id: Optional[int] = None,
    execution_family: str | None = None,
) -> int:
    """In-flight LIVE entry orders: submitted to the broker but not yet filled
    (``state == live_pending_entry`` AND ``entry_submitted`` set in the live-exec
    snapshot, no ``position`` yet). These are positions *born-but-not-yet-held* —
    the resting order can fill into a held position at any instant.

    The decouple_watching fill-boundary cap MUST count these alongside held
    positions: a position only flips to a HOLDING state at fill (seconds after
    submit), so a burst of K simultaneous submits would each read the same held
    count and all fill → overshoot. The advisory lock serializes the
    count-and-submit so each submitter sees the prior one's committed
    ``entry_submitted=True`` here, making the cap exact (B1). ``entry_submitted``
    lives in ``risk_snapshot_json`` (not a column) so this is JSON-inspected over
    the small live-pending set. Alpaca twins excluded; ``exclude_session_id`` drops
    the submitter's own row (defensive — it has not set ``entry_submitted`` yet)."""
    family = normalize_execution_family(execution_family) if execution_family else None
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state == STATE_LIVE_PENDING_ENTRY,
    )
    if family not in _ALPACA_PAPER_RISK_FAMILIES:
        q = q.filter(TradingAutomationSession.user_id == int(user_id))
    q = _scope_account_risk_query(q, execution_family)
    if crypto_only is True:
        q = q.filter(TradingAutomationSession.symbol.like("%-USD"))
    elif crypto_only is False:
        q = q.filter(~TradingAutomationSession.symbol.like("%-USD"))
    if exclude_session_id is not None:
        q = q.filter(TradingAutomationSession.id != int(exclude_session_id))
    n = 0
    claim_owner_cids: set[tuple[int, str]] = set()
    claim_owner_ids: set[int] = set()
    if family in _ALPACA_PAPER_RISK_FAMILIES:
        claims = _alpaca_unresolved_entry_claims(db)
        for claim in claims:
            if (
                exclude_session_id is not None
                and claim.owner_session_id == int(exclude_session_id)
            ):
                continue
            if claim.owner_session_id is not None:
                claim_owner_ids.add(int(claim.owner_session_id))
            if not _alpaca_claim_is_pure_pre_http(claim):
                n += 1
            if claim.owner_session_id is not None and claim.client_order_id:
                claim_owner_cids.add(
                    (int(claim.owner_session_id), str(claim.client_order_id))
                )
    # An unreadable pending-order ledger must block admission, never look empty.
    rows = q.all()
    for s in rows:
        try:
            snap = s.risk_snapshot_json or {}
            le = snap.get("momentum_live_execution") if isinstance(snap, dict) else None
            if isinstance(le, dict) and le.get("entry_submitted") and not le.get("position"):
                legacy_cid = str(le.get("entry_client_order_id") or "").strip()
                if legacy_cid and (int(s.id), legacy_cid) in claim_owner_cids:
                    continue
                n += 1
            elif (
                family in _ALPACA_PAPER_RISK_FAMILIES
                and not (isinstance(le, dict) and le.get("position") is not None)
                and int(s.id) not in claim_owner_ids
            ):
                _raise_unknown_alpaca_risk(
                    "pending_entry_evidence_missing",
                    session_id=s.id,
                )
        except (TypeError, ValueError, AttributeError):
            if family in _ALPACA_PAPER_RISK_FAMILIES:
                _raise_unknown_alpaca_risk(
                    "pending_session_unreadable",
                    session_id=s.id,
                )
            continue
    return n


def sum_inflight_entry_risk_usd(
    db: Session,
    *,
    user_id: int,
    per_trade_fallback_usd: float,
    crypto_only: Optional[bool] = None,
    exclude_session_id: Optional[int] = None,
    execution_family: str | None = None,
) -> float:
    """In-flight (submitted-but-not-yet-held) entry $-at-risk for the dollar budget.

    Mirrors :func:`count_inflight_entry_orders`'s SAME born-but-not-held set
    (``state == live_pending_entry`` AND ``entry_submitted`` set AND no ``position``
    yet) but sums the ACTUAL per-order risk the live runner persists onto each
    session at submit time (``le['entry_inflight_risk_usd']`` = that order's real
    shape-aware ``(entry-stop)*qty``, which already reflects the per-trade
    multiplier). A flat ``count * per_trade_fallback`` under-charges a burst of
    HIGH-multiplier entries; reading the persisted per-order risk makes the
    in-flight charge multiplier-aware.

    CONSERVATIVE FALLBACK: when a sibling has no persisted (positive, finite)
    ``entry_inflight_risk_usd`` (a pre-submit race, or a session written by an
    older image), charge the positive flat ``per_trade_fallback_usd`` estimate
    instead — never $0 (an under-estimate would let a fill-burst slip dollars past
    the ceiling; an over-estimate is the safe side). Same advisory-lock atomicity
    contract as the count: the caller evaluates this INSIDE the per-(user,lane)
    lock so each serialized submitter sees the prior one's committed risk."""
    family = normalize_execution_family(execution_family) if execution_family else None
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state == STATE_LIVE_PENDING_ENTRY,
    )
    if family not in _ALPACA_PAPER_RISK_FAMILIES:
        q = q.filter(TradingAutomationSession.user_id == int(user_id))
    q = _scope_account_risk_query(q, execution_family)
    if crypto_only is True:
        q = q.filter(TradingAutomationSession.symbol.like("%-USD"))
    elif crypto_only is False:
        q = q.filter(~TradingAutomationSession.symbol.like("%-USD"))
    if exclude_session_id is not None:
        q = q.filter(TradingAutomationSession.id != int(exclude_session_id))
    fallback = _positive_finite_number(per_trade_fallback_usd)
    total = 0.0
    claim_owner_cids: set[tuple[int, str]] = set()
    claim_owner_ids: set[int] = set()
    if family in _ALPACA_PAPER_RISK_FAMILIES:
        claims = _alpaca_unresolved_entry_claims(db)
        for claim in claims:
            if (
                exclude_session_id is not None
                and claim.owner_session_id == int(exclude_session_id)
            ):
                continue
            if claim.owner_session_id is not None:
                claim_owner_ids.add(int(claim.owner_session_id))
            if _alpaca_claim_is_pure_pre_http(claim):
                continue
            metadata = claim.metadata_json if isinstance(claim.metadata_json, dict) else {}
            reserved = _positive_finite_number(metadata.get("reserved_risk_usd"))
            if reserved is not None:
                total += reserved
            elif fallback is not None:
                total += fallback
            else:
                _raise_unknown_alpaca_risk("pending_claim_fallback_invalid")
            if claim.owner_session_id is not None and claim.client_order_id:
                claim_owner_cids.add(
                    (int(claim.owner_session_id), str(claim.client_order_id))
                )
    # Unknown in-flight dollars are not zero dollars. Let the caller fail closed.
    rows = q.all()
    for s in rows:
        try:
            snap = s.risk_snapshot_json or {}
            le = snap.get("momentum_live_execution") if isinstance(snap, dict) else None
            if not (isinstance(le, dict) and le.get("entry_submitted") and not le.get("position")):
                if (
                    family in _ALPACA_PAPER_RISK_FAMILIES
                    and not (isinstance(le, dict) and le.get("position") is not None)
                    and int(s.id) not in claim_owner_ids
                ):
                    _raise_unknown_alpaca_risk(
                        "pending_entry_evidence_missing",
                        session_id=s.id,
                    )
                continue
            legacy_cid = str(le.get("entry_client_order_id") or "").strip()
            if legacy_cid and (int(s.id), legacy_cid) in claim_owner_cids:
                continue
            persisted = _positive_finite_number(le.get("entry_inflight_risk_usd"))
            # Persisted real risk when present + sane; else the positive flat estimate.
            if persisted is not None:
                total += persisted
            elif fallback is not None:
                total += fallback
            elif family in _ALPACA_PAPER_RISK_FAMILIES:
                _raise_unknown_alpaca_risk(
                    "pending_session_fallback_invalid",
                    session_id=s.id,
                )
        except (TypeError, ValueError, AttributeError):
            # An un-inspectable sibling still carries real risk; charge the floor.
            if fallback is not None:
                total += fallback
            elif family in _ALPACA_PAPER_RISK_FAMILIES:
                _raise_unknown_alpaca_risk(
                    "pending_session_unreadable",
                    session_id=s.id,
                )
            continue
    if family in _ALPACA_PAPER_RISK_FAMILIES and not math.isfinite(total):
        _raise_unknown_alpaca_risk("pending_risk_total_nonfinite")
    return total


def aggregate_open_crypto_risk_usd(db: Session, *, user_id: int) -> tuple[float, list[dict[str, Any]]]:
    """Crypto mirror of :func:`aggregate_open_risk_usd` (which is equity-only —
    it filters OUT ``-USD``). Sum of entry-to-stop $ at-risk across OPEN live
    CRYPTO (-USD) positions, so the crypto lane has a dollar-precise correlated-
    exposure backstop (decouple_watching B2: the count cap alone can't bound
    dollars once crypto gaps through). Breakeven/locked stops contribute 0."""
    total = 0.0
    rows: list[dict[str, Any]] = []
    held = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(LIVE_POSITION_HOLDING_STATES),
            TradingAutomationSession.symbol.like("%-USD"),
            _real_capital_execution_family_clause(),
        )
        .all()
    )
    for sess in held:
        try:
            snap = sess.risk_snapshot_json or {}
            le = snap.get("momentum_live_execution") if isinstance(snap, dict) else None
            pos = (le or {}).get("position") if isinstance(le, dict) else None
            if not isinstance(pos, dict):
                continue
            qty = float(pos.get("quantity") or 0.0)
            entry = float(pos.get("avg_entry_price") or 0.0)
            stop = float(pos.get("stop_price") or 0.0)
            if qty <= 0 or entry <= 0 or stop <= 0:
                continue
            at_risk = max(0.0, (entry - stop)) * qty
            if at_risk > 0:
                total += at_risk
                rows.append({"symbol": sess.symbol, "session_id": sess.id,
                             "at_risk_usd": round(at_risk, 2)})
        except (TypeError, ValueError):
            continue
    return total, rows


def _viability_age_seconds(via: MomentumSymbolViability) -> float:
    ts = via.freshness_ts
    if ts is None:
        return 1e9
    if ts.tzinfo:
        ts = ts.replace(tzinfo=None)
    return max(0.0, (_utcnow() - ts).total_seconds())


def _recent_eligible_age_seconds(recent_live_eligible_at_utc: Optional[str]) -> Optional[float]:
    """Age (seconds) of the arm/confirm-time live-eligibility anchor, or None if it is
    absent / unparseable. Pure + side-effect-free. FAIL-SAFE: any parse error returns
    None so the caller keeps its conservative BLOCK (the recency grace only relaxes on a
    positively-parsed, in-window anchor — never on a missing or garbage timestamp)."""
    raw = (recent_live_eligible_at_utc or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    age = (_utcnow() - ts).total_seconds()
    # A future-dated anchor (clock skew) is treated as age 0 (still "recent"); a sane
    # positive age flows through to the window comparison.
    return max(0.0, age)


def _live_eligible_recency_grace_active(
    *,
    policy: MomentumAutomationRiskPolicy,
    recent_live_eligible_at_utc: Optional[str],
    live_forward_momentum: Optional[bool],
) -> tuple[bool, dict[str, Any]]:
    """Decide whether a live_eligible=False FLICKER at the entry instant qualifies for the
    adaptive recency grace. Returns ``(active, detail)``. ``active`` is True ONLY when ALL of:
      * the grace flag is ON (flag OFF => byte-identical: never active);
      * the session was live-eligible at ARM/CONFIRM within ``live_eligible_recency_grace_seconds``
        (the anchor parses AND its age <= the window — ONE documented base);
      * there is live FORWARD MOMENTUM (``live_forward_momentum`` is True — signed-tape accel>0
        / OFI / price rising, computed by the runner).
    FAIL-SAFE: a missing/unparseable anchor, an out-of-window anchor, or absent/false momentum
    => ``active=False`` (keep today's BLOCK). Pure + side-effect-free.

    NOTE on the anchor: it is PINNED to the arm/confirm instant and is NEVER refreshed at
    runtime (only ``operator_actions.confirm_live_arm`` writes it; the runner does not re-stamp
    it when live-eligibility is later observed True). This is deliberate and the SAFER
    behavior — a fixed anchor means the grace window cannot creep, so a slow (> window)
    arm-to-entry setup ages out and reverts to the conservative BLOCK."""
    detail: dict[str, Any] = {
        "grace_enabled": bool(policy.live_eligible_recency_grace_enabled),
        "grace_window_s": float(policy.live_eligible_recency_grace_seconds),
        "recent_eligible_age_s": None,
        "recent_eligible_within_window": False,
        "live_forward_momentum": (None if live_forward_momentum is None else bool(live_forward_momentum)),
    }
    if not policy.live_eligible_recency_grace_enabled:
        return False, detail
    age = _recent_eligible_age_seconds(recent_live_eligible_at_utc)
    if age is None:
        return False, detail
    detail["recent_eligible_age_s"] = round(age, 3)
    within = age <= float(policy.live_eligible_recency_grace_seconds)
    detail["recent_eligible_within_window"] = bool(within)
    if not within:
        return False, detail
    if not bool(live_forward_momentum):
        return False, detail
    return True, detail


def _readiness_numbers(exec_json: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not exec_json:
        return out
    for k in (
        "spread_bps",
        "slippage_estimate_bps",
        "fee_to_target_ratio",
        "product_tradable",
        "extra",
    ):
        if k in exec_json:
            out[k] = exec_json.get(k)
    ex = exec_json.get("extra")
    if isinstance(ex, dict):
        for k2 in ("spread_bps", "market_data_retrieved_at_utc", "market_data_max_age_seconds"):
            if k2 in ex and k2 not in out:
                out[k2] = ex[k2]
    return out


def _scope_daily_outcome_query(
    query: Any,
    *,
    user_id: int,
    execution_family: str | None,
) -> Any:
    family = normalize_execution_family(execution_family) if execution_family else None
    query = query.filter(
        TradingAutomationSession.mode == "live",
        MomentumAutomationOutcome.mode == "live",
    )
    if family in _ALPACA_PAPER_RISK_FAMILIES:
        return query.filter(
            TradingAutomationSession.execution_family.in_(_ALPACA_PAPER_RISK_FAMILIES)
        )
    if family:
        return query.filter(
            MomentumAutomationOutcome.user_id == int(user_id),
            TradingAutomationSession.execution_family == family,
        )
    return query.filter(
        MomentumAutomationOutcome.user_id == int(user_id),
        _real_capital_execution_family_clause(),
    )


def _broker_label_available_as_of(
    outcome: MomentumAutomationOutcome,
    *,
    frontier_utc: datetime,
) -> bool:
    """Broker truth must have existed by the decision frontier when the label flag is on."""

    if not bool(getattr(settings, "chili_momentum_broker_truth_label_enabled", False)):
        return True
    reconciled_at = getattr(outcome, "broker_reconciled_at", None)
    if not isinstance(reconciled_at, datetime):
        return False
    if reconciled_at.tzinfo is not None:
        reconciled_at = reconciled_at.astimezone(timezone.utc).replace(tzinfo=None)
    return reconciled_at <= frontier_utc


def _daily_realized_pnl(
    db: Session,
    user_id: int,
    execution_family: str | None = None,
    *,
    as_of_utc: datetime | None = None,
) -> float:
    """Sum realized PnL from all sessions that terminated today for this user.

    Routes through ``authoritative_label_for_outcome``: flag-OFF this is the legacy
    ``realized_pnl_usd`` sum byte-for-byte (accessor returns legacy pnl,
    is_reconciled=True). Flag-ON, the broker-true pnl is summed for reconciled rows
    and unreconciled rows are EXCLUDED (not summed as $0) — ⚠️ this changes the
    daily-loss-cap GATE input, a trading-behavior change to soak deploy-when-flat.
    """
    from .outcome_reconcile import authoritative_label_for_outcome

    decision_as_of = _normalize_decision_as_of_utc(as_of_utc)
    frontier_utc = decision_as_of.replace(tzinfo=None)
    day_start_utc, day_end_utc = _et_day_bounds_utc(as_of_utc=decision_as_of)
    query = (
        db.query(MomentumAutomationOutcome)
        .join(
            TradingAutomationSession,
            TradingAutomationSession.id == MomentumAutomationOutcome.session_id,
        )
        .filter(
            MomentumAutomationOutcome.terminal_at >= day_start_utc,
            MomentumAutomationOutcome.terminal_at < day_end_utc,
            MomentumAutomationOutcome.terminal_at <= frontier_utc,
        )
    )
    rows = _scope_daily_outcome_query(
        query,
        user_id=user_id,
        execution_family=execution_family,
    ).all()
    total = 0.0
    for o in rows:
        if not _broker_label_available_as_of(o, frontier_utc=frontier_utc):
            continue
        pnl, _bps, _win, is_rec = authoritative_label_for_outcome(o)
        if not is_rec:
            continue
        if pnl is not None:
            total += float(pnl)
    return total


def _running_peak_and_total(pnls: Iterable[float]) -> tuple[float, float]:
    """Pure: ``(high-water mark, final total)`` of a running cumulative sum.

    The peak is floored at 0.0 — you start the day flat, so a day that was never
    green has no PEAK PROFIT to give back. Walking close-events in time order, the
    running cumulative sum's max is exactly the peak accumulated realized profit
    Ross's 50%-giveback rule protects. Separated out (no I/O) so the arithmetic is
    unit-testable without a DB.
    """
    peak = 0.0
    running = 0.0
    for p in pnls:
        try:
            running += float(p or 0.0)
        except (TypeError, ValueError):
            continue
        if running > peak:
            peak = running
    return peak, running


def _daily_realized_pnl_peak_and_current(
    db: Session,
    user_id: int,
    execution_family: str | None = None,
    *,
    as_of_utc: datetime | None = None,
) -> tuple[float, float]:
    """``(peak high-water mark, current cumulative)`` of today's realized PnL — one query.

    Walks today's terminated-session outcomes in ``terminal_at`` order accumulating
    ``realized_pnl_usd``; ``current`` is the final cumulative sum (identical to
    ``_daily_realized_pnl``) and ``peak`` is its running max floored at 0.0. Same
    Uses the true ET trading-day window and a causal upper frontier so a replay tick
    cannot observe later same-day outcomes.
    """
    from .outcome_reconcile import authoritative_label_for_outcome

    decision_as_of = _normalize_decision_as_of_utc(as_of_utc)
    frontier_utc = decision_as_of.replace(tzinfo=None)
    day_start_utc, day_end_utc = _et_day_bounds_utc(as_of_utc=decision_as_of)
    query = (
        db.query(MomentumAutomationOutcome)
        .join(
            TradingAutomationSession,
            TradingAutomationSession.id == MomentumAutomationOutcome.session_id,
        )
        .filter(
            MomentumAutomationOutcome.terminal_at >= day_start_utc,
            MomentumAutomationOutcome.terminal_at < day_end_utc,
            MomentumAutomationOutcome.terminal_at <= frontier_utc,
        )
    )
    rows = (
        _scope_daily_outcome_query(
            query,
            user_id=user_id,
            execution_family=execution_family,
        )
        .order_by(
            MomentumAutomationOutcome.terminal_at.asc(),
            MomentumAutomationOutcome.id.asc(),
        )
        .all()
    )

    # Flag-OFF: legacy realized_pnl_usd in terminal_at order, byte-identical.
    # Flag-ON: broker-true pnl for reconciled rows; unreconciled EXCLUDED from the
    # high-water walk (a $0 fill-in would distort the giveback peak).
    def _ordered_pnls():
        for o in rows:
            if not _broker_label_available_as_of(o, frontier_utc=frontier_utc):
                continue
            pnl, _bps, _win, is_rec = authoritative_label_for_outcome(o)
            if not is_rec:
                continue
            yield pnl

    return _running_peak_and_total(_ordered_pnls())


def evaluate_profit_giveback_halt(
    db: Session,
    *,
    user_id: int,
    execution_family: str = "coinbase_spot",
    as_of_utc: datetime | None = None,
) -> dict[str, Any]:
    """Ross-style profit-giveback session halt for the momentum LIVE lane.

    Ross's rule (warriortrading.com/7-day-trading-rules, confirmed in the 2026-06-07
    research): once he gives back 50% of his PEAK accumulated daily profit he STOPS
    trading for the day ("easier to remember half than 40%"). This mirrors it — the
    UPSIDE counterpart of the daily-loss cap: once today's high-water mark of realized
    PnL has reached an equity-relative ACTIVATION threshold (a meaningful green day)
    AND current realized PnL has fallen to ``peak * (1 - giveback_fraction)`` or below,
    new arming is blocked for the rest of the daily window (lock in the green day).
    Resets with the SAME ``date.today()`` window as the daily-loss cap.

    The giveback FRACTION is the single documented knob
    (``chili_momentum_profit_giveback_fraction``, default 0.5). The activation
    threshold is equity-relative — it reuses the equity-relative daily-loss-cap
    magnitude so there is no second fixed-$ magic number (a green day worth protecting
    is, by symmetry, one that exceeds the day's max tolerable red). 0 disables.
    Read-only; mirror of the daily_loss_cap two-layer pattern.
    docs/DESIGN/MOMENTUM_LANE.md [[project_momentum_lane]] [[feedback_adaptive_no_magic]]
    """
    try:
        frac = float(getattr(settings, "chili_momentum_profit_giveback_fraction", 0.5))
    except (TypeError, ValueError):
        frac = 0.5
    # Clamp to [0, 1]: <=0 disables the rule; >1 is nonsensical (cap at full giveback).
    if frac < 0.0 or not (frac == frac):  # NaN-safe
        frac = 0.0
    elif frac > 1.0:
        frac = 1.0
    # Activation threshold is equity-relative (no second fixed-$ knob): reuse the
    # daily-loss-cap magnitude. [[feedback_adaptive_no_magic]]
    activation = equity_relative_daily_loss_cap(
        float(getattr(settings, "chili_momentum_risk_max_daily_loss_usd", 250.0)),
        execution_family,
    )
    peak, current = _daily_realized_pnl_peak_and_current(
        db,
        int(user_id),
        execution_family=execution_family,
        as_of_utc=as_of_utc,
    )
    giveback_floor = peak * (1.0 - frac)
    armed = bool(frac > 0.0 and activation > 0.0 and peak >= activation)
    halted = bool(armed and current <= giveback_floor)
    return {
        "halted": halted,
        "armed": armed,
        "peak_pnl_usd": round(float(peak), 2),
        "daily_pnl_usd": round(float(current), 2),
        "activation_threshold_usd": round(float(activation), 2),
        "giveback_fraction": round(float(frac), 4),
        "giveback_floor_usd": round(float(giveback_floor), 2),
    }


# A green day worth protecting from a FULL round-trip into the red: at least half the
# day's max-tolerable RED (the equity-relative daily-loss cap). Deliberately SMALLER than
# the profit-giveback activation (the full cap) so this catches the small green day the
# giveback — whose floor sits ABOVE $0 — cannot. One documented base, equity-relative.
_GREEN_TO_RED_ACTIVATION_FRAC = 0.5


def evaluate_green_to_red_halt(
    db: Session,
    *,
    user_id: int,
    execution_family: str = "coinbase_spot",
    as_of_utc: datetime | None = None,
) -> dict[str, Any]:
    """Ross green-to-red session breaker (gap #8, videos 37/38): going from green on the
    day back to <= $0 is the emotional-hijack trigger — walk away. The profit-giveback
    halt's floor (``peak * (1 - frac)``) sits ABOVE $0, so a TRUE round-trip into the red
    on a smaller green day is not caught. Once today's realized PnL has PEAKED above a
    small equity-relative activation (half the daily-loss-cap magnitude — no second
    fixed-$ knob) AND current realized PnL is <= 0, new live arming is blocked for the
    rest of the daily window. Read-only; same ``date.today()`` window + two-layer pattern
    as the giveback halt. [[feedback_adaptive_no_magic]]
    """
    activation = _GREEN_TO_RED_ACTIVATION_FRAC * equity_relative_daily_loss_cap(
        float(getattr(settings, "chili_momentum_risk_max_daily_loss_usd", 250.0)),
        execution_family,
    )
    peak, current = _daily_realized_pnl_peak_and_current(
        db,
        int(user_id),
        execution_family=execution_family,
        as_of_utc=as_of_utc,
    )
    armed = bool(activation > 0.0 and peak >= activation)
    halted = bool(armed and current <= 0.0)
    return {
        "halted": halted,
        "armed": armed,
        "peak_pnl_usd": round(float(peak), 2),
        "daily_pnl_usd": round(float(current), 2),
        "activation_threshold_usd": round(float(activation), 2),
    }


def _defensive_trail_candidate_for_session(
    sess: TradingAutomationSession,
) -> tuple[float | None, float | None, float | None, dict[str, Any]]:
    """A2: the target position's OWN most-defensive ALREADY-COMPUTED trail candidate, computed
    from ITS stored snapshot fields (high_water_mark, avg_entry_price, current stop, the entry
    ATR frozen at fill) via the SAME ``cushion_adaptive_trail_stop`` helper the exit machinery
    uses — NEVER an invented breakeven. "Most defensive" = the TIGHTEST band (floor bps, patience
    0), so it strictly reduces at-risk under INVARIANT-A.

    Returns ``(candidate_stop, current_stop, freed_usd, meta)`` where ``freed_usd`` = the at-risk
    reduction (qty * max(0, candidate - current)) if the candidate is strictly tighter, else 0.0.
    FAIL-CLOSED: any missing field / non-tightening candidate => ``(None, None, 0.0, ...)`` so the
    caller grants NO displacement (a plain block). Pure read of the stored snapshot; enqueues
    nothing (the caller writes the request)."""
    try:
        snap = sess.risk_snapshot_json or {}
        le = snap.get("momentum_live_execution") if isinstance(snap, dict) else None
        pos = (le or {}).get("position") if isinstance(le, dict) else None
        if not isinstance(pos, dict):
            return None, None, 0.0, {"reason": "no_position"}
        qty = float(pos.get("quantity") or 0.0)
        entry = float(pos.get("avg_entry_price") or 0.0)
        cur_stop = float(pos.get("stop_price") or 0.0)
        hwm = float(pos.get("high_water_mark") or entry or 0.0)
        if qty <= 0 or entry <= 0 or cur_stop <= 0:
            return None, None, 0.0, {"reason": "degenerate_position"}
        # The entry ATR frozen at fill (the SAME datum the position's own trail reads). No
        # invented number: if it is missing we CANNOT compute the existing trail => fail closed.
        atr_pct = None
        for k in ("entry_stop_atr_pct", "atr_pct", "entry_atr_pct"):
            v = le.get(k) if isinstance(le, dict) else None
            if v is None and isinstance(pos, dict):
                v = pos.get(k)
            try:
                if v is not None and float(v) > 0:
                    atr_pct = float(v)
                    break
            except (TypeError, ValueError):
                continue
        if atr_pct is None:
            return None, None, 0.0, {"reason": "no_entry_atr"}
        try:
            from .paper_execution import cushion_adaptive_trail_stop

            stop_atr_mult = float(getattr(settings, "chili_momentum_stop_atr_mult", 0.60) or 0.60)
            # MOST-DEFENSIVE = the tightest band: zero cushion (day_realized 0, position_risk
            # the entry risk unit) => patience 0 => floor-bps trail off the high-water mark. The
            # helper ratchets max(current_stop, breakeven_floor, trailed) — INVARIANT-A safe.
            candidate = float(
                cushion_adaptive_trail_stop(
                    high_water_mark=hwm,
                    entry_price=entry,
                    atr_pct=atr_pct,
                    stop_atr_mult=stop_atr_mult,
                    day_realized_usd=0.0,
                    position_risk_usd=max(1e-9, (entry - cur_stop) * qty),
                    breakeven_floor=cur_stop,  # never below the live stop (INVARIANT-A)
                    current_stop=cur_stop,
                    side_long=True,
                )
            )
        except Exception:
            return None, None, 0.0, {"reason": "trail_compute_failed"}
        # INVARIANT-A: compose max(candidate, current). Only a STRICTLY tighter candidate frees risk.
        composed = max(candidate, cur_stop)
        freed = max(0.0, (composed - cur_stop)) * qty
        if composed <= cur_stop or freed <= 0.0:
            return None, None, 0.0, {"reason": "candidate_not_tighter", "candidate": round(candidate, 6)}
        return (
            composed,
            cur_stop,
            freed,
            {
                "candidate_stop": round(composed, 6),
                "current_stop": round(cur_stop, 6),
                "freed_usd": round(freed, 2),
                "qty": qty,
                "high_water_mark": round(hwm, 6),
                "atr_pct": round(atr_pct, 6),
            },
        )
    except Exception:
        return None, None, 0.0, {"reason": "error_fail_closed"}


def _enqueue_risk_envelope_displacement(
    db: Session,
    *,
    user_id: int,
    candidate_symbol: str,
    execution_family: str,
    planned_risk_usd: float,
    open_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """A2: when the aggregate-open-risk cap blocks a TOP-RANKED candidate, enqueue a stop-TIGHTEN
    on the LARGEST at-risk open position to ITS OWN most-defensive trail candidate so the NEXT
    candidate tick admits against the freed envelope. THIS tick still blocks (the caller does not
    admit). The tighten is APPLIED by that position's own next live tick (it reads the enqueued
    request and composes max(candidate, current) — INVARIANT-A), so the displacement expresses
    itself through the exit machinery that already owns the stop; this never force-liquidates.

    FAIL-CLOSED at every step: flag off / candidate not the #1 top-ranked name / no at-risk
    position / the position's own defensive candidate cannot free >= the planned risk => returns
    ``{"enqueued": False, ...}`` and the caller keeps the plain block (byte-identical to today).
    """
    meta: dict[str, Any] = {"enqueued": False}
    if not bool(getattr(settings, "chili_momentum_risk_envelope_displacement_enabled", True)):
        meta["reason"] = "disabled"
        return meta
    cand = str(candidate_symbol or "").strip().upper()
    if not cand:
        meta["reason"] = "no_candidate_symbol"
        return meta
    # A1 top-rank predicate reuse: only the #1 freshness-valid live-eligible name (score >= p90)
    # earns a displacement. FAIL-CLOSED on an unreadable rank.
    try:
        from .risk_policy import _top_ranked_live_eligible_symbol

        _crypto = cand.endswith("-USD")
        top_sym, top_score, p90, rank_meta = _top_ranked_live_eligible_symbol(db, crypto=_crypto)
    except Exception:
        meta["reason"] = "rank_read_failed"
        return meta
    meta["rank"] = rank_meta
    if top_sym is None or top_score is None or p90 is None:
        meta["reason"] = "rank_unreadable"
        return meta
    if cand != top_sym or float(top_score) < float(p90):
        meta["reason"] = "not_top_ranked"
        return meta
    # Largest at-risk open position first (the two dying IPW losers on 07-02).
    rows = sorted(
        [r for r in (open_rows or []) if isinstance(r, dict) and float(r.get("at_risk_usd") or 0.0) > 0.0],
        key=lambda r: float(r.get("at_risk_usd") or 0.0),
        reverse=True,
    )
    if not rows:
        meta["reason"] = "no_at_risk_position"
        return meta
    for row in rows:
        try:
            sess_id = int(row.get("session_id"))
        except (TypeError, ValueError):
            continue
        target = (
            db.query(TradingAutomationSession)
            .filter(TradingAutomationSession.id == sess_id)
            .first()
        )
        if target is None:
            continue
        candidate_stop, current_stop, freed, cand_meta = _defensive_trail_candidate_for_session(target)
        if candidate_stop is None or freed < float(planned_risk_usd):
            # This position's most-defensive candidate can't free enough — try the next-largest.
            continue
        # ENQUEUE: write the tighten request onto the target session's snapshot. The target's OWN
        # next live tick reads `pending_risk_displacement_tighten` and composes max(candidate,
        # current) under INVARIANT-A. We do NOT mutate the live stop here (single-writer: the
        # position owns its stop); we only request the tighten it will apply itself.
        try:
            snap = dict(target.risk_snapshot_json or {})
            le = dict(snap.get("momentum_live_execution") or {})
            le["pending_risk_displacement_tighten"] = {
                "candidate_stop": float(candidate_stop),
                "enqueued_at_utc": _utcnow().isoformat(),
                "for_candidate": cand,
                "freed_usd": round(float(freed), 2),
            }
            snap["momentum_live_execution"] = le
            target.risk_snapshot_json = snap
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(target, "risk_snapshot_json")
            db.flush()
        except Exception:
            meta["reason"] = "enqueue_write_failed"
            return meta
        meta.update(
            {
                "enqueued": True,
                "reason": "displacement_enqueued",
                "target_session_id": sess_id,
                "target_symbol": row.get("symbol"),
                "planned_risk_usd": round(float(planned_risk_usd), 2),
                **cand_meta,
            }
        )
        return meta
    meta["reason"] = "no_position_frees_enough"
    return meta


def evaluate_proposed_momentum_automation(
    db: Session,
    *,
    user_id: int,
    symbol: str,
    variant_id: int,
    mode: str,
    execution_family: str = "coinbase_spot",
    exclude_session_id: Optional[int] = None,
    expected_move_bps: Optional[float] = None,
    recent_live_eligible_at_utc: Optional[str] = None,
    live_forward_momentum: Optional[bool] = None,
    decision_as_of_utc: datetime | None = None,
) -> dict[str, Any]:
    """
    Server-side risk gate for operator flows (paper draft, live arm, confirm).

    Returns stable dict: allowed, severity, checks, warnings, errors, governance_state, ...
    Archived/expired/cancelled sessions do not count toward concurrency (query filter).

    ``recent_live_eligible_at_utc`` / ``live_forward_momentum`` carry the live-eligibility
    RECENCY-GRACE evidence (2026-06-29 UPC +500% miss). The runner passes the session's
    arm/confirm-time eligibility anchor (an ISO-8601 UTC string proving the name WAS
    live-eligible at arm/confirm) and a positive forward-momentum read (signed-tape accel > 0
    / OFI / price rising). When ``via.live_eligible`` flickers False at the entry instant but
    BOTH (a) the anchor is within the grace window AND (b) forward momentum is present, the
    eligibility block is DOWNGRADED to a warn — a transient re-scoring flicker cannot terminally
    veto a just-confirmed active mover. FAIL-SAFE: a missing/unparseable anchor or absent
    momentum keeps today's BLOCK (the grace only relaxes on positive evidence, never widens
    risk blindly). Both default ``None`` so non-runner callers are byte-identical.
    """
    decision_as_of = _normalize_decision_as_of_utc(decision_as_of_utc)
    sym = symbol.strip().upper()
    m = mode.lower().strip()
    ef = normalize_execution_family(execution_family)

    # A direct evaluator invocation can run under the ReplayV3 clock without
    # passing through ReplayV3Driver's earlier capability preflight.  Refuse the
    # unimplemented Ross scanner input here, before governance/global state or
    # any route/variant/viability DB read can influence the replay decision.
    # Production is byte-equivalent at this boundary: its replay clock is unset.
    from .live_runner import _SIM_NOW

    if (
        _SIM_NOW.get() is not None
        and _ross_lane_universe_required(
            mode=m,
            execution_family=ef,
            symbol=sym,
        )
        and not _replay_scanner_snapshot_provider_bound()
        and not _captured_live_scanner_snapshot_required()
    ):
        raise ReplayScannerSnapshotUnavailableError(
            "replay scanner_snapshot input is unavailable: "
            "recorded Ross universe snapshot not bound"
        )

    policy = MomentumAutomationRiskPolicy.from_settings()
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []

    gov = get_kill_switch_status()
    governance_state = {"kill_switch_active": bool(gov.get("active")), "kill_switch_reason": gov.get("reason")}

    # ── Governance / kill switch ──────────────────────────────────────
    # A LEGACY single-global daily-loss breach is handled PER BROKER below
    # (global_daily_loss_cap check) when per-broker is enabled, so it does NOT
    # block here — only true-global halts (manual/emergency/price-monitor/backstop) do.
    _ks_reason = str(gov.get("reason") or "")
    _defer_daily_loss = (
        bool(getattr(settings, "chili_per_broker_daily_loss_enabled", True))
        and _ks_reason.startswith("global_daily_loss_breach")
        and "backstop" not in _ks_reason
    )
    if is_kill_switch_active() and not _defer_daily_loss:
        if m == "live" and policy.disable_live_if_governance_inhibit:
            checks.append(
                _check(
                    "governance_kill_switch",
                    False,
                    severity="block",
                    message="Kill switch active — live automation progression blocked.",
                    detail=governance_state,
                )
            )
        elif m == "paper" and policy.block_paper_when_kill_switch:
            checks.append(
                _check(
                    "governance_kill_switch_paper",
                    False,
                    severity="block",
                    message="Kill switch active — paper automation blocked by policy.",
                    detail=governance_state,
                )
            )
        else:
            checks.append(
                _check(
                    "governance_kill_switch",
                    True,
                    severity="ok",
                    message="Kill switch active but mode not blocked by policy.",
                    detail=governance_state,
                )
            )
    else:
        checks.append(
            _check("governance_kill_switch", True, severity="ok", message="Kill switch inactive.", detail=gov)
        )

    # ── Execution family (strategy logic vs routing seam — Phase 11) ─────
    if not is_documented_execution_family(ef):
        checks.append(
            _check(
                "execution_family",
                False,
                severity="block",
                message=f"Unknown execution_family {ef!r} (not in documented registry).",
                detail={"execution_family": ef},
            )
        )
    elif not is_momentum_automation_implemented(ef):
        checks.append(
            _check(
                "execution_family",
                False,
                severity="block",
                message=f"execution_family {ef!r} is documented but not implemented yet.",
                detail={"execution_family": ef},
            )
        )
    else:
        checks.append(
            _check(
                "execution_family",
                True,
                severity="ok",
                message="execution_family supported for momentum automation.",
                detail={"execution_family": ef},
            )
        )

    # The authoritative venue is symbol-routed (E1 per-symbol routing,
    # ``resolve_execution_family_for_symbol``): crypto BASE-USD -> coinbase_spot,
    # equities -> robinhood_spot (the DEFAULT). Validate the REQUEST against the symbol's
    # ASSET CLASS, not the single default-resolved venue: an EQUITY may legitimately route to
    # ANY equity venue — robinhood_spot OR alpaca_spot (the same-name A/B), or the sanctioned
    # MCP rail — while the dangerous CROSS-CLASS case (an equity requested via the crypto
    # venue coinbase_spot, or a crypto pair via an equity venue) is still BLOCKED. This is the
    # bug a pre-flight caught: the old exact `ef != symbol_ef` blocked alpaca_spot for equities
    # because the default resolves to robinhood_spot. (docs/DESIGN/ALPACA_LANE.md)
    symbol_ef: str | None = None
    _symbol_route_error: str | None = None
    try:
        symbol_ef = normalize_execution_family(
            resolve_execution_family_for_symbol(sym, mode="live")
        )
    except Exception as exc:
        # A selected Alpaca-paper primary route is fail-closed in the registry.
        # Convert that typed routing failure into a stable evaluation block rather
        # than crashing the operator/auto-arm request or falling through to a
        # real-money equity family.
        _symbol_route_error = type(exc).__name__
    v_row = (
        db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == int(variant_id)).one_or_none()
    )
    vef = normalize_execution_family(v_row.execution_family) if v_row is not None else None
    _symbol_class = (
        asset_class_of_execution_family(symbol_ef) if symbol_ef is not None else None
    )
    from ..execution_family_registry import execution_family_supports_asset_class

    _direct_alpaca_route_required = bool(
        m == "live"
        and symbol_ef in _ALPACA_PAPER_RISK_FAMILIES
        and (
            (
                _symbol_class == "equity"
                and bool(
                    getattr(
                        settings,
                        "chili_momentum_equity_execution_via_alpaca_paper",
                        False,
                    )
                )
            )
            or (
                _symbol_class == "crypto"
                and bool(
                    getattr(
                        settings,
                        "chili_momentum_crypto_execution_via_alpaca_paper",
                        False,
                    )
                )
            )
        )
    )
    _asset_class_aligned = bool(
        _symbol_class is not None
        and execution_family_supports_asset_class(ef, _symbol_class)
    )
    _exact_primary_route_aligned = bool(
        not _direct_alpaca_route_required or ef == symbol_ef
    )

    if (
        symbol_ef is None
        or not _asset_class_aligned
        or not _exact_primary_route_aligned
    ):
        if symbol_ef is None:
            _alignment_message = (
                "Selected execution route could not be resolved safely."
            )
        elif not _exact_primary_route_aligned:
            _alignment_message = (
                "Requested execution_family differs from the selected direct "
                "Alpaca-paper route."
            )
        else:
            _alignment_message = (
                "Requested execution_family is for a different ASSET CLASS than "
                "the symbol's venue."
            )
        checks.append(
            _check(
                "execution_family_variant_alignment",
                False,
                severity="block",
                message=_alignment_message,
                detail={
                    "request": ef,
                    "request_asset_class": asset_class_of_execution_family(ef),
                    "symbol_resolved": symbol_ef,
                    "symbol_asset_class": _symbol_class,
                    "symbol_route_error_type": _symbol_route_error,
                    "direct_alpaca_route_required": _direct_alpaca_route_required,
                    "variant_execution_family": vef,
                    "variant_id": int(variant_id),
                },
            )
        )
    else:
        checks.append(
            _check(
                "execution_family_variant_alignment",
                True,
                severity="ok",
                message="execution_family matches symbol-resolved venue.",
                detail={
                    "execution_family": ef,
                    "symbol_resolved": symbol_ef,
                    "direct_alpaca_route_required": _direct_alpaca_route_required,
                    "variant_execution_family": vef,
                },
            )
        )

    # ── Viability row ───────────────────────────────────────────────────
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == sym, MomentumSymbolViability.variant_id == int(variant_id))
        .one_or_none()
    )
    viability_state: dict[str, Any] = {"row_present": via is not None}
    freshness_state: dict[str, Any] = {"viability_age_sec": None, "fresh": False}
    if not via:
        checks.append(
            _check(
                "viability_present",
                False,
                severity="block",
                message="No durability viability row for symbol/variant.",
            )
        )
    else:
        viability_state.update(
            {
                "viability_score": via.viability_score,
                "paper_eligible": via.paper_eligible,
                "live_eligible": via.live_eligible,
                "freshness_ts": via.freshness_ts.isoformat() if via.freshness_ts else None,
            }
        )
        age = _viability_age_seconds(via)
        fresh = not policy.require_fresh_viability or age <= policy.viability_max_age_seconds
        freshness_state = {"viability_age_sec": round(age, 3), "fresh": fresh}
        checks.append(
            _check(
                "viability_present",
                True,
                severity="ok",
                message="Viability row present.",
            )
        )
        if policy.require_fresh_viability and not fresh:
            sev = "block" if m == "live" else "warn"
            checks.append(
                _check(
                    "viability_freshness",
                    False,
                    severity=sev,
                    message=f"Viability snapshot stale (age {age:.0f}s > max {policy.viability_max_age_seconds}s).",
                    detail=freshness_state,
                )
            )
        else:
            checks.append(
                _check(
                    "viability_freshness",
                    True,
                    severity="ok",
                    message="Viability freshness within policy.",
                    detail=freshness_state,
                )
            )

        if m == "paper":
            ok_pe = bool(via.paper_eligible)
            checks.append(
                _check(
                    "paper_eligible",
                    ok_pe,
                    severity="block" if not ok_pe else "ok",
                    message="Paper eligible" if ok_pe else "Not paper-eligible per neural viability.",
                )
            )
        if m == "live":
            if ef == "coinbase_spot" and not is_coinbase_spot_symbol(sym):
                checks.append(
                    _check(
                        "symbol_live_compatibility",
                        False,
                        severity="block",
                        message="Symbol is not a Coinbase spot product id for live execution.",
                        detail={"symbol": sym, "execution_family": ef},
                    )
                )
            ok_le = bool(via.live_eligible)
            if policy.require_live_eligible_for_live:
                if ok_le:
                    checks.append(
                        _check("live_eligible", True, severity="ok", message="Live eligible")
                    )
                else:
                    ross_profile_live_ok = False
                    ross_profile_detail: dict[str, Any] = {}
                    if _ross_lane_universe_required(mode=m, execution_family=ef, symbol=sym):
                        try:
                            ross_profile_live_ok, _ross_profile_reason, ross_profile_detail = (
                                _ross_lane_universe_check(sym, via)
                            )
                            ross_profile_detail = dict(ross_profile_detail or {})
                            ross_profile_detail["ross_profile_live_eligible_backfill"] = True
                        except ReplayInputContractError:
                            raise
                        except Exception:
                            ross_profile_live_ok = False
                            ross_profile_detail = {}
                    if ross_profile_live_ok:
                        checks.append(
                            _check(
                                "live_eligible",
                                True,
                                severity="ok",
                                message="Ross profile proof satisfies live eligibility.",
                                detail=ross_profile_detail,
                            )
                        )
                    else:
                        # TOCTOU recency grace: a fast/thin premarket vertical can FLICKER
                        # live_eligible False at the exact entry instant even though the name
                        # armed+confirmed live-eligible seconds earlier (UPC +500%, 2026-06-29).
                        # If the session was live-eligible at arm/confirm within the grace window
                        # AND live forward momentum is present, DOWNGRADE the terminal block to a
                        # warn so a transient flicker can't veto a just-confirmed active mover.
                        # FAIL-SAFE: no recent-eligible evidence / no momentum => keep the block.
                        grace_active, grace_detail = _live_eligible_recency_grace_active(
                            policy=policy,
                            recent_live_eligible_at_utc=recent_live_eligible_at_utc,
                            live_forward_momentum=live_forward_momentum,
                        )
                        if grace_active:
                            checks.append(
                                _check(
                                    "live_eligible",
                                    True,
                                    severity="warn",
                                    message=(
                                        "Live-eligibility FLICKER tolerated by recency grace "
                                        "(recent-eligible at arm/confirm + live forward momentum)."
                                    ),
                                    detail=grace_detail,
                                )
                            )
                        else:
                            checks.append(
                                _check(
                                    "live_eligible",
                                    False,
                                    severity="block",
                                    message="Not live-eligible per neural viability.",
                                    detail=grace_detail,
                                )
                            )
            else:
                checks.append(
                    _check(
                        "live_eligible",
                        ok_le,
                        severity="warn" if not ok_le else "ok",
                        message="Live eligibility optional by policy.",
                    )
                )

        # ── Execution readiness (spread / slip / fee) ──────────────────
            if _ross_lane_universe_required(mode=m, execution_family=ef, symbol=sym):
                ross_ok, ross_reason, ross_detail = _ross_lane_universe_check(sym, via)
                checks.append(
                    _check(
                        "ross_equity_universe",
                        ross_ok,
                        severity="ok" if ross_ok else "block",
                        message=_ross_lane_universe_message(ross_ok, ross_reason),
                        detail={**dict(ross_detail or {}), "reason": ross_reason},
                    )
                )

        ex = via.execution_readiness_json if isinstance(via.execution_readiness_json, dict) else {}
        nums = _readiness_numbers(ex)
        # Live spread cap is volatility-relative (adaptive) when the caller passes
        # the instrument's expected move — the live runner does; other callers fall
        # back to the documented base floor. Paper keeps its fixed cap.
        if m == "live":
            max_spread = adaptive_max_spread_bps(
                policy.max_spread_bps_live, expected_move_bps, policy.spread_to_expected_move_ratio,
                abs_cap_bps=policy.max_spread_bps_abs_cap,
            )
        else:
            max_spread = policy.max_spread_bps_paper
        spread = nums.get("spread_bps")
        if spread is not None:
            try:
                sb = float(spread)
                ok_sp = sb <= max_spread
                checks.append(
                    _check(
                        "spread_bps",
                        ok_sp,
                        severity="block" if not ok_sp and m == "live" else ("warn" if not ok_sp else "ok"),
                        message=f"Spread {sb} bps vs max {max_spread} ({m}).",
                        detail={"spread_bps": sb, "max": max_spread},
                    )
                )
            except (TypeError, ValueError):
                checks.append(
                    _check(
                        "spread_bps",
                        False,
                        severity="warn",
                        message="Spread bps missing or invalid in readiness JSON.",
                    )
                )
        else:
            checks.append(
                _check(
                    "spread_bps",
                    False,
                    severity="warn" if m == "live" else "ok",
                    message="No spread_bps in viability execution readiness (cannot enforce cap).",
                )
            )

        slip = nums.get("slippage_estimate_bps")
        if slip is not None:
            try:
                sl = float(slip)
                ok_sl = sl <= policy.max_estimated_slippage_bps
                checks.append(
                    _check(
                        "slippage_estimate_bps",
                        ok_sl,
                        severity="block" if not ok_sl and m == "live" else ("warn" if not ok_sl else "ok"),
                        message=f"Slippage est {sl} bps vs max {policy.max_estimated_slippage_bps}.",
                    )
                )
            except (TypeError, ValueError):
                pass
        else:
            warnings.append("slippage_estimate_bps not present — cap not enforced.")

        fee = nums.get("fee_to_target_ratio")
        if fee is not None:
            try:
                fr = float(fee)
                ok_f = fr <= policy.max_fee_to_target_ratio
                checks.append(
                    _check(
                        "fee_to_target_ratio",
                        ok_f,
                        severity="block" if not ok_f and m == "live" else ("warn" if not ok_f else "ok"),
                        message=f"Fee/target {fr:.3f} vs max {policy.max_fee_to_target_ratio:.3f}.",
                    )
                )
            except (TypeError, ValueError):
                pass

        pt = nums.get("product_tradable")
        if pt is False and m == "live":
            checks.append(
                _check(
                    "product_tradable",
                    False,
                    severity="block",
                    message="Product marked not tradable in readiness metadata.",
                )
            )
        elif m == "live" and ef == "coinbase_spot" and not is_coinbase_spot_symbol(sym):
            checks.append(
                _check(
                    "product_tradable_symbol",
                    False,
                    severity="block",
                    message="Live readiness requires a Coinbase spot symbol like BTC-USD.",
                    detail={"symbol": sym},
                )
            )

        # Strict Coinbase freshness (optional)
        if policy.require_strict_coinbase_freshness and settings.chili_coinbase_strict_freshness:
            max_age = float(
                min(policy.stale_market_data_max_age_sec, settings.chili_coinbase_market_data_max_age_sec)
            )
            md_age = nums.get("market_data_max_age_seconds")
            if md_age is not None:
                try:
                    mda = float(md_age)
                    ok_md = mda <= max_age
                    checks.append(
                        _check(
                            "market_data_freshness",
                            ok_md,
                            severity="block" if not ok_md and m == "live" else ("warn" if not ok_md else "ok"),
                            message=f"Market data age {mda}s vs max {max_age}s.",
                        )
                    )
                except (TypeError, ValueError):
                    pass
            else:
                checks.append(
                    _check(
                        "market_data_freshness",
                        False,
                        severity="warn",
                        message="Strict freshness requested but market_data_max_age_seconds missing.",
                    )
                )

    # ── Concurrency ─────────────────────────────────────────────────────
    # MODE-SCOPED count (2026-06-12 SpaceX-morning incident): the paper shadow
    # mass (10 overnight crypto paper sessions) filled a mode-blind total cap
    # and starved EVERY live arm through the premarket window. Paper sessions
    # are free simulations — they must never consume the real-money budget.
    # Live proposals are additionally bounded by the adaptive live cap below.
    _decouple = bool(getattr(settings, "chili_momentum_decouple_watching_enabled", False))
    _adaptive_alpaca_arm = bool(
        m == "live" and ef in _ALPACA_PAPER_RISK_FAMILIES
    )
    _alpaca_arm_resource: dict[str, Any] | None = None
    if _adaptive_alpaca_arm:
        # An arm/confirm is a $0-risk watcher, not a hypothetical position.  Do
        # not spend or pre-veto the adaptive paper account with the legacy
        # all-session or held-position caps.  Bound only runner resources here;
        # the final account-wide adaptive reservation is the sole concurrency /
        # gross / buying-power authority for an exposure increase.
        try:
            _alpaca_arm_resource = alpaca_paper_arm_resource_capacity(
                db,
                user_id=user_id,
                exclude_session_id=exclude_session_id,
            )
            _resource_ok = bool(_alpaca_arm_resource.get("available"))
        except Exception as exc:
            _resource_ok = False
            _alpaca_arm_resource = {
                "schema_version": "chili.alpaca-paper-arm-resource-capacity.v1",
                "account_scope": "alpaca:paper",
                "risk_usd": 0.0,
                "available": False,
                "error_type": type(exc).__name__,
                "provenance": {
                    "authority": "resource_only_watch_fanout",
                    "financial_authority": "final_adaptive_reservation",
                },
            }
        checks.append(
            _check(
                "max_concurrent_sessions",
                True,
                severity="ok",
                message=(
                    "Legacy all-session cap is not a financial authority for "
                    "an Alpaca paper watcher."
                ),
                detail={
                    "bypassed": True,
                    "risk_usd": 0.0,
                    "authority": "alpaca_paper_watch_resource_capacity",
                },
            )
        )
        checks.append(
            _check(
                "alpaca_paper_watch_resource_capacity",
                _resource_ok,
                severity="block" if not _resource_ok else "ok",
                message=(
                    "Alpaca paper watcher resource headroom is available."
                    if _resource_ok
                    else "Alpaca paper watcher resource headroom is unavailable."
                ),
                detail=_alpaca_arm_resource,
            )
        )
        checks.append(
            _check(
                "max_concurrent_live_sessions",
                True,
                severity="ok",
                message=(
                    "Legacy live-position slot cap is deferred to the final "
                    "adaptive Alpaca reservation."
                ),
                detail={
                    "bypassed": True,
                    "risk_usd": 0.0,
                    "financial_authority": "final_adaptive_reservation",
                },
            )
        )
    else:
        total_ct = count_concurrent_automation_sessions(
            db,
            user_id=user_id,
            mode=m,
            exclude_session_id=exclude_session_id,
            execution_family=ef,
        )
        _max_total = policy.max_concurrent_sessions
        if _decouple and m == "live":
            # Decoupled: watchers fan out to watch_fanout_max, so the coarse all-states
            # cap must clear (fanout + position cap + slack) or it would silently re-cap
            # the funnel at the legacy 10. It remains a leak-catching backstop (a stuck
            # live_cooldown pile-up still trips it), not the active constraint.
            _fanout = int(getattr(settings, "chili_momentum_watch_fanout_max", 15) or 15)
            _max_total = max(_max_total, _fanout + effective_position_cap(crypto=False) + 5)
        ok_tot = total_ct < _max_total
        checks.append(
            _check(
                "max_concurrent_sessions",
                ok_tot,
                severity="block" if not ok_tot else "ok",
                message=f"Concurrent {m} sessions {total_ct} / max {_max_total}.",
                detail={"count": total_ct, "mode": m},
            )
        )
        if m == "live":
            if _decouple:
                # Charge the risk-budget cap against HELD positions only (watchers are
                # $0-risk). This mirrors the authoritative advisory-locked fill-boundary
                # cap in live_runner; here it is a coarse secondary check at arm time.
                live_ct = count_open_positions(
                    db,
                    user_id=user_id,
                    mode="live",
                    execution_family=ef,
                )
                _live_cap = effective_position_cap(crypto=False)
            else:
                live_ct = count_concurrent_automation_sessions(
                    db,
                    user_id=user_id,
                    mode="live",
                    exclude_session_id=exclude_session_id,
                    execution_family=ef,
                )
                _live_cap = policy.max_concurrent_live_sessions
            ok_lv = live_ct < _live_cap
            checks.append(
                _check(
                    "max_concurrent_live_sessions",
                    ok_lv,
                    severity="block" if not ok_lv else "ok",
                    message=f"Concurrent live sessions {live_ct} / max {_live_cap}.",
                    detail={"count": live_ct},
                )
            )

    # ── Daily loss cap (momentum-local) ───────────────────────────────────
    daily_pnl = _daily_realized_pnl(
        db,
        user_id,
        execution_family=ef,
        as_of_utc=decision_as_of,
    )
    # Equity-relative daily-loss circuit-breaker (no fixed-$ magic); falls back to
    # the fixed cap when equity is unavailable. [[feedback_adaptive_no_magic]]
    max_daily_loss = equity_relative_daily_loss_cap(policy.max_daily_loss_usd, ef)
    ok_dloss = daily_pnl > -max_daily_loss
    checks.append(
        _check(
            "daily_loss_cap",
            ok_dloss,
            severity="block" if not ok_dloss and m == "live" else ("warn" if not ok_dloss else "ok"),
            message=f"Daily realized PnL ${daily_pnl:+.2f} vs max loss -${max_daily_loss:.2f}.",
            detail={"daily_pnl_usd": daily_pnl, "max_daily_loss_usd": max_daily_loss},
        )
    )

    # ── Profit-giveback session halt (Ross 50%-giveback rule) ─────────────
    # The UPSIDE mirror of the daily-loss cap: once today's realized PnL has PEAKED at
    # a meaningful equity-relative green AND has since given back >= giveback_fraction
    # of that peak, block new live arming for the rest of the daily window (lock in the
    # green day instead of round-tripping it back to flat/red). The single documented
    # knob is the giveback fraction; the activation threshold is equity-relative (reuses
    # the daily-loss-cap magnitude — no second fixed-$ number). [[feedback_adaptive_no_magic]]
    gb = evaluate_profit_giveback_halt(
        db,
        user_id=user_id,
        execution_family=ef,
        as_of_utc=decision_as_of,
    )
    checks.append(
        _check(
            "profit_giveback",
            not gb["halted"],
            severity="block" if gb["halted"] and m == "live" else ("warn" if gb["halted"] else "ok"),
            message=(
                f"Profit giveback halt: realized PnL ${gb['daily_pnl_usd']:+.2f} gave back "
                f">= {int(round(gb['giveback_fraction'] * 100))}% of ${gb['peak_pnl_usd']:+.2f} peak "
                f"(halts at <= ${gb['giveback_floor_usd']:+.2f})."
                if gb["halted"]
                else (
                    f"Profit giveback within band (peak ${gb['peak_pnl_usd']:+.2f}, "
                    f"now ${gb['daily_pnl_usd']:+.2f})."
                )
            ),
            detail=gb,
        )
    )

    # ── Green-to-red session breaker (Ross gap #8) ────────────────────────
    # Stricter complement of the giveback halt: once the day PEAKED green above a small
    # equity-relative activation and current realized PnL has round-tripped to <= $0,
    # block new live arming (the green-to-red emotional-hijack walk-away the giveback's
    # above-$0 floor misses). [[feedback_adaptive_no_magic]]
    g2r = evaluate_green_to_red_halt(
        db,
        user_id=user_id,
        execution_family=ef,
        as_of_utc=decision_as_of,
    )
    checks.append(
        _check(
            "green_to_red",
            not g2r["halted"],
            severity="block" if g2r["halted"] and m == "live" else ("warn" if g2r["halted"] else "ok"),
            message=(
                f"Green-to-red halt: peaked ${g2r['peak_pnl_usd']:+.2f} (>= "
                f"${g2r['activation_threshold_usd']:+.2f}) then round-tripped to "
                f"${g2r['daily_pnl_usd']:+.2f} — walk away for the session."
                if g2r["halted"]
                else (
                    f"Green-to-red ok (peak ${g2r['peak_pnl_usd']:+.2f}, "
                    f"now ${g2r['daily_pnl_usd']:+.2f})."
                )
            ),
            detail=g2r,
        )
    )

    # ── Global daily loss cap (P0.2 — spans autotrader + momentum) ────────
    # Read-only here: we block new entries if already breached, but do NOT
    # activate the kill switch from a pre-entry "what if" evaluation. The
    # post-close hooks (feedback_emit / auto_trader_monitor) do the actual
    # activation when a realized-loss event lands.
    try:
        if (
            ef in _ALPACA_PAPER_RISK_FAMILIES
            or bool(getattr(settings, "chili_per_broker_daily_loss_enabled", True))
        ):
            # PER-BROKER: block this candidate only if ITS OWN broker breached its
            # own real-equity cap — a Coinbase-sized breach can't block an RH arm
            # (the literal 2026-06-15 incident). Alpaca PAPER always uses its
            # broker-equity observation even if the generic rollout flag is OFF.
            # Read-only (activate=False).
            from ..governance import _peek_broker_breach

            _pb_breached, _pb = _peek_broker_breach(db, ef, user_id=user_id)
            ok_gdl = not _pb_breached
            checks.append(
                _check(
                    "global_daily_loss_cap",
                    ok_gdl,
                    severity="block" if not ok_gdl and m == "live" else ("warn" if not ok_gdl else "ok"),
                    message=(
                        f"Broker[{_pb.get('family')}] realized PnL "
                        f"${float(_pb.get('realized', 0.0) or 0.0):+.2f} "
                        f"vs cap -${float(_pb.get('limit', _pb.get('cap', 0.0)) or 0.0):.2f}."
                    ),
                    detail=_pb,
                )
            )
        else:
            from ..governance import check_daily_loss_breach
            gdl = check_daily_loss_breach(
                db,
                user_id=user_id,
                activate=False,
                as_of_utc=decision_as_of,
            )
            ok_gdl = not bool(gdl.get("breached"))
            if gdl.get("source") != "none":
                checks.append(
                    _check(
                        "global_daily_loss_cap",
                        ok_gdl,
                        severity="block" if not ok_gdl and m == "live" else ("warn" if not ok_gdl else "ok"),
                        message=(
                            f"Global realized PnL ${float(gdl.get('realized_usd', 0.0)):+.2f} "
                            f"vs cap -${float(gdl.get('limit_usd', 0.0)):.2f} "
                            f"(src={gdl.get('source')})."
                        ),
                        detail={
                            "realized_usd": gdl.get("realized_usd"),
                            "limit_usd": gdl.get("limit_usd"),
                            "source": gdl.get("source"),
                            "breakdown": gdl.get("breakdown"),
                        },
                    )
                )
    except Exception as exc:
        checks.append(
            _check(
                "global_daily_loss_cap",
                False,
                severity="block" if m == "live" else "warn",
                message="Broker daily-loss observation unavailable; admission deferred.",
                detail={"transient": True, "error_type": type(exc).__name__},
            )
        )

    # ── Aggregate open at-risk cap (correlation guard, 2026-06-11) ─────────
    # Low-float momentum positions are REGIME-correlated: they fade together.
    # Cap the SUM of entry-to-stop risk across open equity positions at an
    # equity-relative ceiling; a new entry may not push the pile-up past it.
    try:
        if _adaptive_alpaca_arm:
            # There is no truthful candidate R at watcher-arm time.  Charging a
            # generic 3% envelope plus a hypothetical legacy per-trade loss here
            # both double-governs the account and can reject a setup before its
            # structural stop/quality/liquidity economics exist.  The exact
            # request/packet/claim is recomputed and atomically reserved at the
            # broker boundary; that is the authoritative aggregate exposure gate.
            checks.append(
                _check(
                    "aggregate_open_risk_cap",
                    True,
                    severity="ok",
                    message=(
                        "Hypothetical aggregate-risk admission is deferred to "
                        "the final adaptive Alpaca reservation."
                    ),
                    detail={
                        "bypassed": True,
                        "candidate_risk_usd": None,
                        "watcher_risk_usd": 0.0,
                        "authority": "final_adaptive_reservation",
                        "dimensions": [
                            "structural_risk_usd",
                            "gross_notional_usd",
                            "buying_power_usd",
                        ],
                    },
                )
            )
        else:
            _agg_pct = float(getattr(
                settings, "chili_momentum_max_aggregate_risk_pct_of_equity", 0.03) or 0.0)
        if not _adaptive_alpaca_arm and _agg_pct > 0 and m == "live":
            from .risk_policy import _account_equity_usd

            # Aggregate risk is an account-equity guard for THIS execution rail.
            # The old no-argument call silently resolved to Coinbase even for
            # Alpaca sessions, producing a tiny, wrong-family cap.
            _eq = _account_equity_usd(ef, prefer_equity=True)
            if _eq and float(_eq) > 0:
                _agg_cap = _agg_pct * float(_eq)
                _open_risk, _open_rows = aggregate_open_risk_usd(
                    db,
                    user_id=user_id,
                    execution_family=ef,
                )
                # the candidate entry's planned risk = the lane's per-trade loss cap
                from .risk_policy import equity_relative_loss_cap

                # Passing 0 disabled the helper by contract, so the candidate
                # was always charged $0. Use the policy's real fixed fallback
                # and thread the same execution family used for account equity.
                _planned = float(
                    equity_relative_loss_cap(policy.max_loss_per_trade_usd, ef) or 0.0
                )
                if not math.isfinite(_planned) or _planned <= 0.0:
                    raise RuntimeError("planned aggregate risk unavailable")
                _ok_agg = (_open_risk + _planned) <= _agg_cap
                _agg_detail = {
                    "open_risk_usd": round(_open_risk, 2),
                    "planned_risk_usd": round(_planned, 2),
                    "cap_usd": round(_agg_cap, 2),
                    "positions": _open_rows,
                }
                # ── A2 QUALITY-RANKED RISK-ENVELOPE DISPLACEMENT ──────────────────────
                # The cap blocks. If this candidate is the #1 top-ranked live-eligible mover
                # (CLRO-class), enqueue a stop-TIGHTEN on the largest at-risk open position to
                # ITS OWN most-defensive trail candidate so the NEXT candidate tick admits
                # against the freed envelope. THIS tick STILL BLOCKS (we do not flip _ok_agg).
                # FAIL-CLOSED: non-top-ranked / no candidate level / frees < planned => nothing
                # enqueued, byte-identical block. Never touches this candidate's block result.
                if not _ok_agg and _planned > 0:
                    try:
                        _disp = _enqueue_risk_envelope_displacement(
                            db,
                            user_id=user_id,
                            candidate_symbol=sym,
                            execution_family=ef,
                            planned_risk_usd=_planned,
                            open_rows=_open_rows,
                        )
                        _agg_detail["displacement"] = _disp
                    except Exception:
                        _agg_detail["displacement"] = {"enqueued": False, "reason": "error"}
                checks.append(
                    _check(
                        "aggregate_open_risk_cap",
                        _ok_agg,
                        severity="block" if not _ok_agg else "ok",
                        message=(
                            f"Open at-risk ${_open_risk:,.0f} + planned ${_planned:,.0f} "
                            f"vs cap ${_agg_cap:,.0f} ({_agg_pct:.1%} of equity)."
                        ),
                        detail=_agg_detail,
                    )
                )
            else:
                raise RuntimeError("aggregate-risk equity unavailable")
    except Exception as exc:
        checks.append(
            _check(
                "aggregate_open_risk_cap",
                False,
                severity="block" if m == "live" else "warn",
                message="Aggregate account risk unavailable; admission deferred.",
                detail={"transient": True, "error_type": type(exc).__name__},
            )
        )

    # ── Portfolio drawdown breaker (Hard Rule 2 — spans every entry path) ──
    # The portfolio tier samples ALL closed trades (attributed + no_pattern
    # + manual + reconcile-inferred), independent of the momentum-local
    # daily-loss cap above. Wired here so the AUTHORITATIVE momentum arm
    # path enforces Hard Rule 2 as a hard block — not only at the
    # venue-adapter BUY gate (_assert_portfolio_breaker_ok) + auto_arm
    # Guard 3 (both fail-open pre-checks). check_portfolio_drawdown_breaker
    # returns (False, None) when disabled / shadow / insufficient history /
    # not tripped, and (True, reason) ONLY when enabled AND live AND the
    # trip condition is met (it is fail-CLOSED on its own DB/threshold
    # errors in live mode). Shadow-mode "would_have_tripped" logging is
    # emitted inside the helper. (2026-06-07 momentum-lane audit.)
    try:
        from ..portfolio_risk import check_portfolio_drawdown_breaker

        pdd_tripped, pdd_reason = check_portfolio_drawdown_breaker(db, user_id)
    except Exception as exc:
        # A setup/import failure (NOT a breaker trip — a genuine trip
        # returns normally above and never raises). Fail-open with a warn so
        # an unwired environment is not bricked; the venue-adapter gate is
        # the live-money backstop.
        checks.append(
            _check(
                "portfolio_dd_breaker",
                False,
                severity="block" if m == "live" else "warn",
                message=(
                    "Portfolio drawdown breaker unavailable; admission deferred."
                ),
                detail={"transient": True, "error_type": type(exc).__name__},
            )
        )
    else:
        checks.append(
            _check(
                "portfolio_dd_breaker",
                not pdd_tripped,
                severity="block" if pdd_tripped and m == "live" else ("warn" if pdd_tripped else "ok"),
                message=(
                    str(pdd_reason)
                    if pdd_tripped
                    else "Portfolio drawdown breaker not tripped."
                ),
                detail={"tripped": bool(pdd_tripped)},
            )
        )

    checks.append(
        _check(
            "notional_cap",
            True,
            severity="ok",
            message="Max notional per trade is enforced at the runner order boundary before adapter submission.",
            detail={
                "max_notional_per_trade_usd": policy.max_notional_per_trade_usd,
                "enforcement_boundary": "momentum_live_runner_pre_adapter",
            },
        )
    )

    # ── Aggregate severity ────────────────────────────────────────────────
    has_block = any(c.get("severity") == "block" and not c.get("ok") for c in checks)
    has_warn = any(c.get("severity") == "warn" and not c.get("ok") for c in checks)
    allowed = not has_block
    if has_block:
        severity = "block"
    elif has_warn:
        severity = "warn"
    else:
        severity = "ok"

    for c in checks:
        if not c.get("ok") and c.get("severity") == "warn":
            warnings.append(str(c.get("message", "")))
        if not c.get("ok") and c.get("severity") == "block":
            errors.append(str(c.get("message", "")))

    evaluated_at = decision_as_of.isoformat()
    return {
        "allowed": allowed,
        "severity": severity,
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "effective_policy_summary": {
            "policy_version": POLICY_VERSION,
            "mode": m,
            "execution_family": ef,
            "max_spread_bps": policy.max_spread_bps_live if m == "live" else policy.max_spread_bps_paper,
            "max_concurrent_sessions": policy.max_concurrent_sessions,
            "max_concurrent_live_sessions": policy.max_concurrent_live_sessions,
            "alpaca_arm_resource_capacity": _alpaca_arm_resource,
            "new_risk_concurrency_authority": (
                "final_adaptive_reservation"
                if _adaptive_alpaca_arm
                else "legacy_risk_policy"
            ),
        },
        "governance_state": governance_state,
        "freshness_state": freshness_state,
        "viability_state": viability_state,
        "evaluated_at_utc": evaluated_at,
    }


def evaluate_existing_automation_session(
    db: Session,
    *,
    user_id: int,
    session_id: int,
    decision_as_of_utc: datetime | None = None,
) -> dict[str, Any]:
    decision_as_of = _normalize_decision_as_of_utc(decision_as_of_utc)
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {
            "allowed": False,
            "severity": "block",
            "checks": [_check("session", False, severity="block", message="Session not found.")],
            "warnings": [],
            "errors": ["Session not found."],
            "evaluated_at_utc": decision_as_of.isoformat(),
        }
    return evaluate_proposed_momentum_automation(
        db,
        user_id=user_id,
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        mode=sess.mode,
        execution_family=sess.execution_family,
        exclude_session_id=int(sess.id),
        decision_as_of_utc=decision_as_of,
    )


def summarize_risk_from_snapshot(snap: Any) -> dict[str, Any]:
    """Light read-model for list views (persisted evaluation only)."""
    if not isinstance(snap, dict):
        return {"severity": "unknown", "allowed": True, "reasons": []}
    mr = snap.get("momentum_risk")
    if not isinstance(mr, dict):
        return {"severity": "unknown", "allowed": True, "reasons": ["no_risk_evaluation_stored"]}
    reasons = list(mr.get("errors") or [])[:4]
    reasons.extend(list(mr.get("warnings") or [])[:2])
    return {
        "severity": mr.get("severity", "unknown"),
        "allowed": bool(mr.get("allowed", True)),
        "evaluated_at_utc": mr.get("evaluated_at_utc"),
        "reasons": reasons[:6],
        "governance_inhibit": bool((mr.get("governance_state") or {}).get("kill_switch_active")),
    }
