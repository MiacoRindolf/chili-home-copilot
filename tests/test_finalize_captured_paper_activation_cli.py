from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from scripts import captured_paper_activation_contract as contract
from scripts import finalize_captured_paper_activation as cli


UTC = timezone.utc


def test_finalize_offline_forwards_one_clock_and_every_explicit_hash(
    monkeypatch,
) -> None:
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    preactivation = object()
    built = object()
    calls: dict[str, object] = {}

    def fake_load(path, **kwargs):
        calls["load"] = (path, kwargs)
        assert kwargs["wall_clock"]() == now
        return preactivation

    def fake_finalize(value, **kwargs):
        calls["finalize"] = (value, kwargs)
        assert kwargs["wall_clock"]() == now
        return built

    monkeypatch.setattr(contract, "load_captured_paper_preactivation", fake_load)
    monkeypatch.setattr(contract, "finalize_captured_paper_activation", fake_finalize)

    result = cli.finalize_offline(
        preactivation_path="C:/sealed/pre.json",
        preactivation_sha256="a" * 64,
        candidate_root="D:/candidate",
        no_order_receipt_path="C:/sealed/no-order.json",
        no_order_receipt_sha256="b" * 64,
        output_root="C:/sealed/final",
        allowed_read_roots=("C:/sealed", "D:/candidate"),
        wall_clock=lambda: now,
    )

    assert result is built
    load_path, load_kwargs = calls["load"]
    assert load_path == "C:/sealed/pre.json"
    assert load_kwargs["expected_manifest_sha256"] == "a" * 64
    assert load_kwargs["candidate_root"] == "D:/candidate"
    value, finalize_kwargs = calls["finalize"]
    assert value is preactivation
    assert finalize_kwargs["no_order_smoke_sha256"] == "b" * 64
    assert finalize_kwargs["generated_at"] == now
    assert finalize_kwargs["allowed_read_roots"] == (
        "C:/sealed",
        "D:/candidate",
    )


def test_cli_prints_only_local_final_manifest_identity(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_finalize_offline(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            manifest_path=Path("C:/sealed/final/ab/manifest.json"),
            manifest_sha256="c" * 64,
            preactivation_manifest_sha256="a" * 64,
            no_order_smoke_sha256="b" * 64,
        )

    monkeypatch.setattr(cli, "finalize_offline", fake_finalize_offline)
    exit_code = cli.main(
        [
            "--preactivation",
            "C:/sealed/pre.json",
            "--preactivation-sha256",
            "a" * 64,
            "--candidate-root",
            "D:/candidate",
            "--no-order-receipt",
            "C:/sealed/no-order.json",
            "--no-order-receipt-sha256",
            "b" * 64,
            "--output-root",
            "C:/sealed/final",
            "--allow-read-root",
            "C:/sealed",
            "--allow-read-root",
            "D:/candidate",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["manifest_sha256"] == "c" * 64
    assert report["offline_tooling_only"] is True
    assert report["paper_service_started"] is False
    assert report["orders_submitted"] is False
    assert report["live_cash_authorized"] is False
    assert captured["allowed_read_roots"] == ("C:/sealed", "D:/candidate")


def test_cli_rejection_never_claims_service_or_order_activity(
    monkeypatch, capsys
) -> None:
    def reject(**_kwargs):
        raise contract.CapturedPaperActivationContractError(
            "HASH_MISMATCH", "test-only rejection"
        )

    monkeypatch.setattr(cli, "finalize_offline", reject)
    exit_code = cli.main(
        [
            "--preactivation",
            "C:/sealed/pre.json",
            "--preactivation-sha256",
            "a" * 64,
            "--candidate-root",
            "D:/candidate",
            "--no-order-receipt",
            "C:/sealed/no-order.json",
            "--no-order-receipt-sha256",
            "b" * 64,
            "--output-root",
            "C:/sealed/final",
            "--allow-read-root",
            "C:/sealed",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert report["error_code"] == "HASH_MISMATCH"
    assert report["paper_service_started"] is False
    assert report["orders_submitted"] is False
    assert report["live_cash_authorized"] is False


def test_finalizer_module_imports_no_runtime_or_external_io_library() -> None:
    source_path = Path(cli.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert not any(name == "app" or name.startswith("app.") for name in imports)
    assert {"requests", "sqlalchemy", "socket", "subprocess"}.isdisjoint(imports)
