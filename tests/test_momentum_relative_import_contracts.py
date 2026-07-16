from __future__ import annotations

import ast
import importlib
from pathlib import Path


PACKAGE = "app.services.trading.momentum_neural"
ROOT = Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "momentum_neural"


def test_momentum_relative_import_contracts_exist() -> None:
    missing: list[str] = []
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
                continue
            if any(alias.name == "*" for alias in node.names):
                continue
            try:
                module = importlib.import_module(f"{PACKAGE}.{node.module}")
            except ModuleNotFoundError as exc:
                missing.append(f"{path.name}: module .{node.module} ({exc})")
                continue
            for alias in node.names:
                if not hasattr(module, alias.name):
                    missing.append(f"{path.name}: .{node.module}.{alias.name}")
    assert missing == []
