<!-- sequential-case-synthesis-first: real-chili-preflight-candidate-wins -->

Return JSON immediately. The first non-whitespace character of your response must be `{`.
Do not include analysis, a thinking section, Markdown fences, prose, or a sample/template explanation.
Fill the actual JSON object for this case; do not copy placeholders like `<unified diff for the planned file only>`.

# CHILI Compact Local Model Candidate Prompt Pack

- Schema: chili.model-candidate-drop-prompt-pack.v1
- Drop schema: chili.model-candidate-drop.v1
- Generated UTC: 2026-07-10T21:54:52.679893Z
- Source kind: local_model
- Model name: qwen2.5-coder:7b
- Cases: 1
- Required behavior: return exactly one JSON object with a unified-diff patch or full replacement content for the planned file.
- Safety: work only from the temporary fixture text below; do not touch the real checkout, git state, runtime, database, broker, deployment, credentials, or live-trading controls.

## Output Contract

- Return only valid JSON. Do not wrap the answer in Markdown.
- Replace every placeholder in the template; do not copy the template as your final answer.
- The `patch` value must start with `diff --git`.
- If unified diff formatting is difficult, omit `patch` and instead set `replacement_file_content` to the full corrected planned file content; CHILI will synthesize and replay a diff.
- Put the unified diff in the `patch` string. Do not include unrelated files.
- Escape patch line breaks as `\n` inside the JSON string; do not put literal line breaks inside `patch`.
- Include the listed behavior command exactly in `declared_commands`.
- If you are uncertain, still return your best scoped patch; CHILI will reject unsafe or failing candidates.

## Source-Specific Operating Contract

- Source contract: local-model-frontier-candidate
- Collector source: local_model
- Keep context compact: rely only on the active case, fixture files, drop template, and required behavior command.
- Return one minimal unified-diff patch per case; if uncertain, emit a rejected/incomplete drop rather than inventing hidden context.
- Model identity: qwen2.5-coder:7b
- Every transcript must include the prompt-pack SHA-256, source kind, model name, case id, and final patch/drop decision.

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

### Failure Focus

- This fixture is intentionally not green before the patch.
- At least one visible behavior test must fail against the current source.
- Do not return a no-op, an empty patch, or a `/dev/null` placeholder patch.
- Use the test names and assertions above to infer the smallest behavior fix.

### JSON Response Template

```json
{
  "candidate_id": "local_model-real-chili-preflight-candidate-wins",
  "case_id": "real-chili-preflight-candidate-wins",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_preflight.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "preflight.py"
  ],
  "model_name": "qwen2.5-coder:7b",
  "notes": "Candidate patch for real-chili-preflight-candidate-wins.",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "preflight.py",
  "replacement_file_content": "",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "local_model"
}
```


<!-- sequential-case-synthesis-first: real-chili-runtime-control-partial-loses -->

Return JSON immediately. The first non-whitespace character of your response must be `{`.
Do not include analysis, a thinking section, Markdown fences, prose, or a sample/template explanation.
Fill the actual JSON object for this case; do not copy placeholders like `<unified diff for the planned file only>`.

# CHILI Compact Local Model Candidate Prompt Pack

- Schema: chili.model-candidate-drop-prompt-pack.v1
- Drop schema: chili.model-candidate-drop.v1
- Generated UTC: 2026-07-10T21:54:54.403919Z
- Source kind: local_model
- Model name: qwen2.5-coder:7b
- Cases: 1
- Required behavior: return exactly one JSON object with a unified-diff patch or full replacement content for the planned file.
- Safety: work only from the temporary fixture text below; do not touch the real checkout, git state, runtime, database, broker, deployment, credentials, or live-trading controls.

## Output Contract

- Return only valid JSON. Do not wrap the answer in Markdown.
- Replace every placeholder in the template; do not copy the template as your final answer.
- The `patch` value must start with `diff --git`.
- If unified diff formatting is difficult, omit `patch` and instead set `replacement_file_content` to the full corrected planned file content; CHILI will synthesize and replay a diff.
- Put the unified diff in the `patch` string. Do not include unrelated files.
- Escape patch line breaks as `\n` inside the JSON string; do not put literal line breaks inside `patch`.
- Include the listed behavior command exactly in `declared_commands`.
- If you are uncertain, still return your best scoped patch; CHILI will reject unsafe or failing candidates.

