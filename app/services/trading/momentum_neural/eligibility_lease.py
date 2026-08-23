"""L11b — ELIGIBILITY RETENTION LEASE (2026-08-04).

ANG PROBLEMA. Ang `momentum_symbol_viability.paper_eligible` / `live_eligible` ay
walang anumang TTL: kapag naisulat na, nananatili hanggang OVERWRITE ng producer.
Pero ang mambabasa ng band — ang IQFeed L1 subscription resolver — ay may 24h
freshness window, kaya ang nakikita niyang "eligible band" ay hindi "mga pangalang
tradeable NGAYON" kundi ang UNION ng lahat ng na-score sa nakaraang 24 oras.
Sukat sa prod (08-04): 645 distinct sa band, **482 ang lampas 600s ang edad**,
163 lang ang sariwa. Nang humati ang rail-governor ng capacity papuntang 312,
inubos ng band ang buong budget at 100% ng ross band — ang mga tunay na mover ng
araw — ang na-evict (29,677 eviction lines, 109 distinct symbols).

ANG INVARIANT NA IPINAPATUPAD NITO. Ang eligible band na inilalathala sa
subscription layer ay dapat KATUMBAS ng set ng mga row na tatanggapin ng trading
path mismo. Kung masyado nang luma ang isang row para armahan, masyado na rin
siyang luma para kumain ng subscription slot.

BAKIT LEASE AT HINDI VETO. Ang unang iminungkahing disenyo ay mag-veto kapag
walang Ross evidence ang isang tick. TINANGGIHAN ITO SA DATA: sa 90 na
no-Ross-evidence na simbolo, 72 ay wala sa large-cap scan list at kabilang doon
ang UPC/GME/TNXP/HCWB/DRMA/BLZE/SMTK — TOTOONG small-cap movers (ang UPC ay nasa
golden window library pa nga). Ang blanket veto ay magbe-bench ng napatunayang
mover at bubuwagin ang sinadyang fail-open na property (ross_momentum.py:739-741
"a name is never benched on absent data"). Ang lease ay hindi humahawak sa
ADMISSION — RETENSYON lang ang pinapaikli. Ang tunay na mover ay muling
nasi-score kada refresh cycle kasama ang signals niya, kaya agad siyang bumabalik;
ang namamatay ay ang mga row na tumigil nang i-renew.

ISANG DOKUMENTADONG BASE. `chili_momentum_risk_viability_max_age_seconds`
(config.py, default 600.0) — ito na mismo ang sagot ng sistema sa "gaano katanda
bago tumangging kumilos ang trading path sa isang viability row" (binabasa ng
auto_arm, risk_policy, ross_event_admission, paper_runner, opportunities,
breadth_regime, crypto_l2_drain). WALANG bagong numero: ang refresh interval at
ang lease ay parehong DERIVED dito, at ang lease ay FLOOR — hindi kailanman mas
maikli kaysa dalawang cycle ng producer na dapat mag-renew nito.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Ang refresh interval ng producer, kapareho ng derivation sa
# trading_scheduler._momentum_viability_refresh_interval_seconds — nakasulat dito
# para pure at testable ang predicate (walang scheduler import).
_REFRESH_FLOOR_SECONDS = 60.0


def refresh_interval_seconds(settings_obj: Any) -> float:
    """Ang cadence ng viability producer, derived mula sa iisang base."""
    base = _base_seconds(settings_obj)
    return max(_REFRESH_FLOOR_SECONDS, base / 2.0)


def lease_seconds(settings_obj: Any) -> float:
    """Gaano katagal nananatiling eligible ang isang row nang hindi nire-renew.

    `max(base, 2 * refresh)` — hindi bababa sa DALAWANG na-miss na refresh cycle,
    kaya ang isang lumaktaw na cycle ay HINDI kailanman nagde-demote. FLOOR ito:
    kapag itinaas ng operator ang base, sabay tumataas ang refresh at ang lease.
    """
    base = _base_seconds(settings_obj)
    return max(base, 2.0 * refresh_interval_seconds(settings_obj))


def _base_seconds(settings_obj: Any) -> float:
    try:
        raw = getattr(settings_obj, "chili_momentum_risk_viability_max_age_seconds", 600.0)
        value = float(raw if raw is not None else 600.0)
    except (TypeError, ValueError):
        return 600.0
    return value if value > 0 else 600.0


def should_demote(
    *,
    row_age_seconds: float | None,
    producer_silence_seconds: float | None,
    lease: float,
    protected: bool,
) -> tuple[bool, str]:
    """Pure na desisyon para sa ISANG row. Fail-toward-KEEP sa bawat pagdududa.

    Ang mga reason string ay ini-emit sa summary log para masukat ang bawat sanhi.
    """
    if protected:
        return False, "protected_active_session"
    if row_age_seconds is None or producer_silence_seconds is None:
        return False, "unknown_age"
    if lease <= 0:
        return False, "lease_disabled"
    if producer_silence_seconds > lease:
        # ANG PINAKAMAHALAGANG SAFETY: kapag tumigil ang producer (outage, crash,
        # weekend), ang katahimikan ay HINDI patunay na hindi na tradeable ang
        # pangalan. Kung wala ito, buburahin ng sweep ang BUONG band tuwing may
        # outage at maiiwang bulag ang lane pagbalik ng tape.
        return False, "producer_silent_fail_open"
    if row_age_seconds <= lease:
        return False, "within_lease"
    return True, "lease_expired"


def expire_stale_equity_eligibility(
    db: Session,
    *,
    settings_obj: Any,
    now_utc: datetime | None = None,
    protected_symbols: Iterable[str] | None = None,
) -> dict[str, Any]:
    """I-demote ang mga EQUITY viability row na lumampas na sa lease.

    Hindi ginagalaw ang `freshness_ts` (bawal magpeke ng edad) — ang
    `updated_at` lang ang tinatatakan. Ang susunod na sulat ng producer ang
    magre-renew ng flags sa pamamagitan ng umiiral nang upsert.
    Crypto (`-USD`) ay hindi saklaw — may sariling venue feed at cadence.
    """
    now = now_utc or datetime.now(timezone.utc)
    lease = lease_seconds(settings_obj)
    protected = sorted({str(s).strip().upper() for s in (protected_symbols or []) if s})

    if lease <= 0:
        return {"demoted": 0, "reason": "lease_disabled", "lease_seconds": lease}

    try:
        # SAVEPOINT (2026-08-23): ang producer-liveness read ay nauuna sa
        # bounded na UPDATE drain sa PAREHONG table at PAREHONG session. Kung
        # mamatay ang read nang walang savepoint, nalulunok ang exception —
        # pero patay na ang transaction bago pa marating ang mga write.
        from .optional_db_read import optional_scalar

        silence_row = optional_scalar(db, text(
            "SELECT EXTRACT(EPOCH FROM (:now - max(freshness_ts))) "
            "FROM momentum_symbol_viability WHERE symbol NOT LIKE '%-%'"
        ), {"now": now})
        producer_silence = float(silence_row) if silence_row is not None else None
    except Exception:
        logger.debug("[eligibility_lease] producer-liveness read failed", exc_info=True)
        return {"demoted": 0, "reason": "liveness_read_failed", "lease_seconds": lease}

    # PRODUCER-LIVENESS GATE (ang parehong panuntunan ng `should_demote`, na siyang
    # unit-tested na deklarasyon ng patakaran; dito ito ipinapataw nang isang beses
    # para sa buong sweep sa halip na kada row).
    if producer_silence is None or producer_silence > lease:
        return {
            "demoted": 0,
            "reason": "producer_silent_fail_open" if producer_silence is not None else "unknown_age",
            "lease_seconds": lease,
            "producer_silence_seconds": producer_silence,
        }

    # BOUNDED, PER-BATCH-COMMITTED DRAIN — sinusundan ang umiiral nang convention
    # ng retention sweep para sa MISMONG table na ito (data_retention.py:33-38).
    # Kailangan ito: sa unang sweep, 96,139 rows / 4,018 symbols ang stale (sukat
    # 08-04) — halos buong table. Ang isang malaking UPDATE ay magiging WAL /
    # dead-tuple spike sa prod habang bukas ang market. Newest-stale-FIRST ang
    # pagkakasunod dahil iyon ang mga row na aktwal na nasa loob ng freshness
    # window ng mambabasa; ang mas luma ay hindi na nakikita ng band at
    # hinuhugot ng mga sumunod na sweep.
    from ..data_retention import (
        DEFAULT_VIABILITY_SNAPSHOT_MAX_ROWS_PER_SWEEP as _MAX_PER_SWEEP,
        DEFAULT_VIABILITY_SNAPSHOT_SLIM_BATCH_SIZE as _BATCH,
    )

    demoted = 0
    try:
        while demoted < _MAX_PER_SWEEP:
            result = db.execute(text(
                "UPDATE momentum_symbol_viability SET "
                "  paper_eligible = FALSE, live_eligible = FALSE, updated_at = :now "
                "WHERE id IN ("
                "  SELECT id FROM momentum_symbol_viability "
                "  WHERE symbol NOT LIKE '%-%' "
                "    AND (paper_eligible OR live_eligible) "
                "    AND :now - freshness_ts > make_interval(secs => :lease) "
                "    AND (:n_protected = 0 OR upper(symbol) <> ALL(:protected)) "
                "  ORDER BY freshness_ts DESC LIMIT :batch)"
            ), {
                "now": now,
                "lease": lease,
                "protected": protected,
                "n_protected": len(protected),
                "batch": int(_BATCH),
            })
            n = int(result.rowcount or 0)
            db.commit()
            demoted += n
            if n < int(_BATCH):
                break
    except Exception:
        db.rollback()
        logger.warning("[eligibility_lease] demote failed", exc_info=True)
        return {"demoted": demoted, "reason": "demote_failed", "lease_seconds": lease}

    if demoted:
        logger.info(
            "[eligibility_lease] demoted=%d lease=%.0fs producer_silence=%.0fs protected=%d",
            demoted, lease, producer_silence or 0.0, len(protected),
        )
    return {
        "demoted": demoted,
        "reason": "lease_expired",
        "lease_seconds": lease,
        "producer_silence_seconds": producer_silence,
        "protected_count": len(protected),
    }
