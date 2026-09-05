"""HARNESS GATE 14 (2026-09-05): the replay driver's monkeypatch wrappers must forward
EVERY argument of the function they wrap.

MEASURED: ``scripts/replay_v3_fsm_window.py`` re-pointed ``entry_gates.signed_tape_accel_features``
at the sim clock through a wrapper that re-declared the signature as
``(symbol, *, db, window_s, as_of)``. #1024 (2026-08-11) added ``settings_obj`` to the wrapped
function and ``tape_confirms_hold`` passes it on every call; the wrapper raised TypeError,
``tape_confirms_hold`` swallowed it into its fail-closed branch, and EVERY replay since
returned ``(False, tape_hold_error)`` for every tape-gated trigger (12 pattern triggers,
ORB/ABCD tick confirm, buyers_confirmed, the ignition exemption) -- while the identical
read offline confirmed (JWEL 2026-08-10 11:31:47Z: accel +6,899, tick_rate 137 >= floor 53).

DB-free: the tape read is fed by a stub ``db`` whose ``execute().fetchall()`` returns a
hand-written six-print tape.

Runnable: pytest tests/test_replay_driver_simclock_wrappers.py -v
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import replay_harness_invariants as inv  # noqa: E402

DRIVER = os.path.join(_SCRIPTS, "replay_v3_fsm_window.py")
CLOCK = dt.datetime(2026, 8, 10, 11, 31, 47)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _StubDb:
    """The shape ``optional_fetchall`` needs: ``execute(stmt, params).fetchall()``."""

    def __init__(self, rows):
        self.rows = rows
        self.params = []

    def execute(self, _stmt, params=None):
        self.params.append(dict(params or {}))
        return _Rows(self.rows)


def _six_print_tape(as_of: dt.datetime):
    t0 = as_of.replace(tzinfo=dt.timezone.utc).timestamp()
    out = []
    for i in range(6):
        px = 5.00 + 0.01 * i
        # (price, size, bid, ask, epoch) -- prints at the ask, sizes growing => buyers lifting
        out.append((px, 100 + 200 * i, px - 0.01, px, t0 - 12.0 + 2.0 * i))
    return out


def test_simclock_default_wrapper_forwards_every_argument_and_fills_only_the_key():
    seen = {}

    def fn(symbol, *, db=None, window_s=None, as_of=None, settings_obj=None):
        seen.update(symbol=symbol, db=db, window_s=window_s, as_of=as_of, settings_obj=settings_obj)
        return "ok"

    w = inv.simclock_default_wrapper(fn, lambda: CLOCK, key="as_of")
    assert w("JWEL", db="DB", window_s=15.0, settings_obj="S") == "ok"
    assert seen == {"symbol": "JWEL", "db": "DB", "window_s": 15.0, "as_of": CLOCK, "settings_obj": "S"}
    # an explicit as_of is never overridden
    explicit = CLOCK - dt.timedelta(seconds=30)
    w("JWEL", as_of=explicit)
    assert seen["as_of"] == explicit
    # the wrapper does not own the signature: inspect sees the wrapped function's parameters
    assert "settings_obj" in inspect.signature(w).parameters
    assert w._simclock_wrapped is fn and w._simclock_key == "as_of"


def test_tape_confirms_hold_reaches_the_tape_under_the_driver_wrapper(monkeypatch):
    from app.services.trading.momentum_neural import entry_gates as eg

    orig = eg.signed_tape_accel_features
    monkeypatch.setattr(eg, "signed_tape_accel_features",
                        inv.simclock_default_wrapper(orig, lambda: CLOCK, key="as_of"))
    db = _StubDb(_six_print_tape(CLOCK))
    ok, dbg = eg.tape_confirms_hold("JWEL", db=db)
    assert dbg["reason"] != "tape_hold_error", dbg
    assert dbg.get("n_ticks") == 6, dbg
    # the read was anchored at the sim clock, not at wall time
    assert db.params and db.params[0]["as_of"] == CLOCK, db.params


def test_the_old_signature_owning_wrapper_is_the_measured_failure(monkeypatch):
    """Documents the class: a wrapper that re-declares the signature turns every
    tape_confirms_hold call into ``tape_hold_error`` (fail-closed) the moment the wrapped
    function grows a kwarg."""
    from app.services.trading.momentum_neural import entry_gates as eg

    orig = eg.signed_tape_accel_features

    def _old_shape(symbol, *, db=None, window_s=None, as_of=None, _o=orig):
        return _o(symbol, db=db, window_s=window_s, as_of=(as_of if as_of is not None else CLOCK))

    monkeypatch.setattr(eg, "signed_tape_accel_features", _old_shape)
    ok, dbg = eg.tape_confirms_hold("JWEL", db=_StubDb(_six_print_tape(CLOCK)))
    assert ok is False and dbg["reason"] == "tape_hold_error"


def test_every_driver_monkeypatch_wrapper_forwards_kwargs():
    """AST guard: any ``def``/``lambda`` in the driver that captures the original callable as
    a ``_o=...`` default is a monkeypatch wrapper and must declare ``**kw``."""
    with open(DRIVER, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), DRIVER)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.Lambda)):
            continue
        args = node.args
        names = [a.arg for a in (args.args + args.kwonlyargs)]
        if "_o" not in names:
            continue
        if args.kwarg is None:
            label = getattr(node, "name", "<lambda>")
            offenders.append(f"{label} @ line {node.lineno}")
    assert not offenders, "monkeypatch wrappers without **kw (harness gate 14): " + ", ".join(offenders)


def test_driver_wraps_signed_tape_accel_features_with_the_forwarding_helper():
    with open(DRIVER, encoding="utf-8") as fh:
        src = fh.read()
    assert 'simclock_default_wrapper(_orig_staf, lr._utcnow, key="as_of")' in src
    assert "def _staf_simclock(" not in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
