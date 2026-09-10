"""HELD-tick execution BBO selector — IQFeed L1 first, direct IEX second, nothing third.

[48] BUILD B (2026-09-10). Ang HELD na desisyon (stop / target / HWM / bailout /
opinion exit) ay nagbabasa ng bid mula sa `_live_tick_bbo`. Hanggang sa build na
ito ang HELD branch ay: strict IEX (2.0 s) -> kapag wala, ang buong stand-in
ladder ng `get_execution_bbo` (ang SIP-clocked tape tier MUNA, saka own-clock L1,
ang trade-tick BBO, L2) sa ilalim ng literal na `stand_in_max_age_seconds=15.0`.

ANG PANGYAYARI (PCLA 21592, 13:41:05.80Z): ang bailout bid 8.96 ay galing sa
SIP-clocked tape row 235009576 -- provider 13:40:59.221, received 13:41:04.236,
**6.58 s na luma**, pumasa sa 10-s SIP ceiling dahil ang SIP tier ang UNANG tinatanong --
habang ang fenced IQFeed L1 row (8.88/9.00) ay **1.06 s** lang ang edad sa
parehong sandali. Ang exit ay ipinresyo 13:41:12.128 off sa own-clock row
234991296 na 33.875 s ang edad (cap 900) -> fill 8.88.

ANG DOKTRINA: ang HELD tick ay nagbabasa ng IQFeed L1 (fenced o own-clock, hinahatulan
sa SARILING event-reference clock nito), tapos ang strict na direct IEX, at WALA NANG
tier 3. Kapag parehong tumanggi, `tick=None` at ang `tick_live_session` ay bumabalik
BAGO ang HWM ratchet at exit ladder -- ang nakapahingang broker deadman stop ang sahig.
Bumabagsak ang lane PATUNGO sa deadman, hindi patungo sa isang lumang hilera.

WALANG MAGIC NUMBER: bawat hangganan ay hinango mula sa isang pinangalanang
distribusyon ng fenced L1 tape (10-min trailing window, TTL 60 s), at ang binding
value ay nasa BAWAT resibo (`bbo_bounds`). Kapag hindi masukat (n < n_min, timeout,
walang DB), ang halagang nasukat 2026-09-10 17:40Z ang pumapalit at NAKATATAK ang
pinagmulan (`measured_fallback_20260910T1740Z`).

Puro (pure) ang module na ito: walang DB import sa itaas, lahat ng reader ay
injectable (`HeldBboReads`) para masubok sa fakes. Ang tanging DB touch ay sa
`derive_held_bbo_bounds` (session_factory ang ipinapasa) at sa `current_bounds`
(lazy import ng `SessionLocal`).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from ....config import settings
from ..venue.protocol import FreshnessMeta, NormalizedTicker

_log = logging.getLogger(__name__)

HELD_BBO_SELECTOR_VERSION = "held_bbo_v1"

# --- Hangganan na PINAGSASALUHAN, hindi bago -----------------------------------
# Ang bridge fence ng fenced L1 (`alpaca_spot._IQFEED_AUTHORITY_MAX_AGE_S` ay
# tumutukoy dito): received_at − provider_trade_reference_at ay HINDI lalampas
# sa 2.0 s sa isang real-time na hilera (nasukat A': max 2.000 sa 81,147 hilera).
# Isang hilera na lampas dito ay hindi maaaring real-time -- ito ang lagda ng
# 15-min delayed na NYSE data na dala pa rin ang exchange stamp (≈ 900 s).
IQFEED_L1_RECEIVE_REFERENCE_FENCE_S = 2.0
IQFEED_L1_FUTURE_TOLERANCE_S = 1.0
# = live_runner_loop._EVENT_TICK_MIN_SPACING_S (may parity test). Isang tick
# spacing: ang HELD tick ay nakakakita ng libro nang hindi hihigit sa isang
# delivery + isang tick spacing sa likod ng katotohanan.
EVENT_TICK_MIN_SPACING_S = 2.0

L1_BASIS_FENCED = "iqfeed_q_receive_trade_reference_fenced"
L1_BASIS_OWN_CLOCK = "iqfeed_q_bid_ask_time_clock"
IQFEED_L1_V3_BUILD_PREFIX = "iqfeed-l1-exact-print-provenance-v3"

AUTHORITY_L1_FENCED = "stand_in_iqfeed_l1_fenced"
AUTHORITY_L1_OWN_CLOCK = "stand_in_iqfeed_l1"
AUTHORITY_ALPACA_DIRECT = "alpaca_direct"

TIER_IQFEED_L1 = "iqfeed_l1"
TIER_ALPACA_IEX = "alpaca_iex"
DEFAULT_TIERS = (TIER_IQFEED_L1, TIER_ALPACA_IEX)

# --- Derivation ---------------------------------------------------------------
_HELD_BBO_BOUNDS_WINDOW_MIN = 10          # ang '10 minutes' trailing bound ng bawat tape tier
_HELD_BBO_SIP_WITNESS_WINDOW_MIN = 5      # ang 5-min na bintana ng distribusyon D
_HELD_BBO_SIP_WITNESS_ROW_LIMIT = 3000
_HELD_BBO_BOUNDS_TTL_S = 60.0             # _FILL_READER_CAPABILITY_TTL_SECONDS precedent
# = ang fallback fresh bound (4.917 s), pinaikot pataas sa 10 ms: ang derivation
# ay hindi kailanman makakapagpatigil ng tick nang mas matagal kaysa sa hangganang
# hinahango nito.
_HELD_BBO_DERIVATION_STATEMENT_TIMEOUT_MS = 4920

# Nasukat 2026-09-10 17:30–17:40Z (RTH, live DB, bounded):
#   A. fenced delivery lag available_at − reference: n=81,147, p99.9 = 2.917 s
#   B. per-symbol inter-row gap (fenced):            n=81,651, p99   = 18.832 s
#   D. |L1_asof.bid − SIP.bid| / SIP.bid × 1e4:      n=1,356,  p99   = 66.22 bps
_FALLBACK_SOURCE = "measured_fallback_20260910T1740Z"
_FALLBACK_A_P999_S = 2.917
_FALLBACK_L1_FRESH_BOUND_S = round(_FALLBACK_A_P999_S + EVENT_TICK_MIN_SPACING_S, 3)  # 4.917
_FALLBACK_L1_GAP_CEILING_S = 18.832
_FALLBACK_SIP_DISAGREE_BAND_BPS = 66.22


def n_min_for_percentile(p: float) -> int:
    """ceil(1 / (1 − p)): sa ibaba nito ang isang percentile ay hindi sukat."""
    return int(math.ceil(1.0 / (1.0 - float(p))))


# Ang tatlong tanong, verbatim ang anyo ng §0 na sukat (percentile_cont, fenced
# basis filter, 10-min / 5-min na bintana). Bawat isa ay nagbabalik ng (n, value).
_SQL_A_DELIVERY_LAG_P999 = (
    "SELECT count(*) AS n, "
    "percentile_cont(0.999) WITHIN GROUP (ORDER BY "
    "EXTRACT(EPOCH FROM (available_at - provider_trade_reference_at))) AS p999 "
    "FROM momentum_nbbo_spread_tape "
    f"WHERE source = 'iqfeed_l1' AND timestamp_basis = '{L1_BASIS_FENCED}' "
    "AND message_type = 'Q' AND available_at IS NOT NULL "
    "AND provider_trade_reference_at IS NOT NULL "
    f"AND observed_at > now() - interval '{_HELD_BBO_BOUNDS_WINDOW_MIN} minutes'"
)
_SQL_B_INTER_ROW_GAP_P99 = (
    "WITH g AS ("
    "SELECT EXTRACT(EPOCH FROM (observed_at - LAG(observed_at) OVER "
    "(PARTITION BY symbol ORDER BY observed_at, id))) AS gap "
    "FROM momentum_nbbo_spread_tape "
    f"WHERE source = 'iqfeed_l1' AND timestamp_basis = '{L1_BASIS_FENCED}' "
    "AND message_type = 'Q' "
    f"AND observed_at > now() - interval '{_HELD_BBO_BOUNDS_WINDOW_MIN} minutes') "
    "SELECT count(*) AS n, percentile_cont(0.99) WITHIN GROUP (ORDER BY gap) AS p99 "
    "FROM g WHERE gap IS NOT NULL"
)


def _witness_disagreement_sql() -> str:
    """Distribusyon D: ang SIP-clocked tape row (saksi) LATERAL-joined sa
    pinakabagong L1 row <= SIP event time sa loob ng 60 s. Saksi lang ang SIP:
    hindi ito kailanman pinagmumulan ng desisyon, kaya dito lang nakatira ang
    pangalan ng source nito sa module na ito."""
    return (
        "WITH s AS ("
        "SELECT symbol, bid, provider_event_at FROM momentum_nbbo_spread_tape "
        "WHERE source LIKE 'massive_ws%' AND timestamp_basis = 'massive_sip_unix_ms' "
        "AND bridge_version = 'massive_ws_v2_sip_clock' AND message_type = 'Q' "
        "AND bid > 0 AND provider_event_at IS NOT NULL "
        f"AND observed_at > now() - interval '{_HELD_BBO_SIP_WITNESS_WINDOW_MIN} minutes' "
        f"ORDER BY observed_at DESC LIMIT {_HELD_BBO_SIP_WITNESS_ROW_LIMIT}), "
        "j AS (SELECT s.bid AS sip_bid, l.bid AS l1_bid FROM s CROSS JOIN LATERAL ("
        "SELECT bid FROM momentum_nbbo_spread_tape t "
        "WHERE t.symbol = s.symbol AND t.source = 'iqfeed_l1' AND t.message_type = 'Q' "
        f"AND t.timestamp_basis IN ('{L1_BASIS_FENCED}', '{L1_BASIS_OWN_CLOCK}') "
        "AND t.observed_at <= s.provider_event_at "
        "AND t.observed_at > s.provider_event_at - interval '60 seconds' "
        "ORDER BY t.observed_at DESC, t.id DESC LIMIT 1) l) "
        "SELECT count(*) AS n, percentile_cont(0.99) WITHIN GROUP "
        "(ORDER BY abs(l1_bid - sip_bid) / sip_bid * 1e4) AS p99 FROM j"
    )


@dataclass(frozen=True)
class HeldBboBounds:
    """Ang mga hangganang HINANGO (o tagged fallback) na ginagamit ng selector."""

    l1_fresh_bound_s: float          # A.p99.9 + EVENT_TICK_MIN_SPACING_S
    l1_gap_ceiling_s: float          # B.p99
    heartbeat_bound_s: float         # == l1_fresh_bound_s (parehong distribusyon)
    sip_disagree_band_bps: float     # D.p99
    delay_stamp_threshold_s: float   # == IQFEED_L1_RECEIVE_REFERENCE_FENCE_S, hindi hinahango
    derivation: dict = field(default_factory=dict)


@dataclass
class L1Read:
    """Ang sagot ng `AlpacaSpotAdapter._iqfeed_l1_read`: may dahilan, hindi hubad na None."""

    ticker: NormalizedTicker | None = None
    meta: FreshnessMeta | None = None
    reason: str | None = None
    basis: str | None = None
    event_reference_at: datetime | None = None
    received_at: datetime | None = None
    available_at: datetime | None = None
    tape_row_id: int | None = None
    bridge_version: str | None = None
    delay_signature_s: float | None = None


# Mga dahilang INFRA: hindi tungkol sa merkado, kaya hindi nagbibilang sa halt streak.
L1_INFRA_REASONS = frozenset({
    "bridge_build_unpinned",
    "bridge_build_mismatch",
    "provenance_rejected",
    "invalid_book",
    "clock_impossible",
    "read_failed",
    "reader_missing",
})


@dataclass
class HeldBboDecision:
    tick: NormalizedTicker | None
    snapshot: dict
    envelope: dict
    counts_toward_halt: bool


@dataclass
class HeldBboReads:
    """Injectable na mga reader. Ang default ay tumatawag sa adapter; ang tests ay fakes."""

    l1_read: Callable[[str, float], L1Read]
    heartbeat_age_s: Callable[[], float | None]
    contradicting_print: Callable[..., dict | None]
    sip_witness: Callable[[str], dict | None]
    l1_asof: Callable[[str, datetime], dict | None]
    direct: Callable[[str, float], tuple[Any, dict]]


ENTITLEMENT_REALTIME_BY_STAMP = "realtime_by_stamp"
ENTITLEMENT_REALTIME_BY_STAMP_AND_SIP = "realtime_by_stamp_and_sip_witness"
ENTITLEMENT_DELAYED_BY_STAMP = "delayed_by_stamp"
ENTITLEMENT_SUSPECT_BY_SIP = "suspect_by_sip_disagreement"
ENTITLEMENT_UNKNOWN_NO_L1_ROW = "unknown_no_l1_row"

HELD_BBO_RECEIPT_KEYS: tuple[str, ...] = (
    "bbo_selector_version",
    "bbo_source",
    "bbo_timestamp_basis",
    "bbo_quote_authority",
    "bbo_validity_rule",
    "bbo_age_s",
    "bbo_max_age_s",
    "bbo_bid",
    "bbo_ask",
    "bbo_tape_row_id",
    "bbo_event_at_utc",
    "bbo_received_at_utc",
    "bbo_available_at_utc",
    "bbo_read_at_utc",
    "bbo_fallback_engaged",
    "bbo_fallback_chain",
    "bbo_l1_entitlement_state",
    "bbo_l1_delay_signature_s",
    "bbo_l1_heartbeat_age_s",
    "bbo_sip_witness",
    "bbo_contradicting_print",
    "bbo_bounds",
    "counts_toward_halt",
)


# ---------------------------------------------------------------------------
# maliliit na helper
# ---------------------------------------------------------------------------
def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return None


def _jsonable(value: Any) -> Any:
    """datetime -> ISO, nested; lahat ng iba ay hinahayaan (JSON payload ang resibo)."""
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _symbol_of(product_id: Any) -> str:
    return str(product_id or "").strip().upper()


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _iex_direct_max_age_s() -> float:
    """Ang strict na HELD read ng kahapon, hindi binago: min(2.0, configured)."""
    try:
        configured = float(
            getattr(settings, "chili_momentum_entry_bbo_max_age_seconds", 2.0) or 2.0
        )
    except (TypeError, ValueError):
        configured = 2.0
    return min(2.0, max(0.0, configured))


def _witness_reader(adapter: Any) -> Callable[[str], dict | None]:
    """Ang SIP witness read ng adapter. Saksi LAMANG -- hindi kailanman pinagmumulan
    ng desisyon; sinusubok lang nito ang L1 (kaso (b): re-stamped delayed data ay
    hindi nakikita ng anumang orasan)."""
    fn = getattr(adapter, "_massive_sip_witness", None)

    def _read(sym: str) -> dict | None:
        if not callable(fn):
            return None
        try:
            out = fn(sym)
            return out if isinstance(out, dict) else None
        except Exception:
            _log.debug("[held_bbo] sip witness read failed sym=%s", sym, exc_info=True)
            return None

    return _read


def default_reads(adapter: Any) -> HeldBboReads:
    """Ang mga reader na nakatali sa adapter. Bawat isa ay fail-soft (None / reason)."""

    def _l1_read(sym: str, max_age_s: float) -> L1Read:
        fn = getattr(adapter, "_iqfeed_l1_read", None)
        if not callable(fn):
            return L1Read(reason="reader_missing")
        try:
            out = fn(sym, max_age_seconds=float(max_age_s))
        except Exception:
            _log.debug("[held_bbo] l1 read failed sym=%s", sym, exc_info=True)
            return L1Read(reason="read_failed")
        return out if isinstance(out, L1Read) else L1Read(reason="read_failed")

    def _heartbeat() -> float | None:
        fn = getattr(adapter, "_l1_feed_heartbeat_age_s", None)
        if not callable(fn):
            return None
        try:
            return _float_or_none(fn())
        except Exception:
            return None

    def _contradicting_print(sym: str, *, since_utc: datetime, bid: float, ask: float) -> dict | None:
        fn = getattr(adapter, "_l1_contradicting_print", None)
        if not callable(fn):
            return None
        try:
            out = fn(sym, since_utc=since_utc, bid=bid, ask=ask)
            return out if isinstance(out, dict) else None
        except Exception:
            _log.debug("[held_bbo] print read failed sym=%s", sym, exc_info=True)
            return None

    def _l1_asof(sym: str, at_utc: datetime) -> dict | None:
        fn = getattr(adapter, "_iqfeed_l1_asof", None)
        if not callable(fn):
            return None
        try:
            out = fn(sym, at_utc)
            return out if isinstance(out, dict) else None
        except Exception:
            return None

    def _direct(product_id: str, max_age_s: float) -> tuple[Any, dict]:
        # WALANG allow_stand_in: ito ang eksaktong strict na read ng kahapon.
        from .live_runner import _final_entry_bbo

        return _final_entry_bbo(adapter, product_id, max_age_seconds=float(max_age_s))

    return HeldBboReads(
        l1_read=_l1_read,
        heartbeat_age_s=_heartbeat,
        contradicting_print=_contradicting_print,
        sip_witness=_witness_reader(adapter),
        l1_asof=_l1_asof,
        direct=_direct,
    )


# ---------------------------------------------------------------------------
# ang selector
# ---------------------------------------------------------------------------
def _empty_envelope(*, now: datetime, bounds: HeldBboBounds) -> dict:
    env: dict[str, Any] = {k: None for k in HELD_BBO_RECEIPT_KEYS}
    env["bbo_selector_version"] = HELD_BBO_SELECTOR_VERSION
    env["bbo_read_at_utc"] = _iso(now)
    env["bbo_fallback_chain"] = []
    env["bbo_fallback_engaged"] = None
    env["bbo_l1_entitlement_state"] = ENTITLEMENT_UNKNOWN_NO_L1_ROW
    env["bbo_bounds"] = dict(bounds.derivation or {})
    env["counts_toward_halt"] = False
    return env


def select_held_bbo(
    adapter: Any,
    product_id: str,
    *,
    now: datetime,
    bounds: HeldBboBounds,
    tiers: tuple[str, ...] = DEFAULT_TIERS,
    reads: HeldBboReads | None = None,
) -> HeldBboDecision:
    """Tier 1 IQFeed L1 (fenced o own-clock, sariling reference clock), tier 2 strict
    direct IEX, tier 3 WALA. Tingnan ang module docstring at §4 ng spec."""
    sym = _symbol_of(product_id)
    now = _aware(now) or datetime.now(timezone.utc)
    reads = reads if reads is not None else default_reads(adapter)
    tiers = tuple(str(t) for t in (tiers or ()))
    env = _empty_envelope(now=now, bounds=bounds)
    chain: list[dict[str, Any]] = env["bbo_fallback_chain"]
    counts_toward_halt = False
    accepted_tick: NormalizedTicker | None = None
    snapshot: dict[str, Any] | None = None
    l1_refusal: str | None = None
    l1_age_s: float | None = None
    iex_refusal: str | None = None
    iex_age_s: float | None = None
    hb: float | None = None

    def _heartbeat_alive() -> tuple[float | None, bool]:
        age = reads.heartbeat_age_s()
        age = _float_or_none(age)
        return age, (age is not None and age <= float(bounds.heartbeat_bound_s))

    # ---- Tier 1: IQFeed L1 ------------------------------------------------
    if TIER_IQFEED_L1 in tiers:
        r = reads.l1_read(sym, float(bounds.l1_gap_ceiling_s))
        if not isinstance(r, L1Read):
            r = L1Read(reason="read_failed")
        entry: dict[str, Any] = {
            "tier": TIER_IQFEED_L1,
            "outcome": "refused",
            "reason": None,
            "age_s": None,
            "max_age_s": float(bounds.l1_gap_ceiling_s),
            "basis": r.basis,
            "tape_row_id": r.tape_row_id,
        }
        if r.event_reference_at is not None:
            l1_age_s = round((now - _aware(r.event_reference_at)).total_seconds(), 6)
            entry["age_s"] = l1_age_s
        if r.delay_signature_s is not None:
            env["bbo_l1_delay_signature_s"] = r.delay_signature_s
            entry["delay_signature_s"] = r.delay_signature_s
        reason = r.reason
        if reason in L1_INFRA_REASONS:
            l1_refusal = "l1_" + str(reason)
            entry["reason"] = l1_refusal
        elif reason == "no_row":
            hb, alive = _heartbeat_alive()
            l1_refusal = "l1_no_row"
            entry["reason"] = l1_refusal
            entry["heartbeat_age_s"] = hb
            counts_toward_halt = bool(alive)
        elif reason == "delayed_stamp":
            env["bbo_l1_entitlement_state"] = ENTITLEMENT_DELAYED_BY_STAMP
            l1_refusal = "l1_rejected_delayed_stamp"
            entry["reason"] = l1_refusal
        elif reason == "stale":
            hb, alive = _heartbeat_alive()
            l1_refusal = "l1_gap_ceiling_exceeded"
            entry["reason"] = l1_refusal
            entry["heartbeat_age_s"] = hb
            counts_toward_halt = bool(alive)
        elif reason is None and r.ticker is not None and r.event_reference_at is not None:
            age = float(l1_age_s if l1_age_s is not None else 0.0)
            rule: str | None = None
            max_age: float | None = None
            if age <= float(bounds.l1_fresh_bound_s):
                rule, max_age = "l1_fresh", float(bounds.l1_fresh_bound_s)
            else:
                hb, alive = _heartbeat_alive()
                entry["heartbeat_age_s"] = hb
                if not alive:
                    l1_refusal = "l1_feed_stalled"
                    entry["reason"] = l1_refusal
                else:
                    cp = reads.contradicting_print(
                        sym,
                        since_utc=_aware(r.event_reference_at),
                        bid=float(r.ticker.bid),
                        ask=float(r.ticker.ask),
                    )
                    if isinstance(cp, dict) and cp:
                        env["bbo_contradicting_print"] = _jsonable(cp)
                        entry["contradicting_print"] = _jsonable(cp)
                        l1_refusal = "l1_contradicted_by_print"
                        entry["reason"] = l1_refusal
                    else:
                        rule, max_age = "l1_unchanged_book", float(bounds.l1_gap_ceiling_s)
            if rule is not None and max_age is not None:
                # SIP witness cross-check (kaso (b)): ang re-stamped na delayed data
                # ay hindi nakikita ng anumang orasan; ang saksi lang ang makakakita.
                entitlement = ENTITLEMENT_REALTIME_BY_STAMP
                w = reads.sip_witness(sym)
                if isinstance(w, dict) and w:
                    w_at = _aware(w.get("provider_event_at"))
                    w_bid = _float_or_none(w.get("bid"))
                    l1_at = reads.l1_asof(sym, w_at) if w_at is not None else None
                    l1_at_bid = _float_or_none((l1_at or {}).get("bid")) if isinstance(l1_at, dict) else None
                    witness_out: dict[str, Any] = {**_jsonable(w)}
                    if l1_at_bid is not None and w_bid is not None and w_bid > 0:
                        diff_bps = abs(l1_at_bid - w_bid) / w_bid * 1e4
                        witness_out["l1_asof"] = _jsonable(l1_at)
                        witness_out["diff_bps"] = round(diff_bps, 4)
                        witness_out["band_bps"] = float(bounds.sip_disagree_band_bps)
                        if diff_bps > float(bounds.sip_disagree_band_bps):
                            entitlement = ENTITLEMENT_SUSPECT_BY_SIP
                            l1_refusal = "l1_rejected_sip_disagreement"
                            entry["reason"] = l1_refusal
                            entry["witness"] = witness_out
                        else:
                            entitlement = ENTITLEMENT_REALTIME_BY_STAMP_AND_SIP
                    env["bbo_sip_witness"] = witness_out
                env["bbo_l1_entitlement_state"] = entitlement
                if l1_refusal is None:
                    authority = (
                        AUTHORITY_L1_FENCED
                        if str(r.basis or "") == L1_BASIS_FENCED
                        else AUTHORITY_L1_OWN_CLOCK
                    )
                    accepted_tick, snapshot = _accept_l1(
                        r, sym=sym, authority=authority, rule=rule, age=age, max_age=max_age
                    )
                    entry.update({"outcome": "answered", "reason": rule, "max_age_s": max_age})
                    env.update({
                        "bbo_source": snapshot["source"],
                        "bbo_timestamp_basis": snapshot["timestamp_basis"],
                        "bbo_quote_authority": authority,
                        "bbo_validity_rule": rule,
                        "bbo_age_s": snapshot["age_seconds"],
                        "bbo_max_age_s": max_age,
                        "bbo_bid": snapshot["bid"],
                        "bbo_ask": snapshot["ask"],
                        "bbo_tape_row_id": snapshot["tape_row_id"],
                        "bbo_event_at_utc": snapshot["provider_event_at_utc"],
                        "bbo_received_at_utc": snapshot["received_at_utc"],
                        "bbo_available_at_utc": snapshot["available_at_utc"],
                    })
        else:
            l1_refusal = "l1_" + str(reason or "read_failed")
            entry["reason"] = l1_refusal
        env["bbo_l1_heartbeat_age_s"] = hb
        chain.append(entry)

    # ---- Tier 2: direct IEX, strict (walang stand-in) ----------------------
    if accepted_tick is None and TIER_ALPACA_IEX in tiers:
        max_age = _iex_direct_max_age_s()
        try:
            tick, snap = reads.direct(product_id, max_age)
        except Exception:
            _log.debug("[held_bbo] direct read failed sym=%s", sym, exc_info=True)
            tick, snap = None, {"ok": False, "reason": "execution_bbo_read_failed"}
        snap = dict(snap) if isinstance(snap, dict) else {"ok": False, "reason": "execution_bbo_unavailable"}
        iex_age_s = _float_or_none(snap.get("age_seconds"))
        entry = {
            "tier": TIER_ALPACA_IEX,
            "outcome": "refused",
            "reason": None,
            "age_s": iex_age_s,
            "max_age_s": _float_or_none(snap.get("max_age_seconds")) or max_age,
            "source": snap.get("source"),
        }
        if tick is not None:
            accepted_tick, snapshot = tick, snap
            entry.update({"outcome": "answered", "reason": "iex_direct"})
            env.update({
                "bbo_source": snap.get("source"),
                "bbo_timestamp_basis": snap.get("timestamp_basis"),
                "bbo_quote_authority": snap.get("quote_authority") or AUTHORITY_ALPACA_DIRECT,
                "bbo_validity_rule": "iex_direct",
                "bbo_age_s": iex_age_s,
                "bbo_max_age_s": entry["max_age_s"],
                "bbo_bid": snap.get("bid"),
                "bbo_ask": snap.get("ask"),
                "bbo_tape_row_id": snap.get("tape_row_id"),
                "bbo_event_at_utc": snap.get("provider_event_at_utc"),
                "bbo_received_at_utc": snap.get("received_at_utc"),
                "bbo_available_at_utc": snap.get("available_at_utc"),
            })
        else:
            iex_refusal = "iex_" + str(snap.get("reason") or "unavailable")
            entry["reason"] = iex_refusal
            entry["unavailable_kind"] = snap.get("unavailable_kind")
            # Ang lumang halt mapping ay nagbibilang ng `execution_bbo_stale` ng
            # direct read (:33490); dala pa rin iyon dito para hindi mawala ang
            # halt detection kapag INFRA ang dahilan ng L1 (counts False).
            if str(snap.get("reason") or "") == "execution_bbo_stale":
                counts_toward_halt = True
        chain.append(entry)

    # ---- Tier 3: WALA. Hindi kailanman ang SIP-clocked row, ang own-clock via
    # get_execution_bbo, ang trade-tick BBO, o ang L2. Bumabagsak sa deadman. ----
    env["bbo_fallback_engaged"] = (
        None if not chain
        else (accepted_tick is None or str(chain[-1].get("tier")) != TIER_IQFEED_L1)
    )
    env["counts_toward_halt"] = bool(counts_toward_halt)
    if accepted_tick is None or snapshot is None:
        unavailable_kind = "|".join(x for x in (l1_refusal, iex_refusal) if x) or "held_bbo_no_tier"
        snapshot = {
            "ok": False,
            "reason": "held_bbo_unavailable",
            "symbol": sym,
            "source": HELD_BBO_SELECTOR_VERSION,
            "unavailable_kind": unavailable_kind,
            "l1_refusal": l1_refusal,
            "iex_refusal": iex_refusal,
            "age_seconds": l1_age_s if l1_age_s is not None else iex_age_s,
            "max_age_seconds": float(bounds.l1_gap_ceiling_s),
        }
        return HeldBboDecision(tick=None, snapshot=snapshot, envelope=env, counts_toward_halt=bool(counts_toward_halt))
    return HeldBboDecision(tick=accepted_tick, snapshot=snapshot, envelope=env, counts_toward_halt=bool(counts_toward_halt))


def _accept_l1(
    r: L1Read, *, sym: str, authority: str, rule: str, age: float, max_age: float
) -> tuple[NormalizedTicker, dict[str, Any]]:
    """Ang accept payload ay may PAREHONG mga susi na inilalabas ng
    `_final_entry_bbo` (:21863-21908) para tuloy ang `le['exit_final_bbo']`,
    `_no_bbo_run_observe` at ang blocked receipt."""
    tick = r.ticker
    assert tick is not None
    bid = float(tick.bid)
    ask = float(tick.ask)
    mid = float(tick.mid) if tick.mid is not None else (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10_000.0 if mid > 0 else 0.0
    raw = dict(tick.raw) if isinstance(tick.raw, dict) else {}
    meta = FreshnessMeta(
        retrieved_at_utc=_aware(r.received_at) or (r.meta.retrieved_at_utc if r.meta else datetime.now(timezone.utc)),
        provider_time_utc=_aware(r.event_reference_at),
        max_age_seconds=float(max_age),
    )
    tick = replace(tick, freshness=meta)
    snapshot = {
        "ok": True,
        "reason": "execution_bbo_ok",
        "symbol": sym,
        "source": str(raw.get("feed") or "iqfeed_l1"),
        "tape_row_id": r.tape_row_id,
        "provider_event_at_utc": _iso(r.event_reference_at),
        "received_at_utc": _iso(r.received_at),
        "available_at_utc": _iso(r.available_at),
        "capture_event_sha256": raw.get("capture_event_sha256"),
        "capture_content_sha256": raw.get("capture_content_sha256"),
        "capture_sequence": raw.get("capture_sequence"),
        "timestamp_basis": str(r.basis or raw.get("timestamp_basis") or ""),
        "quote_authority": authority,
        "age_seconds": round(float(age), 6),
        "max_age_seconds": float(max_age),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread_bps": round(spread_bps, 4),
        "event_reference_at_utc": _iso(r.event_reference_at),
        "delay_signature_s": r.delay_signature_s,
        "validity_rule": rule,
    }
    return tick, snapshot


def held_bbo_receipt_fields(le: Any) -> dict[str, Any]:
    """Ang bbo_* na mga susi mula sa `le['last_held_execution_bbo']` para sa bawat resibo.

    Fail-open: hindi kailanman nagtataas -- ang resibo ay hindi dapat ang pumigil sa exit."""
    try:
        env = le.get("last_held_execution_bbo") if isinstance(le, dict) else None
        if not isinstance(env, dict) or "bbo_selector_version" not in env:
            return {
                "bbo_source": None,
                "bbo_age_s": None,
                "bbo_fallback_engaged": None,
                "bbo_receipt": "no_held_bbo_envelope",
            }
        return {k: env.get(k) for k in HELD_BBO_RECEIPT_KEYS}
    except Exception:
        return {
            "bbo_source": None,
            "bbo_age_s": None,
            "bbo_fallback_engaged": None,
            "bbo_receipt": "no_held_bbo_envelope",
        }


# ---------------------------------------------------------------------------
# bounds: derivation + cache
# ---------------------------------------------------------------------------
def _bound_record(
    *,
    distribution: str,
    window_min: int | None,
    n: int | None,
    n_min: int | None,
    percentile: float | None,
    value: float,
    source: str,
    measured_at_utc: str | None,
) -> dict[str, Any]:
    return {
        "distribution": distribution,
        "window_min": window_min,
        "n": n,
        "n_min": n_min,
        "percentile": percentile,
        "value": value,
        "source": source,
        "measured_at_utc": measured_at_utc,
    }


def fallback_bounds(*, now: datetime | None = None) -> HeldBboBounds:
    """Ang mga halagang nasukat 2026-09-10 17:40Z, nakatatak ang pinagmulan."""
    return _assemble_bounds({}, measured_at_utc=_iso(now or datetime.now(timezone.utc)))


def _assemble_bounds(results: dict[str, tuple[int, float | None] | None], *, measured_at_utc: str | None) -> HeldBboBounds:
    def _pick(name: str, *, percentile: float, fallback: float, transform=None) -> tuple[float, dict]:
        n_min = n_min_for_percentile(percentile)
        got = results.get(name)
        n = int(got[0]) if got is not None else None
        raw_value = got[1] if got is not None else None
        value = None
        if got is not None and n is not None and n >= n_min and raw_value is not None:
            v = _float_or_none(raw_value)
            if v is not None and v >= 0.0:
                value = transform(v) if transform is not None else v
        if value is None or not math.isfinite(value) or value <= 0.0:
            return float(fallback), {"n": n, "n_min": n_min, "source": _FALLBACK_SOURCE}
        return round(float(value), 3), {"n": n, "n_min": n_min, "source": "runtime"}

    fresh, fresh_meta = _pick(
        "A", percentile=0.999, fallback=_FALLBACK_L1_FRESH_BOUND_S,
        transform=lambda p: p + EVENT_TICK_MIN_SPACING_S,
    )
    gap, gap_meta = _pick("B", percentile=0.99, fallback=_FALLBACK_L1_GAP_CEILING_S)
    band, band_meta = _pick("D", percentile=0.99, fallback=_FALLBACK_SIP_DISAGREE_BAND_BPS)
    derivation = {
        "l1_fresh_bound_s": _bound_record(
            distribution=(
                "fenced_l1_delivery_lag_s(available_at - provider_trade_reference_at)"
                f".p99.9 + EVENT_TICK_MIN_SPACING_S({EVENT_TICK_MIN_SPACING_S})"
            ),
            window_min=_HELD_BBO_BOUNDS_WINDOW_MIN, percentile=0.999, value=fresh,
            measured_at_utc=measured_at_utc, **fresh_meta,
        ),
        "l1_gap_ceiling_s": _bound_record(
            distribution="fenced_l1_per_symbol_inter_row_gap_s.p99",
            window_min=_HELD_BBO_BOUNDS_WINDOW_MIN, percentile=0.99, value=gap,
            measured_at_utc=measured_at_utc, **gap_meta,
        ),
        "heartbeat_bound_s": _bound_record(
            distribution="== l1_fresh_bound_s (feed alive iff now - max(received_at) <= one delivery p99.9 + one tick)",
            window_min=_HELD_BBO_BOUNDS_WINDOW_MIN, percentile=0.999, value=fresh,
            measured_at_utc=measured_at_utc, **fresh_meta,
        ),
        "sip_disagree_band_bps": _bound_record(
            distribution="abs(l1_asof.bid - sip.bid) / sip.bid * 1e4 over SIP-clocked tape rows.p99 (witness only)",
            window_min=_HELD_BBO_SIP_WITNESS_WINDOW_MIN, percentile=0.99, value=band,
            measured_at_utc=measured_at_utc, **band_meta,
        ),
        "delay_stamp_threshold_s": _bound_record(
            distribution="IQFEED_L1_RECEIVE_REFERENCE_FENCE_S (bridge fence, not derived)",
            window_min=None, n=None, n_min=None, percentile=None,
            value=float(IQFEED_L1_RECEIVE_REFERENCE_FENCE_S),
            source="constant_bridge_fence", measured_at_utc=measured_at_utc,
        ),
    }
    return HeldBboBounds(
        l1_fresh_bound_s=fresh,
        l1_gap_ceiling_s=gap,
        heartbeat_bound_s=fresh,
        sip_disagree_band_bps=band,
        delay_stamp_threshold_s=float(IQFEED_L1_RECEIVE_REFERENCE_FENCE_S),
        derivation=derivation,
    )


_BOUND_QUERIES: tuple[tuple[str, str], ...] = (
    ("A", _SQL_A_DELIVERY_LAG_P999),
    ("B", _SQL_B_INTER_ROW_GAP_P99),
    ("D", _witness_disagreement_sql()),
)


def derive_held_bbo_bounds(session_factory: Callable[[], Any], *, now: datetime | None = None) -> HeldBboBounds:
    """Tatlong bounded na tanong sa iisang transaksyon, `SET LOCAL statement_timeout`
    = ang fallback fresh bound. Anumang pagkabigo / timeout / n < n_min ay
    bumabagsak sa tagged na fallback PARA SA HANGGANANG IYON. Hindi nagtataas."""
    from sqlalchemy import text

    now = now or datetime.now(timezone.utc)
    results: dict[str, tuple[int, float | None] | None] = {}
    try:
        with session_factory() as db:
            try:
                db.execute(text(
                    f"SET LOCAL statement_timeout = {int(_HELD_BBO_DERIVATION_STATEMENT_TIMEOUT_MS)}"
                ))
            except Exception:
                _log.debug("[held_bbo] SET LOCAL statement_timeout failed", exc_info=True)
            for name, sql in _BOUND_QUERIES:
                try:
                    row = db.execute(text(sql)).fetchone()
                    if row is None:
                        results[name] = None
                        continue
                    n = int(row[0] or 0)
                    value = _float_or_none(row[1])
                    results[name] = (n, value)
                except Exception:
                    # Kasama ang statement_timeout: ang transaksyon ay aborted
                    # pagkatapos nito, kaya ang mga susunod ay babagsak din sa
                    # fallback -- ayon sa disenyo (bawat hangganan ay may tag).
                    _log.debug("[held_bbo] bound query %s failed", name, exc_info=True)
                    results[name] = None
            try:
                db.rollback()
            except Exception:
                pass
    except Exception:
        _log.debug("[held_bbo] bounds derivation session failed", exc_info=True)
    return _assemble_bounds(results, measured_at_utc=_iso(now))


_BOUNDS_LOCK = threading.Lock()
_BOUNDS_CACHE: dict[str, Any] = {"bounds": None, "at_monotonic": None, "refreshing": False}


def reset_bounds_cache() -> None:
    with _BOUNDS_LOCK:
        _BOUNDS_CACHE.update({"bounds": None, "at_monotonic": None, "refreshing": False})


def _derive_with_session_local(now: datetime | None) -> HeldBboBounds:
    from ....db import SessionLocal

    return derive_held_bbo_bounds(SessionLocal, now=now)


def _refresh_in_background() -> None:
    try:
        fresh = _derive_with_session_local(None)
        with _BOUNDS_LOCK:
            _BOUNDS_CACHE["bounds"] = fresh
            _BOUNDS_CACHE["at_monotonic"] = time.monotonic()
    except Exception:
        _log.debug("[held_bbo] background bounds refresh failed", exc_info=True)
    finally:
        with _BOUNDS_LOCK:
            _BOUNDS_CACHE["refreshing"] = False


def _start_background_refresh() -> None:
    threading.Thread(
        target=_refresh_in_background, name="held-bbo-bounds-refresh", daemon=True
    ).start()


def current_bounds(*, now: datetime | None = None) -> HeldBboBounds:
    """Ang mga hangganan para sa tick na ito. TTL 60 s; hindi kailanman nagtataas.

    Kapag lumipas ang TTL at MAY naka-cache: ibinabalik agad ang naka-cache
    (stale-while-revalidate) at isang background thread ang nagre-refresh --
    walang HELD tick na naghihintay sa derivation. Ang UNANG tawag lamang (walang
    cache) ang nagde-derive inline, bounded ng statement_timeout = 4.92 s."""
    try:
        mono = time.monotonic()
        with _BOUNDS_LOCK:
            cached = _BOUNDS_CACHE.get("bounds")
            at = _BOUNDS_CACHE.get("at_monotonic")
            fresh_enough = (
                cached is not None and at is not None and (mono - float(at)) < _HELD_BBO_BOUNDS_TTL_S
            )
            if fresh_enough:
                return cached
            if cached is not None and not _BOUNDS_CACHE.get("refreshing"):
                _BOUNDS_CACHE["refreshing"] = True
                spawn = True
            else:
                spawn = False
        if cached is not None:
            if spawn:
                try:
                    _start_background_refresh()
                except Exception:
                    with _BOUNDS_LOCK:
                        _BOUNDS_CACHE["refreshing"] = False
            return cached
        derived = _derive_with_session_local(now)
        with _BOUNDS_LOCK:
            _BOUNDS_CACHE["bounds"] = derived
            _BOUNDS_CACHE["at_monotonic"] = time.monotonic()
        return derived
    except Exception:
        _log.debug("[held_bbo] current_bounds failed; fallback", exc_info=True)
        return fallback_bounds(now=now)
