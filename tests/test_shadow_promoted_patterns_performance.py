from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_shadow_promoted_patterns_ptr_count_is_scoped_to_visible_patterns():
    src = (REPO / "app/routers/trading_sub/ai.py").read_text(encoding="utf-8")

    assert 'bindparam("pattern_ids", expanding=True)' in src
    assert "WHERE scan_pattern_id IN :pattern_ids" in src
    assert (
        "WHERE scan_pattern_id IS NOT NULL\n"
        "                GROUP BY scan_pattern_id"
    ) not in src
