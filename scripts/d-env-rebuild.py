"""Comprehensive .env line-break reconstruction.

Strategy: insert newlines BEFORE every known env-var-name prefix and
before every comment marker (`# `), wherever they appear mid-line.
PEM region is protected — no modifications inside the
`-----BEGIN EC PRIVATE KEY-----...-----END EC PRIVATE KEY-----` span.

VALIDATION (lossless guarantee): SHA256 hash of all NON-WHITESPACE
characters in the original is computed before reconstruction. After
reconstruction (which only adds `\n` characters — never adds or removes
any other character), the same hash is computed on the new content. If
the hashes don't match, ABORT without writing.

Backup written to .env.prerebuild before any change.
"""
import hashlib
import re
import sys
from pathlib import Path

ENV_PATH = Path(".env")
BACKUP_PATH = Path(".env.prerebuild")

# Known env var name prefixes (uppercase, terminated by either underscore
# continuation or `=`). Order matters slightly: longer prefixes first so we
# don't match a shorter prefix that's a substring of a longer one.
# Each entry is the prefix pattern WITHOUT the trailing `=`.
KNOWN_PREFIXES = [
    # Multi-segment prefixes first
    "TRADING_BRAIN_",
    "CHILI_DISPATCH_",
    "CHILI_PATTERN_SURVIVAL_",
    "CHILI_PATTERN_REGIME_",
    "CHILI_PATTERN_EVIDENCE_",
    "CHILI_AUTOTRADER_",
    "CHILI_BRACKET_",
    "CHILI_COINBASE_",
    "CHILI_ROBINHOOD_",
    "CHILI_FAST_PATH_",
    "CHILI_CRYPTO_",
    "CHILI_OPTIONS_",
    "CHILI_PERPS_",
    "CHILI_STRATEGY_",
    "CHILI_CPCV_",
    "BRAIN_PATTERN_REGIME_",
    "BRAIN_LIVE_BRACKETS_",
    "BRAIN_DRIFT_MONITOR_",
    "BRAIN_DIVERGENCE_SCORER_",
    "BRAIN_POSITION_SIZER_",
    "BRAIN_RISK_DIAL_",
    "BRAIN_CAPITAL_REWEIGHT_",
    "BRAIN_RECERT_QUEUE_",
    "BRAIN_TICKER_REGIME_",
    "BRAIN_VOL_DISPERSION_",
    "BRAIN_INTRADAY_SESSION_",
    "BRAIN_MACRO_REGIME_",
    "BRAIN_BREADTH_RELSTR_",
    "BRAIN_CROSS_ASSET_",
    "BRAIN_NET_EDGE_",
    "BRAIN_EXIT_ENGINE_",
    "BRAIN_ECONOMIC_LEDGER_",
    "BRAIN_PIT_AUDIT_",
    "BRAIN_TRIPLE_BARRIER_",
    "BRAIN_PROMOTION_",
    "BRAIN_EXECUTION_COST_",
    "BRAIN_EXECUTION_CAPACITY_",
    "BRAIN_VENUE_TRUTH_",
    "BRAIN_RISK_",
    "BRAIN_OPS_HEALTH_",
    "BRAIN_PREDICTION_",
    # Single-segment prefixes
    "PATTERN_IMMINENT_",
    "PAID_OPENAI_",
    "TEST_DATABASE_URL",
    "DATABASE_URL",
    "USE_POLYGON",
    # Generic prefixes (LAST — catch-all by namespace)
    "TELEGRAM_",
    "ROBINHOOD_",
    "POLYGON_",
    "MASSIVE_",
    "OLLAMA_",
    "PREMIUM_",
    "MASSIVE_",
    "EMAIL_",
    "SMTP_",
    "SMS_",
    "ZEROX_",
    "COINBASE_",
    "LLM_",
    "BRAIN_",
    "CHILI_",
]

PEM_BEGIN = "-----BEGIN EC PRIVATE KEY-----"
PEM_END = "-----END EC PRIVATE KEY-----"


def hash_nonwhitespace(s: str) -> str:
    """SHA256 of all non-whitespace chars. Whitespace can change, content can't."""
    stripped = "".join(c for c in s if not c.isspace())
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()


def find_pem_region(content: str) -> tuple[int, int] | None:
    pem_start = content.find(PEM_BEGIN)
    if pem_start < 0:
        return None
    pem_end = content.find(PEM_END, pem_start)
    if pem_end < 0:
        return None
    pem_end_finish = pem_end + len(PEM_END)
    # Include trailing literal "\n" if present
    if content[pem_end_finish:pem_end_finish + 2] == "\\n":
        pem_end_finish += 2
    return (pem_start, pem_end_finish)


