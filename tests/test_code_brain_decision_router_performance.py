from decimal import Decimal

from app.services.code_brain import decision_router


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.execute_calls = 0

    def execute(self, *_args, **_kwargs):
        self.execute_calls += 1
        return _Rows(self.rows)


def _ctx(brief_body: str) -> decision_router.TaskContext:
    return decision_router.TaskContext(
        task_id=1,
        title="Improve router",
        brief_body=brief_body,
        sub_path="",
        repo_id=1,
        repo_name="repo",
        intended_files=[],
        estimated_diff_loc=10,
        prior_failure_count=0,
        is_high_stakes=False,
    )


def test_glob_to_regex_reuses_cached_compilation(monkeypatch):
    decision_router._glob_to_regex.cache_clear()
    first = decision_router._glob_to_regex("app/services/**/*.py")

    def fail_compile(*_args, **_kwargs):
        raise AssertionError("_glob_to_regex should reuse cached compiled globs")

    monkeypatch.setattr(decision_router.re, "compile", fail_compile)

    second = decision_router._glob_to_regex("app/services/**/*.py")

    assert second is first
    assert second.match("app/services/code_brain/decision_router.py")
    assert decision_router._glob_to_regex.cache_info().hits == 1


def test_glob_to_regex_cache_is_bounded():
    assert decision_router._glob_to_regex.cache_info().maxsize == 1024


def test_novelty_score_uses_precompiled_token_regex(monkeypatch):
    def fail_findall(*_args, **_kwargs):
        raise AssertionError("_novelty_score should not call module-level re.findall")

    monkeypatch.setattr(decision_router.re, "findall", fail_findall)
    db = _FakeDb([("router tests already covered",)])

    score = decision_router._novelty_score(
        db,
        _ctx("router tests need cache coverage"),
    )

    assert score == Decimal("0.6")
    assert db.execute_calls == 1


def test_keyword_json_cache_reuses_parsed_tuple(monkeypatch):
    decision_router._keyword_tuple_from_json.cache_clear()
    raw = '["Router", "Cache"]'

    assert decision_router._keyword_tuple_from_json(raw) == ("router", "cache")

    def fail_json_loads(*_args, **_kwargs):
        raise AssertionError("keyword JSON should be served from the bounded cache")

    monkeypatch.setattr(decision_router.json, "loads", fail_json_loads)

    assert decision_router._keyword_tuple_from_json(raw) == ("router", "cache")
    assert decision_router._keyword_tuple_from_json.cache_info().hits == 1


def test_keyword_json_cache_is_bounded():
    assert decision_router._keyword_tuple_from_json.cache_info().maxsize == 2048


def test_keywords_from_raw_preserves_native_list_behavior(monkeypatch):
    def fail_json_loads(*_args, **_kwargs):
        raise AssertionError("native list keywords should not go through JSON")

    monkeypatch.setattr(decision_router.json, "loads", fail_json_loads)

    assert decision_router._keywords_from_raw(["Router", 123]) == ("router", "123")
