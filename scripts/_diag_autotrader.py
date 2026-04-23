"""Ad-hoc diagnostic: 'stop hits not exiting + few trades starting'."""
from __future__ import annotations

import os
import json
from dotenv import load_dotenv

load_dotenv()

import psycopg2
import psycopg2.extras


def main():
    url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def q(sql, *args):
        cur.execute(sql, args)
        try:
            return cur.fetchall()
        except psycopg2.ProgrammingError:
            return []

    def section(title):
        print("\n" + "=" * 80)
        print(title)
        print("=" * 80)

    # ---------- 1. env + desk runtime ----------
    section("1. AutoTrader env (from process) + desk runtime mode")
    for k in [
        "CHILI_AUTOTRADER_ENABLED",
        "CHILI_AUTOTRADER_LIVE_ENABLED",
        "CHILI_AUTOTRADER_USER_ID",
        "CHILI_AUTOTRADER_RTH_ONLY",
        "CHILI_AUTOTRADER_ALLOW_EXTENDED_HOURS",
        "CHILI_AUTOTRADER_CONFIDENCE_FLOOR",
        "CHILI_AUTOTRADER_MIN_PROJECTED_PROFIT_PCT",
        "CHILI_AUTOTRADER_MAX_SYMBOL_PRICE_USD",
        "CHILI_AUTOTRADER_MAX_ENTRY_SLIPPAGE_PCT",
        "CHILI_AUTOTRADER_MAX_CONCURRENT",
        "CHILI_AUTOTRADER_LLM_REVALIDATION_ENABLED",
        "CHILI_AUTOTRADER_DAILY_LOSS_CAP_USD",
        "CHILI_AUTOTRADER_PER_TRADE_NOTIONAL_USD",
    ]:
        print(f"  {k:50s} = {os.environ.get(k, '<unset>')}")

    rows = q(
        "SELECT slice_name, mode, payload_json, updated_at, updated_by, reason "
        "FROM trading_brain_runtime_modes "
        "WHERE slice_name IN ('autotrader_v1_desk','kill_switch','momentum_neural_desk')"
        " ORDER BY slice_name"
    )
    for r in rows:
        print(
            f"  [rt] {r['slice_name']:24s} mode={r['mode']!s:10s} "
            f"updated={r['updated_at']} by={r['updated_by']!s}"
        )
        if r["payload_json"]:
            print(f"         payload={r['payload_json']}")

    # ---------- 2. kill switch (trading_risk_state) ----------
    section("2. Kill switch persisted state (trading_risk_state, regime='kill_switch')")
    rows = q(
        """
        SELECT id, user_id, snapshot_date, breaker_tripped, breaker_reason
        FROM trading_risk_state
        WHERE regime = 'kill_switch'
        ORDER BY id DESC LIMIT 5
        """
    )
    for r in rows:
        print(f"  {r['snapshot_date']} tripped={r['breaker_tripped']} "
              f"reason={r['breaker_reason']!s}  id={r['id']}")
    if not rows:
        print("  (no trading_risk_state kill_switch rows)")

    # ---------- 3. AutoTraderRun histogram ----------
    section("3. trading_autotrader_runs decision/reason histogram — last 24h")
    rows = q(
        """
        SELECT decision, reason, COUNT(*) AS n, MAX(created_at) AS last_seen
        FROM trading_autotrader_runs
        WHERE created_at > now() - interval '24 hours'
        GROUP BY decision, reason
        ORDER BY n DESC
        """
    )
    if not rows:
        print("  (no AutoTraderRun rows in last 24h)")
    else:
        print(f"  {'decision':12s} {'reason':55s} {'n':>5s}  last_seen")
        for r in rows:
            print(f"  {r['decision'] or '':12s} "
                  f"{str(r['reason'])[:55]:55s} "
                  f"{r['n']:5d}  {r['last_seen']}")

    section("3b. trading_autotrader_runs last 15 raw")
    rows = q(
        "SELECT created_at, ticker, decision, reason, trade_id, breakout_alert_id, "
        "rule_snapshot->>'current_price' AS px, "
        "rule_snapshot->>'confidence' AS conf, "
        "rule_snapshot->>'confidence_floor_effective' AS conf_floor, "
        "rule_snapshot->>'projected_profit_pct' AS ppp, "
        "rule_snapshot->>'min_profit_pct_effective' AS min_pp "
        "FROM trading_autotrader_runs ORDER BY created_at DESC LIMIT 15"
    )
    for r in rows:
        print(f"  {r['created_at']} {r['ticker']:8s} "
              f"dec={r['decision']:10s} reason={(r['reason'] or '')[:40]:40s} "
              f"alert={r['breakout_alert_id']} trade={r['trade_id']} "
              f"px={r['px']} conf={r['conf']}/{r['conf_floor']} "
              f"ppp={r['ppp']}/{r['min_pp']}")

    # ---------- 4. imminent alerts flow ----------
    section("4. trading_breakout_alerts (pattern_imminent) — last 24h")
    rows = q(
        """
        SELECT COUNT(*) AS total,
               COUNT(DISTINCT ticker) AS tickers,
               SUM(CASE WHEN alert_tier='pattern_imminent' THEN 1 ELSE 0 END) AS imminent,
               SUM(CASE WHEN user_id IS NULL THEN 1 ELSE 0 END) AS system_scope,
               SUM(CASE WHEN user_id = 1 THEN 1 ELSE 0 END) AS user_1,
               MAX(alerted_at) AS last_alert
        FROM trading_breakout_alerts
        WHERE alerted_at > now() - interval '24 hours'
        """
    )
    print(" ", dict(rows[0]) if rows else "(none)")

    rows = q(
        """
        SELECT ba.id, ba.alerted_at, ba.ticker, ba.alert_tier, ba.asset_type,
               ba.user_id, ba.price_at_alert, ba.entry_price,
               ba.stop_loss, ba.target_price, ba.score_at_alert
        FROM trading_breakout_alerts ba
        LEFT JOIN trading_autotrader_runs ar ON ar.breakout_alert_id = ba.id
        WHERE ba.alert_tier = 'pattern_imminent'
          AND ba.alerted_at > now() - interval '24 hours'
          AND ar.id IS NULL
        ORDER BY ba.alerted_at DESC
        LIMIT 20
        """
    )
    print(f"\n  Unprocessed pattern_imminent alerts in last 24h: {len(rows)}")
    for r in rows:
        print(f"    id={r['id']} {r['alerted_at']} {r['ticker']:8s} "
              f"asset={r['asset_type']!s:6s} user={r['user_id']!s} "
              f"px={r['price_at_alert']} entry={r['entry_price']} "
              f"stop={r['stop_loss']} tgt={r['target_price']} "
              f"score={r['score_at_alert']}")

    # ---------- 5. Recent STOP_HIT alerts ----------
    section("5. Recent STOP_HIT alerts in trading_alerts (last 48h)")
    rows = q(
        """
        SELECT id, created_at, ticker, alert_type, user_id,
               substring(message from 1 for 120) AS msg
        FROM trading_alerts
        WHERE alert_type IN ('stop_hit', 'STOP_HIT')
          AND created_at > now() - interval '48 hours'
        ORDER BY created_at DESC LIMIT 40
        """
    )
    print(f"  stop_hit alerts in last 48h: {len(rows)}")
    for r in rows:
        trade = q(
            """
            SELECT id, status, entry_price, stop_loss, take_profit, quantity,
                   broker_source, auto_trader_version, scan_pattern_id,
                   related_alert_id, pending_exit_status, pending_exit_reason,
                   pending_exit_requested_at, broker_order_id, pending_exit_order_id,
                   entry_date, exit_date, exit_reason, user_id, direction
            FROM trading_trades
            WHERE ticker = %s AND user_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            r["ticker"], r["user_id"],
        )
        tr = trade[0] if trade else None
        if tr is None:
            tag = "no_trade_for_user"
        else:
            tag = (
                f"trade={tr['id']} status={tr['status']} "
                f"dir={tr['direction']} stop={tr['stop_loss']} "
                f"tgt={tr['take_profit']} qty={tr['quantity']} "
                f"broker={tr['broker_source']!s} v={tr['auto_trader_version']!s} "
                f"pend={tr['pending_exit_status']!s}/{tr['pending_exit_reason']!s} "
                f"pend_ord={tr['pending_exit_order_id']!s} "
                f"exit_reason={tr['exit_reason']!s} exit_date={tr['exit_date']}"
            )
        print(f"  {r['created_at']} {r['ticker']:8s} uid={r['user_id']}  {tag}")

    # ---------- 6. Open trades eligible ----------
    section("6. Open trades for user 1 (what monitor would sweep)")
    rows = q(
        """
        SELECT id, ticker, entry_price, stop_loss, take_profit, quantity, direction,
               broker_source, auto_trader_version, scan_pattern_id,
               related_alert_id, pending_exit_status, pending_exit_reason,
               pending_exit_requested_at, broker_order_id, pending_exit_order_id,
               entry_date, high_watermark, trail_stop, management_scope, status
        FROM trading_trades
        WHERE user_id = 1 AND status = 'open'
        ORDER BY entry_date DESC
        """
    )
    print(f"  Open trades: {len(rows)}")
    for r in rows:
        print(f"    id={r['id']:5d} {r['ticker']:10s} "
              f"entry={r['entry_price']} stop={r['stop_loss']} "
              f"tgt={r['take_profit']} qty={r['quantity']} dir={r['direction']} "
              f"broker={r['broker_source']!s:12s} "
              f"v={r['auto_trader_version']!s:5s} "
              f"scope={r['management_scope']!s:20s} "
              f"pat={r['scan_pattern_id']} alert={r['related_alert_id']} "
              f"pend={r['pending_exit_status']!s}/{r['pending_exit_reason']!s} "
              f"pend_ord={(r['pending_exit_order_id'] or '')[:18]!s} "
              f"ord={(r['broker_order_id'] or '')[:12]!s}")

    # ---------- 7. monitor_paused overrides ----------
    section("7. Per-position monitor_paused overrides (runtime_modes slice)")
    rows = q(
        """
        SELECT slice_name, mode, payload_json, updated_at, updated_by
        FROM trading_brain_runtime_modes
        WHERE slice_name LIKE 'autotrader_v1_position:%%'
        ORDER BY updated_at DESC
        """
    )
    print(f"  total position-override rows: {len(rows)}")
    for r in rows:
        pj = r["payload_json"] or {}
        if isinstance(pj, str):
            try:
                pj = json.loads(pj)
            except Exception:
                pass
        paused = bool(pj.get("monitor_paused", False)) if isinstance(pj, dict) else False
        if paused:
            print(f"    PAUSED {r['slice_name']} updated_at={r['updated_at']} by={r['updated_by']}")

    # ---------- 8. Recent PatternMonitorDecisions for open trades ----------
    section("8. Latest pattern_monitor_decisions per open trade")
    rows = q(
        """
        SELECT DISTINCT ON (d.trade_id)
               d.trade_id, d.created_at, d.action, d.decision_source,
               d.price_at_decision, d.health_score, d.mechanical_action
        FROM trading_pattern_monitor_decisions d
        JOIN trading_trades t ON t.id = d.trade_id
        WHERE t.user_id = 1 AND t.status = 'open'
        ORDER BY d.trade_id, d.created_at DESC
        """
    )
    print(f"  rows: {len(rows)}")
    for r in rows:
        print(f"    trade={r['trade_id']:5d} {r['created_at']} "
              f"action={r['action']!s:14s} mech={r['mechanical_action']!s:14s} "
              f"src={r['decision_source']!s} px={r['price_at_decision']} "
              f"health={r['health_score']}")

    # ---------- 9. STOP_HIT stop_decisions ----------
    section("9. trading_stop_decisions trigger=STOP_HIT — last 48h")
    rows = q(
        """
        SELECT sd.id, sd.as_of_ts, sd.trade_id, sd.trigger, sd.state,
               sd.new_stop, sd.executed, sd.reason,
               t.ticker, t.status, t.broker_source, t.stop_loss, t.user_id,
               t.pending_exit_status, t.pending_exit_reason, t.exit_reason,
               t.exit_date
        FROM trading_stop_decisions sd
        LEFT JOIN trading_trades t ON t.id = sd.trade_id
        WHERE sd.trigger = 'STOP_HIT'
          AND sd.as_of_ts > now() - interval '48 hours'
        ORDER BY sd.as_of_ts DESC
        LIMIT 40
        """
    )
    print(f"  STOP_HIT stop_decisions in last 48h: {len(rows)}")
    for r in rows:
        print(f"    {r['as_of_ts']} trade={r['trade_id']} {r['ticker']!s:10s} "
              f"uid={r['user_id']} status={r['status']!s:7s} "
              f"broker={r['broker_source']!s:12s} "
              f"stop_on_trade={r['stop_loss']} "
              f"pend={r['pending_exit_status']!s}/{r['pending_exit_reason']!s} "
              f"exit_reason={r['exit_reason']!s} "
              f"exit_date={r['exit_date']!s}")

    # ---------- 10. pending-exit rows ----------
    section("10. Any trading_trades with pending_exit_* set")
    rows = q(
        """
        SELECT id, ticker, status, user_id, broker_source,
               pending_exit_status, pending_exit_reason,
               pending_exit_requested_at, broker_order_id, pending_exit_order_id,
               pending_exit_limit_price
        FROM trading_trades
        WHERE pending_exit_status IS NOT NULL
        ORDER BY pending_exit_requested_at DESC NULLS LAST
        LIMIT 30
        """
    )
    print(f"  rows: {len(rows)}")
    for r in rows:
        print(" ", dict(r))

    # ---------- 11. paper trades ----------
    section("11. Open paper trades for user 1 (autotrader_v1 tagged?)")
    rows = q(
        """
        SELECT id, ticker, entry_price, stop_price, target_price, quantity,
               signal_json, entry_date, status
        FROM trading_paper_trades
        WHERE user_id = 1 AND status = 'open'
        ORDER BY entry_date DESC
        LIMIT 30
        """
    )
    print(f"  open paper trades: {len(rows)}")
    for r in rows:
        sj = r["signal_json"] or {}
        if isinstance(sj, str):
            try:
                sj = json.loads(sj)
            except Exception:
                sj = {}
        tagged = bool(sj.get("auto_trader_v1")) if isinstance(sj, dict) else False
        print(f"    id={r['id']} {r['ticker']:8s} entry={r['entry_price']} "
              f"stop={r['stop_price']} tgt={r['target_price']} "
              f"qty={r['quantity']} autotrader={tagged} ts={r['entry_date']}")

    conn.close()


if __name__ == "__main__":
    main()
