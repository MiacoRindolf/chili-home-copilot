"""SEC fails-to-deliver ingestion + squeeze-fuel evidence delta.

Ross "Forcing a Crash" (2026-08-21, LNAI court docs): ang FTD counts ay ang
LIBRENG regulatory hard evidence ng phantom supply — ang LNAI ay may
6.6M/2.7M/5.6M shares failed-to-deliver bago ang lawsuit. Mataas na FTD
relative sa float = tunay na naked-short pressure = squeeze fuel kapag
sumabog ang demand.

Disenyo (parehong pattern ng shelf/reverse-split):
- Semimonthly SEC files (cnsfails{yyyymm}{a|b}.zip, ~30-araw lag), pipe-
  delimited: SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE.
- ``ingest_latest_ftd_files(db)``: BATCH producer (scheduler daily o operator)
  — kinukuha ang pinakabagong hindi-pa-nai-ingest na files, nag-a-upsert
  (immutable historical; ON CONFLICT DO NOTHING). HINDI ito tinatawag sa
  anumang entry/decision path.
- ``ftd_squeeze_viability_delta(symbol, floats, db)``: pipeline-side pure-ish
  reader — ang SELECTION delta (meta-driven; ang scorer core ay dict lookup
  lang, walang import/settings/DB — parehong contract ng reverse-split #1092).
- FAIL-OPEN kahit saan: walang data / lumang data / network error = 0.0.
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_FTD_URL = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails{ym}{half}.zip"
_USER_AGENT = "CHILI-research/1.0 (rindolf.miaco@gmail.com)"
_HTTP_TIMEOUT_S = 30.0
# Dokumentadong mga base (floors/reference — hindi hard cutoffs):
_FTD_STALENESS_DAYS = 45        # lampas dito ang pinakabagong datum = walang delta
_FTD_NOTABLE_FLOAT_FRAC = 0.01  # >=1% ng float ang fails = kapansin-pansin (+0.03)
_FTD_STRONG_FLOAT_FRAC = 0.05   # >=5% ng float = malakas na ebidensya (+0.05)


def _candidate_periods(today: date | None = None) -> list[tuple[str, str]]:
    """Ang huling ~3 semimonthly period keys (bagong-una)."""
    d = today or date.today()
    out: list[tuple[str, str]] = []
    y, m = d.year, d.month
    for _ in range(3):
        out.append((f"{y:04d}{m:02d}", "b"))
        out.append((f"{y:04d}{m:02d}", "a"))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def _fetch_ftd_rows(ym: str, half: str) -> list[tuple[str, str, str, int, float | None]]:
    """Isang semimonthly file → mga row. Empty sa 404/error (fail-open)."""
    import requests

    url = _FTD_URL.format(ym=ym, half=half)
    try:
        resp = requests.get(
            url, headers={"User-Agent": _USER_AGENT}, timeout=_HTTP_TIMEOUT_S,
        )
        if resp.status_code != 200 or not resp.content[:2] == b"PK":
            return []
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        raw = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
    except Exception:
        logger.debug("[ftd] fetch failed %s%s (fail-open)", ym, half, exc_info=True)
        return []
    rows: list[tuple[str, str, str, int, float | None]] = []
    for line in raw.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 6:
            continue
        try:
            sd = datetime.strptime(parts[0].strip(), "%Y%m%d").date().isoformat()
            sym = parts[2].strip().upper()
            qty = int(parts[3].strip() or 0)
            try:
                px = float(parts[5].strip())
            except (TypeError, ValueError):
                px = None
            if sym and qty > 0:
                rows.append((sd, sym, parts[1].strip(), qty, px))
        except (TypeError, ValueError):
            continue
    return rows


def ingest_latest_ftd_files(db: Any) -> dict[str, int]:
    """Batch producer: i-ingest ang mga bagong semimonthly file. Idempotent."""
    from sqlalchemy import text

    report: dict[str, int] = {}
    for ym, half in _candidate_periods():
        key = f"{ym}{half}"
        try:
            already = db.execute(text(
                "SELECT count(*) FROM sec_fails_to_deliver "
                "WHERE settlement_date >= :lo AND settlement_date < "
                "(CAST(:lo AS date) + INTERVAL '16 days')"
            ), {"lo": f"{ym[:4]}-{ym[4:6]}-{'01' if half == 'a' else '16'}"}).scalar()
            if already and int(already) > 0:
                report[key] = 0
                continue
            rows = _fetch_ftd_rows(ym, half)
            if not rows:
                report[key] = 0
                continue
            for i in range(0, len(rows), 5000):
                chunk = rows[i:i + 5000]
                db.execute(text(
                    "INSERT INTO sec_fails_to_deliver "
                    "(settlement_date, symbol, cusip, fail_qty, price) "
                    "VALUES " + ",".join(
                        f"(:d{j}, :s{j}, :c{j}, :q{j}, :p{j})"
                        for j in range(len(chunk))
                    ) + " ON CONFLICT DO NOTHING"
                ), {
                    k: v for j, (sd, sym, cusip, qty, px) in enumerate(chunk)
                    for k, v in ((f"d{j}", sd), (f"s{j}", sym), (f"c{j}", cusip),
                                 (f"q{j}", qty), (f"p{j}", px))
                })
            db.commit()
            report[key] = len(rows)
            logger.info("[ftd] ingested %s: %d rows", key, len(rows))
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning("[ftd] ingest %s failed (fail-open)", key, exc_info=True)
            report[key] = -1
    return report


def ftd_squeeze_viability_delta(
    symbol: str,
    *,
    float_shares: float | None,
    db: Any,
) -> float:
    """Pipeline-side: ang FTD evidence delta para sa isang symbol.

    +0.05 kapag fails >= 5% ng float (malakas na phantom-supply evidence);
    +0.03 kapag >= 1%; 0.0 kung wala/luma/walang float. Hindi kailanman
    negative — ebidensya ito ng GATONG, hindi ng panganib (long-side tayo)."""
    try:
        from sqlalchemy import text

        sym = str(symbol or "").strip().upper()
        if not sym or sym.endswith("-USD") or db is None:
            return 0.0
        if float_shares is None or float(float_shares) <= 0:
            return 0.0
        row = db.execute(text(
            "SELECT settlement_date, fail_qty FROM sec_fails_to_deliver "
            "WHERE symbol = :s ORDER BY settlement_date DESC LIMIT 1"
        ), {"s": sym}).fetchone()
        if row is None:
            return 0.0
        sd, qty = row[0], float(row[1] or 0)
        if isinstance(sd, str):
            sd = datetime.fromisoformat(sd).date()
        if (date.today() - sd) > timedelta(days=_FTD_STALENESS_DAYS):
            return 0.0
        frac = qty / float(float_shares)
        if frac >= _FTD_STRONG_FLOAT_FRAC:
            return 0.05
        if frac >= _FTD_NOTABLE_FLOAT_FRAC:
            return 0.03
        return 0.0
    except Exception:
        logger.debug("[ftd] delta read failed (fail-open)", exc_info=True)
        return 0.0
