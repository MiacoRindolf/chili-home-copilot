# CHILI Model Candidate Drop Prompt Pack

- Schema: chili.model-candidate-drop-prompt-pack.v1
- Drop schema: chili.model-candidate-drop.v1
- Generated UTC: 2026-07-03T23:28:25.804729Z
- Source kind: codex
- Model name: gpt-5.5
- Cases: 6
- Required behavior: return one valid JSON object per case, each with an inline unified-diff `patch` string.
- Safety: work only from the temporary fixture text below; do not touch the real checkout, git state, runtime, database, broker, deployment, credentials, or live-trading controls.

## Output Contract

- Return only JSON objects, either newline-delimited JSON or objects inside one JSON array.
- Return exactly one object for each case section below; no prose, Markdown fences, PR summaries, or readiness claims.
- Keep each patch scoped to the listed planned file.
- Include the listed behavior command exactly in `declared_commands`.
- Use `source_kind` exactly as shown above; do not use `fixture` for real model output.
- The `patch` value must start with `diff --git` and must be inline in the JSON object.
- Do not emit `patch_file`, `collected_at`, or `provenance`; the local recorder writes files and provenance after dry-run validation.
- Do not run commands, compute SHA-256 values, inspect the real checkout, or try to write artifacts.
- If you are uncertain, still return your best scoped patch; CHILI will reject unsafe or failing candidates.

## Source-Specific Operating Contract

- Source contract: hosted-codex-frontier-candidate
- Collector source: codex
- Use the hosted Codex session as transcript evidence, but do not treat Codex PR state or ready claims as proof without current-head receipts.
- Return one minimal unified-diff patch per case and keep external reasoning outside the patch/drop files.
- Model identity: gpt-5.5
- Every transcript must include the prompt-pack SHA-256, source kind, model name, case id, and final patch/drop decision.
- Do not create files, run commands, compute hashes, or include provenance; return only the JSON objects and CHILI records provenance after parsing.

## Case: real-chili-preflight-candidate-wins

- Comparison class: strict_candidate_win
- Planned file: preflight.py
- Expected changed files: preflight.py
- Required behavior command: `C:\Users\rindo\miniconda3\python.exe -m pytest test_preflight.py -q`

### Fixture Files

#### `preflight.py`

```text
def can_enter(
    ticker: str,
    broker_position_qty: float,
    buying_power: float,
    required_cash: float,
    *,
    broker_timeout: bool = False,
) -> bool:
    if broker_position_qty > 0:
        return False
    if buying_power < required_cash:
        return False
    return True
```

#### `test_preflight.py`

```text
from preflight import can_enter


def test_duplicate_broker_position_blocks_entry():
    assert not can_enter('BTC-USD', 0.25, 1000, 100)


def test_insufficient_cash_blocks_entry():
    assert not can_enter('BTC-USD', 0, 50, 100)


def test_broker_timeout_fails_closed():
    assert not can_enter('BTC-USD', 0, 1000, 100, broker_timeout=True)


def test_clean_preflight_allows_entry():
    assert can_enter('BTC-USD', 0, 1000, 100)
```

### Candidate Drop JSON Template

```json
{
  "candidate_id": "codex-real-chili-preflight-candidate-wins",
  "case_id": "real-chili-preflight-candidate-wins",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_preflight.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "preflight.py"
  ],
  "model_name": "gpt-5.5",
  "notes": "<short explanation>",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "preflight.py",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "codex"
}
```

## Case: real-chili-runtime-control-partial-loses

- Comparison class: runtime_control_behavior_regression
- Planned file: autopilot_prompt.py
- Expected changed files: autopilot_prompt.py
- Required behavior command: `C:\Users\rindo\miniconda3\python.exe -m pytest test_autopilot_prompt.py -q`

### Fixture Files

#### `README.md`

```text
CHILI-derived runtime-control prompt quarantine slice
```

#### `autopilot_prompt.py`

```text
def looks_like_repo_edit(prompt: str) -> bool:
    text = (prompt or '').lower()
    return any(word in text for word in ('fix', 'update', 'change', 'add', 'restart', 'deploy'))


def classify_prompt(prompt: str) -> str:
    return 'repo_edit' if looks_like_repo_edit(prompt) else 'blocked'


def plan_if_repo_edit(prompt: str, planner):
    if classify_prompt(prompt) == 'repo_edit':
        return planner(prompt)
    return {'status': 'blocked', 'reason': 'not_repo_edit'}
```

