from unittest.mock import MagicMock

from app.services.code_brain import reviewer


def test_code_reviewer_routes_llm_through_code_review_purpose(monkeypatch):
    llm = MagicMock(
        return_value=(
            "SUMMARY: Adds a safe cache.\n"
            'FINDINGS: [{"severity":"info","category":"test-coverage","message":"ok","file":"x.py"}]\n'
            "SCORE: 8"
        )
    )
    monkeypatch.setattr("app.services.llm_caller.call_llm", llm)

    result = reviewer._review_diff_with_llm(
        "diff --git a/x.py b/x.py\n+print('hi')\n",
        "Repo: demo",
        {"hash": "abcdef123456", "author": "dev", "message": "cache work"},
    )

    assert result["summary"] == "Adds a safe cache."
    assert result["score"] == 8.0
    assert result["findings"][0]["file"] == "x.py"
    assert llm.call_args.kwargs["purpose"] == "code_review"
    assert llm.call_args.kwargs["cacheable"] is True
