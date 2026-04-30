"""Wire the Phase G.2 bracket writer into the reconciliation sweep.

Surgical string-replace edits with ast.parse validation, used because
the Edit tool is truncating large files in this session.

Changes to app/services/trading/bracket_reconciliation_service.py:
  1. Add _invoke_writer_for_decision helper near other helpers.
  2. Add call site inside _stage_log_all loop (staged sweep).
  3. Add call site inside _run_sweep_legacy loop.

The helper is mode-aware (only fires when mode=='authoritative') and
respects the chili_bracket_sweep_writer_enabled flag.
"""
import subprocess, ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "app" / "services" / "trading" / "bracket_reconciliation_service.py"

content = target.read_text(encoding='utf-8')


HELPER = '''

# ── Phase G.2 writer-invocation hook (Round 23) ───────────────────────


def _invoke_writer_for_decision(
    db: Session,
    *,
    mode: str,
    sweep_id: str,
    local: LocalView,
    broker: BrokerView,
    decision: ReconciliationDecision,
) -> dict[str, Any] | None:
    """Invoke the Phase G.2 bracket writer for a classification that
    represents a repairable drift, when mode + flags allow.

    Gates (all must be True for the writer to fire):
      * ``mode == "authoritative"`` (set by sweep entry point only when
        ``chili_bracket_sweep_writer_enabled`` is True).
      * ``local.trade_id`` and ``local.bracket_intent_id`` are present.
      * ``local.broker_source`` is supported (currently Robinhood only).
      * decision.kind is one of {missing_stop, qty_drift+partial_fill}.

    Returns a dict describing the writer action (logged + summarised),
    or ``None`` if no writer was invoked. Failures inside the writer
    are surfaced via the dict's ``ok`` and ``reason`` keys, never as
    exceptions reaching the sweep loop.
    """
    if mode != "authoritative":
        return None
    if local.trade_id is None or local.bracket_intent_id is None:
        return None
    if (local.broker_source or "").lower() != "robinhood":
        return None

    try:
        from .bracket_writer_g2 import (
            place_missing_stop,
            resize_stop_for_partial_fill,
        )
    except Exception:
        logger.warning(
            f"{BRACKET_RECONCILIATION} bracket_writer_g2 import failed for sweep %s",
            sweep_id, exc_info=True,
        )
        return None

    try:
        if decision.kind == "missing_stop":
            if local.stop_price is None or local.quantity is None:
                return None
            action = place_missing_stop(
                db,
                trade_id=int(local.trade_id),
                bracket_intent_id=int(local.bracket_intent_id),
                ticker=str(local.ticker or ""),
                broker_source=str(local.broker_source or ""),
                decision=decision,
                local_quantity=float(local.quantity),
                stop_price=float(local.stop_price),
            )
            return {
                "writer": "place_missing_stop",
                "ok": bool(action.ok),
                "reason": action.reason,
                "new_stop_order_id": action.new_stop_order_id,
                "qty": action.new_stop_qty,
                "stop_price": action.new_stop_price,
            }

        if decision.kind == "qty_drift":
            payload = decision.delta_payload or {}
            if payload.get("drift_kind") != "partial_fill":
                return None
            if local.stop_price is None:
                return None
            prior_id = (
                broker.stop_order_id
                if broker is not None and broker.stop_order_id
                else None
            )
            if prior_id is None:
                return None
            action = resize_stop_for_partial_fill(
                db,
                trade_id=int(local.trade_id),
                bracket_intent_id=int(local.bracket_intent_id),
                ticker=str(local.ticker or ""),
                broker_source=str(local.broker_source or ""),
                decision=decision,
                prior_stop_order_id=str(prior_id),
                stop_price=float(local.stop_price),
            )
            return {
                "writer": "resize_stop_for_partial_fill",
                "ok": bool(action.ok),
                "reason": action.reason,
                "prior_stop_order_id": action.prior_stop_order_id,
                "new_stop_order_id": action.new_stop_order_id,
                "qty": action.new_stop_qty,
                "stop_price": action.new_stop_price,
            }
    except Exception:
        logger.warning(
            f"{BRACKET_RECONCILIATION} writer raised for trade %s sweep %s",
            local.trade_id, sweep_id, exc_info=True,
        )
        return None

    return None
'''


# Insert helper right before _run_sweep_staged
anchor1 = "def _run_sweep_staged("
idx = content.find(anchor1)
if idx == -1:
    print("anchor1 not found"); sys.exit(1)
