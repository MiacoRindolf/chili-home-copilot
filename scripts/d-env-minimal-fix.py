"""Minimal-touch .env restoration: insert exactly 3 newlines at the
COINBASE_API_KEY / COINBASE_API_SECRET / PEM_END boundaries. Leaves the
rest of the giant-line blob alone (other env vars fall back to pydantic
defaults — same as current state).

Pre-conditions verified: KEY value intact, PEM body intact, all 3 pivot
positions present. Aborts cleanly otherwise.

Idempotent: re-running on a fixed .env detects existing newlines and
no-ops.
"""
import sys
from pathlib import Path

ENV_PATH = Path(".env")
KEY_PREFIX = "COINBASE_API_KEY="
SECRET_PREFIX = "COINBASE_API_SECRET="
PEM_BEGIN = "-----BEGIN EC PRIVATE KEY-----"
PEM_END = "-----END EC PRIVATE KEY-----"


def main() -> int:
    if not ENV_PATH.exists():
        print("ERROR: .env not found", file=sys.stderr)
        return 1

    raw = ENV_PATH.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
        print("  pre: stripped UTF-8 BOM (3 bytes)")
    content = raw.decode("utf-8")

    # Locate pivots
    key_idx = content.find(KEY_PREFIX)
    sec_idx = content.find(SECRET_PREFIX)
    pem_end_idx = content.find(PEM_END)

    if key_idx < 0 or sec_idx < 0 or pem_end_idx < 0:
        print(f"ABORT: missing pivot. key={key_idx} sec={sec_idx} pem_end={pem_end_idx}",
              file=sys.stderr)
        return 2
    if not (key_idx < sec_idx < pem_end_idx):
        print(f"ABORT: pivots out of order. key={key_idx} sec={sec_idx} pem_end={pem_end_idx}",
              file=sys.stderr)
        return 3

    # Compute insertion points
    pem_end_finish = pem_end_idx + len(PEM_END)
    # If there's a literal "\n" (backslash+n) after PEM end, it's part of
    # the secret value — include it
    if content[pem_end_finish:pem_end_finish + 2] == "\\n":
        pem_end_finish += 2

    print(f"  byte length before: {len(content)}")
    print(f"  pivots: key={key_idx} sec={sec_idx} pem_end_finish={pem_end_finish}")

    # Idempotency check: if newlines already exist immediately before each
    # pivot AND after pem_end_finish, the file is already fixed
    needs_nl_before_key = key_idx > 0 and content[key_idx - 1] != "\n"
    needs_nl_before_sec = sec_idx > 0 and content[sec_idx - 1] != "\n"
    needs_nl_after_pem = pem_end_finish < len(content) and content[pem_end_finish] != "\n"
    print(f"  needs_nl_before_key:  {needs_nl_before_key}")
    print(f"  needs_nl_before_sec:  {needs_nl_before_sec}")
    print(f"  needs_nl_after_pem:   {needs_nl_after_pem}")

    if not (needs_nl_before_key or needs_nl_before_sec or needs_nl_after_pem):
        print("  no changes needed (idempotent no-op)")
        return 0

    # Build new content by surgical insertion. Walk left-to-right and insert
    # newlines at the 3 boundaries.
    parts = []
    cursor = 0

    # Before COINBASE_API_KEY=
    if needs_nl_before_key:
        parts.append(content[cursor:key_idx])
        parts.append("\n")
        cursor = key_idx
    # KEY value runs from key_idx to sec_idx (assuming SECRET immediately follows)
    # Before COINBASE_API_SECRET=
    if needs_nl_before_sec:
        parts.append(content[cursor:sec_idx])
        parts.append("\n")
        cursor = sec_idx
    # SECRET value runs from sec_idx to pem_end_finish
    # After PEM_END (and trailing literal \n if present)
    if needs_nl_after_pem:
        parts.append(content[cursor:pem_end_finish])
        parts.append("\n")
        cursor = pem_end_finish
    # Tail
    parts.append(content[cursor:])

    new_content = "".join(parts)
    new_content = new_content.replace("\r", "")  # normalize line endings

    print(f"  byte length after: {len(new_content)}")

    # Validate: KEY line + SECRET line should now exist
    lines = new_content.split("\n")
    key_lines = [line for line in lines if line.startswith(KEY_PREFIX)]
    sec_lines = [line for line in lines if line.startswith(SECRET_PREFIX)]
    print(f"  KEY lines found: {len(key_lines)}")
    print(f"  SECRET lines found: {len(sec_lines)}")

    if len(key_lines) != 1 or len(sec_lines) != 1:
        print("ABORT: KEY or SECRET line count not 1", file=sys.stderr)
        return 4

    key_line = key_lines[0]
    sec_line = sec_lines[0]

    # Validate KEY line: should contain the known value
    KNOWN_KEY = "organizations/83a21581-e0e9-4b74-a4ce-47ee2264a9f2/apiKeys/ae81841f-b08f-42fd-8b7a-13ac15abf837"
    if KNOWN_KEY not in key_line:
        print(f"ABORT: known key value not in KEY line", file=sys.stderr)
        return 5

    # Validate SECRET line: should start with PEM_BEGIN, end with PEM_END (+ optional \n literal)
    sec_value = sec_line[len(SECRET_PREFIX):]
    if not sec_value.startswith(PEM_BEGIN):
        print(f"ABORT: SECRET line does not start with PEM_BEGIN", file=sys.stderr)
        return 6
    if not (sec_value.endswith(PEM_END) or sec_value.endswith(PEM_END + "\\n")):
        print(f"ABORT: SECRET line does not end correctly", file=sys.stderr)
        # Print last 40 chars (escaped) to diagnose
        end_repr = sec_value[-40:].replace('\\', '\\\\')
        print(f"  SECRET ends with: ...{end_repr}", file=sys.stderr)
        return 7

    # Backup before write
    backup_path = ENV_PATH.with_name(".env.preforensic")
    if not backup_path.exists():
        backup_path.write_bytes(ENV_PATH.read_bytes())
        print(f"  backup written: {backup_path}")

    # Write back as ASCII
    ENV_PATH.write_bytes(new_content.encode("ascii"))
    print(f"  .env rewritten (ASCII, {len(new_content)} bytes)")
    print("# done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
