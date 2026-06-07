"""Guard against relative-import-depth bugs in the momentum_neural package.

`execution_family_registry` lives in ``app/services/trading/`` — exactly ONE
package above ``momentum_neural`` — so every import of it from inside
``momentum_neural`` must use TWO dots (``from ..execution_family_registry``).
A three-dot import resolves to the non-existent ``app.services.execution_family_registry``
and only blows up at RUNTIME when the (often lazy/function-local) import line
executes — e.g. when the auto-arm pass first tries to actually arm a candidate.
That is exactly how a broken arm path can hide behind a quiet market.

This test fails fast and statically so the class of bug can never ship again.
It also import-checks the modules whose arm path carried the original bug.
"""
from __future__ import annotations

import importlib
import pkgutil
import re

import app.services.trading.momentum_neural as momentum_pkg

# `execution_family_registry` is a sibling of `momentum_neural` (both under
# app.services.trading), so it is always reachable with two dots from inside
# the package. Three dots points one level too high.
_BAD = re.compile(r"^\s*from\s+\.\.\.execution_family_registry\s+import", re.MULTILINE)


def _iter_module_sources():
    pkg_dir = momentum_pkg.__path__[0]
    for mod in pkgutil.iter_modules([pkg_dir]):
        spec = importlib.util.find_spec(f"{momentum_pkg.__name__}.{mod.name}")
        if spec and spec.origin and spec.origin.endswith(".py"):
            with open(spec.origin, "r", encoding="utf-8") as fh:
                yield mod.name, spec.origin, fh.read()


def test_no_three_dot_execution_family_registry_imports():
    offenders = []
    for name, origin, src in _iter_module_sources():
        for m in _BAD.finditer(src):
            line_no = src[: m.start()].count("\n") + 1
            offenders.append(f"{name}.py:{line_no}")
    assert not offenders, (
        "execution_family_registry must be imported with TWO dots from "
        "momentum_neural (it is a sibling under app.services.trading). "
        f"Three-dot offenders: {offenders}"
    )


def test_arm_path_modules_import_clean():
    # These are the modules on the live-arm path; the original bug lived as a
    # lazy import inside operator_actions.begin_live_arm and never ran until an
    # arm fired. Importing them here is cheap and asserts the venue helper is
    # actually reachable from where operator_actions expects it.
    oa = importlib.import_module(
        "app.services.trading.momentum_neural.operator_actions"
    )
    importlib.import_module("app.services.trading.momentum_neural.auto_arm")
    assert hasattr(oa, "venue_for_execution_family")