def _apply_splits(content: str, splits: set[int]) -> str:
    splits_sorted = sorted(splits)
    out_chunks = []
    last = 0
    for pos in splits_sorted:
        out_chunks.append(content[last:pos])
        out_chunks.append("\n")
        last = pos
    out_chunks.append(content[last:])
    return "".join(out_chunks)


def _is_in_comment_line(content: str, pos: int) -> bool:
    """Detect TRUE commented-out env var: the line's first non-whitespace
    char is `#`, AND the env-var-name at `pos` is the IMMEDIATE next thing
    after the `#` and any spaces. This catches lines like:
        # CHILI_FOO=bar     -- comment text
        #CHILI_FOO=bar
    But does NOT catch lines like:
        # Some descriptive textMASSIVE_API_KEY=actual_value  (collapsed)
    where the env-var is glued onto the end of comment text and SHOULD be
    split out.
    """
    # Find start of current line
    scan = pos - 1
    while scan >= 0 and content[scan] != "\n":
        scan -= 1
    line_start = scan + 1
    # Walk forward to first non-whitespace
    j = line_start
    while j < pos and content[j] in " \t":
        j += 1
    if j >= len(content) or content[j] != "#":
        return False  # line doesn't start with #
    # Skip the `#` and any spaces immediately after
    j += 1
    while j < pos and content[j] in " \t":
        j += 1
    # If we're now AT pos, the env-var-name comes immediately after `# `
    # → truly commented-out var → in_comment = True
    if j == pos:
        return True
    # Otherwise there's intervening text between `# ` and the env-var-name
    # → collapsed line → not in_comment, allow split
    return False


def reconstruct(content: str) -> str:
    """Two-pass reconstruction:
      Pass 1: insert newlines before comment markers (`# `, `#-`, `#=`, `##`).
      Pass 2: insert newlines before env-var-name boundaries, skipping any
              that's now inside a comment line (after pass 1's newlines).
    PEM region protected throughout.
    """
    # Strip BOM and CR
    if content.startswith("﻿"):
        content = content[1:]
    content = content.replace("\r", "")

    pem_region = find_pem_region(content)

    # ---- Pass 1: comment markers ----
    comment_splits: set[int] = set()
    for marker in ("# ", "#-", "#=", "##"):
        i = 0
        while True:
            pos = content.find(marker, i)
            if pos < 0:
                break
            i = pos + 1
            if pos == 0 or content[pos - 1] == "\n":
                continue
            if pem_region and pem_region[0] <= pos < pem_region[1]:
                continue
            comment_splits.add(pos)

    after_pass1 = _apply_splits(content, comment_splits)

    # Recompute PEM region in post-pass1 content (positions shifted by
    # inserted newlines; PEM_BEGIN/PEM_END strings still match). The PEM
    # is in a region where we DIDN'T insert newlines, so its position
    # shifts by the number of newlines inserted BEFORE it.
    pem_region2 = find_pem_region(after_pass1)

    # ---- Pass 2: env-var-name boundaries ----
    var_splits: set[int] = set()
    for prefix in KNOWN_PREFIXES:
        i = 0
        while True:
            pos = after_pass1.find(prefix, i)
            if pos < 0:
                break
            i = pos + 1
            j = pos + len(prefix)
            while j < len(after_pass1) and (
                after_pass1[j].isupper()
                or after_pass1[j].isdigit()
                or after_pass1[j] == "_"
            ):
                j += 1
            if j >= len(after_pass1) or after_pass1[j] != "=":
                continue
            # Already at start of line?
            if pos == 0 or after_pass1[pos - 1] == "\n":
                continue
            # Substring of a longer env var name? Walk back over name-chars.
            # If we hit `\n` (or BOF) without seeing `=` first, the chain
            # back is all name-chars — we're INSIDE another name (skip).
            # If we hit `=` first, we're past a value boundary (split OK).
            # If we hit any other non-name-char first (`-`, ` `, `.`, etc.),
            # we're at a comment/divider/value boundary (split OK).
            scan = pos - 1
            while scan >= 0 and (
                after_pass1[scan].isupper()
                or after_pass1[scan].isdigit()
                or after_pass1[scan] == "_"
            ):
                scan -= 1
            if scan < 0:
                # Walked all the way to BOF over name-chars — inside a name
                continue
            if after_pass1[scan] == "\n":
                # Already at start of line (chain to \n was name-chars);
                # treat as already-split (no insert needed)
                continue
            # else: scan-char is `=` or some other punctuation/space — boundary OK
            # Inside a comment line?
            if _is_in_comment_line(after_pass1, pos):
                continue
            # Inside PEM region?
            if pem_region2 and pem_region2[0] <= pos < pem_region2[1]:
                continue
            var_splits.add(pos)

    new_content = _apply_splits(after_pass1, var_splits)

    # Collapse 3+ consecutive newlines to 2
    new_content = re.sub(r"\n{3,}", "\n\n", new_content)
    new_content = new_content.rstrip("\n") + "\n"
    return new_content


