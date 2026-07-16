"""Diagnostic-only OHLCV loader for legacy JSON replay artifacts.

The legacy ``*_live.json`` artifacts contain after-the-fact minute aggregates under
``series.<SYMBOL>``.  They are useful for reconstructing price geometry, but they do
not contain the event/availability clocks or capture provenance required for a
certifiable replay.  This module keeps that boundary explicit: callers must state
the artifact's assumed timezone, and no API in this module permits certification or
coverage credit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd


LEGACY_EVIDENCE_ROLE = "legacy_after_fact_aggregate"
_BAR_COLUMNS = ("Open", "High", "Low", "Close", "Volume")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_BASE_MISSING_PROVENANCE_REASONS = (
    "after_fact_aggregate_not_event_stream",
    "content_addressed_source_build_missing",
    "capture_run_uuid_missing",
    "capture_generation_missing",
    "provider_event_time_missing",
    "available_at_missing",
    "watermark_missing",
    "bounded_lateness_contract_missing",
    "continuous_capture_coverage_unproven",
    "quote_event_clock_missing",
    "artifact_timezone_not_declared",
)


class LegacyReplayArtifactError(ValueError):
    """Raised when a legacy replay artifact cannot be parsed without guessing."""


class LegacyReplayCertificationError(RuntimeError):
    """Raised when legacy aggregate evidence is requested for certification."""


@dataclass(frozen=True, slots=True)
class LegacyReplayEvidence:
    """Immutable evidence boundary accompanying diagnostic legacy OHLCV."""

    source_path: str
    sha256: str
    artifact_date: date
    assumed_timezone: str
    ran_at_utc: datetime | None
    file_created_at_utc: datetime | None
    file_modified_at_utc: datetime | None
    engine: str | None
    role: str = field(default=LEGACY_EVIDENCE_ROLE, init=False)
    certification_ready: bool = field(default=False, init=False)
    coverage_credit_allowed: bool = field(default=False, init=False)
    missing_provenance_reasons: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        reasons = list(_BASE_MISSING_PROVENANCE_REASONS)
        if self.ran_at_utc is None:
            reasons.append("artifact_ran_at_missing")
        object.__setattr__(self, "missing_provenance_reasons", tuple(reasons))


@dataclass(frozen=True, slots=True)
class LegacyReplaySymbolOhlcv:
    """Validated diagnostic bars for one symbol in a legacy replay artifact."""

    symbol: str
    evidence: LegacyReplayEvidence
    _frames_by_interval: Mapping[str, pd.DataFrame] = field(repr=False)

    @property
    def frames_by_interval(self) -> Mapping[str, pd.DataFrame]:
        """Return defensive frame copies keyed for ``RecordedOhlcvProvider``."""

        return MappingProxyType(
            {
                interval: frame.copy(deep=True)
                for interval, frame in self._frames_by_interval.items()
            }
        )

    def recorded_provider(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        certification_mode: bool = False,
        coverage_credit: bool = False,
    ) -> Any:
        """Build a diagnostic provider while refusing certification/coverage use."""

        if certification_mode or coverage_credit:
            raise LegacyReplayCertificationError(
                "legacy after-fact OHLCV cannot be used for certification or "
                "replay coverage credit"
            )
        # Import lazily so parsing/validation remains a small, pure dependency.
        from app.services.trading.momentum_neural.replay_v3 import RecordedOhlcvProvider

        return RecordedOhlcvProvider(
            dict(self.frames_by_interval),
            clock=clock,
            certification_mode=False,
        )


def _parse_artifact_date(value: Any) -> date:
    if not isinstance(value, str):
        raise LegacyReplayArtifactError("artifact date must be an ISO YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise LegacyReplayArtifactError(
            f"invalid artifact date {value!r}; expected YYYY-MM-DD"
        ) from exc
    if parsed.isoformat() != value:
        raise LegacyReplayArtifactError(
            f"invalid artifact date {value!r}; expected canonical YYYY-MM-DD"
        )
    return parsed


def _parse_optional_utc_datetime(value: Any, *, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise LegacyReplayArtifactError(f"{field_name} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LegacyReplayArtifactError(f"invalid {field_name}: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LegacyReplayArtifactError(f"{field_name} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _finite_number(value: Any, *, symbol: str, row_number: int, field_name: str) -> float:
    if isinstance(value, bool):
        raise LegacyReplayArtifactError(
            f"{symbol} row {row_number}: {field_name} must be numeric"
        )
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LegacyReplayArtifactError(
            f"{symbol} row {row_number}: {field_name} must be numeric"
        ) from exc
    if not math.isfinite(number):
        raise LegacyReplayArtifactError(
            f"{symbol} row {row_number}: {field_name} must be finite"
        )
    return number


def _localize_minute(
    artifact_date: date,
    hhmm: Any,
    *,
    assumed_zone: ZoneInfo,
    symbol: str,
    row_number: int,
) -> pd.Timestamp:
    if not isinstance(hhmm, str) or not _TIME_RE.fullmatch(hhmm):
        raise LegacyReplayArtifactError(
            f"{symbol} row {row_number}: time must be canonical HH:MM"
        )
    naive = pd.Timestamp(f"{artifact_date.isoformat()}T{hhmm}:00")
    try:
        localized = naive.tz_localize(
            assumed_zone,
            ambiguous="raise",
            nonexistent="raise",
        )
    except (TypeError, ValueError) as exc:
        raise LegacyReplayArtifactError(
            f"{symbol} row {row_number}: ambiguous or nonexistent local time {hhmm!r}"
        ) from exc
    return localized.tz_convert("UTC")


def _parse_symbol_rows(
    raw_rows: Any,
    *,
    symbol: str,
    artifact_date: date,
    assumed_zone: ZoneInfo,
) -> pd.DataFrame:
    if not isinstance(raw_rows, list):
        raise LegacyReplayArtifactError(f"series.{symbol} must be a list of minute rows")

    rows_by_timestamp: dict[pd.Timestamp, tuple[float, float, float, float, float]] = {}
    for row_number, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, list) or len(raw) != 6:
            raise LegacyReplayArtifactError(
                f"{symbol} row {row_number}: expected [HH:MM,O,H,L,C,V]"
            )
        ts = _localize_minute(
            artifact_date,
            raw[0],
            assumed_zone=assumed_zone,
            symbol=symbol,
            row_number=row_number,
        )
        open_px, high_px, low_px, close_px, volume = (
            _finite_number(raw[index], symbol=symbol, row_number=row_number, field_name=name)
            for index, name in enumerate(("open", "high", "low", "close", "volume"), start=1)
        )
        if min(open_px, high_px, low_px, close_px) <= 0:
            raise LegacyReplayArtifactError(
                f"{symbol} row {row_number}: OHLC prices must be positive"
            )
        if volume < 0:
            raise LegacyReplayArtifactError(
                f"{symbol} row {row_number}: volume must be non-negative"
            )
        if high_px < max(open_px, low_px, close_px):
            raise LegacyReplayArtifactError(
                f"{symbol} row {row_number}: high is below another OHLC value"
            )
        if low_px > min(open_px, high_px, close_px):
            raise LegacyReplayArtifactError(
                f"{symbol} row {row_number}: low is above another OHLC value"
            )
        values = (open_px, high_px, low_px, close_px, volume)
        existing = rows_by_timestamp.get(ts)
        if existing is not None and existing != values:
            raise LegacyReplayArtifactError(
                f"{symbol} has conflicting duplicate minute {raw[0]!r}"
            )
        # Exact duplicates are harmless and collapse deterministically.
        rows_by_timestamp[ts] = values

    if not rows_by_timestamp:
        return pd.DataFrame(columns=_BAR_COLUMNS, index=pd.DatetimeIndex([], tz="UTC"))

    ordered = sorted(rows_by_timestamp.items(), key=lambda item: item[0])
    return pd.DataFrame(
        [values for _, values in ordered],
        index=pd.DatetimeIndex([ts for ts, _ in ordered], name="timestamp"),
        columns=_BAR_COLUMNS,
        dtype=float,
    )


def _aggregate_minutes(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy(deep=True)
    aggregated = frame.resample(
        interval,
        label="left",
        closed="left",
        origin="start_day",
    ).agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    # Sparse symbols legitimately have empty time buckets. Do not fabricate bars.
    return aggregated.dropna(subset=["Open", "High", "Low", "Close"])[
        list(_BAR_COLUMNS)
    ]


def load_legacy_replay_symbol_ohlcv(
    path: str | Path,
    *,
    symbol: str,
    assumed_timezone: str,
    certification_mode: bool = False,
    coverage_credit: bool = False,
) -> LegacyReplaySymbolOhlcv:
    """Load one symbol's 1m/5m/15m bars from a legacy replay JSON artifact.

    ``assumed_timezone`` is deliberately mandatory because legacy artifacts do not
    declare the timezone of their ``HH:MM`` labels.  Returned indexes are UTC bar
    start times.  Aggregates are labeled at the left edge, so
    :class:`RecordedOhlcvProvider` releases them only after their full interval has
    closed.
    """

    if certification_mode or coverage_credit:
        raise LegacyReplayCertificationError(
            "legacy after-fact OHLCV cannot be used for certification or replay "
            "coverage credit"
        )
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise LegacyReplayArtifactError("symbol is required")
    timezone_name = str(assumed_timezone or "").strip()
    if not timezone_name:
        raise LegacyReplayArtifactError("assumed_timezone is required")
    try:
        assumed_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise LegacyReplayArtifactError(
            f"unknown assumed_timezone {timezone_name!r}"
        ) from exc

    source = Path(path).expanduser().resolve(strict=True)
    payload_bytes = source.read_bytes()
    sha256 = hashlib.sha256(payload_bytes).hexdigest()
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyReplayArtifactError(f"invalid JSON artifact {source}") from exc
    if not isinstance(payload, dict):
        raise LegacyReplayArtifactError("artifact root must be an object")

    artifact_date = _parse_artifact_date(payload.get("date"))
    series = payload.get("series")
    if not isinstance(series, dict):
        raise LegacyReplayArtifactError("artifact series must be an object")
    raw_rows = series.get(normalized_symbol)
    if raw_rows is None:
        # Accept differently-cased keys only when the normalized match is unique.
        matches = [key for key in series if str(key).strip().upper() == normalized_symbol]
        if len(matches) != 1:
            raise LegacyReplayArtifactError(
                f"symbol {normalized_symbol!r} is not present in artifact series"
            )
        raw_rows = series[matches[0]]

    minute_frame = _parse_symbol_rows(
        raw_rows,
        symbol=normalized_symbol,
        artifact_date=artifact_date,
        assumed_zone=assumed_zone,
    )
    frames = MappingProxyType(
        {
            "1m": minute_frame,
            "5m": _aggregate_minutes(minute_frame, "5min"),
            "15m": _aggregate_minutes(minute_frame, "15min"),
        }
    )

    stat = source.stat()
    evidence = LegacyReplayEvidence(
        source_path=str(source),
        sha256=sha256,
        artifact_date=artifact_date,
        assumed_timezone=timezone_name,
        ran_at_utc=_parse_optional_utc_datetime(
            payload.get("ran_at_utc"), field_name="ran_at_utc"
        ),
        file_created_at_utc=datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc),
        file_modified_at_utc=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        engine=str(payload["engine"]) if payload.get("engine") is not None else None,
    )
    return LegacyReplaySymbolOhlcv(
        symbol=normalized_symbol,
        evidence=evidence,
        _frames_by_interval=frames,
    )


__all__ = [
    "LEGACY_EVIDENCE_ROLE",
    "LegacyReplayArtifactError",
    "LegacyReplayCertificationError",
    "LegacyReplayEvidence",
    "LegacyReplaySymbolOhlcv",
    "load_legacy_replay_symbol_ohlcv",
]
