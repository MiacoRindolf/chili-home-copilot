"""Read-only extractor: pulls COINBASE_API_KEY and COINBASE_API_SECRET
from the corrupted .env, prints them to stdout so the operator can copy
them into a fresh .env.

OPERATOR RUNS THIS MANUALLY in their own host terminal — NOT via the
Cowork daemon. The script's output contains the actual secret value
and must not be captured to any log file Cowork can read.

Usage (in operator's host PowerShell, NOT via _claude_pending.txt):
    cd C:\\dev\\chili-home-copilot
    python scripts\\d-env-extract-secrets.py

Does NOT write any file. Operator pastes output into their editor.
"""
import sys
from pathlib import Path

ENV_PATH = Path(".env")
KEY_PREFIX = "COINBASE_API_KEY="
SECRET_PREFIX = "COINBASE_API_SECRET="
PEM_BEGIN = "-----BEGIN EC PRIVATE KEY-----"
PEM_END = "-----END EC PRIVATE KEY-----"


def main() -> int:
    raw = ENV_PATH.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    content = raw.decode("utf-8", errors="replace")

    # Find KEY assignment + value (KEY value runs until COINBASE_API_SECRET=)
    key_idx = content.find(KEY_PREFIX)
    sec_idx = content.find(SECRET_PREFIX)
    if key_idx < 0 or sec_idx < 0 or sec_idx <= key_idx:
        print("ERROR: cannot locate KEY/SECRET in expected order", file=sys.stderr)
        return 1

    key_value = content[key_idx + len(KEY_PREFIX):sec_idx]
    # Strip any trailing whitespace/newlines that may have collapsed in
    key_value = key_value.rstrip("\r\n ")

    # Find SECRET value: from after SECRET= to after END marker (incl. trailing \n literal if any)
    pem_end_idx = content.find(PEM_END, sec_idx)
    if pem_end_idx < 0:
        print("ERROR: PEM END marker not found", file=sys.stderr)
        return 1
    sec_val_start = sec_idx + len(SECRET_PREFIX)
    sec_val_end = pem_end_idx + len(PEM_END)
    # Include trailing literal `\n` if present (operator typically pastes with trailing \n)
    if content[sec_val_end:sec_val_end + 2] == "\\n":
        sec_val_end += 2
    sec_value = content[sec_val_start:sec_val_end]

    # Print restoration template
    print()
    print("=" * 70)
    print("PASTE THIS INTO YOUR .env (replace the existing Coinbase section):")
    print("=" * 70)
    print()
    print(f"{KEY_PREFIX}{key_value}")
    print(f"{SECRET_PREFIX}{sec_value}")
    print()
    print("=" * 70)
    print("Quick verification:")
    print(f"  KEY  length: {len(key_value)} chars  starts: {key_value[:14]}...")
    print(f"  SEC  length: {len(sec_value)} chars  starts: {sec_value[:30]}...")
    print(f"  SEC  ends:   ...{sec_value[-32:]}")
    print()
    print("Each line above MUST be on its own line in .env, terminated by")
    print("a real newline. The PEM uses LITERAL `\\n` (backslash + n) inside")
    print("the value — do not convert those to real newlines.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
