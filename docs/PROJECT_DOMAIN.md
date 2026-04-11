# Project Domain

The project domain is the developer cockpit behind `/brain?domain=project`.

## Goals

- Make the project surface feel like a daily-driver coding cockpit instead of a loose collection of agent panels.
- Keep repo setup, planner handoff, suggestion generation, snapshot save, dry-run apply, real apply, and validation explicitly user-triggered.
- Share one canonical workspace binding across repo registration, planner coding tasks, search, apply, and validation.
- Fail closed when a planner task is not bound to an active repo.

## Page Structure

The project page is split into four panes:

- `Workspace`
- `Planner Handoff`
- `Agents`
- `Feed`

`Workspace` is the first paint and is backed by `GET /api/brain/project/bootstrap`.

## Backend Shape

- `app/routers/brain_project.py` owns the live project/code HTTP surface.
- `app/services/project_domain_service.py` builds the bootstrap payload used for first paint.
- `app/services/coding_task/workspaces.py` resolves the canonical repo binding for planner coding tasks.
- `app/routers/planner_coding.py` reads and writes the planner coding profile, including `code_repo_id`.

## Frontend Shape

- `app/templates/brain_project_domain.html` is now just the top-level project shell and include list.
- `app/templates/brain_project_workspace_header.html` contains the shared workspace/header shell.
- `app/templates/brain_project_pane_handoff.html`, `app/templates/brain_project_pane_workspace.html`, `app/templates/brain_project_pane_agents.html`, and `app/templates/brain_project_pane_feed.html` split the four cockpit panes into maintainable partials.
- `app/templates/brain_project_agent_panel_product_owner.html`, `app/templates/brain_project_agent_panel_project_manager.html`, and `app/templates/brain_project_agent_panel_architect.html` keep the default agent panels separate from the top-level pane wrapper.
- `app/static/components/brain-project-domain.css` owns the project-only cockpit styling that previously lived inline in `brain.html`.
- `app/static/components/brain-project-domain.js` owns the workspace-first bootstrap, repo management, indexing controls, code search, feed refresh, and dev-assistant client actions.
- `app/static/components/brain-project-agents.js` owns the project agent tabs, PO/PM/architect dashboards, generic agent panels, and explicit cycle actions.
- `app/static/components/brain-project-handoff.js` owns the planner handoff bridge, snapshot save/apply controls, validation bridge, and launch-param behavior.
- `app/templates/brain.html` now provides the shared Brain shell, cross-domain styling, global helpers, tiny template-provided planner hint globals, and the final page boot sequence.

## Workspace Binding

Planner coding tasks bind to repos through `plan_task_coding_profile.code_repo_id`.

Expected behavior:

- If the bound repo exists and is active, suggest/apply/validate may be enabled.
- If the bound repo is missing or stale, the UI shows the reason and disables mutation actions.
- Legacy `repo_index` is only a fallback path during migration.

## Safe Workflow

The intended operator flow is:

1. Register a repo.
2. Run indexing.
3. Load planner handoff explicitly.
4. Generate a suggestion explicitly.
5. Save a suggestion snapshot explicitly.
6. Dry-run the patch explicitly.
7. Apply the snapshot explicitly.
8. Run validation explicitly.

Read-only summary calls should not mutate state.

## Testing

Focused coverage for the project domain currently lives in:

- `tests/test_brain_page_domain.py`
- `tests/test_brain_http_domain.py`
- `tests/test_brain_project_bootstrap.py`
- `tests/test_brain_project_routes.py`
- `tests/test_brain_project_static_assets.py`
- `tests/test_planner_coding*.py`

`tests/test_brain_project_static_assets.py` parses the extracted project JavaScript assets and checks their exported hooks so the split frontend modules stay valid even when the shared page shell changes.

The project/coding test harness was also adjusted so these slices avoid full-schema truncate/reset behavior between cases.
