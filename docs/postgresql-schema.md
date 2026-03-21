# PostgreSQL schema notes

## Trading — learning cycle AI reports

Table `trading_learning_cycle_ai_reports` (migration `037_learning_cycle_ai_reports`):

| Column        | Type      | Description                                      |
|---------------|-----------|--------------------------------------------------|
| id            | SERIAL PK |                                                  |
| user_id       | INTEGER   | Nullable; matches other trading user scoping   |
| content       | TEXT      | Markdown report (LLM or fallback)                |
| metrics_json  | JSONB     | Whitelisted snapshot of the cycle `report` dict  |
| created_at    | TIMESTAMP | UTC, default `CURRENT_TIMESTAMP`                 |

Index: `(user_id, created_at DESC)` for newest-first listing per user.
