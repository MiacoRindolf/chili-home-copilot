"""Unit tests for the workspace dashboard builder (DB-free; query helpers patched)."""
import json
from unittest.mock import patch

from app.services import dashboard_summary as ds


_TRADING = {
    "net_pnl": 340.12, "win_rate": 0.35,
    "closes": [{"ticker": "ACHC", "pnl": 86.0, "pattern": "537 Reclaim", "reason": "target"},
               {"ticker": "EKSO", "pnl": -5.4, "pattern": "585 Wedge", "reason": "stop"}],
    "open_positions": [{"ticker": "NVDA", "side": "long"}],
    "top_patterns": [{"id": "537 Reclaim", "pnl": 86.0, "trades": 1, "payoff": 29.6}],
}


def test_no_user_yields_empty_but_valid():
    d = ds.build_dashboard(object(), None)
    assert d["kpis"][0]["val"] == "$0.00"
    assert d["trading"]["closes_fmt"] == []
    assert d["research"] == []
    assert d["has_any"] is False


def test_builds_from_trading_summary():
    with patch("app.services.trading_summary.build_trading_summary", return_value=_TRADING), \
         patch.object(ds, "_research", return_value=[{"topic": "NVDA", "summary": "x", "source": "reuters.com"}]):
        d = ds.build_dashboard(object(), 1)
    # KPIs
    assert d["kpis"][0]["val"] == "+$340.12" and d["kpis"][0]["cls"] == "ws-up"
    assert d["kpis"][1]["val"] == "35%"
    assert d["kpis"][2]["val"] == "1"   # open positions
    # closes formatted with sign + up/down
    closes = d["trading"]["closes_fmt"]
    assert closes[0]["pnl_fmt"] == "+$86.00" and closes[0]["pnl_up"] is True
    assert closes[1]["pnl_fmt"] == "-$5.40" and closes[1]["pnl_up"] is False
    # top patterns carry payoff ratio
    assert d["trading"]["top_patterns"][0]["payoff"] == "29.60:1"
    assert d["research"][0]["topic"] == "NVDA"
    assert d["has_any"] is True


def test_negative_net_pnl_marks_down():
    with patch("app.services.trading_summary.build_trading_summary",
               return_value={"net_pnl": -50.0, "closes": [], "open_positions": [], "top_patterns": []}), \
         patch.object(ds, "_research", return_value=[]):
        d = ds.build_dashboard(object(), 1)
    assert d["kpis"][0]["val"] == "-$50.00" and d["kpis"][0]["cls"] == "ws-down"


def test_trading_failure_degrades_gracefully():
    with patch("app.services.trading_summary.build_trading_summary", side_effect=Exception("boom")), \
         patch.object(ds, "_research", return_value=[]):
        d = ds.build_dashboard(object(), 1)
    assert d["trading"]["closes_fmt"] == []   # no crash
    assert d["has_any"] is False


# --------------------------------------------------------------------------- #
# _fmt_money (isolated formatter)
# --------------------------------------------------------------------------- #

def test_fmt_money_positive():
    assert ds._fmt_money(1234.56) == "+$1,234.56"


def test_fmt_money_negative():
    assert ds._fmt_money(-1234.5) == "-$1,234.50"


def test_fmt_money_zero_is_positive_sign():
    # 0 is not < 0, so it gets the '+' sign.
    assert ds._fmt_money(0) == "+$0.00"


def test_fmt_money_none():
    assert ds._fmt_money(None) is None


def test_fmt_money_non_numeric_string():
    assert ds._fmt_money("abc") is None


def test_fmt_money_numeric_string_is_parsed():
    # float("12.3") succeeds, so numeric strings are accepted.
    assert ds._fmt_money("12.3") == "+$12.30"


# --------------------------------------------------------------------------- #
# _fmt_pct (isolated formatter)
# --------------------------------------------------------------------------- #

def test_fmt_pct_basic():
    assert ds._fmt_pct(0.62) == "62%"


def test_fmt_pct_banker_rounding():
    # round(62.5) uses banker's rounding -> 62, not 63.
    assert ds._fmt_pct(0.625) == "62%"


