"""Proposed migration 171: chili_dispatch tables.

NOT WIRED INTO app/migrations.py YET. Review before integrating.

Adds:
  - llm_call_log         (every prompt/completion captured for distillation)
  - code_agent_runs      (audit trail of the autonomous coding loop)
  - code_kill_switch_state (singleton; survives restart)
  - distillation_runs    (fine-tune attempts and promotion decisions)
  - frozen_scope_paths   (glob -> severity, the hard-rule guard)

Idempotent: safe to re-run. Follows the convention from migrations 014-170:
  CREATE TABLE IF NOT EXISTS, INSERT ... ON CONFLICT DO NOTHING.

After review:
  1. Move this function body into app/migrations.py near migration 170.
  2. Add the tuple ("171_chili_dispatch_tables", _migration_171_chili_dispatch_tables)
     to the MIGRATIONS list (preserve the order with 170).
  3. Run .\scripts\verify-migration-ids.ps1 to confirm uniqueness.
  4. Restart the app to apply on the next migration sweep.
"""
from __future__ import annotations

from sqlalchemy import text


def _migration_171_chili_dispatch_tables(conn) -> None:
    # --- llm_call_log: distillation training set source -----------------
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS llm_call_log (
            id            BIGSERIAL PRIMARY KEY,
            trace_id      TEXT NOT NULL,
            cycle_id      BIGINT,
            provider      TEXT NOT NULL,
            model         TEXT NOT NULL,
            tier          INTEGER NOT NULL,
            purpose       TEXT NOT NULL,
            system_prompt TEXT,
            user_prompt   TEXT NOT NULL,
            completion    TEXT,
            tokens_in     INTEGER,
            tokens_out    INTEGER,
            latency_ms    INTEGER,
            cost_usd      NUMERIC(10, 6),
            success       BOOLEAN,
            weak_response BOOLEAN DEFAULT FALSE,
            failure_kind  TEXT,
            validation_status TEXT,
            distillable   BOOLEAN DEFAULT FALSE,
            created_at    TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS llm_call_log_distillable_idx "
        "ON llm_call_log (distillable, validation_status, created_at)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS llm_call_log_trace_idx "
        "ON llm_call_log (trace_id)"
    ))

    # --- code_agent_runs: 8-step cycle audit trail ----------------------
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS code_agent_runs (
            id                BIGSERIAL PRIMARY KEY,
            started_at        TIMESTAMP NOT NULL DEFAULT NOW(),
            finished_at       TIMESTAMP,
            task_id           BIGINT,
            repo_id           BIGINT,
            cycle_step        TEXT NOT NULL,
            decision          TEXT,
            rule_snapshot     JSONB,
            llm_snapshot      JSONB,
            diff_summary      JSONB,
            validation_run_id BIGINT,
            branch_name       TEXT,
            commit_sha        TEXT,
            merged_to         TEXT,
            escalation_reason TEXT,
            notify_user       BOOLEAN DEFAULT FALSE,
            notified_at       TIMESTAMP
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS code_agent_runs_started_idx "
        "ON code_agent_runs (started_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS code_agent_runs_task_idx "
        "ON code_agent_runs (task_id)"
    ))

    # --- code_kill_switch_state: singleton row, survives restart --------
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS code_kill_switch_state (
            id            INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            active        BOOLEAN NOT NULL DEFAULT FALSE,
            reason        TEXT,
            activated_at  TIMESTAMP,
            activated_by  TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            last_run_id   BIGINT
        )
        """
    ))
    conn.execute(text(
        "INSERT INTO code_kill_switch_state (id, active) "
        "VALUES (1, false) ON CONFLICT (id) DO NOTHING"
    ))

    # --- distillation_runs: fine-tune and promotion decisions -----------
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS distillation_runs (
            id                   BIGSERIAL PRIMARY KEY,
            started_at           TIMESTAMP NOT NULL DEFAULT NOW(),
            finished_at          TIMESTAMP,
            base_model           TEXT NOT NULL,
            candidate_tag        TEXT,
            train_rows           INTEGER,
            eval_rows            INTEGER,
            incumbent_pass       NUMERIC(5, 4),
            candidate_pass       NUMERIC(5, 4),
            candidate_latency_ms INTEGER,
            decision             TEXT,
            decision_reason      TEXT,
            artifact_path        TEXT
        )
        """
    ))

    # --- frozen_scope_paths: hard-rule guard ----------------------------
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS frozen_scope_paths (
            id          SERIAL PRIMARY KEY,
            glob        TEXT NOT NULL UNIQUE,
            severity    TEXT NOT NULL,
            reason      TEXT NOT NULL,
            added_at    TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    seed_rows = [
        ('app/services/trading/**',           'block',           'CLAUDE.md Hard Rules 1-2 (kill switch, drawdown breaker)'),
        ('app/trading_brain/**',              'block',           'CLAUDE.md Hard Rule 5 (prediction mirror authority frozen)'),
        ('app/migrations.py',                 'review_required', 'CLAUDE.md Hard Rule 6 (sequential idempotent migrations)'),
        ('app/services/trading/governance.py','block',           'kill switch and frozen-scope guard logic itself'),
        ('docs/KILL_SWITCH_RUNBOOK.md',       'review_required', 'incident playbook'),
        ('docs/PHASE_ROLLBACK_RUNBOOK.md',    'review_required', 'rollback playbook'),
        ('docs/DRAWDOWN_BREAKER_RUNBOOK.md',  'review_required', 'incident playbook'),
        ('certs/**',                          'block',           'TLS certs'),
        ('.env',                              'block',           'secrets'),
        ('docker-compose.yml',                'review_required', 'production topology'),
    ]
    for glob, severity, reason in seed_rows:
        conn.execute(
            text(
                "INSERT INTO frozen_scope_paths (glob, severity, reason) "
                "VALUES (:glob, :severity, :reason) "
                "ON CONFLICT (glob) DO NOTHING"
            ),
            {"glob": glob, "severity": severity, "reason": reason},
        )

    # --- coding_tasks: optional force_tier override ---------------------
    # Only add the column if coding_tasks exists; quietly skip if not.
    conn.execute(text(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'coding_tasks') THEN
            IF NOT EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name = 'coding_tasks' AND column_name = 'force_tier'
            ) THEN
              ALTER TABLE coding_tasks ADD COLUMN force_tier INTEGER;
            END IF;
            IF NOT EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name = 'coding_tasks' AND column_name = 'intended_files'
            ) THEN
              ALTER TABLE coding_tasks ADD COLUMN intended_files JSONB;
            END IF;
          END IF;
        END$$;
        """
    ))
