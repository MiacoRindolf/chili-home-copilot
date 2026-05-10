"""Forensic restoration of .env after PowerShell Out-File line-break stripping.

Strategy: read .env content, identify env-var-name boundaries, insert proper
newlines, write back as ASCII bytes (no BOM). Preserves all values verbatim
including the multi-line PEM private key (which uses literal `\\n` escapes).

Validation: pre + post structural checks. Aborts without writing if any
expected line is missing or malformed. Idempotent: re-running on a fixed
.env is a no-op.

Outputs status to stdout; never prints any value.
"""
import os
import re
import sys
from pathlib import Path

ENV_PATH = Path(".env")
KNOWN_KEY_VALUE = (
    "organizations/83a21581-e0e9-4b74-a4ce-47ee2264a9f2/"
    "apiKeys/ae81841f-b08f-42fd-8b7a-13ac15abf837"
)
PEM_BEGIN = "-----BEGIN EC PRIVATE KEY-----"
PEM_END = "-----END EC PRIVATE KEY-----"

# Env var name pattern. Allow A-Za-z + digits + underscore. Min 3 chars
# (the first char + 2 more) to reduce false positives in base64 strings
# while catching typical env var names like RH_*, etc.
# Excludes the PEM body via separate logic in reconstruct().
ENV_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}(?==)")


def read_env() -> str:
    raw = ENV_PATH.read_bytes()
    # Strip leading UTF-8 BOM if present (defensive)
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
        print(f"  pre: stripped leading UTF-8 BOM (3 bytes)")
    # Decode as UTF-8 (PEM body is ASCII; URLs are ASCII; safe)
    return raw.decode("utf-8")


def write_env_ascii(content: str) -> None:
    # Write as ASCII (rejects any non-ASCII; .env should be all ASCII)
    ENV_PATH.write_bytes(content.encode("ascii"))


def validate_pre(content: str) -> dict:
    """Verify the file has the data we need to reconstruct."""
    checks = {}
    checks["has_known_key"] = KNOWN_KEY_VALUE in content
    checks["has_pem_begin"] = PEM_BEGIN in content
    checks["has_pem_end"] = PEM_END in content
    checks["has_secret_assignment"] = "COINBASE_API_SECRET=" in content
    checks["has_key_assignment"] = "COINBASE_API_KEY=" in content
    return checks


def reconstruct(content: str) -> str:
    """Insert newlines before every env-var-name pattern, EXCEPT inside the
    PEM block (which contains its own internal structure that must not be
    split).

    Also normalize: no leading whitespace; one trailing newline; no CR.
    """
    # Identify the PEM region's boundaries to PROTECT from splitting
    pem_start_idx = content.find(PEM_BEGIN)
    pem_end_idx = content.find(PEM_END)
    if pem_start_idx < 0 or pem_end_idx < 0 or pem_end_idx < pem_start_idx:
        raise ValueError("PEM markers not found or out of order")
    pem_value_end = pem_end_idx + len(PEM_END)

    # Strip any CRs (we'll write LF-only)
    content = content.replace("\r", "")

    # Walk through and split at env-var-name positions outside the PEM range.
    # Positions of env-var-name matches:
    splits = []
    for m in ENV_VAR_NAME_RE.finditer(content):
        pos = m.start()
        # Recompute PEM bounds in current content (after CR strip)
        # We do this once after CR strip; positions shift if CRs were present
        # but PEM_BEGIN/PEM_END don't contain CR so the find is stable.
        cur_pem_start = content.find(PEM_BEGIN)
        cur_pem_end = content.find(PEM_END) + len(PEM_END)
        if cur_pem_start <= pos < cur_pem_end:
            continue  # protected: inside PEM body
        splits.append(pos)

    if not splits:
        raise ValueError("no env-var-name patterns found")

    # Build new content with \n inserted before each split position (except 0)
    # Walk left-to-right
    out = []
    last = 0
    for idx, pos in enumerate(splits):
        chunk = content[last:pos]
        # Strip trailing whitespace/newlines from the chunk (we'll add fresh \n)
        chunk = chunk.rstrip("\n").rstrip()
        if chunk and idx > 0:
            out.append(chunk + "\n")
        elif chunk:
            out.append(chunk + "\n")
        last = pos
    # Append the final chunk (from last split to end)
    final = content[last:].rstrip("\n").rstrip()
    if final:
        out.append(final + "\n")

    new_content = "".join(out)
    # Ensure single trailing newline
    new_content = new_content.rstrip("\n") + "\n"
    return new_content


