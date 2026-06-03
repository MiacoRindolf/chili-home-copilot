"""Guard tests for CHILI OS window snap-to-thirds (WS-61).

On wide monitors, ⌘⌥←/→ cycles the focused window through half → one-third →
two-thirds on that side (repeat to advance), and ChiliOS.snap(app, zone) exposes
the tiling zones. DB-free source guards.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")
_TEMPLATES = os.path.join(_HERE, "..", "app", "templates")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_snap_handles_third_zones():
    src = _read(_STATIC, "js", "os.js")
    for zone in ("'lthird'", "'l2third'", "'rthird'", "'r2third'"):
        assert zone in src, f"snap() lost the {zone} zone"
    # The geometry uses W/3 and 2*W/3.
    assert "(W / 3)" in src and "(2 * W / 3)" in src, "third-width geometry changed unexpectedly"


def test_arrow_keys_cycle_via_cycleSnap():
    src = _read(_STATIC, "js", "os.js")
    assert "function cycleSnap" in src, "cycleSnap (half→third→two-thirds) is gone"
    assert "cycleSnap(el, 'left')" in src and "cycleSnap(el, 'right')" in src, \
        "⌘⌥←/→ no longer drive the thirds cycle"
    assert "dataset.cyc" in src, "the per-window cycle state is gone"


def test_snap_api_exposed():
    src = _read(_STATIC, "js", "os.js")
    assert "snap: function (app, zone)" in src, "ChiliOS.snap API is gone"


def test_help_mentions_thirds():
    shell = _read(_TEMPLATES, "_workspace.html")
    assert "thirds" in shell.lower(), "the shortcuts help no longer documents thirds"