def main() -> int:
    if not ENV_PATH.exists():
        print("ERROR: .env not found", file=sys.stderr)
        return 1

    raw = ENV_PATH.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
        print("  pre: stripped UTF-8 BOM (3 bytes)")
    content = raw.decode("utf-8", errors="strict")

    pre_hash = hash_nonwhitespace(content)
    pre_len = len(content)
    print(f"  pre  byte length: {pre_len}")
    print(f"  pre  non-whitespace SHA256: {pre_hash[:16]}...")

    # Count expected vars BEFORE
    important_vars = [
        "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
        "PREMIUM_API_KEY", "PREMIUM_MODEL", "PREMIUM_BASE_URL",
        "ZEROX_API_KEY", "ROBINHOOD_USERNAME", "ROBINHOOD_PASSWORD",
        "PAID_OPENAI_API_KEY", "PAID_OPENAI_MODEL", "PAID_OPENAI_BASE_URL",
        "SMS_PHONE", "SMS_CARRIER", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "EMAIL_USER", "EMAIL_PASSWORD", "SMTP_HOST", "SMTP_PORT",
        "MASSIVE_API_KEY", "MASSIVE_USE_WEBSOCKET", "POLYGON_API_KEY",
        "POLYGON_BASE_URL", "USE_POLYGON", "DATABASE_URL", "TEST_DATABASE_URL",
        "COINBASE_API_KEY", "COINBASE_API_SECRET",
        "CHILI_AUTOTRADER_ENABLED", "CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED",
        "CHILI_COINBASE_AUTOTRADER_LIVE",
        "CHILI_DISPATCH_GITHUB_TOKEN",
    ]
    pre_counts = {v: content.count(f"{v}=") for v in important_vars}

    new_content = reconstruct(content)

    post_hash = hash_nonwhitespace(new_content)
    post_len = len(new_content)
    print(f"  post byte length: {post_len}")
    print(f"  post non-whitespace SHA256: {post_hash[:16]}...")

    if pre_hash != post_hash:
        # Determine where they diverge
        pre_strip = "".join(c for c in content if not c.isspace())
        post_strip = "".join(c for c in new_content if not c.isspace())
        for k, (a, b) in enumerate(zip(pre_strip, post_strip)):
            if a != b:
                print(f"  HASH MISMATCH at non-whitespace char {k}: pre={a!r} post={b!r}")
                ctx_pre = pre_strip[max(0, k-30):k+30]
                ctx_post = post_strip[max(0, k-30):k+30]
                print(f"  pre  context: {ctx_pre!r}")
                print(f"  post context: {ctx_post!r}")
                break
        else:
            print(f"  HASH MISMATCH at length: pre={len(pre_strip)} post={len(post_strip)}")
        print("ABORT: non-whitespace content changed; refusing to write")
        return 4

    print("  [OK] non-whitespace SHA256 match -- lossless")

    post_counts = {v: new_content.count(f"{v}=") for v in important_vars}
    delta = {k: (pre_counts[k], post_counts[k]) for k in important_vars if pre_counts[k] != post_counts[k]}
    if delta:
        print(f"  count delta (var: pre->post):")
        for k, (a, b) in delta.items():
            print(f"    {k}: {a}->{b}")
    else:
        print(f"  [OK] all {len(important_vars)} important var counts unchanged")

    line_count_pre = content.count("\n") + 1
    line_count_post = new_content.count("\n") + 1
    print(f"  line count: {line_count_pre} -> {line_count_post}")

    # Backup
    if not BACKUP_PATH.exists():
        BACKUP_PATH.write_bytes(ENV_PATH.read_bytes())
        print(f"  backup written: {BACKUP_PATH}")

    ENV_PATH.write_bytes(new_content.encode("ascii", errors="strict"))
    print(f"  .env rewritten ({post_len} bytes ASCII, no BOM)")
    print("# done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
