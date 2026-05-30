"""Validate a governed evidence consumer manifest JSON file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.trading.evidence_consumer_manifest import validate_consumer_manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail closed unless a consumer manifest binds the current v34 blocker "
            "index and v14 validator spec by exact path and SHA256."
        )
    )
    parser.add_argument("manifest", type=Path, help="Path to the consumer manifest JSON file.")
    args = parser.parse_args()

    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        result = {
            "accepted": False,
            "status": "FAIL_CLOSED",
            "errors": [f"manifest_read_or_parse_error:{type(exc).__name__}"],
        }
    else:
        result = validate_consumer_manifest(manifest).to_dict()

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
