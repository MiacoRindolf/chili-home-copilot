Return JSON immediately. The first non-whitespace character of your response must be `{`.
Do not include analysis, a thinking section, Markdown fences, prose, or a sample/template explanation.
Fill the actual JSON object for this case; do not copy placeholders like `<unified diff for the planned file only>`.

# CHILI Compact Local Model Candidate Prompt Pack

- Schema: chili.model-candidate-drop-prompt-pack.v1
- Drop schema: chili.model-candidate-drop.v1
- Generated UTC: 2026-07-10T12:58:15.295510Z
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

## Replay Failure Feedback

CHILI replayed your previous candidate in the isolated fixture and it did not pass.
Return one corrected JSON object only. Keep the same case_id, planned_file, expected_changed_files, and declared_commands.
Prefer `replacement_file_content` if unified diff formatting caused the failure.

### Previous Candidate JSON

```json
{
  "candidate_id": "local_model-extracted-candidate",
  "notes": "Patch extracted from non-JSON local-model response.",
  "patch": "diff --git a/preflight.py b/preflight.py\nindex 3e4f0a1..1b2c3d4 100644\n--- a/preflight.py\n+++ b/preflight.py\n@@ -8,4 +8,4 @@ def can_enter(\n         return False\n     if buying_power < required_cash:\n         return False\n-    return True\n+    return broker_timeout or (broker_position_qty == 0 and buying_power >= required_cash)\n"
}
```

### Replay Result

```json
{
  "candidate_id": "local_model-extracted-candidate",
  "changed_files": [
    "preflight.py"
  ],
  "passed": false,
  "reason": "behavior_tests_failed",
  "score": 0,
  "status": "failed",
  "test_output": "..F.                                                                     [100%] | ================================== FAILURES =================================== | ______________________ test_broker_timeout_fails_closed _______________________ | def test_br..."
}
```
