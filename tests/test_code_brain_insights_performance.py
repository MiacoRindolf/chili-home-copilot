from __future__ import annotations

from types import SimpleNamespace

from app.services.code_brain import insights


def test_top_complexity_files_uses_bounded_heap(monkeypatch) -> None:
    def fail_sort(*_args, **_kwargs):
        raise AssertionError("top complexity evidence should not sort every snapshot")

    monkeypatch.setattr(insights, "sorted", fail_sort, raising=False)
    snapshots = [
        SimpleNamespace(file_path=f"app/file_{i}.py", complexity_score=float(i))
        for i in range(100)
    ]

    top = insights._top_complexity_files(snapshots, limit=5)

    assert top == [
        "app/file_99.py",
        "app/file_98.py",
        "app/file_97.py",
        "app/file_96.py",
        "app/file_95.py",
    ]


def test_top_complexity_files_handles_empty_and_non_positive_limits() -> None:
    assert insights._top_complexity_files([], limit=20) == []
    assert insights._top_complexity_files(
        [SimpleNamespace(file_path="app/a.py", complexity_score=10.0)],
        limit=0,
    ) == []
