# Roadmap deviation 001 — migration numbering vs 12-month megaprompt

The trading-brain roadmap megaprompt (Q1 2026) assumed the next PostgreSQL migration after `091_momentum_automation_outcomes` would be `092`. In **this** repository, numeric migration IDs through **`162_project_domain_recovery`** are already reserved in [`app/migrations.py`](../app/migrations.py).

## What to do

- Before adding any new migration, confirm the latest `(version_id, …)` entry in the `MIGRATIONS` list and use the **next unused integer**.
- Run `.\scripts\verify-migration-ids.ps1` before merge.
- **Q1.T1 (CPCV promotion evidence)** ships as migration **`163_cpcv_promotion_gate_evidence`** on table **`scan_patterns`** (not a generic `pattern` table).

## skfolio pin

The megaprompt specified `skfolio==0.11.x`. Release **0.11.0** fails to import on Python 3.11 (`TypeError` mixing `typing.Union` with a function in `skfolio.typing`). This codebase pins **`skfolio==0.20.1`**, which provides `CombinatorialPurgedCV` and `optimal_folds_number` with a compatible API (`purged_size` / `embargo_size`).

## Renumbering

All megaprompt migration IDs **092–097** (Q1) and later **098+** (Q2) are **not** available as written. Each task must take the next free ID at implementation time, and this file should be updated if a batch renumbering doc is added.
