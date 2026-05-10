"""Spot-check specific lines in rebuilt .env to verify parse correctness."""
from pathlib import Path

content = Path(".env").read_text(encoding="ascii")
lines = content.split("\n")

# Find every occurrence of these vars (uncommented vs commented)
targets = [
    "CHILI_PATTERN_SURVIVAL_CLASSIFIER_ENABLED",
    "CHILI_PATTERN_SURVIVAL_DECISIONS_ENABLED",
    "CHILI_PATTERN_SURVIVAL_SIZING_ENABLED",
    "CHILI_PATTERN_SURVIVAL_DEMOTE_ENABLED",
    "CHILI_PATTERN_SURVIVAL_PROMOTE_GATE_ENABLED",
    "CHILI_AUTOTRADER_CRYPTO_ENABLED",
    "CHILI_AUTOTRADER_OPTIONS_ENABLED",
    "CHILI_AUTOTRADER_LLM_REVALIDATION_ENABLED",
    "CHILI_DISPATCH_GIT_PUSH_ENABLED",
    "CHILI_FAST_PATH_UNIVERSE_MIN_VOLUME_24H_USD",
    "CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE",
    "BRAIN_PATTERN_REGIME_AUTOPILOT_ENABLED",
    "TRADING_BRAIN_NEURAL_MESH_ENABLED",
]

for var in targets:
    print(f"\n=== {var} ===")
    found_uncommented = []
    found_commented = []
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(f"{var}="):
            found_uncommented.append((i + 1, line[:120]))
        elif s.startswith(f"# {var}=") or s.startswith(f"#{var}="):
            found_commented.append((i + 1, line[:120]))
    if found_uncommented:
        for ln, txt in found_uncommented:
            print(f"  line {ln} (active): {txt!r}")
    if found_commented:
        for ln, txt in found_commented:
            print(f"  line {ln} (commented): {txt!r}")
    if not found_uncommented and not found_commented:
        print(f"  NOT FOUND anywhere")
