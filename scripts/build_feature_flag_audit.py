"""Generate docs/FEATURE_FLAG_AUDIT.md from Settings + heuristics (Q1.T8).

Run from repo root: conda run -n chili-env python scripts/build_feature_flag_audit.py
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic_core import PydanticUndefined as PU

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "app" / "config.py"
OUT_PATH = ROOT / "docs" / "FEATURE_FLAG_AUDIT.md"

# Explicit Q1.T8 operator overrides (must match task spec).
CAT2_FORCE: dict[str, str] = {
    "chili_cpcv_promotion_gate_enabled": "HR1 promotion blocking; readiness in docs/CPCV_PROMOTION_GATE_RUNBOOK.md — do not flip.",
    "chili_regime_force_cold_fit": "Quarterly operator action; one-shot cold EM — default stays OFF.",
}

CAT1_FORCE: dict[str, str] = {
    "chili_unified_signal_enabled": "Additive INSERTs to unified_signals; no consumer enforces yet.",
    "chili_regime_classifier_enabled": "Regime tags + regime_snapshot; /brain heatmap early-return only.",
    "chili_cpcv_weekly_backfill_enabled": "Registers weekly CPCV backfill job; diagnostic accumulation on canonical DB.",
    "brain_prediction_ops_log_enabled": "Mirror ops log prefix `[chili_prediction_ops]`; telemetry for rollout reviews.",
}

# Substrings → category 2 (enforcement / money / lifecycle / live routing).
CAT2_SUBSTR = (
    "autotrader",
    "live_runner",
    "paper_runner",
    "momentum_entry_gates",
    "momentum_performance_sizing",
    "killswitch_kill",
    "read_authoritative",
    "hard_block_live",
    "live_hard_block",
    "live_soft_block",
    "auto_challenged",
    "depromotion",
    "stuck_order",
    "bracket_watchdog",
    "bracket_writer_g2",
    "order_state_machine",
    "drift_escalation",
    "execution_event_lag",
    "pattern_regime_autopilot_enabled",
    "walk_forward_enabled",
    "feature_parity_enabled",
    "liquidity_gate",
    "mesh_plasticity",
    "coinbase_ws",
    "autopilot_price_bus",
    "trading_automation_hud",
    "venue_rate_limit",
    "autotrader_llm",
    "autotrader_broker_equity",
    "cycle_lease_enforcement",
    "delegate_queue_from_cycle",
    "oos_gate_enabled",
    "live_drift_auto_challenged",
    "execution_robustness_hard_block",
    "miner_scanpattern_bridge",
    "momentum_neural_enabled",
    "momentum_neural_feedback",
    "robinhood_spot_adapter",
    "coinbase_spot_adapter",
)

# Prefer category 3 unless overridden.
CAT3_SUBSTR = (
    "database_",
    "staging_",
    "pool_",
    "smtp_",
    "session_",
    "secret",
    "api_key",
    "_url",
    "_host",
    "_base_url",
    "_ws_url",
    "password",
    "token",
    "webhook",
    "vapid",
    "google_client",
    "robinhood_",
    "coinbase_api",
    "massive_",
    "polygon_",
    "scheduler_role",
    "interval_",
    "cron_",
    "lease_seconds",
    "retry_",
    "timeout",
    "parallel",
    "batch_size",
    "dispatch_batch",
    "workers",
    "io_workers",
    "mp_child",
    "process_cap",
    "queue_backtest",
    "smart_bt",
    "config_profile",
    "modules",
    "module_registry",
    "weather_",
    "sms_",
    "telegram_",
    "discord_",
    "twilio_",
    "zerox_",
    "chili_modules",
    "desktop_refinement",
    "project_domain",
    "coding_validation",
    "top_picks_warn",
    "proposal_warn",
    "pick_warn",
    "email_user",
    "learning_interval",
    "learning_cycle_stale",
    "brain_regime_classifier_n_iter",  # tuning under regime
    "brain_regime_classifier_random",
    "brain_regime_classifier_weekly",
    "chili_regime_classifier_weekly",
    "regime_classifier_random",  # duplicate naming
)

CAT4_SUBSTR = (
    "brain_miner_scanpattern_bridge_enabled",
)

ROLLOUT_DOC = {
    "brain_net_edge_ranker_mode": "docs/TRADING_BRAIN_NET_EDGE_RANKER_ROLLOUT.md",
    "brain_exit_engine_mode": "docs/TRADING_BRAIN_EXIT_ENGINE_ROLLOUT.md",
    "brain_economic_ledger_mode": "docs/TRADING_BRAIN_ECONOMIC_LEDGER_ROLLOUT.md",
    "brain_pit_audit_mode": "docs/TRADING_BRAIN_PIT_HYGIENE_ROLLOUT.md",
    "brain_triple_barrier_mode": "docs/TRADING_BRAIN_TRIPLE_BARRIER_ROLLOUT.md",
    "brain_execution_cost_mode": "docs/TRADING_BRAIN_EXECUTION_REALISM_ROLLOUT.md",
    "brain_venue_truth_mode": "docs/TRADING_BRAIN_EXECUTION_REALISM_ROLLOUT.md",
    "brain_live_brackets_mode": "docs/TRADING_BRAIN_LIVE_BRACKETS_ROLLOUT.md",
    "brain_position_sizer_mode": "docs/TRADING_BRAIN_POSITION_SIZER_ROLLOUT.md",
    "brain_risk_dial_mode": "docs/TRADING_BRAIN_RISK_DIAL_ROLLOUT.md",
    "brain_capital_reweight_mode": "docs/TRADING_BRAIN_RISK_DIAL_ROLLOUT.md",
    "brain_prediction_dual_write_enabled": "docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md",
    "brain_prediction_read_compare_enabled": "docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md",
    "brain_prediction_read_authoritative_enabled": "docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md",
    "brain_prediction_ops_log_enabled": "docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md",
    "chili_cpcv_promotion_gate_enabled": "docs/CPCV_PROMOTION_GATE_RUNBOOK.md",
    "chili_cpcv_weekly_backfill_enabled": "docs/CPCV_PROMOTION_GATE_RUNBOOK.md",
    "chili_unified_signal_enabled": "docs/ROADMAP_DEVIATION_005.md (context)",
    "chili_regime_classifier_enabled": "docs/REGIME_CLASSIFIER_RUNBOOK.md",
}


def _field_lines() -> dict[str, int]:
    out: dict[str, int] = {}
    text = CONFIG_PATH.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
        m = re.match(r"^    ([a-z_][a-z0-9_]*)\s*:", line)
        if m:
            out[m.group(1)] = i
    return out


def _env_hint(name: str) -> str:
    finfo = Settings.model_fields[name]
    alias = finfo.validation_alias
    if alias is None:
        return name.upper()
    if hasattr(alias, "choices"):
        ch = [c for c in alias.choices if isinstance(c, str)]
        return ", ".join(ch) if ch else name.upper()
    if isinstance(alias, str):
        return alias
    return name.upper()


def _raw_default(name: str):
    d = Settings.model_fields[name].default
    if d is PU:
        return None
    return d


def _classify(name: str) -> tuple[int, str]:
    if name in CAT2_FORCE:
        return 2, CAT2_FORCE[name]
    if name in CAT1_FORCE:
        return 1, CAT1_FORCE[name]
    if name in CAT4_SUBSTR or name.endswith("_unknown_placeholder"):
        return 4, "Purpose/narrow consumer not fully traced in Q1.T8 pass; treat as investigation before flip."

    d = _raw_default(name)
    if d == "off":
        return (
            1,
            "Rollout `off→shadow→compare→authoritative`; shadow persists telemetry and MUST NOT gate money per module contract.",
        )

    for s in CAT2_SUBSTR:
        if s in name:
            return 2, f"Matches enforcement/live-routing heuristic `{s!r}` — review consumer before diagnostic flip."

    if name.startswith("brain_prediction_") and name not in (
        "brain_prediction_ops_log_enabled",
        "brain_prediction_read_max_age_seconds",
        "brain_prediction_mirror_write_dedicated",
        "brain_prediction_io_workers",
    ):
        return 2, "Prediction-mirror phase flag (ADR-004); progressive rollout — not blanket diagnostic."

    if name in ("brain_prediction_ops_log_enabled",):
        return 1, "Ops log lines for prediction mirror; telemetry-only when compare/dual-write phases are off."

    if name.startswith("brain_prediction_"):
        return 3, "Prediction-mirror tuning / IO — infrastructure or phased auxiliary."

    for s in CAT3_SUBSTR:
        if s in name:
            return 3, f"Infrastructure / connectivity / tuning bucket (`{s!r}`)."

    finfo = Settings.model_fields[name]
    ann = finfo.annotation
    if ann is bool:
        if d is False:
            return (
                3,
                "Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc.",
            )
        if d is True:
            for s in CAT2_SUBSTR:
                if s in name:
                    return 2, f"Default-on bool matches enforcement heuristic `{s!r}`."
            return 3, "Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target."
        return 3, "Boolean configuration; context-dependent."

    if ann is int or ann is float or "int" in str(ann) or "float" in str(ann):
        return 3, "Numeric tuning / limit; not a mode flag."

    if ann is str or str(ann).endswith("str]"):
        return 3, "String / URL / identifier; infrastructure."

    return 4, "Annotation/default combo unclear; needs spot-check."


def _shadow_line(name: str, cat: int) -> str:
    d = _raw_default(name)
    if d != "off":
        return "—"
    if cat != 1:
        return "—"
    return "`shadow`: persist diagnostic tables/logs; MUST NOT change authoritative sizing, routing, or lifecycle (per rollout doc)."


def main() -> None:
    lines_map = _field_lines()
    names = sorted(Settings.model_fields.keys())
    rows: list[tuple[str, int, str, str, str, str, str, str]] = []
    for name in names:
        cat, note = _classify(name)
        ln = lines_map.get(name, 0)
        gate = f"`app/config.py:{ln}`" if ln else "`app/config.py`"
        dfl = _raw_default(name)
        if dfl is PU:
            dfl_s = "*(required / no static default)*"
        else:
            dfl_s = repr(dfl)
        env = _env_hint(name)
        doc = ROLLOUT_DOC.get(name, "—")
        sh = _shadow_line(name, cat)
        rows.append((name, cat, env, dfl_s, gate, note, doc, sh))

    by_cat: dict[int, list] = {1: [], 2: [], 3: [], 4: []}
    for r in rows:
        by_cat[r[1]].append(r)

    buf: list[str] = []
    buf.append("# FEATURE_FLAG_AUDIT — Q1.T8\n")
    buf.append(
        "\nSource inventory: `audit_readonly_inventory.md` (721 fields at generation time; "
        "this doc uses live `Settings.model_fields`, **723** fields in this checkout).\n"
    )
    buf.append(
        "\n**Operator overrides (Q1.T8):** `chili_cpcv_promotion_gate_enabled` → Category **2** (runbook readiness; **not** flipped). "
        "`chili_unified_signal_enabled`, `chili_regime_classifier_enabled`, `chili_cpcv_weekly_backfill_enabled` → Category **1**. "
        "`chili_regime_force_cold_fit` → Category **2** (quarterly one-shot).\n"
    )
    buf.append("\n## Categories\n")
    buf.append("\n| Cat | Meaning |\n|-----|---------|\n")
    buf.append("| **1** | Diagnostic-safe-to-flip — shadow/telemetry, additive DB, no money enforcement. |\n")
    buf.append("| **2** | Enforcement-gated — trading, sizing, lifecycle, live routing; runbook review. |\n")
    buf.append("| **3** | Infrastructure — URLs, pools, scheduler tuning, numeric limits, LLM routing. |\n")
    buf.append("| **4** | Unknown / needs investigation — stale docs or narrow consumer. |\n")
    buf.append("\n## Category 1 — Diagnostic-safe (action list for default flip)\n")
    buf.append("\n| Flag | Env alias(es) | Default | Config | What / why | Shadow semantics | Rollout doc |\n")
    buf.append("|------|---------------|---------|--------|------------|------------------|-------------|\n")
    for name, cat, env, dfl_s, gate, note, doc, sh in sorted(by_cat[1], key=lambda x: x[0]):
        buf.append(f"| `{name}` | `{env}` | {dfl_s} | {gate} | {note} | {sh} | {doc} |\n")

    for label, c in (("Category 2 — Enforcement-gated", 2), ("Category 3 — Infrastructure / tuning", 3), ("Category 4 — Unknown / investigate", 4)):
        buf.append(f"\n## {label}\n")
        buf.append("\n| Flag | Env alias(es) | Default | Config | What / why | Shadow | Rollout doc |\n")
        buf.append("|------|---------------|---------|--------|------------|--------|-------------|\n")
        for name, cat, env, dfl_s, gate, note, doc, sh in sorted(by_cat[c], key=lambda x: x[0]):
            buf.append(f"| `{name}` | `{env}` | {dfl_s} | {gate} | {note} | {sh} | {doc} |\n")

    buf.append("\n## Counts\n")
    for c in (1, 2, 3, 4):
        buf.append(f"- **Category {c}:** {len(by_cat[c])}\n")
    buf.append(f"- **Total `Settings` fields:** {len(names)}\n")
    buf.append("\n*Generated by `scripts/build_feature_flag_audit.py`; regenerate after large config changes.*\n")

    OUT_PATH.write_text("".join(buf), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(names)} fields)")


if __name__ == "__main__":
    main()
