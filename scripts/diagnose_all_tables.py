"""Comprehensive DB diagnostic: orphan refs, data quality, row counts, empty tables."""
from __future__ import annotations

import sys
import os
import json
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CHILI_MP_BACKTEST_CHILD", "")

from sqlalchemy import text
from app.db import engine


ORPHAN_CHECKS: list[tuple[str, str, str, str]] = [
    # (source_table, source_col, target_table, target_col)
    # --- Trading domain ---
    ("trading_backtests", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_backtests", "related_insight_id", "trading_insights", "id"),
    ("trading_backtests", "param_set_id", "trading_backtest_param_sets", "id"),
    ("trading_trades", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_trades", "strategy_proposal_id", "trading_proposals", "id"),
    ("trading_trades", "user_id", "users", "id"),
    ("trading_journal", "trade_id", "trading_trades", "id"),
    ("trading_journal", "user_id", "users", "id"),
    ("trading_paper_trades", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_paper_trades", "user_id", "users", "id"),
    ("trading_watchlist", "user_id", "users", "id"),
    ("trading_scans", "user_id", "users", "id"),
    ("trading_alerts", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_alerts", "user_id", "users", "id"),
    ("trading_breakout_alerts", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_breakout_alerts", "related_insight_id", "trading_insights", "id"),
    ("trading_breakout_alerts", "user_id", "users", "id"),
    ("trading_breakout_alerts", "scan_cycle_id", "brain_batch_jobs", "id"),
    ("trading_proposals", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_proposals", "trade_id", "trading_trades", "id"),
    ("trading_proposals", "user_id", "users", "id"),
    ("trading_pattern_trades", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_pattern_trades", "related_insight_id", "trading_insights", "id"),
    ("trading_pattern_trades", "backtest_result_id", "trading_backtests", "id"),
    ("trading_pattern_evidence_hypotheses", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_learning_events", "related_insight_id", "trading_insights", "id"),
    ("trading_learning_events", "user_id", "users", "id"),
    ("brain_validation_slice_ledger", "scan_pattern_id", "scan_patterns", "id"),
    ("scan_patterns", "parent_id", "scan_patterns", "id"),
    ("scan_patterns", "user_id", "users", "id"),
    ("trading_insights", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_insight_evidence", "insight_id", "trading_insights", "id"),
    ("trading_stop_decisions", "trade_id", "trading_trades", "id"),
    ("trading_alert_delivery_attempts", "alert_id", "trading_alerts", "id"),
    ("trading_execution_events", "trade_id", "trading_trades", "id"),
    ("trading_execution_events", "proposal_id", "trading_proposals", "id"),
    ("trading_execution_events", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_prescreen_candidates", "snapshot_id", "trading_prescreen_snapshots", "id"),
    ("trading_prescreen_candidates", "user_id", "users", "id"),
    ("trading_decision_packets", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_decision_packets", "user_id", "users", "id"),
    ("trading_decision_packets", "automation_session_id", "trading_automation_sessions", "id"),
    ("trading_decision_packets", "linked_trade_id", "trading_trades", "id"),
    ("trading_decision_candidates", "decision_packet_id", "trading_decision_packets", "id"),
    ("trading_decision_candidates", "scan_pattern_id", "scan_patterns", "id"),
    ("trading_deployment_states", "user_id", "users", "id"),
    ("momentum_strategy_variants", "scan_pattern_id", "scan_patterns", "id"),
    ("momentum_strategy_variants", "parent_variant_id", "momentum_strategy_variants", "id"),
    ("momentum_symbol_viability", "variant_id", "momentum_strategy_variants", "id"),
    ("trading_automation_sessions", "user_id", "users", "id"),
    ("trading_automation_sessions", "variant_id", "momentum_strategy_variants", "id"),
    ("trading_automation_events", "session_id", "trading_automation_sessions", "id"),
    ("trading_automation_runtime_snapshots", "session_id", "trading_automation_sessions", "id"),
    ("trading_automation_runtime_snapshots", "user_id", "users", "id"),
    ("trading_automation_session_bindings", "session_id", "trading_automation_sessions", "id"),
    ("trading_automation_simulated_fills", "session_id", "trading_automation_sessions", "id"),
    ("momentum_automation_outcomes", "session_id", "trading_automation_sessions", "id"),
    ("momentum_automation_outcomes", "user_id", "users", "id"),
    ("momentum_automation_outcomes", "variant_id", "momentum_strategy_variants", "id"),
    ("brain_batch_jobs", "user_id", "users", "id"),
    ("brain_graph_edges", "source_node_id", "brain_graph_nodes", "id"),
    ("brain_graph_edges", "target_node_id", "brain_graph_nodes", "id"),
    ("brain_node_states", "node_id", "brain_graph_nodes", "id"),
    ("brain_activation_events", "source_node_id", "brain_graph_nodes", "id"),
    ("brain_fire_log", "node_id", "brain_graph_nodes", "id"),
    ("brain_work_events", "parent_event_id", "brain_work_events", "id"),
    ("brain_stage_job", "cycle_run_id", "brain_learning_cycle_run", "id"),
    ("brain_cycle_lease", "cycle_run_id", "brain_learning_cycle_run", "id"),
    ("brain_prediction_line", "snapshot_id", "brain_prediction_snapshot", "id"),
    # --- Planner / Coding ---
    ("conversations", "project_id", "projects", "id"),
    ("chat_messages", "conversation_id", "conversations", "id"),
    ("plan_tasks", "project_id", "plan_projects", "id"),
    ("plan_tasks", "parent_id", "plan_tasks", "id"),
    ("plan_tasks", "assigned_to", "users", "id"),
    ("plan_tasks", "reporter_id", "users", "id"),
    ("plan_tasks", "depends_on", "plan_tasks", "id"),
    ("task_comments", "task_id", "plan_tasks", "id"),
    ("task_comments", "user_id", "users", "id"),
    ("task_activities", "task_id", "plan_tasks", "id"),
    ("task_labels", "task_id", "plan_tasks", "id"),
    ("task_labels", "label_id", "plan_labels", "id"),
    ("task_watchers", "task_id", "plan_tasks", "id"),
    ("task_watchers", "user_id", "users", "id"),
    ("plan_labels", "project_id", "plan_projects", "id"),
    ("project_members", "project_id", "plan_projects", "id"),
    ("project_members", "user_id", "users", "id"),
    ("plan_task_coding_profile", "task_id", "plan_tasks", "id"),
    ("plan_task_coding_profile", "code_repo_id", "code_repos", "id"),
    ("task_clarification", "task_id", "plan_tasks", "id"),
    ("coding_task_brief", "task_id", "plan_tasks", "id"),
    ("coding_task_brief", "created_by", "users", "id"),
    ("coding_task_validation_run", "task_id", "plan_tasks", "id"),
    ("coding_validation_artifact", "run_id", "coding_task_validation_run", "id"),
    ("coding_agent_suggestion", "task_id", "plan_tasks", "id"),
    ("coding_agent_suggestion", "user_id", "users", "id"),
    ("coding_agent_suggestion_apply", "suggestion_id", "coding_agent_suggestion", "id"),
    ("coding_agent_suggestion_apply", "task_id", "plan_tasks", "id"),
    ("coding_agent_suggestion_apply", "user_id", "users", "id"),
    ("coding_blocker_report", "task_id", "plan_tasks", "id"),
    ("coding_blocker_report", "run_id", "coding_task_validation_run", "id"),
    # --- Code brain ---
    ("code_insights", "repo_id", "code_repos", "id"),
    ("code_insights", "user_id", "users", "id"),
    ("code_snapshots", "repo_id", "code_repos", "id"),
    ("code_hotspots", "repo_id", "code_repos", "id"),
    ("code_learning_events", "user_id", "users", "id"),
    ("code_dependencies", "repo_id", "code_repos", "id"),
    ("code_quality_snapshots", "repo_id", "code_repos", "id"),
    ("code_reviews", "repo_id", "code_repos", "id"),
    ("code_reviews", "user_id", "users", "id"),
    ("code_dep_alerts", "repo_id", "code_repos", "id"),
    ("code_search_index", "repo_id", "code_repos", "id"),
    # --- Reasoning brain ---
    ("reasoning_user_models", "user_id", "users", "id"),
    ("reasoning_interests", "user_id", "users", "id"),
    ("reasoning_research", "user_id", "users", "id"),
    ("reasoning_anticipations", "user_id", "users", "id"),
    ("reasoning_events", "user_id", "users", "id"),
    ("reasoning_learning_goals", "user_id", "users", "id"),
    ("reasoning_hypotheses", "user_id", "users", "id"),
    ("reasoning_confidence_snapshots", "user_id", "users", "id"),
    # --- Project brain ---
    ("project_agent_states", "user_id", "users", "id"),
    ("agent_findings", "user_id", "users", "id"),
    ("agent_research", "user_id", "users", "id"),
    ("agent_goals", "user_id", "users", "id"),
    ("agent_evolutions", "user_id", "users", "id"),
    ("agent_messages", "user_id", "users", "id"),
    ("po_questions", "user_id", "users", "id"),
    ("po_requirements", "user_id", "users", "id"),
    ("qa_test_cases", "user_id", "users", "id"),
    ("qa_test_runs", "user_id", "users", "id"),
    ("qa_bug_reports", "user_id", "users", "id"),
    # --- Household / Intercom ---
    ("chores", "assigned_to", "users", "id"),
    ("activity_logs", "user_id", "users", "id"),
    ("housemate_profiles", "user_id", "users", "id"),
    ("user_statuses", "user_id", "users", "id"),
    ("user_memories", "user_id", "users", "id"),
    ("user_memories", "source_message_id", "chat_messages", "id"),
    ("intercom_messages", "from_user_id", "users", "id"),
    ("intercom_messages", "to_user_id", "users", "id"),
    ("intercom_consents", "user_id", "users", "id"),
    # --- Core ---
    ("devices", "user_id", "users", "id"),
    ("pair_codes", "user_id", "users", "id"),
    ("broker_credentials", "user_id", "users", "id"),
    ("projects", "user_id", "users", "id"),
    ("project_files", "project_id", "projects", "id"),
    ("plan_projects", "user_id", "users", "id"),
]


def _table_exists(conn, table: str) -> bool:
    r = conn.execute(
        text("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name=:t)"),
        {"t": table},
    )
    return bool(r.scalar())


def run_orphan_checks(conn) -> list[dict]:
    results = []
    for src_table, src_col, tgt_table, tgt_col in ORPHAN_CHECKS:
        if not _table_exists(conn, src_table) or not _table_exists(conn, tgt_table):
            results.append({
                "source": f"{src_table}.{src_col}",
                "target": f"{tgt_table}.{tgt_col}",
                "orphans": -1,
                "note": "table missing",
            })
            continue
        try:
            sql = text(
                f"SELECT COUNT(*) FROM {src_table} s "
                f"WHERE s.{src_col} IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {tgt_table} t WHERE t.{tgt_col} = s.{src_col})"
            )
            count = conn.execute(sql).scalar() or 0
            if count > 0:
                sample_sql = text(
                    f"SELECT s.{src_col} FROM {src_table} s "
                    f"WHERE s.{src_col} IS NOT NULL "
                    f"AND NOT EXISTS (SELECT 1 FROM {tgt_table} t WHERE t.{tgt_col} = s.{src_col}) "
                    f"LIMIT 5"
                )
                samples = [r[0] for r in conn.execute(sample_sql).fetchall()]
                results.append({
                    "source": f"{src_table}.{src_col}",
                    "target": f"{tgt_table}.{tgt_col}",
                    "orphans": count,
                    "sample_ids": samples,
                })
            else:
                results.append({
                    "source": f"{src_table}.{src_col}",
                    "target": f"{tgt_table}.{tgt_col}",
                    "orphans": 0,
                })
        except Exception as e:
            results.append({
                "source": f"{src_table}.{src_col}",
                "target": f"{tgt_table}.{tgt_col}",
                "orphans": -1,
                "error": str(e)[:200],
            })
    return results


def run_data_quality(conn) -> dict:
    checks = {}
    try:
        checks["scan_patterns_win_rate_over_1"] = conn.execute(
            text("SELECT COUNT(*) FROM scan_patterns WHERE win_rate > 1")
        ).scalar() or 0
    except Exception as e:
        checks["scan_patterns_win_rate_over_1"] = f"ERROR: {e}"

    try:
        checks["backtests_win_rate_over_1"] = conn.execute(
            text("SELECT COUNT(*) FROM trading_backtests WHERE win_rate > 1")
        ).scalar() or 0
    except Exception as e:
        checks["backtests_win_rate_over_1"] = f"ERROR: {e}"

    try:
        checks["stuck_batch_jobs"] = conn.execute(
            text(
                "SELECT COUNT(*) FROM brain_batch_jobs "
                "WHERE status = 'running' AND started_at < NOW() - INTERVAL '2 hours'"
            )
        ).scalar() or 0
    except Exception as e:
        checks["stuck_batch_jobs"] = f"ERROR: {e}"

    try:
        checks["stuck_work_events"] = conn.execute(
            text(
                "SELECT COUNT(*) FROM brain_work_events "
                "WHERE status = 'pending' AND attempts >= max_attempts"
            )
        ).scalar() or 0
    except Exception as e:
        checks["stuck_work_events"] = f"ERROR: {e}"

    try:
        checks["duplicate_backtests"] = conn.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "  SELECT strategy_name, ticker, scan_pattern_id, COUNT(*) c "
                "  FROM trading_backtests "
                "  WHERE scan_pattern_id IS NOT NULL "
                "  GROUP BY strategy_name, ticker, scan_pattern_id HAVING COUNT(*) > 1"
                ") sub"
            )
        ).scalar() or 0
    except Exception as e:
        checks["duplicate_backtests"] = f"ERROR: {e}"

    try:
        checks["active_patterns_empty_rules"] = conn.execute(
            text(
                "SELECT COUNT(*) FROM scan_patterns "
                "WHERE active = true AND (rules_json IS NULL OR rules_json::text IN ('{}', 'null', ''))"
            )
        ).scalar() or 0
    except Exception as e:
        checks["active_patterns_empty_rules"] = f"ERROR: {e}"

    try:
        checks["stale_open_trades"] = conn.execute(
            text(
                "SELECT COUNT(*) FROM trading_trades "
                "WHERE status = 'open' AND entry_date < NOW() - INTERVAL '90 days'"
            )
        ).scalar() or 0
    except Exception as e:
        checks["stale_open_trades"] = f"ERROR: {e}"

    try:
        checks["backtests_oos_win_rate_over_1"] = conn.execute(
            text("SELECT COUNT(*) FROM trading_backtests WHERE oos_win_rate > 1")
        ).scalar() or 0
    except Exception as e:
        checks["backtests_oos_win_rate_over_1"] = f"ERROR: {e}"

    try:
        checks["patterns_oos_win_rate_over_1"] = conn.execute(
            text("SELECT COUNT(*) FROM scan_patterns WHERE oos_win_rate > 1")
        ).scalar() or 0
    except Exception as e:
        checks["patterns_oos_win_rate_over_1"] = f"ERROR: {e}"

    return checks


def run_row_counts(conn) -> dict:
    counts = {}
    rows = conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        )
    ).fetchall()
    for (table_name,) in rows:
        try:
            c = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0
            counts[table_name] = c
        except Exception as e:
            counts[table_name] = f"ERROR: {e}"
    return counts


def main() -> int:
    print("=" * 70)
    print(f"  CHILI DB Diagnostic — {datetime.utcnow().isoformat()}")
    print("=" * 70)

    with engine.connect() as conn:
        # --- Row counts ---
        print("\n--- TABLE ROW COUNTS ---")
        counts = run_row_counts(conn)
        empty_tables = []
        for table, cnt in sorted(counts.items()):
            flag = ""
            if cnt == 0:
                empty_tables.append(table)
                flag = " [EMPTY]"
            elif isinstance(cnt, str):
                flag = " [ERROR]"
            print(f"  {table:55s} {cnt:>10}{flag}")

        print(f"\n  Total tables: {len(counts)}")
        print(f"  Empty tables: {len(empty_tables)}")
        if empty_tables:
            print(f"    -> {', '.join(empty_tables)}")

        # --- Orphan checks ---
        print("\n--- ORPHAN FK REFERENCE CHECKS ---")
        orphans = run_orphan_checks(conn)
        problem_orphans = [o for o in orphans if o["orphans"] not in (0, -1)]
        missing_tables = [o for o in orphans if o["orphans"] == -1 and o.get("note") == "table missing"]
        error_checks = [o for o in orphans if o["orphans"] == -1 and "error" in o]
        clean = [o for o in orphans if o["orphans"] == 0]

        print(f"\n  Total checks: {len(orphans)}")
        print(f"  Clean: {len(clean)}")
        print(f"  ORPHANS FOUND: {len(problem_orphans)}")
        print(f"  Missing tables (skipped): {len(missing_tables)}")
        print(f"  Errors: {len(error_checks)}")

        if problem_orphans:
            print("\n  --- ORPHAN DETAILS ---")
            for o in sorted(problem_orphans, key=lambda x: -x["orphans"]):
                print(f"  [ORPHAN] {o['source']:55s} -> {o['target']:30s}  count={o['orphans']}")
                if "sample_ids" in o:
                    print(f"           sample IDs: {o['sample_ids']}")

        if missing_tables:
            print("\n  --- MISSING TABLES ---")
            for o in missing_tables:
                print(f"  [SKIP] {o['source']} -> {o['target']}")

        if error_checks:
            print("\n  --- ERRORS ---")
            for o in error_checks:
                print(f"  [ERROR] {o['source']} -> {o['target']}: {o.get('error', '?')}")

        # --- Data quality ---
        print("\n--- DATA QUALITY CHECKS ---")
        quality = run_data_quality(conn)
        for k, v in quality.items():
            flag = ""
            if isinstance(v, int) and v > 0:
                flag = " [ISSUE]"
            elif isinstance(v, str):
                flag = " [ERROR]"
            print(f"  {k:45s} {v:>10}{flag}")

        # --- Summary ---
        total_orphan_rows = sum(o["orphans"] for o in problem_orphans)
        issue_count = len(problem_orphans) + sum(
            1 for v in quality.values() if isinstance(v, int) and v > 0
        )

        print("\n" + "=" * 70)
        print(f"  SUMMARY: {issue_count} issue(s) found")
        print(f"  Total orphan rows across all checks: {total_orphan_rows}")
        print("=" * 70)

    return 0 if issue_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
