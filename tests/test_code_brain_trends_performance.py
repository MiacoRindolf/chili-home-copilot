from __future__ import annotations

from app.services.code_brain import trends


def test_is_test_file_avoids_generator_any(monkeypatch) -> None:
    def fail_any(*_args, **_kwargs):
        raise AssertionError("_is_test_file should use direct substring checks")

    monkeypatch.setattr(trends, "any", fail_any, raising=False)

    assert trends._is_test_file("tests/test_code_brain.py")
    assert trends._is_test_file("app/foo_test.py")
    assert trends._is_test_file("web/Button.spec.ts")
    assert trends._is_test_file("web/Button.test.ts")
    assert not trends._is_test_file("app/services/code_brain/trends.py")


def test_is_test_file_is_case_insensitive() -> None:
    assert trends._is_test_file("APP/__TESTS__/Widget.PY")
