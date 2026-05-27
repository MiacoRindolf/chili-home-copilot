from __future__ import annotations

from app.services import brain_worker_docker as docker_helper


class _FakeContainer:
    def __init__(self, status: str = "running") -> None:
        self.id = "abcdef1234567890"
        self.status = status
        self.labels = {"com.docker.compose.service": "brain-worker"}
        self.reload_count = 0
        self.started = False
        self.stopped = False

    def reload(self) -> None:
        self.reload_count += 1

    def start(self) -> None:
        self.started = True
        self.status = "running"

    def stop(self, timeout: int = 90) -> None:
        self.stopped = True
        self.status = "exited"


class _FakeContainers:
    def __init__(self, containers: list[_FakeContainer]) -> None:
        self._containers = containers
        self.list_count = 0

    def list(self, all: bool = False):  # noqa: A002 - mirrors Docker SDK
        self.list_count += 1
        return self._containers


class _FakeClient:
    def __init__(self, containers: list[_FakeContainer]) -> None:
        self.containers = _FakeContainers(containers)
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def test_brain_worker_liveness_closes_client_and_caches(monkeypatch):
    clients: list[_FakeClient] = []

    def client_factory() -> _FakeClient:
        client = _FakeClient([_FakeContainer("running")])
        clients.append(client)
        return client

    monkeypatch.setattr(docker_helper, "_docker_sdk_client", client_factory)
    docker_helper._clear_liveness_cache()

    assert docker_helper.brain_worker_liveness_for_ui() == "alive"
    assert clients[0].close_count == 1

    assert docker_helper.brain_worker_liveness_for_ui() == "alive"
    assert len(clients) == 1

    docker_helper._clear_liveness_cache()


def test_brain_worker_liveness_missing_container_is_explicit(monkeypatch):
    clients: list[_FakeClient] = []

    def client_factory() -> _FakeClient:
        client = _FakeClient([])
        clients.append(client)
        return client

    monkeypatch.setattr(docker_helper, "_docker_sdk_client", client_factory)
    docker_helper._clear_liveness_cache()

    assert docker_helper.brain_worker_liveness_for_ui() == "missing"
    assert clients[0].close_count == 1

    docker_helper._clear_liveness_cache()


def test_brain_worker_liveness_missing_when_docker_unavailable(monkeypatch):
    def client_factory():
        raise RuntimeError("docker socket unavailable")

    monkeypatch.setattr(docker_helper, "_docker_sdk_client", client_factory)
    docker_helper._clear_liveness_cache()

    assert docker_helper.brain_worker_liveness_for_ui() == "missing"

    docker_helper._clear_liveness_cache()


def test_brain_worker_start_stop_close_clients(monkeypatch):
    container = _FakeContainer("exited")
    clients: list[_FakeClient] = []

    def client_factory() -> _FakeClient:
        client = _FakeClient([container])
        clients.append(client)
        return client

    monkeypatch.setattr(docker_helper, "_docker_sdk_client", client_factory)

    start = docker_helper.brain_worker_start_docker()
    assert start["ok"] is True
    assert start["started"] is True
    assert container.started is True
    assert clients[-1].close_count == 1

    stop = docker_helper.brain_worker_stop_docker()
    assert stop["ok"] is True
    assert stop["stopped"] is True
    assert container.stopped is True
    assert clients[-1].close_count == 1

    docker_helper._clear_liveness_cache()
