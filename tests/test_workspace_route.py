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
