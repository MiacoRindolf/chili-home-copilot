from pathlib import Path


class _FakeSession:
    def __init__(self, *, rollback_raises: bool = False) -> None:
        self.rollbacks = 0
        self.invalidates = 0
        self.rollback_raises = rollback_raises

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_raises:
            raise RuntimeError("rollback unavailable")

    def invalidate(self) -> None:
        self.invalidates += 1


def test_disconnect_recovery_rolls_back_and_invalidates_session():
    from app.db import recover_session_after_db_error

    session = _FakeSession()

    mode = recover_session_after_db_error(
        session,
        RuntimeError("server closed the connection unexpectedly"),
        context="unit",
    )

    assert mode == "invalidated"
    assert session.rollbacks == 1
    assert session.invalidates == 1


def test_non_disconnect_recovery_only_rolls_back():
    from app.db import recover_session_after_db_error

    session = _FakeSession()

    mode = recover_session_after_db_error(session, RuntimeError("ordinary failure"))

    assert mode == "rolled_back"
    assert session.rollbacks == 1
    assert session.invalidates == 0


def test_app_name_respects_explicit_local_web_label():
    from app.db import _resolve_app_name

    assert (
        _resolve_app_name(
            argv0="python.exe",
            environ={
                "CHILI_APP_NAME": "chili-local-web",
                "CHILI_SCHEDULER_ROLE": "none",
            },
        )
        == "chili-local-web"
    )


def test_app_name_distinguishes_worker_roles():
    from app.db import _resolve_app_name

    assert (
        _resolve_app_name(
            argv0="scheduler_worker.py",
            environ={"CHILI_SCHEDULER_ROLE": "autotrader_only"},
        )
        == "chili-autotrader-worker"
    )
    assert (
        _resolve_app_name(
            argv0="scripts/brain_worker.py",
            environ={"CHILI_SCHEDULER_ROLE": "none"},
        )
        == "chili-brain-worker"
    )
    assert (
        _resolve_app_name(
            argv0="python.exe",
            environ={"CHILI_SCHEDULER_ROLE": " none "},
        )
        == "chili-app"
    )


def test_local_launchers_default_to_api_only_named_web_process():
    root = Path(__file__).resolve().parents[1]
    for script in ("scripts/start-dev.ps1", "scripts/start-https.ps1"):
        text = (root / script).read_text(encoding="utf-8")
        assert '$env:CHILI_SCHEDULER_ROLE = "none"' in text
        assert '$env:CHILI_APP_NAME = "chili-local-web"' in text


def test_runtime_mode_override_rolls_back_owned_read_session(monkeypatch):
    from app import db as app_db
    from app.services.trading import runtime_mode_override as rmo

    class _FakeResult:
        def fetchone(self):
            return ("shadow",)

    class _RuntimeModeSession:
        def __init__(self) -> None:
            self.events = []

        def execute(self, *_args, **_kwargs):
            self.events.append("execute")
            return _FakeResult()

        def rollback(self) -> None:
            self.events.append("rollback")

        def close(self) -> None:
            self.events.append("close")

    session = _RuntimeModeSession()
    rmo.invalidate_cache("pattern_regime_tilt")
    monkeypatch.setattr(app_db, "SessionLocal", lambda: session)

    try:
        assert (
            rmo.get_runtime_mode_override("pattern_regime_tilt", bypass_cache=True)
            == "shadow"
        )
        assert session.events == ["execute", "rollback", "close"]
    finally:
        rmo.invalidate_cache("pattern_regime_tilt")
