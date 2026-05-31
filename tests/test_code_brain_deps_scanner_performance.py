from __future__ import annotations

import json
from collections import OrderedDict

from app.models.code_brain import CodeDepAlert
from app.services.code_brain import deps_scanner
from app.services.code_brain.deps_scanner import _alerts_by_package


class _NoSnapshotOrderedDict(OrderedDict):
    def items(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot items")

    def keys(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot keys")

    def values(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot values")


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filter_calls = 0

    def filter(self, *_args, **_kwargs):
        self.filter_calls += 1
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None
        self.query_calls = 0

    def query(self, model):
        assert model is CodeDepAlert
        self.query_calls += 1
        self.last_query = _FakeQuery(self._rows)
        return self.last_query


def test_alerts_by_package_batches_lookup():
    rows = [
        CodeDepAlert(repo_id=7, package_name="fastapi"),
        CodeDepAlert(repo_id=7, package_name="pytest"),
    ]
    db = _FakeSession(rows)

    result = _alerts_by_package(db, 7, ["fastapi", "pytest", "fastapi"])

    assert sorted(result) == ["fastapi", "pytest"]
    assert result["fastapi"].package_name == "fastapi"
    assert db.query_calls == 1
    assert db.last_query.filter_calls == 1


def test_alerts_by_package_skips_empty_names():
    db = _FakeSession([])

    assert _alerts_by_package(db, 7, []) == {}
    assert db.query_calls == 0


def test_latest_cache_get_removes_stale_entry() -> None:
    cache = OrderedDict({"fastapi": ("1.0.0", 900.0)})

    assert deps_scanner._latest_cache_get(cache, "fastapi", now=5_000.0) is None

    assert "fastapi" not in cache


def test_latest_cache_get_refreshes_hit_recency() -> None:
    cache = OrderedDict(
        {
            "fastapi": ("1.0.0", 1_000.0),
            "pytest": ("2.0.0", 1_000.0),
        }
    )

    assert deps_scanner._latest_cache_get(cache, "fastapi", now=1_001.0) == "1.0.0"

    assert list(cache) == ["pytest", "fastapi"]


def test_latest_cache_set_prunes_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(deps_scanner, "_LATEST_CACHE_MAX", 3)
    cache = _NoSnapshotOrderedDict(
        (f"pkg-{idx}", (f"{idx}.0.0", 990.0 + idx))
        for idx in range(4)
    )

    deps_scanner._latest_cache_set(cache, "new", "5.0.0", now=1_000.0)

    assert list(cache) == ["pkg-2", "pkg-3", "new"]


def test_parse_version_uses_precompiled_pattern(monkeypatch) -> None:
    def fail_findall(*_args, **_kwargs):
        raise AssertionError("_parse_version should not call module-level re.findall")

    monkeypatch.setattr(deps_scanner.re, "findall", fail_findall)

    assert deps_scanner._parse_version("2.13.4-rc1") == (2, 13, 4)


def test_parse_requirements_uses_precompiled_patterns(tmp_path, monkeypatch) -> None:
    (tmp_path / "requirements.txt").write_text(
        "fastapi>=0.110.0\npytest ~= 8.2\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry.dependencies]\nhttpx = \"0.27.0\"\n",
        encoding="utf-8",
    )

    def fail_match(*_args, **_kwargs):
        raise AssertionError("_parse_requirements should not call module-level re.match")

    monkeypatch.setattr(deps_scanner.re, "match", fail_match)

    deps = deps_scanner._parse_requirements(tmp_path)

    assert [dep["name"] for dep in deps] == ["fastapi", "pytest", "httpx"]


def test_parse_package_json_uses_precompiled_version_cleaner(tmp_path, monkeypatch) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"vite": "^5.2.0"}}),
        encoding="utf-8",
    )

    def fail_sub(*_args, **_kwargs):
        raise AssertionError("_parse_package_json should not call module-level re.sub")

    monkeypatch.setattr(deps_scanner.re, "sub", fail_sub)

    assert deps_scanner._parse_package_json(tmp_path) == [
        {"name": "vite", "current_version": "5.2.0", "ecosystem": "npm"}
    ]
