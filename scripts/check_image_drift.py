"""Iulat kung gaano na kaluma ang tumatakbong container images laban sa main.

BAKIT ITO UMIIRAL. Noong 2026-08-24 ay natagpuang ang scheduler container ay
tumatakbo sa ``chili-app:main-2e1eb77`` -- **53 commit ang atras**, binuo apat na
araw bago iyon. Walang nagbabantay niyon, at walang senyales kahit saan.

Ang bunga ay hindi halata: ang lane at ang mga bridge ay tumatakbo sa HOST mula sa
``E:/dev/wt-window2`` at umaabot agad ang bawat merge; ang mga CONTAINER ay
tumatakbo sa isang BAKED na image at hindi umaabot ang anuman hangga't walang
rebuild. Kaya ang "na-merge sa main" ay nangangahulugan ng dalawang magkaibang
bagay depende sa kung aling proseso ang bumabasa nito -- at ang pagkakaibang iyon
ay tahimik.

Kung ano ang ginagawa nito: hanapin ang bawat tumatakbong ``chili-app:*``
container, kunin ang commit SHA mula sa tag nito, at bilangin kung ilang commit sa
``origin/main`` ang wala doon.

Read-only. Walang docker mutation, walang network maliban sa ``git fetch`` na
opsyonal. Exit 0 kapag pasado ang lahat sa threshold, 1 kapag may lumagpas.

    python scripts/check_image_drift.py --max-behind 20
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

_TAG_SHA = re.compile(r"^chili-app:(?:.*-)?([0-9a-f]{7,40})(?:-.*)?$")


def _run(args: list[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout or ""


def running_chili_images() -> list[tuple[str, str]]:
    """[(pangalan ng container, image tag)] para sa bawat tumatakbong chili-app."""
    raw = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"])
    rows: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        name, image = line.split("\t", 1)
        if image.strip().startswith("chili-app:"):
            rows.append((name.strip(), image.strip()))
    return rows


def commits_behind(sha: str, ref: str = "origin/main") -> int | None:
    """Ilang commit sa ``ref`` ang wala sa ``sha``. None kapag di-kilala ang sha."""
    if not _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"]) and subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True
    ).returncode != 0:
        return None
    out = _run(["git", "rev-list", "--count", f"{sha}..{ref}"]).strip()
    try:
        return int(out)
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-behind", type=int, default=20,
                    help="ilang commit ang katanggap-tanggap bago mag-ulat ng drift")
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rows = running_chili_images()
    report: list[dict] = []
    worst = 0
    for name, image in rows:
        m = _TAG_SHA.match(image)
        sha = m.group(1) if m else None
        behind = commits_behind(sha, args.ref) if sha else None
        if behind is not None:
            worst = max(worst, behind)
        report.append({
            "container": name,
            "image": image,
            "sha": sha,
            "commits_behind": behind,
            "stale": bool(behind is not None and behind > args.max_behind),
        })

    unknown = [r["container"] for r in report if r["commits_behind"] is None]

    if args.json:
        print(json.dumps({"max_behind": args.max_behind, "worst": worst,
                          "unknown_sha": unknown,
                          "containers": report}, indent=1))
    else:
        if not report:
            print("  walang tumatakbong chili-app na container")
        for r in report:
            behind = r["commits_behind"]
            mark = "STALE" if r["stale"] else ("ok" if behind is not None else "?")
            shown = "hindi kilala ang SHA" if behind is None else f"{behind} commit atras"
            print(f"  [{mark:5}] {r['container']:42} {r['image']:34} {shown}")
        if worst > args.max_behind:
            print(f"\n  WARNING: ang pinakaluma ay {worst} commit atras (hangganan {args.max_behind}).")
            print("     Ang merge sa main ay HINDI umaabot sa mga container nang walang rebuild.")
        if unknown:
            # HINDI ito benign. Ang isang image na hindi masabi kung saang commit
            # nakatayo ay hindi masasabing sariwa -- mas malala pa ito kaysa sa
            # isang kilalang-luma, dahil walang bilang na maaaring tumaas nang
            # sapat para mapansin. Iulat ito nang hiwalay para hindi mapagkamalang
            # "pasado" ang isang buong container.
            print(f"\nHINDI MASUKAT ang SHA ng: {', '.join(unknown)}")
            print("     Ang isang image na walang commit sa tag nito ay hindi mapatutunayang sariwa.")

    return 1 if worst > args.max_behind else 0


if __name__ == "__main__":
    raise SystemExit(main())
