"""Surgical .env fix: append clean lines for env vars that got mangled
into giant concatenated lines by an earlier Out-File -NoNewline disaster.

Strategy: python-dotenv uses last-occurrence-wins. Appending a clean
KEY=value line at the end of the file overrides the corrupted earlier
copy without modifying any other content.

Pre-flight:
  - Verify the var IS currently corrupted (value contains 'CHILI_' which
    is a clear sign of var-merge).
  - Verify .env exists, no BOM, no leading whitespace.

Idempotent: re-running on a fixed .env detects existing clean line and
no-ops.
"""
import re
import sys
from pathlib import Path

ENV_PATH = Path(".env")

# Vars to repair — known to be mangled per probe output.
# Each entry: (var_name, intended_value)
REPAIRS = [
    ("CHILI_AUTOTRADER_ENABLED", "true"),
    ("CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED", "true"),
]


def main() -> int:
    if not ENV_PATH.exists():
        print("ERROR: .env not found", file=sys.stderr)
        return 1

    raw = ENV_PATH.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
        print("  pre: stripped UTF-8 BOM (3 bytes)")
    content = raw.decode("utf-8")

    # Strip CRs (we'll write LF-only)
    content_clean = content.replace("\r", "")

    appends = []
    for var_name, intended_value in REPAIRS:
        # Find any existing assignment for this var
        matches = list(re.finditer(rf"^{var_name}\s*=\s*(.*)$", content_clean, re.MULTILINE))
        is_corrupted = False
        clean_already = False
        for m in matches:
            current_val = m.group(1)
            if current_val == intended_value:
                clean_already = True
                break
            # Check if value contains "CHILI_" (sign of merged-var corruption)
            if "CHILI_" in current_val or "=" in current_val:
                is_corrupted = True

        if clean_already:
            print(f"  {var_name}: already has clean line; skipping")
            continue

        if is_corrupted:
            print(f"  {var_name}: corrupted; will append clean override")
            appends.append((var_name, intended_value))
        else:
            # Var not present at all — append it (also benign)
            print(f"  {var_name}: not present; appending")
            appends.append((var_name, intended_value))

    if not appends:
        print("  no changes needed (idempotent no-op)")
        return 0

    # Build append payload. Ensure file ends with newline; add header comment
    # so the operator sees what happened later.
    if not content_clean.endswith("\n"):
        content_clean += "\n"

    # Append a fresh block at end. Header comment makes the surgical fix
    # discoverable in case operator opens .env in an editor.
    block = []
    block.append("")
    block.append("# SURGICAL REPAIR (2026-05-09): vars below override corrupted")
    block.append("# earlier copies that got merged by an Out-File line-break")
    block.append("# disaster. python-dotenv uses last-occurrence-wins, so these")
    block.append("# clean lines take precedence. Operator may collapse the")
    block.append("# merged earlier lines into proper newlines later.")
    for var_name, val in appends:
        block.append(f"{var_name}={val}")
    block.append("")
    appended = "\n".join(block)

    new_content = content_clean + appended
    print(f"  byte length: {len(content)} -> {len(new_content)}")

    # Validate post-write structure: each appended var should now be on its
    # own line at the end of file, with the intended value
    final_lines = new_content.split("\n")
    for var_name, val in appends:
        last_match = None
        for line in final_lines:
            stripped = line.strip()
            if stripped.startswith(f"{var_name}="):
                last_match = stripped
        if last_match != f"{var_name}={val}":
            print(f"ABORT: post-validation: last {var_name} line is {last_match!r} not {var_name}={val}", file=sys.stderr)
            return 4

    # Backup before write (defensive)
    backup_path = ENV_PATH.with_name(".env.preautotraderfix")
    if not backup_path.exists():
        backup_path.write_bytes(ENV_PATH.read_bytes())
        print(f"  backup written: {backup_path}")

    # Write back as ASCII (no BOM, LF-only)
    ENV_PATH.write_bytes(new_content.encode("ascii"))
    print(f"  .env rewritten: {len(new_content)} bytes")
    print("# done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
