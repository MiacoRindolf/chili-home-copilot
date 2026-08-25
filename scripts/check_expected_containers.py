"""Iulat ang bawat CHILI container na dapat tumatakbo pero hindi.

BAKIT ITO UMIIRAL. Noong 2026-08-25 ay natagpuang ang
``chili-clean-recovery-broker-sync`` at ``chili-clean-recovery-autotrader`` ay
``Exited (137)`` -- pitong linggo na. Walang alarma, walang log, walang
nakapansin.

Ang bunga ay hindi halata. Ang mga role gate sa code ay TAMA: ang
``include_broker_sync`` ay nakalista na ang ``broker_sync_only`` at ang
``include_autotrader`` ay ang ``autotrader_only``. Walang masisisi sa source. Ang
mga proseso na dapat magdala ng mga role na iyon ay patay lamang, kaya ang
epekto ay kapareho ng isang tinanggal na job: walang broker-DB position sync,
walang stuck-order canceller, walang disconnect alarm, walang bracket repair --
at walang sinuman ang nag-e-evaluate ng software stop/target ng crypto habang
may buhay na posisyon.

⚠️ ANG ARAL. Ang "naka-register ba ang job" ay MALING TANONG. Ang tamang tanong
ay "may prosesong may role na iyon ba na buhay". Ang isang tsekeng nagbibilang
ng job sa loob ng isang tumatakbong scheduler ay bulag sa isang scheduler na
hindi tumatakbo -- ito nga ang dahilan kung bakit hindi nahuli ng umiiral nang
canonical-job assertion ang pitong linggong ito.

Read-only. Walang sinisimulan, walang pinapatay. Exit 1 kapag may nawawala.

    python scripts/check_expected_containers.py
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

# Pangalan -> kung bakit ito mahalaga. Ang teksto ay lumilitaw sa alarma, kaya
# dapat nitong sagutin ang "ano ang nawawala sa akin" nang hindi kailangang
# magbukas ng code.
EXPECTED: dict[str, str] = {
    "chili-clean-recovery-scheduler": (
        "lahat ng cron at learning job na dala ng role rnd_only, kasama ang triple-barrier labeling at ang mga retention sweep"
    ),
    "chili-clean-recovery-web": (
        "ang HTTPS UI, ang chat, at ang lahat ng SSE stream -- ito ang nakikita mong CHILI"
    ),
    "chili-clean-recovery-brain": (
        "ang learning cycle ng neural brain -- pattern mining, backtest, evolve"
    ),
    "chili-home-copilot-postgres-1": (
        "ANG database. Kapag wala ito ay walang tumatakbo -- tape, session, trade, lahat"
    ),
    "chili-clean-recovery-broker-sync": (
        "broker-DB position sync kada 2 min, stuck-order canceller, "
        "disconnect alarm, bracket repair sweep (role broker_sync_only)"
    ),
    "chili-clean-recovery-autotrader": (
        "ANG TANGING nag-e-evaluate ng software stop/target ng crypto "
        "(run_crypto_exit_pass; role autotrader_only) -- ang crypto ay "
        "nangangalakal 24/7, kasama ang katapusan ng linggo"
    ),
}


def _run(args: list[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout or ""


def running() -> set[str]:
    return {n.strip() for n in _run(["docker", "ps", "--format", "{{.Names}}"]).splitlines() if n.strip()}


def status_of(name: str) -> str:
    raw = _run(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Status}}"])
    return raw.strip().splitlines()[0] if raw.strip() else "(walang ganitong container)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    up = running()
    missing = [(n, why) for n, why in EXPECTED.items() if n not in up]

    if args.json:
        print(json.dumps({
            "expected": len(EXPECTED), "running": len(EXPECTED) - len(missing),
            "missing": [{"container": n, "status": status_of(n), "why": w} for n, w in missing],
        }, indent=1))
    else:
        print(f"  {len(EXPECTED) - len(missing)}/{len(EXPECTED)} na inaasahang container ang tumatakbo")
        for name, why in missing:
            print(f"  [PATAY ] {name}")
            print(f"           {status_of(name)}")
            print(f"           nawawala: {why}")
        if not missing:
            print("  lahat ay nasa itaas")

    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
