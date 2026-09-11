"""The bench must be able to show a payload, not only count events.

WHY THIS EXISTS. `_BENCH_PAYLOAD_KEYS_MAX` and `_BENCH_VALUE_CHARS_MAX` were USED at the
bottom of `_bench_payload` and never DEFINED. Every payload read therefore raised
NameError, and all ten runs of the 2026-09-08 bench wrote exactly ONE event apiece:
`_receipt_event_read_failed`. The event histogram survived, so the run looked healthy — it
reported 35 entries, 35 exits and +$1,076.92, and it was only caught when an extraction of
`live_pullback_add_vetoed` reasons returned 0 against a histogram that said 242.

THE PART WORTH REMEMBERING: the receipt DID say so, in band, in its own error field. The
instrument reported its blindness and nobody read the report. So this test does not merely
assert the constants exist — it drives the real function and asserts a real key survives,
which is the property the bench actually needs.

The RECORDED side of the bench (scripts/rossbench_export_recorded_events.py) shapes the live
lane's payloads with a reproduction of this function, because it cannot import the driver.
The last test here holds that reproduction to this function's output, payload for payload.

DB-free. Runnable: pytest tests/test_bench_payload_is_readable.py -v
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_rv3_window_for_test",
    Path(__file__).resolve().parents[1] / "scripts" / "replay_v3_fsm_window.py",
)
_EXPORTER_SPEC = importlib.util.spec_from_file_location(
    "_rossbench_exporter_for_test",
    Path(__file__).resolve().parents[1] / "scripts" / "rossbench_export_recorded_events.py",
)


@pytest.fixture(scope="module")
def mod():
    """Import the driver module directly; it is a script, not a package member."""
    m = importlib.util.module_from_spec(_SPEC)
    sys.modules[_SPEC.name] = m
    try:
        _SPEC.loader.exec_module(m)
    except Exception as exc:                      # pragma: no cover - diagnostic path
        pytest.fail(f"replay_v3_fsm_window.py failed to import: {exc!r}")
    return m


def test_the_two_bounds_are_defined(mod):
    """The literal defect: used, never defined."""
    assert isinstance(mod._BENCH_PAYLOAD_KEYS_MAX, int)
    assert isinstance(mod._BENCH_VALUE_CHARS_MAX, int)


def test_an_ordinary_payload_survives_intact(mod):
    """The property the bench needs: a real reason must come back readable."""
    out = mod._bench_payload("live_pullback_add_vetoed", {
        "reason": "depth_band_below_floor",
        "depth_frac": 0.031,
        "trigger": "micro_pullback",
        "viability_score": 0.71,
    })
    assert out["reason"] == "depth_band_below_floor"
    assert out["depth_frac"] == 0.031
    assert out["trigger"] == "micro_pullback"
    assert "_bench_trimmed" not in out, "an ordinary 4-key payload must not be trimmed"


def test_the_bounds_do_not_bind_on_the_measured_population(mod):
    """Measured 2026-09-09 over 58,205 live payloads from the prior three days: keys
    p50 4 / p99 18 / p99.99 27 / MAX 33; bytes p50 168 / p99 658 / MAX 7,910. These are
    guard rails against pathology, not a filter on what may be measured, so they must sit
    clear of the whole observed population — a bound that binds on real data silently
    destroys evidence, which is the defect the whitelist was removed for."""
    assert mod._BENCH_PAYLOAD_KEYS_MAX > 33, "would trim the widest payload ever observed"
    assert mod._BENCH_VALUE_CHARS_MAX > 7910, "would trim the largest payload ever observed"
    wide = {f"k{i}": i for i in range(33)}
    assert "_bench_trimmed" not in mod._bench_payload("wide", wide)


def test_pathology_is_still_bounded_and_says_so(mod):
    """The bound must still exist, and a trim must be announced in band."""
    huge = mod._bench_payload("boom", {"traceback": "x" * (mod._BENCH_VALUE_CHARS_MAX + 500)})
    assert len(huge["traceback"]) == mod._BENCH_VALUE_CHARS_MAX
    assert "traceback" in huge.get("_bench_trimmed", []), "a silent trim is the original sin"
    many = mod._bench_payload("boom", {f"k{i}": i for i in range(mod._BENCH_PAYLOAD_KEYS_MAX + 40)})
    assert len(many) <= mod._BENCH_PAYLOAD_KEYS_MAX + 1
    assert any("more keys" in str(t) for t in many.get("_bench_trimmed", []))


def test_a_non_dict_payload_does_not_raise(mod):
    """Receipts are written in the driver's hot path; a bad payload must degrade, not throw."""
    assert "_payload_not_a_dict" in mod._bench_payload("odd", ["not", "a", "dict"])


