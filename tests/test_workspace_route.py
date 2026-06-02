"""Integration test for the /workspace dashboard route (renders the shell)."""


class TestWorkspaceRoute:
    def test_renders_shell_and_dashboard(self, client):
        resp = client.get("/workspace")
        assert resp.status_code == 200
        body = resp.text
        # shell present
        assert "ws-app" in body and "ws-rail" in body
        # dashboard content + KPIs + command palette
        assert "ws-kpis" in body
        assert "ws-palette" in body
        assert "here&#39;s your desk" in body or "here's your desk" in body
        # the design system stylesheet is linked
        assert "/static/css/workspace.css" in body

    def test_os_window_layer_present(self, client):
        body = client.get("/workspace").text
        # CHILI OS desktop + window manager
        assert "os-desktop" in body and "os-home" in body
        assert "/static/css/os.css" in body and "/static/js/os.js" in body
        # dock items open real routes as windows
        assert 'data-app="chat" data-src="/chat"' in body
        assert 'data-app="trading" data-src="/trading"' in body

    def test_front_door_is_the_os(self, client):
        # / now renders the CHILI OS dashboard; classic home moved to /home.
        root = client.get("/")
        assert root.status_code == 200 and "ws-app" in root.text
        home = client.get("/home")
        assert home.status_code == 200 and "CHILI Home" in home.text

    def test_embed_mode_strips_chrome(self, client):
        # ?embed=1 hides a page's own header so it's seamless inside an OS window.
        full = client.get("/chat")
        embedded = client.get("/chat?embed=1")
        assert full.status_code == 200 and embedded.status_code == 200
        assert "CHILI Chat" in full.text          # header present normally
        assert "CHILI Chat" not in embedded.text   # ...gone when embedded
        # planner header gated too
        assert "planner-header" not in client.get("/planner?embed=1").text
