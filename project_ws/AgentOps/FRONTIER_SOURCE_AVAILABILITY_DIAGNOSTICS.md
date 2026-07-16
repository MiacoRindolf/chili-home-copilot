# CHILI Frontier Source Availability Diagnostics

- Schema: chili.frontier-source-availability-diagnostics.v1
- Generated UTC: 2026-07-10T23:07:25.471468Z
- Status: passed
- Promotion impact: clear
- Raw source root: D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\raw_sources_56_chili_default_comparison
- Source count: 3
- Blockers: 0
- Codex source status: ready
- Codex probe status: live_probe_passed
- Codex blocker: none
- Codex credential status: account_probe_passed
- Codex source auth mode: account
- Codex API-key probe status: none
- Codex source runner command: python scripts/autopilot_frontier_source_runner.py --source-kind codex --source-auth-mode account --json
- Codex next action: none
- Claude source status: ready
- Claude probe status: live_probe_passed
- Claude blocker: none
- Claude credential status: env_credentials_absent; logged_in
- Claude source auth mode: subscription
- Claude API-key probe status: api_key_missing
- Claude source runner command: python scripts/autopilot_frontier_source_runner.py --source-kind claude --source-auth-mode auto --json
- Claude next action: none
- Local Model source status: ready
- Local Model probe status: source_bundle_ready
- Local Model blocker: none
- Local Model credential status: none
- Local Model source auth mode: none
- Local Model API-key probe status: none
- Local Model source runner command: none
- Local Model next action: none
- Safety: read-only diagnostics only; no source/test edit, git, PR, deploy, runtime, database, broker, or live-trading action.

| Source | Source status | Raw drops | Probe status | Blocker | Credential status | Credential detail | Source auth mode | API-key probe | Source runner command | Missing files | Probe command | Exit | Stdout | Stderr | Next action |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- | --- |
| codex | ready | 6 | live_probe_passed | none | account_probe_passed | none | account | none | python scripts/autopilot_frontier_source_runner.py --source-kind codex --source-auth-mode account --json | none | codex exec --ignore-user-config --ignore-rules --model gpt-5.6-sol --ephemeral --sandbox read-only --skip-git-repo-check -c model_reasoning_effort="xhigh" - | 0 | frontier-probe-ok | 2026-07-10T23:07:15.015024Z WARN codex_core::shell_snapshot: Failed to create shell snapshot for powershell: Shell snapshot not supported yet for PowerShell OpenAI Codex v0.144.1 -------- workdir: D:\dev\chili-home-copilot model: gpt-5.6-sol provider: opena... | none |
| claude | ready | 6 | live_probe_passed | none | env_credentials_absent; logged_in | auth_method=claude.ai; provider=firstParty; subscription=max | subscription | api_key_missing | python scripts/autopilot_frontier_source_runner.py --source-kind claude --source-auth-mode auto --json | none | claude --print --model claude-fable-5 --output-format text --permission-mode dontAsk --no-session-persistence --effort max --max-budget-usd 0.5 | 0 | ok | none | none |
| local_model | ready | 6 | source_bundle_ready | none | none | none | none | none | none | none | none |  | none | none | none |
