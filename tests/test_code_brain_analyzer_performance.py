from app.services.code_brain import analyzer


class _NoSplitString(str):
    def split(self, *args, **kwargs):  # pragma: no cover - called only on regression
        raise AssertionError("_estimate_complexity should not split every line")


class _Line(str):
    def strip(self, *args, **kwargs):
        return _NoSplitString(super().strip(*args, **kwargs))


def _legacy_estimate_complexity(lines: list[str]) -> float:
    branch_kw = {
        "if",
        "elif",
        "else",
        "for",
        "while",
        "except",
        "catch",
        "case",
        "switch",
        "&&",
        "||",
        "?",
    }
    score = 1
    for line in lines:
        stripped = line.strip()
        tokens = stripped.split()
        for kw in branch_kw:
            if kw in tokens or kw in stripped:
                score += 1
                break
    return min(score / max(len(lines), 1) * 100, 100.0)


def test_estimate_complexity_avoids_per_line_tokenization():
    lines = [
        _Line("def example(value):"),
        _Line("    if value:"),
        _Line("        return value"),
    ]

    assert analyzer._estimate_complexity(lines, "python") == 66.66666666666666


def test_estimate_complexity_preserves_legacy_substring_semantics():
    lines = [
        "def alpha(value):",
        "    if value:",
        "        return value",
        "    diff = value - 1",
        "    choice = a ? b : c",
        "    while choice:",
        "        break",
    ]

    assert analyzer._estimate_complexity(lines, "python") == _legacy_estimate_complexity(lines)
