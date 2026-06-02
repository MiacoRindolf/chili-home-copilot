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
