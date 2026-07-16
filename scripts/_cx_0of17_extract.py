"""0/17 forensics — step 1: extract every live coinbase momentum trade from the DB.

Reads via `docker exec ... psql` (host loopback avoided per ops note). Writes
scripts/_cx_cache/cx_0of17_db.json with sessions, outcomes, events, ledger rows
and decision-packet params for every live coinbase session that FILLED an entry.

Read-only. Part of the 2026-06-12 night-ops crypto forensics pass.
"""
import json
import pathlib
import subprocess

CACHE = pathlib.Path(__file__).resolve().parent / "_cx_cache"
CACHE.mkdir(exist_ok=True)
OUT = CACHE / "cx_0of17_db.json"


def psql_json(query: str):
    """Run a query that returns ONE json/jsonb value (use json_agg) via docker exec."""
    cmd = [
        "docker", "exec", "-i", "chili-home-copilot-postgres-1",
        "psql", "-U", "chili", "-d", "chili", "-t", "-A", "-c", query,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"psql failed: {p.stderr[:500]}")
    txt = p.stdout.strip()
    return json.loads(txt) if txt else None


def main() -> None:
    out: dict = {}

    # Every live coinbase session that ever submitted/filled an entry OR booked pnl.
    out["outcomes"] = psql_json("""
        SELECT json_agg(row_to_json(t)) FROM (
          SELECT o.id outcome_id, o.session_id, o.symbol, o.outcome_class, o.exit_reason,
                 o.realized_pnl_usd, o.return_bps, o.hold_seconds,
                 s.started_at, s.ended_at, o.terminal_at, s.state session_state,
                 o.extracted_summary_json->>'entry_decision_packet_id' entry_dp_id,
                 o.extracted_summary_json->>'variant_key' variant_key,
                 o.extracted_summary_json->>'notional_basis_usd' notional_basis_usd
          FROM momentum_automation_outcomes o
          JOIN trading_automation_sessions s ON s.id=o.session_id
          WHERE o.mode='live' AND s.venue='coinbase'
            AND (o.realized_pnl_usd IS NOT NULL
                 OR EXISTS (SELECT 1 FROM trading_automation_events e
                            WHERE e.session_id=s.id AND e.event_type='live_entry_filled'))
          ORDER BY s.started_at
        ) t""")

    sess_ids = sorted({r["session_id"] for r in out["outcomes"]})
    ids_csv = ",".join(str(i) for i in sess_ids)

    # Full event timeline for those sessions (skip the high-noise wait/risk spam,
    # but COUNT it per session so exit-submit failures are visible).
    out["events"] = psql_json(f"""
        SELECT json_agg(row_to_json(t)) FROM (
          SELECT e.session_id, e.ts, e.event_type, e.payload_json
          FROM trading_automation_events e
          WHERE e.session_id IN ({ids_csv})
            AND e.event_type NOT IN ('live_blocked_by_risk','live_entry_trigger_wait')
          ORDER BY e.session_id, e.ts
        ) t""")

    out["noise_counts"] = psql_json(f"""
        SELECT json_agg(row_to_json(t)) FROM (
          SELECT e.session_id, e.event_type, count(*) n,
                 min(e.ts) first_ts, max(e.ts) last_ts
          FROM trading_automation_events e
          WHERE e.session_id IN ({ids_csv})
            AND e.event_type IN ('live_blocked_by_risk','live_entry_trigger_wait',
                                 'live_exit_submit_failed')
          GROUP BY 1,2 ORDER BY 1,2
        ) t""")

    out["ledger"] = psql_json("""
        SELECT json_agg(row_to_json(t)) FROM (
          SELECT id, ticker, event_type, direction, quantity, price, fee, cash_delta,
                 realized_pnl_delta, created_at,
                 provenance_json->>'automation_session_id' session_id,
                 provenance_json->>'entry_order_id' entry_order_id,
                 provenance_json->>'decision_packet_id' decision_packet_id
          FROM trading_economic_ledger
          WHERE venue='coinbase' AND mode='live'
          ORDER BY id
        ) t""")

    # Variant params + expected costs from the entry decision packets.
    dp_ids = sorted({int(r["entry_dp_id"]) for r in out["outcomes"] if r.get("entry_dp_id")})
    dp_csv = ",".join(str(i) for i in dp_ids)
    out["packets"] = psql_json(f"""
        SELECT json_agg(row_to_json(t)) FROM (
          SELECT id dp_id, automation_session_id, chosen_ticker, created_at,
                 expected_slippage_bps, expected_edge_net, size_notional,
                 allocator_input_json->'spread_bps' spread_bps_est,
                 allocator_input_json->'runner_feature_snapshot'->'variant'->'params_json' variant_params,
                 allocator_input_json->'runner_feature_snapshot'->'viability'->'viability_score' viability_score
          FROM trading_decision_packets
          WHERE id IN ({dp_csv})
        ) t""")

    OUT.write_text(json.dumps(out, indent=1, default=str))
    n_ev = len(out["events"] or [])
    print(f"sessions={len(sess_ids)} outcomes={len(out['outcomes'])} events={n_ev} "
          f"ledger={len(out['ledger'])} packets={len(out['packets'] or [])} -> {OUT}")


if __name__ == "__main__":
    main()
