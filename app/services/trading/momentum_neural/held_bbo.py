"""HELD-tick execution BBO selector — IQFeed L1 first, direct IEX second, a labelled
SIP-clocked floor third and ONLY while the resting broker deadman cannot fire.

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

ANG DOKTRINA (inayos sa review, 2026-09-10 gabi): ang HELD tick ay nagbabasa ng
IQFeed L1 (fenced o own-clock, hinahatulan sa SARILING event-reference clock nito
laban sa SARILING fresh bound ng basis), tapos ang strict na direct IEX. Ang
SIP witness ay NAGBABABA ng label (`suspect_by_sip_disagreement`), hindi
nagve-veto: ang isang L1 row na pinagdudahan ng saksi -- o isang `l1_unchanged_book`
na row -- ay DEMOTED sa likod ng IEX at sumasagot lamang kapag walang mas sariwang
tier. Dahilan: ang saksi ay 5.2 s p50 / 6.9-8.4 s p99 na huli, at ang as-of L1 na
ikinukumpara rito ay may quote content na hanggang 2 s mas bago kaysa sa fenced
observed_at (received − observed hanggang 1.9999 s sa live DB) -- kaya sa isang
gap-down ang saksi ang mali at ang sariwang L1 ang tama, at ang veto ay bumubulag
sa lane sa eksaktong sandaling kailangan nito ang libro.

ANG SAHIG. Sa REGULAR session ang nakapahingang broker deadman stop ay makakaputok,
kaya kapag tumanggi ang L1 at IEX ay `tick=None` at ang `tick_live_session` ay
bumabalik BAGO ang HWM ratchet at exit ladder -- bumabagsak PATUNGO sa deadman.
Sa LABAS ng regular session ang premise na iyon ay MALI: ang Alpaca ay tumatanggap
lamang ng limit order sa extended hours, kaya ang stop ay `status=new` hanggang sa
open (`live_deadman_stop_inert_until_rth`, 34 event / 16 session sa 3 araw) at ang
runner ang TANGING proteksyon. Doon ang tier 3 ang sahig: ang SIP-clocked tape row
sa ilalim ng SARILING kontrata nito (ang configured SIP ceiling, hindi ang
900-s ladder), naka-label sa sarili nitong authority, at may `bbo_floor_gate`
sa resibo na nagsasabi KUNG BAKIT ito tinanong. Kapag buhay ang deadman
(`_deadman_protection_is_live`), hindi ito tinatanong at ang chain ay nagtatala
ng `skipped: resting_deadman_live`.

WALANG MAGIC NUMBER: bawat hangganan ay hinango mula sa isang pinangalanang
distribusyon ng L1 tape (10-min trailing window, TTL 60 s), at ang binding
value ay nasa BAWAT resibo (`bbo_bounds`) -- kasama ang mga konstanteng DALA
(bridge fence, future tolerance, strict IEX ceiling) na nakatatak bilang
`constant_*`. Kapag hindi masukat (n < n_min, timeout, walang DB), ang halagang
nasukat 2026-09-10 ang pumapalit at NAKATATAK ang pinagmulan
(`measured_fallback_20260910T1740Z`, `measured_fallback_20260910T1913Z`).

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
# Tier 3: ang SIP-clocked tape row sa ilalim ng SARILING kontrata nito. Tinatanong
# LAMANG kapag hindi makakaputok ang nakapahingang broker deadman (labas ng
# regular session) -- tingnan ang `resting_floor_live` ng `select_held_bbo`.
TIER_SIP_CLOCKED_FLOOR = "sip_clocked_floor"
DEFAULT_TIERS = (TIER_IQFEED_L1, TIER_ALPACA_IEX, TIER_SIP_CLOCKED_FLOOR)
# Ang apat na PROTECTIVE exit-PRICING site: bumagsak na ang strict IEX bago
# umabot doon, kaya L1 tapos ang SIP-clocked floor (laging pinapayagan: presyo
# ito ng exit na NAPAGPASYAHAN na, at ang alternatibo ay ang 900-s ladder).
EXIT_PRICING_TIERS = (TIER_IQFEED_L1, TIER_SIP_CLOCKED_FLOOR)

# Ang mga validity rule na maaaring lumabas sa `bbo_validity_rule`.
RULE_L1_FRESH = "l1_fresh"
RULE_L1_UNCHANGED_BOOK = "l1_unchanged_book"
RULE_L1_SUSPECT = "l1_suspect_by_sip_witness"
RULE_L1_UNCHANGED_BOOK_SUSPECT = "l1_unchanged_book_suspect_by_sip_witness"
RULE_IEX_DIRECT = "iex_direct"
RULE_SIP_CLOCKED_FLOOR = "sip_clocked_floor"
# Ang dalawang dahilan ng DEMOTION (hindi veto) ng isang L1 row.
DEMOTION_UNCHANGED_BOOK = "unchanged_book"
DEMOTION_SIP_WITNESS = "sip_witness_disagreement"

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
# Nasukat 2026-09-10 19:13Z (RTH, live DB, 10-min window, statement_timeout 20 s):
#   A2. own-clock delivery lag available_at − provider_event_at:
#       n=30,937, p99 = 1.272 s, p99.9 = 2.337 s, max = 2.572 s
#       (received_at − provider_event_at p99.9 = 1.985 s sa parehong hilera).
# Ang own-clock na row ay may SARILING distribusyon; ang fenced bound (2.917)
# ay hindi nito sukat (review finding: ang isang own-clock row ay tinatanggap
# noon bilang 'fresh' nang ~0.6-2.3 s mas matagal kaysa sa sarili nitong lag).
_FALLBACK_SOURCE_A2 = "measured_fallback_20260910T1913Z"
_FALLBACK_A2_P999_S = 2.337
_FALLBACK_L1_OWN_CLOCK_FRESH_BOUND_S = round(_FALLBACK_A2_P999_S + EVENT_TICK_MIN_SPACING_S, 3)  # 4.337


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
# A2 (review fix): ang own-clock na row ay hinahatulan sa SARILING delivery-lag
# distribusyon, hindi sa fenced na A. Parehong anyo ng A, ibang basis at ibang
# event reference (provider_event_at = Bid/Ask Time ng IQFeed).
_SQL_A2_OWN_CLOCK_DELIVERY_LAG_P999 = (
    "SELECT count(*) AS n, "
    "percentile_cont(0.999) WITHIN GROUP (ORDER BY "
    "EXTRACT(EPOCH FROM (available_at - provider_event_at))) AS p999 "
    "FROM momentum_nbbo_spread_tape "
    f"WHERE source = 'iqfeed_l1' AND timestamp_basis = '{L1_BASIS_OWN_CLOCK}' "
    "AND message_type = 'Q' AND available_at IS NOT NULL "
    "AND provider_event_at IS NOT NULL "
    f"AND observed_at > now() - interval '{_HELD_BBO_BOUNDS_WINDOW_MIN} minutes'"
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

    l1_fresh_bound_s: float          # A.p99.9 + EVENT_TICK_MIN_SPACING_S (fenced rows)
    l1_gap_ceiling_s: float          # B.p99
    heartbeat_bound_s: float         # == l1_fresh_bound_s (parehong distribusyon)
    sip_disagree_band_bps: float     # D.p99
    delay_stamp_threshold_s: float   # == IQFEED_L1_RECEIVE_REFERENCE_FENCE_S, hindi hinahango
    # A2.p99.9 + EVENT_TICK_MIN_SPACING_S (own-clock rows). None = walang
    # sariling sukat (lumang caller) -> ang fenced bound ang gagamitin, receipted.
    l1_own_clock_fresh_bound_s: float | None = None
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
    # Tier 3 (review fix): ang SIP-clocked tape row sa ilalim ng SARILING kontrata,
    # (tick, payload) o None. Ang adapter ang naglalagay ng source / authority /
    # max_age sa payload -- ang module na ito ay hindi nagpapangalan ng tape tier.
    sip_clocked_floor: Callable[[str], tuple[Any, dict] | None] | None = None


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
    # review fixes: aling tier ang sumagot; bakit (hindi) tinanong ang floor
    "bbo_answering_tier",
    "bbo_floor_gate",
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

    def _sip_clocked_floor(sym: str) -> tuple[Any, dict] | None:
        fn = getattr(adapter, "_sip_clocked_floor_quote", None)
        if not callable(fn):
            return None
        try:
            out = fn(sym)
        except Exception:
            _log.debug("[held_bbo] floor read failed sym=%s", sym, exc_info=True)
            return None, {"ok": False, "reason": "read_failed"}
        if isinstance(out, tuple) and len(out) == 2 and isinstance(out[1], dict):
            return out
        return None, {"ok": False, "reason": "read_failed"}

    return HeldBboReads(
        l1_read=_l1_read,
        heartbeat_age_s=_heartbeat,
        contradicting_print=_contradicting_print,
        sip_witness=_witness_reader(adapter),
        l1_asof=_l1_asof,
        direct=_direct,
        sip_clocked_floor=_sip_clocked_floor,
    )


def bounds_derivable(adapter: Any) -> bool:
    """May L1 reader ba ang adapter? Kung wala (replay `MockBrokerAdapter`, bench
    fakes) ay walang saysay ang derivation: hindi maaapektuhan ng hangganan ang
    anumang desisyon, kaya hindi dapat magpasimula ng DB read o daemon thread."""
    return callable(getattr(adapter, "_iqfeed_l1_read", None))


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


def _own_clock_fresh_bound_s(bounds: HeldBboBounds) -> float:
    v = _float_or_none(getattr(bounds, "l1_own_clock_fresh_bound_s", None))
    return float(v) if v is not None and v > 0.0 else float(bounds.l1_fresh_bound_s)


def select_held_bbo(
    adapter: Any,
    product_id: str,
    *,
    now: datetime,
    bounds: HeldBboBounds,
    tiers: tuple[str, ...] = DEFAULT_TIERS,
    reads: HeldBboReads | None = None,
    resting_floor_live: bool | None = None,
    floor_gate: dict | None = None,
) -> HeldBboDecision:
    """Tier 1 IQFeed L1 (fenced o own-clock, sariling reference clock at sariling
    fresh bound), tier 2 strict direct IEX, tier 3 ang SIP-clocked floor -- LAMANG
    kapag `resting_floor_live` ay hindi True (hindi makakaputok ang broker deadman;
    `None` = hindi malaman = fail-suspicious gaya ng `_deadman_protection_is_live`).

    Isang L1 row na `l1_unchanged_book` (lampas sa fresh bound, buhay ang feed,
    walang kumokontrang print) o pinagdudahan ng SIP witness ay HINDI tinatanggihan:
    DEMOTED ito sa likod ng IEX at sumasagot kapag walang mas sariwang tier.
    Ang `floor_gate` ay ang ebidensya ng gate (nasa resibo bilang `bbo_floor_gate`).
    Tingnan ang module docstring."""
    sym = _symbol_of(product_id)
    now = _aware(now) or datetime.now(timezone.utc)
    reads = reads if reads is not None else default_reads(adapter)
    tiers = tuple(str(t) for t in (tiers or ()))
    env = _empty_envelope(now=now, bounds=bounds)
    env["bbo_floor_gate"] = (
        _jsonable(dict(floor_gate))
        if isinstance(floor_gate, dict) and floor_gate
        else {"resting_floor_live": resting_floor_live, "source": "caller"}
    )
    chain: list[dict[str, Any]] = env["bbo_fallback_chain"]
    counts_toward_halt = False
    accepted_tick: NormalizedTicker | None = None
    accepted_rule: str | None = None
    snapshot: dict[str, Any] | None = None
    l1_refusal: str | None = None
    l1_age_s: float | None = None
    iex_refusal: str | None = None
    iex_age_s: float | None = None
    floor_refusal: str | None = None
    hb: float | None = None
    # Ang DEMOTED na L1 candidate: sumasagot lamang kapag walang mas sariwang tier.
    l1_candidate: dict[str, Any] | None = None

    def _heartbeat_alive() -> tuple[float | None, bool]:
        age = reads.heartbeat_age_s()
        age = _float_or_none(age)
        return age, (age is not None and age <= float(bounds.heartbeat_bound_s))

    def _accept(tick: NormalizedTicker, snap: dict[str, Any], *, tier: str, rule: str, authority: Any) -> None:
        nonlocal accepted_tick, accepted_rule, snapshot
        accepted_tick, accepted_rule, snapshot = tick, rule, snap
        env.update({
            "bbo_source": snap.get("source"),
            "bbo_timestamp_basis": snap.get("timestamp_basis"),
            "bbo_quote_authority": authority,
            "bbo_validity_rule": rule,
            "bbo_age_s": snap.get("age_seconds"),
            "bbo_max_age_s": snap.get("max_age_seconds"),
            "bbo_bid": snap.get("bid"),
            "bbo_ask": snap.get("ask"),
            "bbo_tape_row_id": snap.get("tape_row_id"),
            "bbo_event_at_utc": snap.get("provider_event_at_utc"),
            "bbo_received_at_utc": snap.get("received_at_utc"),
            "bbo_available_at_utc": snap.get("available_at_utc"),
            "bbo_answering_tier": tier,
        })

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
            basis = str(r.basis or "")
            # Bawat basis ay hinahatulan sa SARILING delivery-lag distribusyon (A vs A2).
            fresh_bound = (
                _own_clock_fresh_bound_s(bounds)
                if basis == L1_BASIS_OWN_CLOCK
                else float(bounds.l1_fresh_bound_s)
            )
            entry["fresh_bound_s"] = fresh_bound
            rule: str | None = None
            max_age: float | None = None
            demotions: list[str] = []
            if age <= fresh_bound:
                rule, max_age = RULE_L1_FRESH, fresh_bound
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
                        # Tahimik na libro, buhay na feed, walang kumokontrang
                        # print: tinatanggap -- pero DEMOTED sa likod ng IEX
                        # (review fix): isang buhay na IEX tick ang saksi na
                        # hindi kayang ibigay ng patay na per-symbol na watch.
                        rule, max_age = RULE_L1_UNCHANGED_BOOK, float(bounds.l1_gap_ceiling_s)
                        demotions.append(DEMOTION_UNCHANGED_BOOK)
            if rule is not None and max_age is not None:
                # SIP witness cross-check (kaso (b)): ang re-stamped na delayed data
                # ay hindi nakikita ng anumang orasan; ang saksi lang ang makakakita.
                # Review fix: NAGBABABA ng label at nagde-demote, HINDI nagve-veto --
                # ang saksi ay 5-8 s na huli at ang as-of na L1 ay hanggang 2 s mas
                # bago ang content kaysa sa observed_at, kaya sa gap-down ang saksi
                # ang mali. Ang pinagdudahang row ay sumasagot lamang kapag walang
                # strict IEX.
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
                            demotions.append(DEMOTION_SIP_WITNESS)
                            entry["witness"] = witness_out
                        else:
                            entitlement = ENTITLEMENT_REALTIME_BY_STAMP_AND_SIP
                    env["bbo_sip_witness"] = witness_out
                env["bbo_l1_entitlement_state"] = entitlement
                authority = (
                    AUTHORITY_L1_FENCED
                    if basis == L1_BASIS_FENCED
                    else AUTHORITY_L1_OWN_CLOCK
                )
                if not demotions:
                    tick, snap = _accept_l1(
                        r, sym=sym, authority=authority, rule=rule, age=age, max_age=max_age
                    )
                    _accept(tick, snap, tier=TIER_IQFEED_L1, rule=rule, authority=authority)
                    entry.update({"outcome": "answered", "reason": rule, "max_age_s": max_age})
                else:
                    if DEMOTION_SIP_WITNESS in demotions and DEMOTION_UNCHANGED_BOOK in demotions:
                        final_rule = RULE_L1_UNCHANGED_BOOK_SUSPECT
                    elif DEMOTION_SIP_WITNESS in demotions:
                        final_rule = RULE_L1_SUSPECT
                    else:
                        final_rule = RULE_L1_UNCHANGED_BOOK
                    l1_candidate = {
                        "read": r, "authority": authority, "rule": final_rule,
                        "age": age, "max_age": max_age, "demotions": list(demotions),
                    }
                    entry.update({
                        "outcome": "demoted",
                        "reason": "|".join("l1_" + d for d in demotions),
                        "max_age_s": max_age,
                        "demoted_behind": [t for t in tiers if t == TIER_ALPACA_IEX],
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
            snap.setdefault("max_age_seconds", entry["max_age_s"])
            _accept(
                tick, snap, tier=TIER_ALPACA_IEX, rule=RULE_IEX_DIRECT,
                authority=snap.get("quote_authority") or AUTHORITY_ALPACA_DIRECT,
            )
            entry.update({"outcome": "answered", "reason": RULE_IEX_DIRECT})
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

    # ---- Tier 2b: ang DEMOTED na L1 row ay sumasagot kapag walang mas sariwa ----
    if accepted_tick is None and l1_candidate is not None:
        c = l1_candidate
        tick, snap = _accept_l1(
            c["read"], sym=sym, authority=c["authority"], rule=c["rule"],
            age=c["age"], max_age=c["max_age"],
        )
        _accept(tick, snap, tier=TIER_IQFEED_L1, rule=c["rule"], authority=c["authority"])
        chain.append({
            "tier": TIER_IQFEED_L1,
            "outcome": "answered",
            "reason": c["rule"],
            "age_s": round(float(c["age"]), 6),
            "max_age_s": float(c["max_age"]),
            "basis": c["read"].basis,
            "tape_row_id": c["read"].tape_row_id,
            "demotions": list(c["demotions"]),
            "after": [e["tier"] for e in chain if e.get("tier") != TIER_IQFEED_L1],
        })

    # ---- Tier 3: ang SIP-clocked floor, LAMANG kapag hindi makakaputok ang deadman ----
    if accepted_tick is None and TIER_SIP_CLOCKED_FLOOR in tiers:
        entry = {
            "tier": TIER_SIP_CLOCKED_FLOOR,
            "outcome": "refused",
            "reason": None,
            "age_s": None,
            "max_age_s": None,
            "source": None,
        }
        if resting_floor_live is True:
            entry.update({"outcome": "skipped", "reason": "resting_deadman_live"})
        elif reads.sip_clocked_floor is None:
            floor_refusal = "floor_reader_missing"
            entry["reason"] = floor_refusal
        else:
            try:
                out = reads.sip_clocked_floor(sym)
            except Exception:
                _log.debug("[held_bbo] floor read failed sym=%s", sym, exc_info=True)
                out = None
            if isinstance(out, tuple) and len(out) == 2:
                tick, snap = out
            else:
                tick, snap = None, {"ok": False, "reason": "reader_missing"}
            snap = dict(snap) if isinstance(snap, dict) else {"ok": False, "reason": "read_failed"}
            entry["age_s"] = _float_or_none(snap.get("age_seconds"))
            entry["max_age_s"] = _float_or_none(snap.get("max_age_seconds"))
            entry["source"] = snap.get("source")
            if tick is not None:
                tick, snap = _accept_floor(tick, snap, sym=sym)
                _accept(
                    tick, snap, tier=TIER_SIP_CLOCKED_FLOOR, rule=RULE_SIP_CLOCKED_FLOOR,
                    authority=snap.get("quote_authority"),
                )
                entry.update({"outcome": "answered", "reason": RULE_SIP_CLOCKED_FLOOR})
            else:
                floor_refusal = "floor_" + str(snap.get("reason") or "unavailable")
                entry["reason"] = floor_refusal
        chain.append(entry)

    # Isang kahulugan lang: ang sagot ay HINDI ang unang pili (sariwang L1).
    env["bbo_fallback_engaged"] = (
        None if not chain
        else (accepted_tick is None or accepted_rule != RULE_L1_FRESH)
    )
    # Review fix: ang halt signal ay para sa HARANG lamang -- isang tick na sumagot
    # ay hindi kailanman nagbibilang, kahit tahimik ang L1 ng simbolo.
    counts_toward_halt = bool(counts_toward_halt) and accepted_tick is None
    env["counts_toward_halt"] = counts_toward_halt
    if accepted_tick is None or snapshot is None:
        unavailable_kind = "|".join(
            x for x in (l1_refusal, iex_refusal, floor_refusal) if x
        ) or "held_bbo_no_tier"
        snapshot = {
            "ok": False,
            "reason": "held_bbo_unavailable",
            "symbol": sym,
            "source": HELD_BBO_SELECTOR_VERSION,
            "unavailable_kind": unavailable_kind,
            "l1_refusal": l1_refusal,
            "iex_refusal": iex_refusal,
            "floor_refusal": floor_refusal,
            "age_seconds": l1_age_s if l1_age_s is not None else iex_age_s,
            "max_age_seconds": float(bounds.l1_gap_ceiling_s),
        }
        return HeldBboDecision(tick=None, snapshot=snapshot, envelope=env, counts_toward_halt=counts_toward_halt)
    return HeldBboDecision(tick=accepted_tick, snapshot=snapshot, envelope=env, counts_toward_halt=counts_toward_halt)


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


def _accept_floor(
    tick: NormalizedTicker, snap: dict[str, Any], *, sym: str
) -> tuple[NormalizedTicker, dict[str, Any]]:
    """Ang accept payload ng tier 3 -- PAREHONG mga susi ng `_accept_l1` para tuloy
    ang bawat consumer ng `exit_final_bbo`. Ang source / authority / basis / max
    age ay galing sa adapter payload: ang module na ito ay hindi nagpapangalan
    ng tape tier."""
    bid = float(tick.bid)
    ask = float(tick.ask)
    mid = float(tick.mid) if tick.mid is not None else (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10_000.0 if mid > 0 else 0.0
    raw = dict(tick.raw) if isinstance(tick.raw, dict) else {}
    age = _float_or_none(snap.get("age_seconds"))
    max_age = _float_or_none(snap.get("max_age_seconds"))
    event_at = _aware(snap.get("provider_event_at_utc") or raw.get("provider_event_at_utc"))
    received_at = _aware(snap.get("received_at_utc") or raw.get("received_at_utc"))
    delay = (
        round((received_at - event_at).total_seconds(), 6)
        if event_at is not None and received_at is not None
        else None
    )
    snapshot = {
        "ok": True,
        "reason": "execution_bbo_ok",
        "symbol": sym,
        "source": str(snap.get("source") or raw.get("feed") or ""),
        "tape_row_id": snap.get("tape_row_id", raw.get("tape_row_id")),
        "provider_event_at_utc": _iso(event_at),
        "received_at_utc": _iso(received_at),
        "available_at_utc": snap.get("available_at_utc"),
        "capture_event_sha256": raw.get("capture_event_sha256"),
        "capture_content_sha256": raw.get("capture_content_sha256"),
        "capture_sequence": raw.get("capture_sequence"),
        "timestamp_basis": str(snap.get("timestamp_basis") or raw.get("timestamp_basis") or ""),
        "quote_authority": snap.get("quote_authority"),
        "age_seconds": round(float(age), 6) if age is not None else None,
        "max_age_seconds": float(max_age) if max_age is not None else None,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread_bps": round(spread_bps, 4),
        "event_reference_at_utc": _iso(event_at),
        "delay_signature_s": delay,
        "validity_rule": RULE_SIP_CLOCKED_FLOOR,
    }
    return tick, snapshot


def held_bbo_receipt_fields(
    le: Any, *, binding: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Ang bbo_* na mga susi mula sa `le['last_held_execution_bbo']` para sa bawat resibo.

    `binding` (review fix): kapag ang resibo ay HINDI sa parehong tick na nagbasa
    ng envelope (hal. ang quote-independent flatten ay nag-e-emit BAGO ang
    `_live_tick_bbo` ng tick na iyon), sabihin iyon -- `bbo_binding` at
    `bbo_envelope_age_s` (now − `bbo_read_at_utc`) -- sa halip na magpanggap na
    ito ang desisyong quote ng order.

    Fail-open: hindi kailanman nagtataas -- ang resibo ay hindi dapat ang pumigil sa exit."""
    missing = {
        "bbo_source": None,
        "bbo_age_s": None,
        "bbo_fallback_engaged": None,
        "bbo_receipt": "no_held_bbo_envelope",
    }
    try:
        env = le.get("last_held_execution_bbo") if isinstance(le, dict) else None
        if not isinstance(env, dict) or "bbo_selector_version" not in env:
            out = dict(missing)
        else:
            out = {k: env.get(k) for k in HELD_BBO_RECEIPT_KEYS}
        if binding is not None:
            out["bbo_binding"] = str(binding)
            read_at = _aware(out.get("bbo_read_at_utc")) if "bbo_read_at_utc" in out else None
            now_a = _aware(now) or datetime.now(timezone.utc)
            out["bbo_envelope_age_s"] = (
                round((now_a - read_at).total_seconds(), 6) if read_at is not None else None
            )
        return out
    except Exception:
        return dict(missing)


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
    def _pick(
        name: str, *, percentile: float, fallback: float, transform=None,
        fallback_source: str = _FALLBACK_SOURCE,
    ) -> tuple[float, dict]:
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
            return float(fallback), {"n": n, "n_min": n_min, "source": fallback_source}
        return round(float(value), 3), {"n": n, "n_min": n_min, "source": "runtime"}

    fresh, fresh_meta = _pick(
        "A", percentile=0.999, fallback=_FALLBACK_L1_FRESH_BOUND_S,
        transform=lambda p: p + EVENT_TICK_MIN_SPACING_S,
    )
    own_fresh, own_fresh_meta = _pick(
        "A2", percentile=0.999, fallback=_FALLBACK_L1_OWN_CLOCK_FRESH_BOUND_S,
        transform=lambda p: p + EVENT_TICK_MIN_SPACING_S,
        fallback_source=_FALLBACK_SOURCE_A2,
    )
    gap, gap_meta = _pick("B", percentile=0.99, fallback=_FALLBACK_L1_GAP_CEILING_S)
    band, band_meta = _pick("D", percentile=0.99, fallback=_FALLBACK_SIP_DISAGREE_BAND_BPS)
    iex_max_age = _iex_direct_max_age_s()
    derivation = {
        "l1_fresh_bound_s": _bound_record(
            distribution=(
                "fenced_l1_delivery_lag_s(available_at - provider_trade_reference_at)"
                f".p99.9 + EVENT_TICK_MIN_SPACING_S({EVENT_TICK_MIN_SPACING_S})"
            ),
            window_min=_HELD_BBO_BOUNDS_WINDOW_MIN, percentile=0.999, value=fresh,
            measured_at_utc=measured_at_utc, **fresh_meta,
        ),
        "l1_own_clock_fresh_bound_s": _bound_record(
            distribution=(
                "own_clock_l1_delivery_lag_s(available_at - provider_event_at)"
                f".p99.9 + EVENT_TICK_MIN_SPACING_S({EVENT_TICK_MIN_SPACING_S})"
            ),
            window_min=_HELD_BBO_BOUNDS_WINDOW_MIN, percentile=0.999, value=own_fresh,
            measured_at_utc=measured_at_utc, **own_fresh_meta,
        ),
        # Mga konstanteng DALA, nakatatak (review fix: walang literal na walang resibo).
        "l1_future_tolerance_s": _bound_record(
            distribution="IQFEED_L1_FUTURE_TOLERANCE_S (bridge clock tolerance, not derived)",
            window_min=None, n=None, n_min=None, percentile=None,
            value=float(IQFEED_L1_FUTURE_TOLERANCE_S),
            source="constant_bridge_fence", measured_at_utc=measured_at_utc,
        ),
        "iex_direct_max_age_s": _bound_record(
            distribution=(
                "min(2.0, chili_momentum_entry_bbo_max_age_seconds) -- yesterday's strict "
                "HELD read, carried unchanged as tier 2; no IEX delivery-lag tape exists to derive it"
            ),
            window_min=None, n=None, n_min=None, percentile=None,
            value=float(iex_max_age),
            source="constant_strict_iex_ceiling", measured_at_utc=measured_at_utc,
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
        l1_own_clock_fresh_bound_s=own_fresh,
        derivation=derivation,
    )


_BOUND_QUERIES: tuple[tuple[str, str], ...] = (
    ("A", _SQL_A_DELIVERY_LAG_P999),
    ("B", _SQL_B_INTER_ROW_GAP_P99),
    ("D", _witness_disagreement_sql()),
    ("A2", _SQL_A2_OWN_CLOCK_DELIVERY_LAG_P999),
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


def current_bounds(*, now: datetime | None = None, allow_derive: bool = True) -> HeldBboBounds:
    """Ang mga hangganan para sa tick na ito. TTL 60 s; hindi kailanman nagtataas,
    at HINDI KAILANMAN nagde-derive sa tick path (review fix).

    Kapag may naka-cache ay ibinabalik agad iyon; kapag lumipas ang TTL ay isang
    background thread ang nagre-refresh (stale-while-revalidate). Kapag WALANG
    cache pa (unang tawag ng proseso) ay ibinabalik ang tagged na fallback at
    isang background thread ang nagde-derive -- dati ay inline ang unang tawag
    (hanggang 4.92 s), at sa `tick_live_session` ang unang tumatawag pagkatapos
    ng restart na may hawak na posisyon ay maaaring ang quote-independent
    EMERGENCY flatten (`_service_quote_independent_emergency_exit` ay tumatakbo
    BAGO ang `_live_tick_bbo`). Walang order ang naghihintay sa isang percentile.

    `allow_derive=False` (adapter na walang L1 reader: replay `MockBrokerAdapter`,
    bench fakes): walang DB read, walang thread -- ang naka-cache kung mayroon,
    kung hindi ang fallback."""
    try:
        mono = time.monotonic()
        spawn = False
        with _BOUNDS_LOCK:
            cached = _BOUNDS_CACHE.get("bounds")
            at = _BOUNDS_CACHE.get("at_monotonic")
            fresh_enough = (
                cached is not None and at is not None and (mono - float(at)) < _HELD_BBO_BOUNDS_TTL_S
            )
            if not fresh_enough and allow_derive and not _BOUNDS_CACHE.get("refreshing"):
                _BOUNDS_CACHE["refreshing"] = True
                spawn = True
        if spawn:
            try:
                _start_background_refresh()
            except Exception:
                with _BOUNDS_LOCK:
                    _BOUNDS_CACHE["refreshing"] = False
        if cached is not None:
            return cached
        return fallback_bounds(now=now)
    except Exception:
        _log.debug("[held_bbo] current_bounds failed; fallback", exc_info=True)
        return fallback_bounds(now=now)
