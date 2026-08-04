"""L11b — ang SCHEDULER JOB WRAPPER mismo (hindi lang ang pure predicate).

BAKIT UMIIRAL ANG FILE NA ITO: ang unang deploy ng L11b ay bumagsak sa
`NameError: name 'settings' is not defined` — walang module-level na `settings`
sa trading_scheduler; local import ang convention doon. Ang 13 unit test ng
lease ay pawang PUMASA dahil ang pure predicate lang ang sinusubok nila; ang
wrapper ang sira. Dagdag pa, sinasakmal ng wrapper ang lahat ng exception
(sinadya — hindi dapat mamatay ang scheduler dahil sa maintenance sweep), kaya
TAHIMIK ang pagkabigo maliban sa isang WARNING line.

Kaya ang test na ito ay tumatawag sa TUNAY na job function at IGINIGIIT na
walang na-log na exception — iyon lang ang paraan para mahuli ang klase ng
bug na ito bago ang deploy.
"""
from __future__ import annotations

import logging

from app.services import trading_scheduler


def _no_exception_logged(caplog) -> bool:
    return not any(
        rec.exc_info or "eligibility lease sweep failed" in rec.getMessage()
        for rec in caplog.records
    )


def test_job_walang_name_error_kapag_naka_off(monkeypatch, caplog):
    """Flag OFF: dapat maaga siyang bumalik — pero BASA muna ang flag, na siyang
    eksaktong linyang nag-NameError sa produksyon."""
    from app.config import settings as runtime_settings

    monkeypatch.setattr(
        runtime_settings, "chili_momentum_eligibility_lease_enabled", False, raising=False
    )
    with caplog.at_level(logging.WARNING, logger="app.services.trading_scheduler"):
        trading_scheduler._run_eligibility_lease_sweep_job()
    assert _no_exception_logged(caplog), [r.getMessage() for r in caplog.records]


def test_job_umaabot_sa_sweep_kapag_naka_on(monkeypatch, caplog):
    """Flag ON: dapat maabot ang sweep call na may TUNAY na settings object.
    Ang DB layer ay pineke para hindi tumama sa database — ang sinusubok dito ay
    ang wiring ng wrapper (name resolution, argument shape), hindi ang SQL."""
    from app.config import settings as runtime_settings

    monkeypatch.setattr(
        runtime_settings, "chili_momentum_eligibility_lease_enabled", True, raising=False
    )
    monkeypatch.setattr(trading_scheduler, "_active_equity_session_symbols", lambda _db: ["AAA"])

    seen: dict[str, object] = {}

    class _FakeSession:
        closed = False

        def close(self):
            type(self).closed = True

    import app.db as _db_mod

    monkeypatch.setattr(_db_mod, "SessionLocal", lambda: _FakeSession())

    def _fake_expire(db, *, settings_obj, protected_symbols=None):
        seen["settings_obj"] = settings_obj
        seen["protected"] = list(protected_symbols or [])
        return {"demoted": 3, "reason": "lease_expired"}

    import app.services.trading.momentum_neural.eligibility_lease as lease_mod

    monkeypatch.setattr(lease_mod, "expire_stale_equity_eligibility", _fake_expire)

    with caplog.at_level(logging.WARNING, logger="app.services.trading_scheduler"):
        trading_scheduler._run_eligibility_lease_sweep_job()

    assert _no_exception_logged(caplog), [r.getMessage() for r in caplog.records]
    assert seen.get("protected") == ["AAA"]
    assert _FakeSession.closed, "dapat isinasara ang session (walang leak)"
    # Ang ipinasang settings ay dapat ang TUNAY na runtime settings — dito
    # nabubuo ang lease, kaya ang maling object ay tahimik na magbibigay ng
    # maling lease.
    assert seen.get("settings_obj") is runtime_settings