@pytest.fixture(scope="module")
def rex():
    m = importlib.util.module_from_spec(_EXPORTER_SPEC)
    sys.modules[_EXPORTER_SPEC.name] = m
    _EXPORTER_SPEC.loader.exec_module(m)
    return m


def test_the_recorded_side_exporter_reproduces_this_function(mod, rex):
    """Parity for a dual code path: identical input to both, identical output asserted.

    The exporter reads the two bounds out of this file with ``ast`` (it cannot import it:
    the import raises SystemExit unless TEST_DATABASE_URL names a *_test DB), so the bounds
    are checked against the imported values and the bounding ALGORITHM against this
    function's output. On 2026-09-11 the exporter was still filtering by the deleted
    ``_BENCH_PAYLOAD_KEYS`` — a recorded payload of 19 permitted keys graded against a
    whole replay payload — and refused to run at all once the name was gone."""
    bounds = rex.bench_payload_bounds()
    assert bounds == {"_BENCH_PAYLOAD_KEYS_MAX": mod._BENCH_PAYLOAD_KEYS_MAX,
                      "_BENCH_VALUE_CHARS_MAX": mod._BENCH_VALUE_CHARS_MAX}
    chars, keys = mod._BENCH_VALUE_CHARS_MAX, mod._BENCH_PAYLOAD_KEYS_MAX
    beyond_the_cap = {f"k{i}": i for i in range(keys + 40)}
    beyond_the_cap["reason"] = "a load-bearing key past the key cap"
    cases = [
        ("live_pullback_add_vetoed", {"reason": "depth_band_below_floor", "depth_frac": 0.031,
                                      "trigger": "micro_pullback", "viability_score": 0.71}),
        ("live_entry_filled", {"fill_price": 4.21, "reason": "double_bottom_break",
                               "not_load_bearing": "kept", "detector_rejects": {"a": 1}}),
        ("edge", {"at_str_bound": "e" * chars,                          # exactly AT a bound
                  "at_json_bound": "f" * (chars - 2)}),                # is not over it
        ("boom", {"traceback": "x" * (chars + 500)}),                  # str over the bound
        ("boom", {"raw": b"y" * (chars + 1)}),                         # bytes over the bound
        ("boom", {"blob": {"k": "z" * chars}}),                        # JSON over the bound
        ("boom", {"reason": "r" * (chars + 10)}),                      # trimmed, then restored
        ("boom", {"bad": {(1, 2): "t" * chars}}),                      # except branch: a tuple
                                                                       # key json.dumps refuses
        ("boom", {"when": datetime(2026, 9, 11, 13, 30), "short": b"ok", 7: "int key"}),
        ("boom", {f"k{i}": i for i in range(keys + 40)}),              # key cap
        ("live_exit_filled", beyond_the_cap),
        ("odd", ["not", "a", "dict"]),
        ("empty", {}),
        ("none", None),
    ]
    for event_type, payload in cases:
        want = mod._bench_payload(event_type, copy.deepcopy(payload))
        got = rex.bench_payload(event_type, copy.deepcopy(payload), bounds=bounds)
        assert got == want, (event_type, sorted(map(str, want))[:8])