#### `test_autopilot_prompt.py`

```text
from autopilot_prompt import plan_if_repo_edit


def test_runtime_control_prompt_blocks_before_planner():
    calls = []

    def planner(prompt):
        calls.append(prompt)
        return {'status': 'planned'}

    result = plan_if_repo_edit('docker compose restart chili and scheduler-worker', planner)

    assert result['status'] == 'blocked'
    assert calls == []


def test_repo_edit_still_reaches_planner():
    result = plan_if_repo_edit(
        'update app/example.py to fix the button',
        lambda prompt: {'status': 'planned'},
    )

    assert result['status'] == 'planned'
```

### Candidate Drop JSON Template

```json
{
  "candidate_id": "codex-real-chili-runtime-control-partial-loses",
  "case_id": "real-chili-runtime-control-partial-loses",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_autopilot_prompt.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "autopilot_prompt.py"
  ],
  "model_name": "gpt-5.5",
  "notes": "<short explanation>",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "autopilot_prompt.py",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "codex"
}
```

## Case: real-chili-startup-static-partial-loses

- Comparison class: startup_contract_behavior_regression
- Planned file: startup_contracts.py
- Expected changed files: startup_contracts.py
- Required behavior command: `C:\Users\rindo\miniconda3\python.exe -m pytest test_startup_contracts.py -q`

### Fixture Files

#### `startup_contracts.py`

```text
REQUIRED_ASSETS = (
    'app/static/components/brain-project-domain.js',
    'app/static/components/brain-project-domain.css',
)

ASSET_MANIFEST = {
    'app/static/components/brain-project-domain.js': 'sha-js',
}


def missing_assets() -> list[str]:
    return [path for path in REQUIRED_ASSETS if path not in ASSET_MANIFEST]


def db_pool_size(settings: dict[str, str]) -> int:
    return max(int(settings.get('DB_POOL_SIZE', '5')), 1)


def schema_startup_wait_seconds(settings: dict[str, str]) -> int:
    return max(int(settings.get('SCHEMA_STARTUP_WAIT_SECONDS', '60')), 30)
```

#### `test_startup_contracts.py`

```text
from startup_contracts import db_pool_size, missing_assets, schema_startup_wait_seconds


def test_static_asset_manifest_contains_required_assets():
    assert missing_assets() == []


def test_db_pool_size_never_zero():
    assert db_pool_size({'DB_POOL_SIZE': '0'}) == 1


def test_schema_startup_wait_covers_crash_recovery_window():
    assert schema_startup_wait_seconds({'SCHEMA_STARTUP_WAIT_SECONDS': '5'}) == 30
```

### Candidate Drop JSON Template

```json
{
  "candidate_id": "codex-real-chili-startup-static-partial-loses",
  "case_id": "real-chili-startup-static-partial-loses",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_startup_contracts.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "startup_contracts.py"
  ],
  "model_name": "gpt-5.5",
  "notes": "<short explanation>",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "startup_contracts.py",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "codex"
}
```

## Case: real-chili-broker-timeout-partial-loses

- Comparison class: preflight_behavior_regression
- Planned file: preflight.py
- Expected changed files: preflight.py
- Required behavior command: `C:\Users\rindo\miniconda3\python.exe -m pytest test_preflight.py -q`

### Fixture Files

#### `preflight.py`

```text
def can_enter(
    ticker: str,
    broker_position_qty: float,
    buying_power: float,
    required_cash: float,
    *,
    broker_timeout: bool = False,
) -> bool:
    if broker_position_qty > 0:
        return False
    if buying_power < required_cash:
        return False
    return True
```

#### `test_preflight.py`

```text
from preflight import can_enter


def test_duplicate_broker_position_blocks_entry():
    assert not can_enter('BTC-USD', 0.25, 1000, 100)


def test_insufficient_cash_blocks_entry():
    assert not can_enter('BTC-USD', 0, 50, 100)


def test_broker_timeout_fails_closed():
    assert not can_enter('BTC-USD', 0, 1000, 100, broker_timeout=True)


def test_clean_preflight_allows_entry():
    assert can_enter('BTC-USD', 0, 1000, 100)
```

### Candidate Drop JSON Template

