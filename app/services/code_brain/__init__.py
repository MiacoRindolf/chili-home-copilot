"""Code Brain package — continuous codebase intelligence.

Modules:
- indexer       — scan repo paths, build file index, detect languages/frameworks
- analyzer      — parse files for complexity, function counts, naming conventions
- git_miner     — read git log for churn, hotspots, commit frequency
- insights      — mine conventions and architectural patterns from indexed data
- learning      — orchestrate full learning cycle with status tracking
- agent         — Code Agent: analyze requests, gather context, propose changes
- graph         — architecture import graph, circular dependency detection
- trends        — quality metrics time-series and degradation alerts
- reviewer      — LLM-powered automatic code review on new commits
- deps_scanner  — dependency health: outdated/vulnerable package detection
- search        — function/class symbol indexing and multi-strategy code search
- lenses        — role-based lens system for the Project Management domain
"""

from .indexer import scan_repo, get_registered_repos, register_repo, unregister_repo
from .analyzer import analyze_file, analyze_repo_files
from .git_miner import mine_git_history
from .insights import mine_insights, get_insights
from .learning import (
    run_code_learning_cycle,
    get_code_learning_status,
    get_code_brain_metrics,
    get_project_metrics,
    get_project_chat_context,
)
from . import lenses as lenses_mod
from .agent import run_code_agent
from .graph import build_dependency_graph, get_graph_data
from .trends import record_quality_snapshot, get_quality_trends, compute_trend_deltas
from .reviewer import review_recent_commits, get_recent_reviews
from .deps_scanner import scan_dependencies, get_dep_health
from .search import index_symbols, search_code, search_with_llm

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
    "get_project_metrics",
    "get_project_chat_context",
    "lenses_mod",
    "run_code_agent",
    "build_dependency_graph",
    "get_graph_data",
    "record_quality_snapshot",
    "get_quality_trends",
    "compute_trend_deltas",
    "review_recent_commits",
    "get_recent_reviews",
    "scan_dependencies",
    "get_dep_health",
    "index_symbols",
    "search_code",
    "search_with_llm",
]
