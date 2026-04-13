"""brain_io_concurrency: effective CPU + worker tiers under cgroup / Docker hints."""
from __future__ import annotations

import pytest


def test_effective_cpu_docker_hint_caps_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.trading import brain_io_concurrency as bic

    monkeypatch.delenv("CHILI_CONTAINER_CPU_LIMIT", raising=False)
    monkeypatch.setenv("CHILI_RUNNING_IN_DOCKER", "1")
    monkeypatch.setattr(bic, "cgroup_cpu_limit", lambda: None)
    monkeypatch.setattr(bic.os, "cpu_count", lambda: 64)

    class _S:
        brain_io_effective_cpus_override = None

    assert bic.effective_cpu_budget(_S()) == 4.0


def test_effective_cpu_explicit_limit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.trading import brain_io_concurrency as bic

    monkeypatch.setenv("CHILI_RUNNING_IN_DOCKER", "1")
    monkeypatch.setenv("CHILI_CONTAINER_CPU_LIMIT", "2")
    monkeypatch.setattr(bic, "cgroup_cpu_limit", lambda: None)
    monkeypatch.setattr(bic.os, "cpu_count", lambda: 64)

    class _S:
        brain_io_effective_cpus_override = None

    assert bic.effective_cpu_budget(_S()) == 2.0


def test_io_workers_high_respects_small_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.trading import brain_io_concurrency as bic

    monkeypatch.setenv("CHILI_RUNNING_IN_DOCKER", "1")
    monkeypatch.setenv("CHILI_CONTAINER_CPU_LIMIT", "2")
    monkeypatch.setattr(bic, "cgroup_cpu_limit", lambda: None)
    monkeypatch.setattr(bic.os, "cpu_count", lambda: 32)

    class _S:
        brain_io_effective_cpus_override = None
        brain_io_workers_high = None
        brain_io_workers_med = None
        brain_io_workers_low = None
        brain_snapshot_io_workers = None
        brain_prediction_io_workers = None

    assert bic.io_workers_high(_S()) == 4
