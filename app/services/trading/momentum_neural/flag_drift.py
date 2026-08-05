"""Momentum flag-drift detector — ginagawang NAKIKITA ang env-vs-default drift.

ANG PROBLEMANG SINASAGOT NITO (2026-08-04). May TATLONG runtime vector sa CHILI,
at akala ng lahat ay dalawa lang:

  1. `chili-clean-recovery-scheduler` container — hand-launched, 134 na
     CHILI_MOMENTUM_* env pin. `role=rnd_only`, kaya HINDI kasama ang
     `include_momentum_exec`; ang huling trading_automation_session ay 07-13.
     HINDI ito ang trading lane.
  2. Captured-paper service — HOST process, hindi container, kaya HINDI nito
     namamana ang container env. Nagba-load ng `.env`. ITO ang aktwal na
     nagta-trade.
  3. Replay container — walang override.

Dahil hindi nakikita ang pagkakaiba, isang buong araw ang nagamit sa maling
premise ("ang prod ay tumatakbo sa X kaya dapat sumunod ang code"), gayong ang
lane na nagta-trade at ang replay ay parehong tumatakbo sa CODE DEFAULT at ang
scheduler container pala ang outlier. Ang mali sanang aksyon — bulk-flip ng 31
default — ay mag-o-ON ng 31 hindi-napatunayang feature sa trading lane nang
sabay-sabay.

ANG SAGOT AY HINDI PAGPILIT NG PAGKAKAPAREHO. Ang env override ay lehitimo (may
kanya-kanyang tungkulin ang bawat container). Ang HINDI katanggap-tanggap ay ang
KATAHIMIKAN. Ini-emit ng module na ito ang eksaktong drift sa bawat boot, kaya
ang bawat susunod na tanong na "ano ba talaga ang tumatakbo?" ay may sagot sa log
sa halip na sa hinuha. WALANG binabago sa anumang halaga — pagmamasid lang.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Mapping

_log = logging.getLogger(__name__)

_PREFIX = "CHILI_MOMENTUM_"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _as_bool(raw: str) -> bool | None:
    v = str(raw).strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    return None


def boolean_flag_drift(
    environ: Mapping[str, str], model_fields: Mapping[str, Any]
) -> list[tuple[str, bool, bool]]:
    """Pure: ``[(field_name, env_value, code_default), ...]`` na hindi tugma.

    Ang mga hindi-boolean na field at ang hindi mabasang halaga ay nilalaktawan —
    ang detector ay hindi dapat mag-imbento ng drift na hindi niya sigurado.
    """
    out: list[tuple[str, bool, bool]] = []
    for key, raw in environ.items():
        if not key.startswith(_PREFIX):
            continue
        name = key.lower()
        field = model_fields.get(name)
        if field is None:
            continue
        default = getattr(field, "default", None)
        if not isinstance(default, bool):
            continue
        env_val = _as_bool(raw)
        if env_val is None or env_val == default:
            continue
        out.append((name, env_val, default))
    return sorted(out)


def log_flag_drift(*, environ: Mapping[str, str] | None = None, context: str = "") -> int:
    """I-emit ang drift nang isang beses. Ibinabalik ang bilang; best-effort."""
    try:
        from ....config import Settings

        drift = boolean_flag_drift(
            environ if environ is not None else os.environ, Settings.model_fields
        )
        if not drift:
            _log.warning(
                "[flag_drift] %s walang boolean momentum env override na "
                "sumasalungat sa code default", context or "(walang konteksto)",
            )
            return 0
        on_off = [n for n, e, d in drift if e and not d]
        off_on = [n for n, e, d in drift if d and not e]
        _log.warning(
            "[flag_drift] %s %d na boolean momentum flag ang SUMASALUNGAT sa code "
            "default — env_ON/code_OFF=%d env_OFF/code_ON=%d. Ang env override ay "
            "lehitimo, pero ang bawat isa ay ibang polisiya kaysa sa nakasulat sa "
            "code, at HINDI ito nakikita ng replay. ON_but_default_OFF=%s | "
            "OFF_but_default_ON=%s",
            context or "(walang konteksto)",
            len(drift),
            len(on_off),
            len(off_on),
            ",".join(n.replace("chili_momentum_", "") for n in on_off) or "-",
            ",".join(n.replace("chili_momentum_", "") for n in off_on) or "-",
        )
        return len(drift)
    except Exception:
        _log.debug("[flag_drift] detector failed", exc_info=True)
        return 0
