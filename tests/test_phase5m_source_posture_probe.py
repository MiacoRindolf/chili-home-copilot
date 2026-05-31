from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "d-phase5m-source-posture-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5m_source_posture_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_source_to_repo_root_maps_app_mounts_to_worktree_root() -> None:
    module = _load_module()

    root = r"D:\dev\chili-home-copilot\project_ws\_worktrees\phase5l-h"
    assert module.source_to_repo_root(root + r"\app", "/app/app") == root
    assert module.source_to_repo_root(root + r"\docs", "/app/docs") == root
    assert module.source_to_repo_root(root, "/workspace") == root
    assert module.source_to_repo_root(r"D:\CHILI-Docker\chili-data", "/app/data") is None


def test_build_report_alerts_on_dirty_root_and_flag_mismatch(monkeypatch) -> None:
    module = _load_module()

    dirty_root = r"D:\dev\chili-home-copilot"
    clean_root = r"D:\dev\chili-home-copilot\project_ws\_worktrees\phase5l-h"

    def fake_inspect(container: str):
        if container == "web":
            return {
                "State": {"Status": "running", "Health": {"Status": "healthy"}},
                "Config": {
                    "Env": [
                        "CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true",
                        "CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true",
                    ]
                },
                "Mounts": [
                    {"Source": clean_root + r"\app", "Destination": "/app/app"},
                    {"Source": clean_root, "Destination": "/workspace"},
                ],
            }
        return {
            "State": {"Status": "running"},
            "Config": {
                "Env": [
                    "CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true",
                    "CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=false",
                ]
            },
            "Mounts": [{"Source": dirty_root, "Destination": "/workspace"}],
        }

    def fake_git_posture(root: str, *, base_ref: str):
        return module.GitPosture(
            root=root,
            branch="codex/test",
            commit="abc123",
            dirty=(root == dirty_root),
            ancestor_of_base=True,
        )

    monkeypatch.setattr(module, "docker_inspect", fake_inspect)
    monkeypatch.setattr(module, "git_posture", fake_git_posture)

    report = module.build_report(
        containers=("web", "worker"),
        dirty_root=dirty_root,
        base_ref="origin/codex/brain-work-done-marker-recovery",
    )

    assert report["verdict"] == "ALERT"
    assert report["using_dirty_root"] is True
    assert report["dirty_worktrees"] == [dirty_root]
    assert report["flag_mismatches"] == {
        "CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES": ["false", "true"]
    }