def test_fmt_pct_rounds_up_above_half():
    assert ds._fmt_pct(0.626) == "63%"


def test_fmt_pct_none():
    assert ds._fmt_pct(None) is None


def test_fmt_pct_invalid():
    assert ds._fmt_pct("nope") is None


# --------------------------------------------------------------------------- #
# _trading (isolated section builder)
# --------------------------------------------------------------------------- #

def _sample_summary():
    return {
        "net_pnl": 1500.25,
        "win_rate": 0.62,
        "closes": [
            {"ticker": "AAPL", "pattern": "breakout", "pnl": 120.5, "reason": "target"},
            {"ticker": "TSLA", "pattern": None, "pnl": -40.0, "reason": None},
            "junk-non-dict",
        ],
        "open_positions": [
            {"ticker": "BTC", "side": "long"},
            None,
            {"ticker": "NVDA", "side": "long"},
        ],
        "top_patterns": [
            {"id": 585, "pnl": 300.0, "trades": 12, "payoff": 2.5},
            {"id": 586, "pnl": -10.0, "trades": 4, "payoff": None},
            "junk-non-dict",
        ],
    }


def test_trading_formats_summary():
    with patch("app.services.trading_summary.build_trading_summary",
               return_value=_sample_summary()):
        out = ds._trading(object(), 1)

    assert out["net_pnl"] == 1500.25
    assert out["net_pnl_fmt"] == "+$1,500.25"
    assert out["win_rate_fmt"] == "62%"

    # closes_fmt: non-dict skipped, pattern em-dash fallback, pnl formatted/up flag.
    assert len(out["closes_fmt"]) == 2
    c0, c1 = out["closes_fmt"]
    assert c0 == {"ticker": "AAPL", "pattern": "breakout",
                  "pnl_fmt": "+$120.50", "pnl_up": True, "reason": "target"}
    assert c1["pattern"] == "—" and c1["pnl_fmt"] == "-$40.00"
    assert c1["pnl_up"] is False and c1["reason"] == ""

    # open_positions: non-dict rows filtered out.
    assert out["open_positions"] == [{"ticker": "BTC", "side": "long"},
                                     {"ticker": "NVDA", "side": "long"}]

    # top_patterns: payoff formatted "N.NN:1" or em dash; non-dict skipped.
    assert len(out["top_patterns"]) == 2
    p0, p1 = out["top_patterns"]
    assert p0 == {"id": 585, "pnl_fmt": "+$300.00", "pnl_up": True,
                  "trades": 12, "payoff": "2.50:1"}
    assert p1["payoff"] == "—" and p1["pnl_up"] is False


def test_trading_degrades_when_builder_raises():
    with patch("app.services.trading_summary.build_trading_summary",
               side_effect=RuntimeError("boom")):
        out = ds._trading(object(), 1)
    # No exception; everything empty-ish.
    assert out["net_pnl"] is None
    assert out["net_pnl_fmt"] is None
    assert out["win_rate_fmt"] is None
    assert out["closes"] == []
    assert out["closes_fmt"] == []
    assert out["open_positions"] == []
    assert out["top_patterns"] == []


def test_trading_guest_skips_builder():
    # user_id None short-circuits to {} without ever calling the builder.
    with patch("app.services.trading_summary.build_trading_summary",
               side_effect=AssertionError("should not be called")) as m:
        out = ds._trading(object(), None)
    m.assert_not_called()
    assert out["net_pnl"] is None and out["closes"] == [] and out["top_patterns"] == []


# --------------------------------------------------------------------------- #
# _research (isolated section builder)
# --------------------------------------------------------------------------- #

class _Row:
    def __init__(self, topic, summary, sources):
        self.topic = topic
        self.summary = summary
        self.sources = sources


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)


