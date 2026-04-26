"""Trivial / vacuous test pattern detection (Phase D.2.5)."""
from __future__ import annotations

from app.services.code_dispatch import validation_audit


def test_pass_only_body(tmp_path) -> None:
    f = tmp_path / "test_x.py"
    f.write_text("def test_x():\n    pass\n", encoding="utf-8")
    fings = validation_audit.diff_adds_trivial_tests(
        [str(f.name)], str(tmp_path)
    )
    assert fings, "expected a finding for pass-only test"
    assert any(x["kind"] == "module_all_trivial" for x in fings)


def test_assert_true(tmp_path) -> None:
    f = tmp_path / "test_a.py"
    f.write_text("def test_r():\n    assert True\n", encoding="utf-8")
    fings = validation_audit.diff_adds_trivial_tests(
        [str(f.name)], str(tmp_path)
    )
    assert fings
    assert any("assert_true" in x["kind"] for x in fings) or any(
        x["kind"] == "module_all_trivial" for x in fings
    )


def test_assert_self_equal(tmp_path) -> None:
    f = tmp_path / "test_eq.py"
    f.write_text(
        "def test_m():\n    assert foo == foo\n", encoding="utf-8"
    )
    fings = validation_audit.diff_adds_trivial_tests(
        [str(f.name)], str(tmp_path)
    )
    assert fings, "self-eq assert is a vacuous one-liner"
    assert any("self_eq" in x["kind"] for x in fings) or any(
        x["kind"] == "module_all_trivial" for x in fings
    )


def test_xfail_no_reason(tmp_path) -> None:
    f = tmp_path / "test_xfail.py"
    f.write_text(
        "import pytest\n"
        "@pytest.mark.xfail\n"
        "def test_m():\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    fings = validation_audit.diff_adds_trivial_tests(
        [str(f.name)], str(tmp_path)
    )
    assert any(
        x["kind"] == "skip_or_xfail_no_reason" for x in fings
    ), fings


def test_real_test_passes(tmp_path) -> None:
    f = tmp_path / "test_ok.py"
    f.write_text(
        "def test_something():\n"
        "    x = 1\n"
        "    y = 2\n"
        "    assert x + y == 3\n",
        encoding="utf-8",
    )
    fings = validation_audit.diff_adds_trivial_tests(
        [str(f.name)], str(tmp_path)
    )
    assert not fings


def test_non_test_file_ignored(tmp_path) -> None:
    f = tmp_path / "helpers.py"
    f.write_text("def test_thing():\n    pass\n", encoding="utf-8")
    fings = validation_audit.diff_adds_trivial_tests(
        [str(f.name)], str(tmp_path)
    )
    assert not fings


def test_module_with_only_trivial(tmp_path) -> None:
    f = tmp_path / "test_mod.py"
    f.write_text(
        "def test_a():\n    pass\n\ndef test_b():\n    pass\n", encoding="utf-8"
    )
    fings = validation_audit.diff_adds_trivial_tests(
        [str(f.name)], str(tmp_path)
    )
    assert any(x["kind"] == "module_all_trivial" for x in fings)
