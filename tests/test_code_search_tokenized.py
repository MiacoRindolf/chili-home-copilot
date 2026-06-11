"""Tokenized code search — natural-language queries must find code.

The historical search ran ILIKE '%<entire prompt>%' for every strategy, so
any query longer than one identifier matched nothing and the code agent
planned blind (filename-word fallback). Strategy 4 tokenizes the query and
scores per-term hits by coverage.
"""

from __future__ import annotations

import pytest

from app.models.code_brain import CodeRepo, CodeSearchEntry
from app.services.code_brain.search import _query_terms, search_code


# ── pure tokenizer ───────────────────────────────────────────────────────


def test_query_terms_drops_stopwords_and_short_tokens():
    terms = _query_terms("add a guard to the fetch_quote price in quotes service")
    assert "fetch_quote" in terms
    assert "price" in terms
    assert "quotes" in terms
    assert "the" not in terms and "to" not in terms and "a" not in terms


def test_query_terms_dedupes_and_caps():
    terms = _query_terms("alpha alpha beta gamma delta epsilon zeta eta theta")
    assert terms[0] == "alpha"
    assert len(terms) == len(set(terms)) <= 6


def test_query_terms_keeps_code_signal_words():
    assert "fix" in _query_terms("fix the broken validator")
    assert "error" in _query_terms("error handling for timeouts")


# ── DB search behavior ───────────────────────────────────────────────────


@pytest.fixture
def seeded_repo(db):
    repo = CodeRepo(name="r", path="/tmp/r", user_id=1)
    db.add(repo)
    db.flush()
    entries = [
        CodeSearchEntry(
            repo_id=repo.id, file_path="app/services/quotes.py",
            symbol_name="fetch_quote", symbol_type="function",
            signature="def fetch_quote(symbol: str) -> float",
            docstring="Fetch latest price for a symbol from the quotes API.",
            line_number=10,
        ),
        CodeSearchEntry(
            repo_id=repo.id, file_path="app/services/orders.py",
            symbol_name="place_order", symbol_type="function",
            signature="def place_order(symbol: str, qty: float)",
            docstring="Submit an order to the broker.",
            line_number=20,
        ),
        CodeSearchEntry(
            repo_id=repo.id, file_path="app/utils/strings.py",
            symbol_name="slugify", symbol_type="function",
            signature="def slugify(s: str) -> str",
            docstring="Lowercase and hyphenate.",
            line_number=5,
        ),
    ]
    db.add_all(entries)
    db.commit()
    return repo


def test_natural_language_query_finds_relevant_symbol(db, seeded_repo):
    """The regression: this query used to return [] (full-phrase ILIKE)."""
    results = search_code(db, "add validation for bad price in quote fetching", repo_id=seeded_repo.id)
    assert results, "tokenized search returned nothing for a natural-language query"
    assert results[0]["file"] == "app/services/quotes.py"


def test_coverage_ranks_multi_term_match_above_single(db, seeded_repo):
    results = search_code(db, "quote price symbol", repo_id=seeded_repo.id)
    files = [r["file"] for r in results]
    assert files[0] == "app/services/quotes.py"
    # orders.py matches only 'symbol' (signature) — lower coverage, ranks below.
    if "app/services/orders.py" in files:
        assert files.index("app/services/orders.py") > 0


def test_single_identifier_query_unchanged(db, seeded_repo):
    results = search_code(db, "slugify", repo_id=seeded_repo.id)
    assert results and results[0]["symbol"] == "slugify"
    assert results[0]["score"] == 1.0