def validate_post(content: str) -> dict:
    """Verify reconstruction looks structurally sane."""
    lines = content.split("\n")
    checks = {}
    checks["line_count"] = len(lines)
    checks["has_known_key_line"] = any(
        line.startswith("COINBASE_API_KEY=") and KNOWN_KEY_VALUE in line
        for line in lines
    )
    secret_lines = [line for line in lines if line.startswith("COINBASE_API_SECRET=")]
    checks["has_secret_line"] = len(secret_lines) == 1
    if checks["has_secret_line"]:
        sec_val = secret_lines[0][len("COINBASE_API_SECRET="):]
        checks["secret_starts_with_pem_begin"] = sec_val.startswith(PEM_BEGIN)
        checks["secret_ends_with_pem_end"] = sec_val.rstrip("\\n").endswith(PEM_END) or sec_val.endswith(PEM_END + "\\n")
    # No giant lines (>1000 chars) other than the secret line
    long_lines = [
        (i, len(line))
        for i, line in enumerate(lines)
        if len(line) > 1000 and not line.startswith("COINBASE_API_SECRET=")
    ]
    checks["no_unexpected_giant_lines"] = (len(long_lines) == 0)
    if long_lines:
        checks["long_lines_detail"] = long_lines
    return checks


def main() -> int:
    if not ENV_PATH.exists():
        print("ERROR: .env not found", file=sys.stderr)
        return 1

    print("# step A: read .env")
    content = read_env()
    print(f"  byte length: {len(content.encode('utf-8'))}")

    print("# step B: validate-pre")
    pre = validate_pre(content)
    for k, v in pre.items():
        print(f"  {k}: {v}")
    if not all(pre.values()):
        print("ABORT: pre-validation failed; cannot reconstruct safely")
        return 2

    print("# step C: reconstruct line breaks")
    try:
        new_content = reconstruct(content)
    except Exception as e:
        print(f"ABORT: reconstruction failed: {e}")
        return 3
    print(f"  byte length after: {len(new_content.encode('ascii'))}")

    print("# step D: validate-post")
    post = validate_post(new_content)
    for k, v in post.items():
        print(f"  {k}: {v}")
    must_pass = ["has_known_key_line", "has_secret_line", "no_unexpected_giant_lines"]
    if not all(post.get(k, False) for k in must_pass):
        print("ABORT: post-validation failed; .env unchanged")
        # Diagnostic: print first 80 chars of any unexpected long lines (mask values)
        for i, line in enumerate(new_content.split("\n")):
            if len(line) > 1000 and not line.startswith("COINBASE_API_SECRET="):
                head = line[:80]
                masked = re.sub(r"=.*$", "=<MASKED>", head)
                print(f"  long_line {i} ({len(line)} chars) starts: {masked}")
        return 4

    print("# step E: write back as ASCII")
    # Backup current .env before write
    backup_path = ENV_PATH.with_suffix(".env.preforensic")
    if not backup_path.exists():
        backup_path.write_bytes(ENV_PATH.read_bytes())
        print(f"  backup written: {backup_path}")
    write_env_ascii(new_content)
    print(f"  .env rewritten: {len(new_content.encode('ascii'))} bytes")

    print("# step F: final structural sanity")
    final_lines = new_content.split("\n")
    print(f"  total lines: {len(final_lines)}")
    print(f"  COINBASE_API_KEY line present: {any(line.startswith('COINBASE_API_KEY=') for line in final_lines)}")
    print(f"  COINBASE_API_SECRET line present: {any(line.startswith('COINBASE_API_SECRET=') for line in final_lines)}")
    print(f"  CHILI_COINBASE_AUTOTRADER_LIVE line present: {any(line.startswith('CHILI_COINBASE_AUTOTRADER_LIVE=') for line in final_lines)}")
    print("# done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
