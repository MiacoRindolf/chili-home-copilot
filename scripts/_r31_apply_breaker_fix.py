"""R31: tighten the consecutive-loss breaker rule.

Two changes to portfolio_risk.check_drawdown_breaker rule #4:

1. Filter out synthetic / reconcile-only exits — those don't represent
   a CHILI decision and shouldn't count toward a "consecutive losing
   trades" streak.

2. Add a magnitude floor — the total streak loss must exceed 1% of
   capital before tripping. A streak of N micro-losses summing to
   <1% is statistically normal noise.

Today's false trip (5 consecutive synthetic losses summing to -$3.90)
would NOT fire with these guards.
"""
import subprocess, ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "app" / "services" / "trading" / "portfolio_risk.py"

head = subprocess.check_output(
    ["git", "show", "HEAD:app/services/trading/portfolio_risk.py"],
    cwd=str(ROOT),
).decode("utf-8")


OLD = '''    # Consecutive losses — YY: CHILI-placed only
    qcons = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
    )
    qcons = _breaker_trade_filter(qcons)
    last_n = qcons.order_by(Trade.exit_date.desc()).limit(limits.max_consecutive_losses).all()
    if len(last_n) >= limits.max_consecutive_losses:
        if all((t.pnl or 0) < 0 for t in last_n):
            _breaker_tripped = True
            _breaker_reason = f"{limits.max_consecutive_losses} consecutive losing trades"
            _persist_breaker_state(True, _breaker_reason)
            logger.warning("[circuit_breaker] TRIPPED: %s", _breaker_reason)
            return True, _breaker_reason'''


NEW = '''    # Consecutive losses — YY: CHILI-placed only.
    #
    # R31 (2026-04-30): two refinements after a false trip on 5 micro-
    # losses summing to -$3.90 (-0.016% of capital):
    #
    # 1. Exclude synthetic / reconcile-only exits. These are NOT CHILI
    #    decisions -- broker_sync inferred the close after the position
    #    disappeared, often with crude price estimates. A streak of
    #    those looks like consecutive losses but reflects book-keeping
    #    reconciliation, not bad signals.
    # 2. Apply a magnitude floor. A streak of N micro-losses summing
    #    to less than ``brain_risk_min_streak_loss_pct`` of capital is
    #    statistically normal and not a circuit-breaker event. Only
    #    material streaks fire.
    #
    # Per-pattern attribution lives in a different layer (Phase 1
    # Flags 5 and 6 -- chili_pattern_survival_sizing_enabled +
    # chili_pattern_survival_demote_enabled). The breaker is the LAST
    # defense, not the per-strategy adaptation surface.
    SYNTHETIC_EXIT_REASONS = (
        "broker_reconcile_position_gone",
        "broker_reconcile_no_exit_price",
        "phantom_no_broker_id",
        "phantom_no_broker_id_205",
        "phantom_zero_entry_price",
        "zombie_reconcile_orphan",
        "sync_duplicate",
    )
    qcons = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        # exit_reason can be NULL on legitimate closes, so the test must
        # accept NULL.
        (Trade.exit_reason.is_(None) | Trade.exit_reason.notin_(SYNTHETIC_EXIT_REASONS)),
    )
    qcons = _breaker_trade_filter(qcons)
    last_n = qcons.order_by(Trade.exit_date.desc()).limit(limits.max_consecutive_losses).all()
    if len(last_n) >= limits.max_consecutive_losses:
        if all((t.pnl or 0) < 0 for t in last_n):
            streak_loss = sum(float(t.pnl or 0.0) for t in last_n)
            try:
                from ...config import settings as _s
                min_pct = float(getattr(_s, "brain_risk_min_streak_loss_pct", 1.0))
            except Exception:
                min_pct = 1.0
            min_loss_dollars = abs(min_pct / 100.0 * (capital or 0.0))
            if abs(streak_loss) > min_loss_dollars:
                _breaker_tripped = True
                _breaker_reason = (
                    f"{limits.max_consecutive_losses} consecutive losing trades "
                    f"(real CHILI decisions, total=${streak_loss:.2f}, "
                    f"floor=${min_loss_dollars:.2f})"
                )
                _persist_breaker_state(True, _breaker_reason)
                logger.warning("[circuit_breaker] TRIPPED: %s", _breaker_reason)
                return True, _breaker_reason
            else:
                logger.info(
                    "[circuit_breaker] consecutive-loss streak ignored: "
                    "sum=$%.2f below %s%% floor=$%.2f (synthetic exits filtered)",
                    streak_loss, min_pct, min_loss_dollars,
                )'''


def apply():
    if OLD not in head:
        print("OLD block not found in HEAD")
        sys.exit(1)
    content = head.replace(OLD, NEW)

    try:
        ast.parse(content)
        print("ast OK")
    except SyntaxError as e:
        print(f"SYNTAX line {e.lineno}: {e.msg}")
        for i, line in enumerate(
            content.split("\n")[max(0, e.lineno - 5):e.lineno + 3],
            start=max(1, e.lineno - 4),
        ):
            print(f"{i}: {line[:120]}")
        sys.exit(1)

    target.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {len(content.splitlines())} lines to {target}")


if __name__ == "__main__":
    apply()