## Source-Specific Operating Contract

- Source contract: local-model-frontier-candidate
- Collector source: local_model
- Keep context compact: rely only on the active case, fixture files, drop template, and required behavior command.
- Return one minimal unified-diff patch per case; if uncertain, emit a rejected/incomplete drop rather than inventing hidden context.
- Model identity: qwen2.5-coder:7b
- Every transcript must include the prompt-pack SHA-256, source kind, model name, case id, and final patch/drop decision.

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

### Failure Focus

- This fixture is intentionally not green before the patch.
- At least one visible behavior test must fail against the current source.
- Do not return a no-op, an empty patch, or a `/dev/null` placeholder patch.
- Use the test names and assertions above to infer the smallest behavior fix.

### JSON Response Template

```json
{
  "candidate_id": "local_model-real-chili-runtime-control-partial-loses",
  "case_id": "real-chili-runtime-control-partial-loses",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_autopilot_prompt.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "autopilot_prompt.py"
  ],
  "model_name": "qwen2.5-coder:7b",
  "notes": "Candidate patch for real-chili-runtime-control-partial-loses.",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "autopilot_prompt.py",
  "replacement_file_content": "",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "local_model"
}
```


<!-- sequential-case-synthesis-first: real-chili-startup-static-partial-loses -->

Return JSON immediately. The first non-whitespace character of your response must be `{`.
Do not include analysis, a thinking section, Markdown fences, prose, or a sample/template explanation.
Fill the actual JSON object for this case; do not copy placeholders like `<unified diff for the planned file only>`.

# CHILI Compact Local Model Candidate Prompt Pack

- Schema: chili.model-candidate-drop-prompt-pack.v1
- Drop schema: chili.model-candidate-drop.v1
- Generated UTC: 2026-07-10T21:54:56.005151Z
- Source kind: local_model
- Model name: qwen2.5-coder:7b
- Cases: 1
- Required behavior: return exactly one JSON object with a unified-diff patch or full replacement content for the planned file.
- Safety: work only from the temporary fixture text below; do not touch the real checkout, git state, runtime, database, broker, deployment, credentials, or live-trading controls.

## Output Contract

- Return only valid JSON. Do not wrap the answer in Markdown.
- Replace every placeholder in the template; do not copy the template as your final answer.
- The `patch` value must start with `diff --git`.
- If unified diff formatting is difficult, omit `patch` and instead set `replacement_file_content` to the full corrected planned file content; CHILI will synthesize and replay a diff.
- Put the unified diff in the `patch` string. Do not include unrelated files.
- Escape patch line breaks as `\n` inside the JSON string; do not put literal line breaks inside `patch`.
- Include the listed behavior command exactly in `declared_commands`.
- If you are uncertain, still return your best scoped patch; CHILI will reject unsafe or failing candidates.

## Source-Specific Operating Contract

- Source contract: local-model-frontier-candidate
- Collector source: local_model
- Keep context compact: rely only on the active case, fixture files, drop template, and required behavior command.
- Return one minimal unified-diff patch per case; if uncertain, emit a rejected/incomplete drop rather than inventing hidden context.
- Model identity: qwen2.5-coder:7b
- Every transcript must include the prompt-pack SHA-256, source kind, model name, case id, and final patch/drop decision.

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

### Failure Focus

- This fixture is intentionally not green before the patch.
- At least one visible behavior test must fail against the current source.
- Do not return a no-op, an empty patch, or a `/dev/null` placeholder patch.
- Use the test names and assertions above to infer the smallest behavior fix.

### JSON Response Template

```json
{
  "candidate_id": "local_model-real-chili-startup-static-partial-loses",
  "case_id": "real-chili-startup-static-partial-loses",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_startup_contracts.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "startup_contracts.py"
  ],
  "model_name": "qwen2.5-coder:7b",
  "notes": "Candidate patch for real-chili-startup-static-partial-loses.",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "startup_contracts.py",
  "replacement_file_content": "",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "local_model"
}
```


<!-- sequential-case-synthesis-first: real-chili-broker-timeout-partial-loses -->

Return JSON immediately. The first non-whitespace character of your response must be `{`.
Do not include analysis, a thinking section, Markdown fences, prose, or a sample/template explanation.
Fill the actual JSON object for this case; do not copy placeholders like `<unified diff for the planned file only>`.

