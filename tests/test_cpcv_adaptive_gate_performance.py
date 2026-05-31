from __future__ import annotations

import builtins

from app.services.trading import cpcv_adaptive_gate as gate


def test_z_from_ci_caches_repeated_ci_levels(monkeypatch) -> None:
    gate._z_from_ci.cache_clear()
    real_import = builtins.__import__
    scipy_imports = 0

    def counting_import(name, *args, **kwargs):
        nonlocal scipy_imports
        if name == "scipy.stats":
            scipy_imports += 1
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", counting_import)

    first = gate._z_from_ci(0.90)
    imports_after_first = scipy_imports
    second = gate._z_from_ci(0.90)

    assert first == second
    assert imports_after_first > 0
    assert scipy_imports == imports_after_first
    info = gate._z_from_ci.cache_info()
    assert info.hits == 1
    assert info.maxsize == 32


def test_z_from_ci_cache_is_bounded() -> None:
    gate._z_from_ci.cache_clear()

    for i in range(40):
        gate._z_from_ci(0.50 + i / 1000)

    info = gate._z_from_ci.cache_info()
    assert info.maxsize == 32
    assert info.currsize == 32
