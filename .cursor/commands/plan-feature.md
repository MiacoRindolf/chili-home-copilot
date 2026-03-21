You are an assistant working on the CHILI (Conversational Home Interface & Life Intelligence) FastAPI project — a household assistant with Trading Brain, Code Brain, Reasoning Brain, and Project Brain domains.

When I invoke this command, I will describe a feature or change I want to make.

Follow this workflow:

1. Briefly restate the goal in your own words.
2. Identify which parts of the architecture are likely involved:
   - **Routers** (`app/routers/`, `app/routers/trading_sub/`)
   - **Services** (`app/services/`, `app/services/trading/`, `app/services/project_brain/`, `app/services/code_brain/`, `app/services/reasoning_brain/`)
   - **Models** (`app/models/` — domain-split: `trading.py`, `project_brain.py`, `core.py`, etc.)
   - **Schemas** (`app/schemas/` — domain-split: `trading.py`, `planner.py`, etc.)
   - **Migrations** (`app/migrations.py` — version-tracked `schema_version` table)
   - **LLM / Prompts** (`app/openai_client.py`, `app/services/llm_caller.py`, `app/prompts/`)
   - **Scheduler** (`app/services/trading_scheduler.py` — APScheduler background jobs)
   - **Templates / Frontend** (`app/templates/` — vanilla JS, LightweightCharts, inline CSS)
   - **Config** (`app/config.py` — Pydantic Settings, `.env`)
   - **Brain agents** (`app/services/project_brain/agents/` — PO, PM, Architect, devs, QA, etc.)
   - **External integrations** (brokers, market data providers, web research)
3. Propose a concrete implementation plan with ordered steps. For each step, name:
   - the specific files to inspect or create (by path)
   - the kind of change (new endpoint, new service method, model migration, scheduler job, pattern engine rule, agent capability, etc.)
4. Call out any risks, unknowns, or design decisions I should resolve before coding:
   - DB migration safety (new columns need sensible `DEFAULT` for PostgreSQL `ALTER TABLE` where applicable)
   - Circular import risks (use `llm_caller.py` instead of importing `chat_service` in services)
   - Scheduler job conflicts or interval tuning
   - Frontend state management considerations (module-level vars, localStorage persistence)
5. End with a short checklist I can track, e.g.:

- [ ] Update data models + add migration in `app/migrations.py`
- [ ] Add or update router endpoints
- [ ] Implement / update services
- [ ] Register scheduler job if periodic
- [ ] Integrate with LLM / prompt templates if applicable
- [ ] Add or update tests
- [ ] Update templates / UX if applicable
- [ ] Wire into brain agent if applicable

Keep the answer concise but specific to this codebase.