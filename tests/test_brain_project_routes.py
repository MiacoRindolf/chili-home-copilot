from __future__ import annotations


def _route_for(app, path: str, method: str):
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        if getattr(route, "path", None) == path and method in methods:
            return route
    return None


def _route_count(app, path: str, method: str) -> int:
    count = 0
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        if getattr(route, "path", None) == path and method in methods:
            count += 1
    return count


def test_live_project_status_route_comes_from_brain_project_router(fastapi_app):
    route = _route_for(fastapi_app, "/api/brain/project/status", "GET")
    assert route is not None
    assert route.endpoint.__module__ == "app.routers.brain_project"
    assert _route_count(fastapi_app, "/api/brain/project/status", "GET") == 1


def test_live_code_repos_route_comes_from_brain_project_router(fastapi_app):
    route = _route_for(fastapi_app, "/api/brain/code/repos", "GET")
    assert route is not None
    assert route.endpoint.__module__ == "app.routers.brain_project"
    assert _route_count(fastapi_app, "/api/brain/code/repos", "GET") == 1
