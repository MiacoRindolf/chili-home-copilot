"""Finalize a captured PAPER activation envelope with local files only.

This is offline operator tooling, not a runtime authority source and not a
service entry point.  It never imports application settings, opens a database,
contacts a provider or broker, changes a task, or starts a process.  The final
manifest becomes usable only when the separately hash-bound PAPER service
loads and re-verifies it.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable, Sequence

from scripts import captured_paper_activation_contract as contract


UTC = timezone.utc
FINALIZER_REPORT_SCHEMA_VERSION = "chili.captured-paper-finalizer-report.v1"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preactivation", required=True)
    parser.add_argument("--preactivation-sha256", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--no-order-receipt", required=True)
    parser.add_argument("--no-order-receipt-sha256", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--allow-read-root", action="append", required=True)
    return parser


def finalize_offline(
    *,
    preactivation_path: str | Path,
    preactivation_sha256: str,
    candidate_root: str | Path,
    no_order_receipt_path: str | Path,
    no_order_receipt_sha256: str,
    output_root: str | Path,
    allowed_read_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> contract.BuiltCapturedPaperActivation:
    """Load preactivation and publish its content-addressed final envelope."""

    now = wall_clock()
    preactivation = contract.load_captured_paper_preactivation(
        preactivation_path,
        expected_manifest_sha256=preactivation_sha256,
        candidate_root=candidate_root,
        allowed_read_roots=allowed_read_roots,
        wall_clock=lambda: now,
    )
    return contract.finalize_captured_paper_activation(
        preactivation,
        no_order_smoke_path=no_order_receipt_path,
        no_order_smoke_sha256=no_order_receipt_sha256,
        output_root=output_root,
        allowed_read_roots=allowed_read_roots,
        generated_at=now,
        wall_clock=lambda: now,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        built = finalize_offline(
            preactivation_path=args.preactivation,
            preactivation_sha256=args.preactivation_sha256,
            candidate_root=args.candidate_root,
            no_order_receipt_path=args.no_order_receipt,
            no_order_receipt_sha256=args.no_order_receipt_sha256,
            output_root=args.output_root,
            allowed_read_roots=tuple(args.allow_read_root),
        )
        report = {
            "schema_version": FINALIZER_REPORT_SCHEMA_VERSION,
            "verdict": "CAPTURED_ALPACA_PAPER_FINAL_MANIFEST_PUBLISHED",
            "manifest_path": str(built.manifest_path),
            "manifest_sha256": built.manifest_sha256,
            "preactivation_manifest_sha256": (
                built.preactivation_manifest_sha256
            ),
            "no_order_smoke_sha256": built.no_order_smoke_sha256,
            "offline_tooling_only": True,
            "paper_service_started": False,
            "orders_submitted": False,
            "live_cash_authorized": False,
        }
        exit_code = 0
    except (contract.CapturedPaperActivationContractError, OSError, ValueError) as exc:
        report = {
            "schema_version": FINALIZER_REPORT_SCHEMA_VERSION,
            "verdict": "CAPTURED_ALPACA_PAPER_FINALIZATION_REJECTED",
            "error_code": str(getattr(exc, "code", "OFFLINE_FINALIZATION_REJECTED")),
            "offline_tooling_only": True,
            "paper_service_started": False,
            "orders_submitted": False,
            "live_cash_authorized": False,
        }
        exit_code = 2
    sys.stdout.buffer.write(_canonical_json_bytes(report) + b"\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FINALIZER_REPORT_SCHEMA_VERSION",
    "finalize_offline",
    "main",
]
