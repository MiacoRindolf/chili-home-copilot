from app.config import settings
keys = [
    "chili_momentum_pullback_entry_interval",
    "chili_momentum_micropull_enabled",
    "chili_momentum_first_pullback_enabled",
    "chili_momentum_first_pullback_interval",
    "chili_momentum_entry_trigger_mode",
    "chili_momentum_pullback_require_retest",
    "chili_momentum_entry_verticality_atr_mult",
    "chili_momentum_entry_runaway_min_volume_spike",
    "chili_momentum_auto_arm_max_watch_seconds",
    "chili_momentum_reap_cooldown_sec",
    "chili_momentum_entry_reject_cooldown_sec",
]
for k in keys:
    print(f"{k} = {getattr(settings, k, '<MISSING>')!r}")
