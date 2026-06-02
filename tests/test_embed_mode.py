"""Guard tests for CHILI OS "embed mode".

Every surface opened as an OS window is loaded with ``?embed=1`` and must strip
its own top nav/header (the OS window frame already provides chrome) — otherwise
it shows a redundant double header. These tests prevent a future change from
silently dropping the embed wrap.

Split into two layers:
- **Template-level guards** (DB-free): assert the embed conditional / flag is
  present in each surface's source. These run without Postgres.
- **Runtime guards**: actually GET the public, guest-renderable surfaces and
  assert the header toggles. These use the guest ``client`` fixture (needs the
  test DB). Auth-gated surfaces (profile/admin) are covered by the template
  guard + a manual live smoke, not here, to avoid the ``paired_client`` path.
"""
import os

_HERE = os.path.dirname(__file__)
_TEMPLATES = os.path.join(_HERE, "..", "app", "templates")
_EMBED_GATE = "request.query_params.get('embed')"

# Every Jinja surface that is opened as an OS window (dock app or tray surface).
_SURFACES = [
    "chat.html", "trading.html", "brain.html", "planner.html",
    "profile.html", "admin.html", "metrics.html", "home.html",
]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Template-level guards (DB-free) ──────────────────────────────────────────

def test_all_os_surfaces_gate_their_header_on_embed():
    for tpl in _SURFACES:
        src = _read(os.path.join(_TEMPLATES, tpl))
        assert _EMBED_GATE in src, f"{tpl} lost its ?embed=1 header conditional"


def test_research_generator_supports_embed():
    # The Research report is generated HTML (not a Jinja template), so its
    # generator must accept an embed flag that hides the toolbar + hero.
    src = _read(os.path.join(_HERE, "..", "app", "visual_report.py"))
    assert "embed: bool" in src, "generate_report lost its embed param"
    assert ".embed .toolbar" in src, "the embed-mode CSS rule is missing"


def test_household_is_wired_into_the_dock():
    shell = _read(os.path.join(_TEMPLATES, "_workspace.html"))
    assert 'data-app="household"' in shell and 'data-src="/home"' in shell


# ── Runtime guards (public, guest-renderable; need the test DB) ───────────────

def test_metrics_strips_topbar_on_embed(client):
    plain = client.get("/metrics")
    emb = client.get("/metrics?embed=1")
    assert plain.status_code == 200 and emb.status_code == 200
    # "Analytics Dashboard" lives only in the .m-topbar div, not the CSS.
    assert "Analytics Dashboard" in plain.text
    assert "Analytics Dashboard" not in emb.text
    assert "metrics-shell" in emb.text  # body still renders


def test_home_strips_header_on_embed(client):
    plain = client.get("/home")
    emb = client.get("/home?embed=1")
    assert plain.status_code == 200 and emb.status_code == 200
    assert "CHILI Home" in plain.text         # the .home-header <h1>
    assert "CHILI Home" not in emb.text
    assert 'class="dashboard"' in emb.text    # body still renders


def test_workspace_is_the_os_shell_and_home_is_not(client):
    ws = client.get("/workspace")
    assert ws.status_code == 200 and "os-desktop" in ws.text
    home = client.get("/home")
    assert home.status_code == 200 and "os-desktop" not in home.text  # classic page