```json
{
  "candidate_id": "codex-real-chili-broker-timeout-partial-loses",
  "case_id": "real-chili-broker-timeout-partial-loses",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_preflight.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "preflight.py"
  ],
  "model_name": "gpt-5.5",
  "notes": "<short explanation>",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "preflight.py",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "codex"
}
```

## Case: real-chili-runtime-control-no-evidence-loses

- Comparison class: evidence_regression
- Planned file: autopilot_prompt.py
- Expected changed files: autopilot_prompt.py
- Required behavior command: `C:\Users\rindo\miniconda3\python.exe -m pytest test_autopilot_prompt.py -q`

### Fixture Files

#### `README.md`

```text
CHILI-derived runtime-control prompt quarantine slice
```

#### `autopilot_prompt.py`

```text
def looks_like_repo_edit(prompt: str) -> bool:
    text = (prompt or '').lower()
    return any(word in text for word in ('fix', 'update', 'change', 'add', 'restart', 'deploy'))


def classify_prompt(prompt: str) -> str:
    return 'repo_edit' if looks_like_repo_edit(prompt) else 'blocked'


def plan_if_repo_edit(prompt: str, planner):
    if classify_prompt(prompt) == 'repo_edit':
        return planner(prompt)
    return {'status': 'blocked', 'reason': 'not_repo_edit'}
```

#### `test_autopilot_prompt.py`

```text
from autopilot_prompt import plan_if_repo_edit


def test_runtime_control_prompt_blocks_before_planner():
    calls = []

    def planner(prompt):
        calls.append(prompt)
        return {'status': 'planned'}

    result = plan_if_repo_edit('docker compose restart chili and scheduler-worker', planner)

    assert result['status'] == 'blocked'
    assert calls == []


def test_repo_edit_still_reaches_planner():
    result = plan_if_repo_edit(
        'update app/example.py to fix the button',
        lambda prompt: {'status': 'planned'},
    )

    assert result['status'] == 'planned'
```

### Candidate Drop JSON Template

```json
{
  "candidate_id": "codex-real-chili-runtime-control-no-evidence-loses",
  "case_id": "real-chili-runtime-control-no-evidence-loses",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_autopilot_prompt.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "autopilot_prompt.py"
  ],
  "model_name": "gpt-5.5",
  "notes": "<short explanation>",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "autopilot_prompt.py",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "codex"
}
```

## Case: real-chili-runtime-control-unscoped-loses

- Comparison class: scope_regression
- Planned file: autopilot_prompt.py
- Expected changed files: autopilot_prompt.py
- Required behavior command: `C:\Users\rindo\miniconda3\python.exe -m pytest test_autopilot_prompt.py -q`

### Fixture Files

#### `README.md`

```text
CHILI-derived runtime-control prompt quarantine slice
```

#### `autopilot_prompt.py`

```text
def looks_like_repo_edit(prompt: str) -> bool:
    text = (prompt or '').lower()
    return any(word in text for word in ('fix', 'update', 'change', 'add', 'restart', 'deploy'))


def classify_prompt(prompt: str) -> str:
    return 'repo_edit' if looks_like_repo_edit(prompt) else 'blocked'


def plan_if_repo_edit(prompt: str, planner):
    if classify_prompt(prompt) == 'repo_edit':
        return planner(prompt)
    return {'status': 'blocked', 'reason': 'not_repo_edit'}
```

#### `test_autopilot_prompt.py`

```text
from autopilot_prompt import plan_if_repo_edit


def test_runtime_control_prompt_blocks_before_planner():
    calls = []

    def planner(prompt):
        calls.append(prompt)
        return {'status': 'planned'}

    result = plan_if_repo_edit('docker compose restart chili and scheduler-worker', planner)

    assert result['status'] == 'blocked'
    assert calls == []


def test_repo_edit_still_reaches_planner():
    result = plan_if_repo_edit(
        'update app/example.py to fix the button',
        lambda prompt: {'status': 'planned'},
    )

    assert result['status'] == 'planned'
```

### Candidate Drop JSON Template

```json
{
  "candidate_id": "codex-real-chili-runtime-control-unscoped-loses",
  "case_id": "real-chili-runtime-control-unscoped-loses",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_autopilot_prompt.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "autopilot_prompt.py"
  ],
  "model_name": "gpt-5.5",
  "notes": "<short explanation>",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "autopilot_prompt.py",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "codex"
}
```
