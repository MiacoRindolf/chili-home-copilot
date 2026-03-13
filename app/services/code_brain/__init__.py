"""Code Brain package — continuous codebase intelligence.

Modules:
- indexer   — scan repo paths, build file index, detect languages/frameworks
- analyzer  — parse files for complexity, function counts, naming conventions
- git_miner — read git log for churn, hotspots, commit frequency
- insights  — mine conventions and architectural patterns from indexed data
- learning  — orchestrate full learning cycle with status tracking
- agent     — Code Agent: analyze requests, gather context, propose changes
"""

from .indexer import scan_repo, get_registered_repos, register_repo, unregister_repo
from .analyzer import analyze_file, analyze_repo_files
from .git_miner import mine_git_history
from .insights import mine_insights, get_insights
from .learning import (
    run_code_learning_cycle,
    get_code_learning_status,
    get_code_brain_metrics,
)
from .agent import run_code_agent

__all__ = [
    "scan_repo",
    "get_registered_repos",
    "register_repo",
    "unregister_repo",
    "analyze_file",
    "analyze_repo_files",
    "mine_git_history",
    "mine_insights",
    "get_insights",
    "run_code_learning_cycle",
    "get_code_learning_status",
    "get_code_brain_metrics",
    "run_code_agent",
]