# CHILI Compact Local Model Candidate Prompt Pack

- Schema: chili.model-candidate-drop-prompt-pack.v1
- Drop schema: chili.model-candidate-drop.v1
- Generated UTC: 2026-07-10T21:54:57.524882Z
- Source kind: local_model
- Model name: qwen2.5-coder:7b
- Cases: 1
- Required behavior: return exactly one JSON object with a unified-diff patch or full replacement content for the planned file.
- Safety: work only from the temporary fixture text below; do not touch the real checkout, git state, runtime, database, broker, deployment, credentials, or live-trading controls.

## Output Contract

- Return only valid JSON. Do not wrap the answer in Markdown.
- Replace every placeholder in the template; do not copy the template as your final answer.
- The `patch` value must start with `diff --git`.
- If unified diff formatting is difficult, omit `patch` and instead set `replacement_file_content` to the full corrected planned file content; CHILI will synthesize and replay a diff.
- Put the unified diff in the `patch` string. Do not include unrelated files.
- Escape patch line breaks as `\n` inside the JSON string; do not put literal line breaks inside `patch`.
- Include the listed behavior command exactly in `declared_commands`.
- If you are uncertain, still return your best scoped patch; CHILI will reject unsafe or failing candidates.

## Source-Specific Operating Contract

- Source contract: local-model-frontier-candidate
- Collector source: local_model
- Keep context compact: rely only on the active case, fixture files, drop template, and required behavior command.
- Return one minimal unified-diff patch per case; if uncertain, emit a rejected/incomplete drop rather than inventing hidden context.
- Model identity: qwen2.5-coder:7b
- Every transcript must include the prompt-pack SHA-256, source kind, model name, case id, and final patch/drop decision.

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

### Failure Focus

- This fixture is intentionally not green before the patch.
- At least one visible behavior test must fail against the current source.
- Do not return a no-op, an empty patch, or a `/dev/null` placeholder patch.
- Use the test names and assertions above to infer the smallest behavior fix.

### JSON Response Template

```json
{
  "candidate_id": "local_model-real-chili-broker-timeout-partial-loses",
  "case_id": "real-chili-broker-timeout-partial-loses",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_preflight.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "preflight.py"
  ],
  "model_name": "qwen2.5-coder:7b",
  "notes": "Candidate patch for real-chili-broker-timeout-partial-loses.",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "preflight.py",
  "replacement_file_content": "",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "local_model"
}
```


<!-- sequential-case-synthesis-first: real-chili-runtime-control-no-evidence-loses -->

Return JSON immediately. The first non-whitespace character of your response must be `{`.
Do not include analysis, a thinking section, Markdown fences, prose, or a sample/template explanation.
Fill the actual JSON object for this case; do not copy placeholders like `<unified diff for the planned file only>`.

# CHILI Compact Local Model Candidate Prompt Pack

- Schema: chili.model-candidate-drop-prompt-pack.v1
- Drop schema: chili.model-candidate-drop.v1
- Generated UTC: 2026-07-10T21:54:59.116143Z
- Source kind: local_model
- Model name: qwen2.5-coder:7b
- Cases: 1
- Required behavior: return exactly one JSON object with a unified-diff patch or full replacement content for the planned file.
- Safety: work only from the temporary fixture text below; do not touch the real checkout, git state, runtime, database, broker, deployment, credentials, or live-trading controls.

## Output Contract

- Return only valid JSON. Do not wrap the answer in Markdown.
- Replace every placeholder in the template; do not copy the template as your final answer.
- The `patch` value must start with `diff --git`.
- If unified diff formatting is difficult, omit `patch` and instead set `replacement_file_content` to the full corrected planned file content; CHILI will synthesize and replay a diff.
- Put the unified diff in the `patch` string. Do not include unrelated files.
- Escape patch line breaks as `\n` inside the JSON string; do not put literal line breaks inside `patch`.
- Include the listed behavior command exactly in `declared_commands`.
- If you are uncertain, still return your best scoped patch; CHILI will reject unsafe or failing candidates.

## Source-Specific Operating Contract

