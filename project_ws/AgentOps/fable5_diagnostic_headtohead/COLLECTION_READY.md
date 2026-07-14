# Fable 5 Diagnostic Head-to-Head Collection Readiness

## Frozen Inputs

- Target: `claude-fable-5`
- Suite: independently authored eighth real-world diagnostic holdout
- Cases: 8
- Prompt pack: `project_ws/AgentOps/fable5_diagnostic_headtohead/prompt_pack.md`
- Prompt pack SHA-256: `c279126fb23319a30f9b440645062a54bff98acaf8b6e88252b83a476b307eec`
- Existing untouched CHILI result:
  `project_ws/AgentOps/fable5_class_diagnostic_blinded_eighth_run.json`

Prompt generation reads only manifest and public case files. A post-generation scan found zero references to
sealed primary dimensions, expected decisions/statuses, safety-oracle fields, or oracle paths.

## Authentication Preflight

- Claude Code CLI: installed, version `2.1.206`
- First-party Claude authentication: ready
- Subscription: Max
- Exact model name supported by the CLI: `claude-fable-5`
- Premium calls made while preparing this packet: **0**

No account identifiers or credentials are stored in this artifact.

## Guarded Collector

The collector is intentionally inert without `-Execute`. The executing form makes one premium request:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_fable5_diagnostic_headtohead.ps1 -Execute
```

It verifies the frozen prompt hash and first-party login, requests exactly `claude-fable-5` with no fallback model,
runs in an isolated temporary directory with safe mode and tools disabled, captures the provider-native session
transcript, binds the exact prompt and response to the native Fable 5 event, and runs a no-write scoring preflight
before publishing results.

## Claim Boundary

This pending run will compare Fable 5 with the already sealed untouched CHILI result on the same eight cases. It is
not a rerun of current CHILI and therefore calibrates the frozen CHILI implementation represented by that result.
Even after a successful provider run, parity or superiority remains unclaimed until blind human adjudication and
broader replicated same-task evidence are complete.
