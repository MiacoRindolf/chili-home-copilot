import asyncio

from app.routers import health_routes


def test_liveness_routes_are_async_and_db_free():
    assert asyncio.run(health_routes.healthz()) == {"ok": True, "service": "chili"}
    assert asyncio.run(health_routes.api_healthz()) == {"ok": True, "service": "chili"}
