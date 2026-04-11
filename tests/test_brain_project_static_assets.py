"""Static project cockpit asset coverage."""

from __future__ import annotations

from pathlib import Path

import esprima

_STATIC_COMPONENTS = Path(__file__).resolve().parents[1] / "app" / "static" / "components"


def test_project_javascript_assets_parse() -> None:
    for name in (
        "brain-project-domain.js",
        "brain-project-agents.js",
        "brain-project-handoff.js",
    ):
        source = (_STATIC_COMPONENTS / name).read_text(encoding="utf-8")
        program = esprima.parseScript(source, tolerant=False)
        assert program.type == "Program"


def test_project_javascript_assets_export_expected_hooks() -> None:
    exports = {
        "brain-project-domain.js": (
            "window.brainProjectDomainInit = init;",
            "window.triggerCodeLearn = triggerCodeLearn;",
            "window.runCodeAgent = runCodeAgent;",
        ),
        "brain-project-agents.js": (
            "window.initProjectAgentBar = initProjectAgentBar;",
            "window.switchProjectAgent = switchProjectAgent;",
            "window.triggerAgentCycle = triggerAgentCycle;",
        ),
        "brain-project-handoff.js": (
            "window.brainHandoffPlannerTaskId = brainHandoffPlannerTaskId;",
            "window.initBrainPlannerHandoffBridge = initBrainPlannerHandoffBridge;",
            "window.brainApplyHandoffLaunchParamOnce = brainApplyHandoffLaunchParamOnce;",
        ),
    }

    for name, expected_snippets in exports.items():
        source = (_STATIC_COMPONENTS / name).read_text(encoding="utf-8")
        for snippet in expected_snippets:
            assert snippet in source


def test_project_stylesheet_contains_project_shell_utilities() -> None:
    source = (_STATIC_COMPONENTS / "brain-project-domain.css").read_text(encoding="utf-8")
    assert ".project-pane-tab" in source
    assert ".project-summary-grid" in source
    assert ".project-inline-actions" in source
