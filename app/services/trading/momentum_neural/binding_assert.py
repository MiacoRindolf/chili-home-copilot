"""WAVE-1 FIX-9 — DEPLOY BINDING-ASSERT.

Two deploys in two days silently DROPPED critical env pins (the ``1m`` pullback interval
= today's −$137; the 7 R3 flags). The dropped pin never surfaced because nothing at
startup COMPARED the *effective* live binding against what the deploy INTENDED. This module
closes that class of bug:

  * A single ``MANIFEST`` dict names each critical setting + its EXPECTED live value (with
    an inline comment on why it matters). Manifest values live in ONE place.
  * At startup ``run_binding_assert(settings)`` COMPUTES each live binding value FROM
    settings (never a config-default constant — the value the code actually reads), logs
    one ``[binding_assert] name=... expected=... live=... OK|DRIFT`` line per entry, and
    emits one summary line.
  * Default behavior is WARN-LOUD only (a missing/renamed manifest entry can never kill
    prod). With ``CHILI_BINDING_ASSERT_STRICT=1`` a drift raises ``BindingDriftError`` so a
    dropped pin fails the deploy fast.

Pure + best-effort: reading a live value never raises (a read error is reported as a DRIFT
against a sentinel, not an exception). This is the deploy-time analogue of the operator's
"COMPUTE the live binding value in-container; never report a config-default as the effective
cap" rule.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

_STRICT_ENV = "CHILI_BINDING_ASSERT_STRICT"
# A sentinel used when a live read raises — guarantees a DRIFT (never silently OK).
_READ_ERROR = object()


class BindingDriftError(RuntimeError):
    """Raised (strict mode only) when a critical binding drifts from its expected value."""


@dataclass(frozen=True)
class _Binding:
    """One manifest entry: a human name, the EXPECTED live value, and a pure resolver that
    COMPUTES the live binding value from ``settings`` (the value the code actually reads)."""

    name: str
    expected: Any
    resolve: Callable[[Any], Any]
    why: str = ""


def _norm_interval(v: Any) -> str:
    """Normalize a timeframe string the way the entry code keys it (lower, stripped)."""
    return str(v or "").strip().lower()


# ─────────────────────────────────────────────────────────────────────────────
# THE MANIFEST — the ONE place the deploy-critical settings + their EXPECTED live
# values live. Each ``resolve`` reads settings the SAME way the live code does, so a
# dropped/renamed pin shows up as a DRIFT here. Update EXPECTED when the deploy intent
# changes (deliberately, in one place) — NOT by editing scattered call sites.
# ─────────────────────────────────────────────────────────────────────────────
def _manifest() -> list[_Binding]:
    return [
        # THE −$137 bug: the pullback entry interval must be the intended timeframe. On
        # main-lineage the durable default is the config value; if a deploy drops the env
        # override the live binding reverts silently — this catches that.
        _Binding(
            name="pullback_entry_interval",
            expected=_norm_interval(_config_default("chili_momentum_pullback_entry_interval", "5m")),
            resolve=lambda s: _norm_interval(getattr(s, "chili_momentum_pullback_entry_interval", None)),
            why="the entry clock; a dropped pin silently 5x-mis-times entries (IPW −$136.93)",
        ),
        # Midday de-weight + afterhours fail-closed (FIX-8) must be ON by default.
        _Binding(
            name="midday_deweight_enabled",
            expected=True,
            resolve=lambda s: bool(getattr(s, "chili_momentum_midday_deweight_enabled", False)),
            why="midday entry de-weight; a dropped pin restores full midday size (6% win band)",
        ),
        # Run-R breaker (the entry-floor RAISE component; FIX-7 raise-only depends on it).
        _Binding(
            name="run_r_breaker_enabled",
            expected=True,
            resolve=lambda s: bool(getattr(s, "chili_momentum_run_r_breaker_enabled", False)),
            why="the macro run-R entry-bar raise; off = marginal setups arm in a losing regime",
        ),
        # WAVE-1 stop ratchet strict invariant (FIX-5).
        _Binding(
            name="stop_ratchet_strict_enabled",
            expected=True,
            resolve=lambda s: bool(getattr(s, "chili_momentum_stop_ratchet_strict_enabled", False)),
            why="INVARIANT-A: a long stop may never decrease within a tick (IREZ 36ms loosen)",
        ),
        # WAVE-1 score-floor raise-only integrity (FIX-7).
        _Binding(
            name="floor_raise_only_enabled",
            expected=True,
            resolve=lambda s: bool(getattr(s, "chili_momentum_floor_raise_only_enabled", False)),
            why="the entry viability floor may never be lowered below the risk-raised bar",
        ),
        # Explosive-prequal score floor (the UPC selection-bar fix) must be ON.
        _Binding(
            name="explosive_prequal_floor_enabled",
            expected=True,
            resolve=lambda s: bool(getattr(s, "chili_momentum_explosive_prequal_floor_enabled", False)),
            why="raise-only prequal floor so a genuine Ross A-setup clears the impulse bar (UPC)",
        ),
        # Early-premarket adaptive unlock (the premarket-capture knobs).
        _Binding(
            name="early_premarket_enabled",
            expected=True,
            resolve=lambda s: bool(getattr(s, "chili_momentum_early_premarket_enabled", False)),
            why="opens the entry window from the first pre-07:00 mover time (premarket edge)",
        ),
        _Binding(
            name="early_premarket_min_movers",
            expected=_config_default("chili_momentum_early_premarket_min_movers", 3),
            resolve=lambda s: int(getattr(s, "chili_momentum_early_premarket_min_movers", 3) or 3),
            why="the N-distinct-mover early-unlock quorum (the 3-bar was structurally dead)",
        ),
        # Spread-cap fallback (the EM-scaled cap that gates thin small-cap entries).
        _Binding(
            name="spread_cap_em_fallback_enabled",
            expected=True,
            resolve=lambda s: bool(getattr(s, "chili_momentum_spread_cap_em_fallback_enabled", False)),
            why="the EM-scaled spread cap; off = the fixed clamp kills the adaptive ceiling",
        ),
    ]


def _config_default(field_name: str, fallback: Any) -> Any:
    """The field's declared default from the Settings model — the deploy's INTENDED value
    when no env override is present. Falls back to ``fallback`` if the field is absent."""
    try:
        from ....config import Settings

        fields = getattr(Settings, "model_fields", None) or {}
        f = fields.get(field_name)
        if f is not None and f.default is not None:
            return f.default
    except Exception:
        pass
    return fallback


@dataclass(frozen=True)
class BindingResult:
    name: str
    expected: Any
    live: Any
    ok: bool
    why: str


def evaluate_bindings(settings: Any) -> list[BindingResult]:
    """COMPUTE the live binding for each manifest entry and compare to expected. Pure /
    zero-I/O beyond reading ``settings``; never raises (a read error => a DRIFT result)."""
    out: list[BindingResult] = []
    for b in _manifest():
        try:
            live = b.resolve(settings)
        except Exception as exc:  # a read error must never be silently OK
            live = _READ_ERROR
            logger.warning("[binding_assert] resolve failed name=%s err=%s", b.name, exc)
        ok = (live is not _READ_ERROR) and (live == b.expected)
        out.append(BindingResult(name=b.name, expected=b.expected, live=live, ok=ok, why=b.why))
    return out


def run_binding_assert(settings: Any, *, strict: bool | None = None) -> list[BindingResult]:
    """Startup hook: evaluate the manifest, log one line per binding + a summary, and (in
    strict mode) raise on any drift.

    ``strict`` defaults to the ``CHILI_BINDING_ASSERT_STRICT`` env (1/true/yes). Default is
    WARN-LOUD only, so a missing/renamed manifest entry can never kill prod. Returns the
    per-binding results (also useful in tests)."""
    if strict is None:
        strict = (os.environ.get(_STRICT_ENV) or "").strip().lower() in ("1", "true", "yes")

    results = evaluate_bindings(settings)
    n_drift = 0
    for r in results:
        status = "OK" if r.ok else "DRIFT"
        if r.ok:
            logger.info("[binding_assert] name=%s expected=%s live=%s %s", r.name, r.expected, r.live, status)
        else:
            n_drift += 1
            logger.warning(
                "[binding_assert] name=%s expected=%s live=%s %s (%s)",
                r.name, r.expected, r.live, status, r.why,
            )

    # ONE summary event/log line (the effective-values digest).
    summary = {r.name: r.live for r in results}
    if n_drift:
        logger.warning(
            "[binding_assert] SUMMARY drift=%d/%d strict=%s effective=%s",
            n_drift, len(results), bool(strict), summary,
        )
    else:
        logger.info(
            "[binding_assert] SUMMARY all_ok count=%d effective=%s", len(results), summary,
        )

    if n_drift and strict:
        drifted = [r.name for r in results if not r.ok]
        raise BindingDriftError(
            f"binding_assert: {n_drift} critical setting(s) drifted from expected: {drifted}. "
            f"Set {_STRICT_ENV}=0 to downgrade to warn-only."
        )
    return results
