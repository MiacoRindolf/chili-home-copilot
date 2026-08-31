"""Ross Day-Trade-Dash mirror (#1250) — parser + lane intake union.

Ang fixture text ay hinango sa TUNAY na dashboard page text (2026-08-30
madaling-araw): SY squeeze alerts sa HOD, hating-tatlong-linya na WBUY row sa
gainers, at continuation ladder. Ang lane helper ay freshness-gated na
file-based union — fail-open sa lahat ng error.

Runnable: pytest tests/test_ross_dash_mirror.py -v
"""
from __future__ import annotations

import json
import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from ross_dash_mirror_parse import parse_dashboard_text  # noqa: E402

from app.config import settings
from app.services.trading.momentum_neural.auto_arm import (
    _ross_dash_mirror_symbols,
)


SAMPLE = """
Small Cap - offline
Ross's 5 Pillars Scan: 06:48:30 - 06:53:30(Online)
No qualified trading opportunity found for the trading strategies yet.

Time Symbol / News Price Volume Float Relative Volume(Daily Rate) Relative Volume(5 min %) Gap(%) Change From Close(%) Short Interest Strategy Name

06:45:45 am
SY
3.21 3.38M 45.54M 23,101.93 13,456,548.39 33.75 33.75 1.92M Medium Float - High Rel Vol - Price under $20
06:45:45 am
SY
3.21 3.38M 45.54M 23,101.93 13,456,548.39 33.75 33.75 1.92M Squeeze Alert - Up 10% in 10min

05:33:19 am

(3 in 5sec)

WBUY
1.23 7.03M 2.93M 6,794.75 1,063,172.49 48.19 48.19 105.23K Low Float - High Rel Vol
05:30:13 am BRNX 4.96 402.35K 720.42K 2.32 314.20 27.51 27.51 248.53K Former Momo Stock
Change From Close(%)

Symbol / News

Price

Volume

Float

Relative Volume(Daily Rate)

Relative Volume(5 min %)

Gap(%)

Short Interest

74.01 ▲ AEHL 6.16 4.01M 1.31M 103.08 14,997.48 74.01 10.23K
20.51 ▼
WBUY
1.00 18.21M 2.93M 8,668.83 768,728.62 20.51 105.23K
5.27 MIMI 1.01 87.08K 7.72M 0.39 9.53 5.27 124.56K
Moving - 2 Week (%)

Symbol / News

Price

Volume

Float

Relative Volume(Daily Rate)

Relative Volume(5 min %)

Change From Close(%)

Gap(%)

Short Interest

538.80 BTCT 1.96 282.53K 11.79M 0.13 2.67 1.02 1.02 2.60M

Charts Powered by TradingView
"""


def test_parser_hod_alerts():
    m = parse_dashboard_text(SAMPLE)
    hod = m["hod_alerts"]
    assert len(hod) == 4
    sy = hod[0]
    assert sy["symbol"] == "SY"
    assert sy["price"] == 3.21
    assert sy["volume"] == 3_380_000
    assert sy["float"] == 45_540_000
    assert sy["change_pct"] == 33.75
    assert sy["strategy"].startswith("Medium Float")
    assert hod[1]["strategy"] == "Squeeze Alert - Up 10% in 10min"
    # split-row na WBUY (may burst line sa gitna)
    wbuy = hod[2]
    assert wbuy["symbol"] == "WBUY" and wbuy["price"] == 1.23
    assert hod[3]["symbol"] == "BRNX"
    assert hod[3]["strategy"] == "Former Momo Stock"


def test_parser_gainers_and_split_rows():
    m = parse_dashboard_text(SAMPLE)
    g = {r["symbol"]: r for r in m["top_gainers"]}
    assert g["AEHL"]["pct"] == 74.01 and g["AEHL"]["float"] == 1_310_000
    # hating-tatlong-linya: "20.51 ▼" / "WBUY" / numbers
    assert g["WBUY"]["pct"] == 20.51 and g["WBUY"]["price"] == 1.00
    assert g["MIMI"]["pct"] == 5.27


def test_parser_continuation_and_symbols():
    m = parse_dashboard_text(SAMPLE)
    assert m["continuation"][0]["symbol"] == "BTCT"
    assert m["continuation"][0]["pct"] == 538.80
    # intake: HOD lahat + gainers >= 10% (MIMI 5.27 ay HINDI kasama)
    assert m["symbols"] == ["SY", "WBUY", "BRNX", "AEHL"]


def _write_mirror(tmp_path, symbols, age_s=0.0):
    p = tmp_path / "mirror.json"
    gen = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    p.write_text(json.dumps({
        "generated_at_utc": gen.isoformat(),
        "symbols": symbols,
    }), encoding="utf-8")
    return str(p)


def test_lane_helper_fresh_file(tmp_path, monkeypatch):
    path = _write_mirror(tmp_path, ["sy", "AEHL", "BTC-USD", ""])
    monkeypatch.setattr(settings, "chili_momentum_ross_dash_mirror_path", path, raising=False)
    out = _ross_dash_mirror_symbols()
    assert out == {"SY", "AEHL"}  # normalized; crypto/blank tinanggal


def test_lane_helper_stale_file_is_empty(tmp_path, monkeypatch):
    path = _write_mirror(tmp_path, ["SY"], age_s=3600.0)
    monkeypatch.setattr(settings, "chili_momentum_ross_dash_mirror_path", path, raising=False)
    assert _ross_dash_mirror_symbols() == set()


def test_lane_helper_missing_file_is_empty(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_ross_dash_mirror_path",
        "D:/wala/ito/mirror.json", raising=False,
    )
    assert _ross_dash_mirror_symbols() == set()


def test_lane_helper_flag_off_is_empty(tmp_path, monkeypatch):
    path = _write_mirror(tmp_path, ["SY"])
    monkeypatch.setattr(settings, "chili_momentum_ross_dash_mirror_path", path, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_ross_dash_mirror_enabled", False, raising=False)
    assert _ross_dash_mirror_symbols() == set()


def test_mirror_rvol_lookup(tmp_path, monkeypatch):
    """#1251: rvol rescue — ang pinakamataas na sariwang rvol_daily ng symbol."""
    import json as _json
    from app.services.trading.momentum_neural.auto_arm import _ross_dash_mirror_rvol

    p = tmp_path / "m.json"
    gen = datetime.now(timezone.utc).isoformat()
    p.write_text(_json.dumps({
        "generated_at_utc": gen,
        "hod_alerts": [{"symbol": "AEHL", "rvol_daily": 103.08}],
        "top_gainers": [{"symbol": "AEHL", "rvol_daily": 146.48},
                         {"symbol": "MIMI", "rvol_daily": None}],
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "chili_momentum_ross_dash_mirror_path", str(p), raising=False)
    assert _ross_dash_mirror_rvol("AEHL") == 146.48
    assert _ross_dash_mirror_rvol("MIMI") is None
    assert _ross_dash_mirror_rvol("WALA") is None