def test_research_maps_rows():
    long_summary = "x" * 200
    rows = [
        _Row("AI capex", long_summary,
             json.dumps([{"url": "https://www.example.com/path?q=1"}])),
        _Row("Rates", "short summary", json.dumps([{"url": "http://news.bloomberg.com/a"}])),
    ]
    out = ds._research(_FakeDB(rows), 1)

    assert len(out) == 2
    # summary truncated to 90 chars, hostname parsed with www. stripped.
    assert out[0]["topic"] == "AI capex"
    assert out[0]["summary"] == "x" * 90
    assert out[0]["source"] == "example.com"
    assert out[1] == {"topic": "Rates", "summary": "short summary", "source": "news.bloomberg.com"}


def test_research_handles_bad_sources_json():
    rows = [_Row("Topic", "sum", "not-valid-json"),
            _Row("Empty", None, "[]")]
    out = ds._research(_FakeDB(rows), 1)
    # bad/empty sources -> source is "", and None summary -> "".
    assert out[0] == {"topic": "Topic", "summary": "sum", "source": ""}
    assert out[1] == {"topic": "Empty", "summary": "", "source": ""}


def test_research_guest_returns_empty():
    out = ds._research(_FakeDB([_Row("t", "s", "[]")]), None)
    assert out == []


def test_research_degrades_when_query_raises():
    class _BoomDB:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    assert ds._research(_BoomDB(), 1) == []


# --------------------------------------------------------------------------- #
# build_dashboard (KPI keys + has_any)
# --------------------------------------------------------------------------- #

def test_build_dashboard_kpi_keys_and_has_any_true():
    with patch.object(ds, "_trading", return_value={
            "net_pnl": 1500.25, "net_pnl_fmt": "+$1,500.25", "win_rate_fmt": "62%",
            "closes": [{"ticker": "AAPL"}],
            "closes_fmt": [{"ticker": "AAPL"}],
            "open_positions": [{"ticker": "BTC"}, {"ticker": "NVDA"}],
            "top_patterns": [{"id": 585}],
         }), \
         patch.object(ds, "_research", return_value=[{"topic": "AI", "summary": "s", "source": "x"}]):
        out = ds.build_dashboard(object(), 1)

    assert [k["key"] for k in out["kpis"]] == ["net_pnl", "win_rate", "open", "patterns"]
    by_key = {k["key"]: k for k in out["kpis"]}
    assert by_key["net_pnl"]["val"] == "+$1,500.25" and by_key["net_pnl"]["cls"] == "ws-up"
    assert by_key["win_rate"]["val"] == "62%"
    assert by_key["open"]["val"] == "2"
    assert by_key["patterns"]["val"] == "1"
    assert out["has_any"] is True
    assert out["trading"]["net_pnl"] == 1500.25
    assert out["research"][0]["topic"] == "AI"


def test_build_dashboard_empty_has_any_false_and_down_class():
    with patch.object(ds, "_trading", return_value={
            "net_pnl": -5.0, "net_pnl_fmt": "-$5.00", "win_rate_fmt": None,
            "closes": [], "closes_fmt": [], "open_positions": [], "top_patterns": [],
         }), \
         patch.object(ds, "_research", return_value=[]):
        out = ds.build_dashboard(object(), 1)

    by_key = {k["key"]: k for k in out["kpis"]}
    # negative net_pnl -> ws-down; missing win_rate -> em dash fallback.
    assert by_key["net_pnl"]["cls"] == "ws-down"
    assert by_key["win_rate"]["val"] == "—"
    assert by_key["open"]["val"] == "0" and by_key["patterns"]["val"] == "0"
    assert out["has_any"] is False


def test_build_dashboard_has_any_true_from_research_only():
    with patch.object(ds, "_trading", return_value={
            "net_pnl": None, "net_pnl_fmt": None, "win_rate_fmt": None,
            "closes": [], "closes_fmt": [], "open_positions": [], "top_patterns": [],
         }), \
         patch.object(ds, "_research", return_value=[{"topic": "AI", "summary": "s", "source": "x"}]):
        out = ds.build_dashboard(object(), 1)
    # net_pnl None -> "$0.00" fallback, cls ws-up (0 >= 0); has_any true via research.
    by_key = {k["key"]: k for k in out["kpis"]}
    assert by_key["net_pnl"]["val"] == "$0.00" and by_key["net_pnl"]["cls"] == "ws-up"
    assert out["has_any"] is True