- Source contract: local-model-frontier-candidate
- Collector source: local_model
- Keep context compact: rely only on the active case, fixture files, drop template, and required behavior command.
- Return one minimal unified-diff patch per case; if uncertain, emit a rejected/incomplete drop rather than inventing hidden context.
- Model identity: qwen2.5-coder:7b
- Every transcript must include the prompt-pack SHA-256, source kind, model name, case id, and final patch/drop decision.

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

### Failure Focus

- This fixture is intentionally not green before the patch.
- At least one visible behavior test must fail against the current source.
- Do not return a no-op, an empty patch, or a `/dev/null` placeholder patch.
- Use the test names and assertions above to infer the smallest behavior fix.

### JSON Response Template

```json
{
  "candidate_id": "local_model-real-chili-runtime-control-no-evidence-loses",
  "case_id": "real-chili-runtime-control-no-evidence-loses",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_autopilot_prompt.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "autopilot_prompt.py"
  ],
  "model_name": "qwen2.5-coder:7b",
  "notes": "Candidate patch for real-chili-runtime-control-no-evidence-loses.",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "autopilot_prompt.py",
  "replacement_file_content": "",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "local_model"
}
```


<!-- sequential-case-synthesis-first: real-chili-runtime-control-unscoped-loses -->

Return JSON immediately. The first non-whitespace character of your response must be `{`.
Do not include analysis, a thinking section, Markdown fences, prose, or a sample/template explanation.
Fill the actual JSON object for this case; do not copy placeholders like `<unified diff for the planned file only>`.

# CHILI Compact Local Model Candidate Prompt Pack

- Schema: chili.model-candidate-drop-prompt-pack.v1
- Drop schema: chili.model-candidate-drop.v1
- Generated UTC: 2026-07-10T21:55:00.609419Z
- Source kind: local_model
- Model name: qwen2.5-coder:7b
- Cases: 1
- Required behavior: return exactly one JSON object with a unified-diff patch or full replacement content for the planned file.
- Safety: work only from the temporary fixture text below; do not touch the real checkout, git state, runtime, database, broker, deployment, credentials, or live-trading controls.

## Output Contract

- Return only valid JSON. Do not wrap the answer in Markdown.
- Replace every placeholder in the template; do not copy the template as your final answer.
- The `patch` value must start with `diff --git`.
- If unified diff formatting is difficult, omit `patch` and instead set `replacement_file_content` to the full corrected planned file content; CHILI will synthesize and replay a diff.
- Put the unified diff in the `patch` string. Do not include unrelated files.
- Escape patch line breaks as `\n` inside the JSON string; do not put literal line breaks inside `patch`.
- Include the listed behavior command exactly in `declared_commands`.
- If you are uncertain, still return your best scoped patch; CHILI will reject unsafe or failing candidates.

## Source-Specific Operating Contract

- Source contract: local-model-frontier-candidate
- Collector source: local_model
- Keep context compact: rely only on the active case, fixture files, drop template, and required behavior command.
- Return one minimal unified-diff patch per case; if uncertain, emit a rejected/incomplete drop rather than inventing hidden context.
- Model identity: qwen2.5-coder:7b
- Every transcript must include the prompt-pack SHA-256, source kind, model name, case id, and final patch/drop decision.

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

### Failure Focus

- This fixture is intentionally not green before the patch.
- At least one visible behavior test must fail against the current source.
- Do not return a no-op, an empty patch, or a `/dev/null` placeholder patch.
- Use the test names and assertions above to infer the smallest behavior fix.

### JSON Response Template

```json
{
  "candidate_id": "local_model-real-chili-runtime-control-unscoped-loses",
  "case_id": "real-chili-runtime-control-unscoped-loses",
  "cost_units": 0.0,
  "declared_commands": [
    "C:\\Users\\rindo\\miniconda3\\python.exe -m pytest test_autopilot_prompt.py -q"
  ],
  "duration_seconds": 0.0,
  "expected_changed_files": [
    "autopilot_prompt.py"
  ],
  "model_name": "qwen2.5-coder:7b",
  "notes": "Candidate patch for real-chili-runtime-control-unscoped-loses.",
  "patch": "<unified diff for the planned file only>",
  "planned_file": "autopilot_prompt.py",
  "replacement_file_content": "",
  "schema": "chili.model-candidate-drop.v1",
  "source_kind": "local_model"
}
```
