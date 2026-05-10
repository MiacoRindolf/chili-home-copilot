"""Find every occurrence of these vars (substring match) and show 60 chars
of context before+after."""
from pathlib import Path

content = Path(".env").read_text(encoding="ascii")

targets = [
    "CHILI_PATTERN_SURVIVAL_CLASSIFIER_ENABLED",
    "CHILI_PATTERN_SURVIVAL_DECISIONS_ENABLED",
    "CHILI_AUTOTRADER_CRYPTO_ENABLED",
    "CHILI_AUTOTRADER_OPTIONS_ENABLED",
    "CHILI_AUTOTRADER_LLM_REVALIDATION_ENABLED",
    "CHILI_DISPATCH_GIT_PUSH_ENABLED",
    "CHILI_FAST_PATH_UNIVERSE_MIN_VOLUME_24H_USD",
    "CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE",
    "BRAIN_PATTERN_REGIME_AUTOPILOT_ENABLED",
    "TRADING_BRAIN_NEURAL_MESH_ENABLED",
    "MASSIVE_API_KEY",
    "POLYGON_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "ROBINHOOD_USERNAME",
    "EMAIL_USER",
]

for var in targets:
    print(f"\n=== {var} ===")
    i = 0
    found = 0
    while True:
        pos = content.find(var, i)
        if pos < 0:
            break
        i = pos + 1
        found += 1
        # Show context: 30 chars before, the var match, 30 after
        ctx_start = max(0, pos - 30)
        ctx_end = min(len(content), pos + len(var) + 30)
        ctx = content[ctx_start:ctx_end]
        # Mark line breaks visibly
        ctx_repr = ctx.replace("\n", "\\n")
        # Find line number for this position
        line_num = content.count("\n", 0, pos) + 1
        # What's the char immediately before?
        prev = content[pos - 1] if pos > 0 else "<bof>"
        print(f"  match #{found} at byte={pos} line={line_num} prev={prev!r}")
        print(f"    context: ...{ctx_repr}...")
