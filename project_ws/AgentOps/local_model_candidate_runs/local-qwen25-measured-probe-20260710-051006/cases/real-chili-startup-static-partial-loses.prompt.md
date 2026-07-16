Return JSON immediately. The first non-whitespace character of your response must be `{`.
Do not include analysis, a thinking section, Markdown fences, prose, or a sample/template explanation.
Fill the actual JSON object for this case; do not copy placeholders like `<unified diff for the planned file only>`.

# CHILI Compact Local Model Candidate Prompt Pack

- Schema: chili.model-candidate-drop-prompt-pack.v1
- Drop schema: chili.model-candidate-drop.v1
- Generated UTC: 2026-07-10T12:13:15.602161Z
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