# Walk back to start of the line above (right after the previous function)
line_start = content.rfind('\n', 0, idx) + 1
content = content[:line_start] + HELPER.lstrip("\n") + "\n\n" + content[line_start:]
print("inserted helper before _run_sweep_staged")


# Inject call site in _stage_log_all — after the if/elif bump block, before
# the ops_log_enabled discrepancy log.
needle2 = '''        if _ops_log_enabled() and decision.kind != "agree":
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="discrepancy",
                    mode=batch.mode,
                    sweep_id=batch.sweep_id,
                    trade_id=lv.trade_id,
                    bracket_intent_id=lv.bracket_intent_id,
                    ticker=lv.ticker,
                    broker_source=lv.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                )
            )
    batch.rows_written = rows_written'''

replacement2 = '''        if _ops_log_enabled() and decision.kind != "agree":
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="discrepancy",
                    mode=batch.mode,
                    sweep_id=batch.sweep_id,
                    trade_id=lv.trade_id,
                    bracket_intent_id=lv.bracket_intent_id,
                    ticker=lv.ticker,
                    broker_source=lv.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                )
            )

        # Phase G.2 writer hook (Round 23). No-op unless mode=authoritative.
        writer_res = _invoke_writer_for_decision(
            db,
            mode=batch.mode,
            sweep_id=batch.sweep_id,
            local=lv,
            broker=bv,
            decision=decision,
        )
        if writer_res is not None and _ops_log_enabled():
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="writer_action",
                    mode=batch.mode,
                    sweep_id=batch.sweep_id,
                    trade_id=lv.trade_id,
                    bracket_intent_id=lv.bracket_intent_id,
                    ticker=lv.ticker,
                    broker_source=lv.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                    writer=writer_res.get("writer"),
                    ok=writer_res.get("ok"),
                    reason=writer_res.get("reason"),
                )
            )
    batch.rows_written = rows_written'''

if needle2 not in content:
    print("needle2 not found"); sys.exit(1)
content = content.replace(needle2, replacement2)
print("inserted writer hook in _stage_log_all")


# Inject call site in _run_sweep_legacy — same location relative to its own
# discrepancy ops log block. The legacy block is the same ops_log call but
# inside a different scope (decisions list instead of batch.decisions).
needle3 = '''        if _ops_log_enabled() and decision.kind != "agree":
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="discrepancy",
                    mode=mode,
                    sweep_id=sweep_id,
                    trade_id=local.trade_id,
                    bracket_intent_id=local.bracket_intent_id,
                    ticker=local.ticker,
                    broker_source=local.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                )
            )

    took_ms = (time.perf_counter() - start) * 1000.0'''

replacement3 = '''        if _ops_log_enabled() and decision.kind != "agree":
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="discrepancy",
                    mode=mode,
                    sweep_id=sweep_id,
                    trade_id=local.trade_id,
                    bracket_intent_id=local.bracket_intent_id,
                    ticker=local.ticker,
                    broker_source=local.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                )
            )

        # Phase G.2 writer hook (Round 23). No-op unless mode=authoritative.
        writer_res = _invoke_writer_for_decision(
            db,
            mode=mode,
            sweep_id=sweep_id,
            local=local,
            broker=broker,
            decision=decision,
        )
        if writer_res is not None and _ops_log_enabled():
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="writer_action",
                    mode=mode,
                    sweep_id=sweep_id,
                    trade_id=local.trade_id,
                    bracket_intent_id=local.bracket_intent_id,
                    ticker=local.ticker,
                    broker_source=local.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                    writer=writer_res.get("writer"),
                    ok=writer_res.get("ok"),
                    reason=writer_res.get("reason"),
                )
            )

    took_ms = (time.perf_counter() - start) * 1000.0'''

if needle3 not in content:
    print("needle3 not found"); sys.exit(1)
content = content.replace(needle3, replacement3)
print("inserted writer hook in _run_sweep_legacy")


try:
    ast.parse(content)
    print("ast OK")
except SyntaxError as e:
    print(f"SYNTAX line {e.lineno}: {e.msg}")
    lines = content.split('\n')
    for i in range(max(0, e.lineno - 5), min(len(lines), e.lineno + 3)):
        print(f"{i+1}: {lines[i][:120]}")
    sys.exit(1)

target.write_text(content, encoding='utf-8', newline='\n')
print(f"wrote {len(content.splitlines())} lines to {target}")
