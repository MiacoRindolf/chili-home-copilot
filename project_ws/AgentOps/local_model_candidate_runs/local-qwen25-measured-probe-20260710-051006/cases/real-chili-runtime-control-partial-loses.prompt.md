Return JSON immediately. The first non-whitespace character of your response must be `{`.
Do not include analysis, a thinking section, Markdown fences, prose, or a sample/template explanation.
Fill the actual JSON object for this case; do not copy placeholders like `<unified diff for the planned file only>`.

# CHILI Compact Local Model Candidate Prompt Pack

- Schema: chili.model-candidate-drop-prompt-pack.v1
- Drop schema: chili.model-candidate-drop.v1
- Generated UTC: 2026-07-10T12:11:39.340044Z
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
