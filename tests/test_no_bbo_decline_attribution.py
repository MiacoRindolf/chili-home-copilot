"""Ang `no_bbo` na terminal decline ay nagdadala na ng ebidensya (#1269).

SINUKAT 2026-09-01 sa prod. Sa 1,518 sesyong nag-terminal bilang
`live_declined:no_bbo` mula 2026-08-25, ito ang TUNAY na huling block reason:

      709  execution_bbo_unavailable / stale_beyond_ceiling
      478  no_bbo (tunay na walang ebidensya)
      234  execution_bbo_unavailable / no_provider_timestamp
       58  execution_bbo_unavailable / rejected_other
       39  execution_bbo_stale mula sa iqfeed_l2

**1,040 sa 1,518 (68.5%) ay may TUMPAK NANG dahilan** na itinapon ng hard-coded
na ``reason="no_bbo"`` sa terminal seam. At ang 478 ay hindi isang buhay na
klase -- kada araw: 08-25 = 466/466, 08-26 = 12/296, **08-27 pasulong = 0**
(naibalik ng #1177 ang pre-entry snapshot).

Ang "pinakamalaking bucket ng namiss na pera" ay hindi kailanman naging isang
hindi kilalang depekto. Isa itong depekto sa PAGLA-LABEL: ang isang
``GROUP BY reason`` ay nagsasabi ng tatlong magkakaibang bagay bilang iisa.

Runnable: pytest tests/test_no_bbo_decline_attribution.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import (
    _HELD_LIVE_STATES,
    _NO_BBO_HISTOGRAM_MAX_KEYS,
    _no_bbo_decline_detail,
    _no_bbo_run_observe,
    _no_bbo_run_reset,
)


class _Sess:
    def __init__(self, state="watching_live"):
        self.state = state


# Ang eksaktong hugis ng 709-row na bucket na natuklasan ngayong 2026-09-01.
STALE_SNAPSHOT = {
    "ok": False,
    "reason": "execution_bbo_unavailable",
    "unavailable_kind": "stale_beyond_ceiling",
    "age_seconds": 205.568,
    "max_age_seconds": 10.0,
    "adapter_quote_max_age_seconds": 60.0,
    "provider_event_at_utc": "2026-09-01T13:14:59.657924+00:00",
    "source": "alpaca_direct",
    "capture_event_sha256": "hindi-dapat-lumitaw",
    "tape_row_id": 12345,
}


def _run(le, snapshot, reason, n):
    for _ in range(n):
        _no_bbo_run_observe(le, quote_reason=reason, snapshot=snapshot)


def test_reason_is_the_truth_not_the_class():
    """ANG PANGUNAHIN. 68.5% ng bucket ay nagsisinungaling tungkol sa sarili."""
    le: dict = {}
    _run(le, STALE_SNAPSHOT, "execution_bbo_unavailable", 3)
    d = _no_bbo_decline_detail(
        le, _Sess(), quote_reason="execution_bbo_unavailable",
        snapshot=STALE_SNAPSHOT, seen=3, need=3,
        execution_family="alpaca_spot", held_states=_HELD_LIVE_STATES,
    )
    assert d["quote_reason"] == "execution_bbo_unavailable"
    assert d["decline_class"] == "no_bbo", "kailangan pa rin ng klase para sa kasaysayan"
    assert d["last_unavailable_kind"] == "stale_beyond_ceiling"


def test_histograms_cover_the_whole_run_not_just_the_last_tick():
    """Ang tanong ay 'ano ang nangyari sa 30 segundo', hindi 'ang huling tick'."""
    le: dict = {}
    _run(le, STALE_SNAPSHOT, "execution_bbo_unavailable", 2)
    _no_bbo_run_observe(
        le, quote_reason="execution_bbo_stale",
        snapshot={"unavailable_kind": None, "source": "iqfeed_l2"},
    )
    d = _no_bbo_decline_detail(
        le, _Sess(), quote_reason="execution_bbo_stale",
        snapshot={"source": "iqfeed_l2"}, seen=3, need=3,
        execution_family="alpaca_spot", held_states=_HELD_LIVE_STATES,
    )
    assert d["reason_histogram"] == {
        "execution_bbo_unavailable": 2, "execution_bbo_stale": 1,
    }
    assert d["quote_source_histogram"]["alpaca_direct"] == 2
    assert d["quote_source_histogram"]["iqfeed_l2"] == 1


def test_span_is_measured_not_assumed():
    le: dict = {}
    _run(le, STALE_SNAPSHOT, "execution_bbo_unavailable", 1)
    d = _no_bbo_decline_detail(
        le, _Sess(), quote_reason="execution_bbo_unavailable",
        snapshot=STALE_SNAPSHOT, seen=1, need=3,
        execution_family="alpaca_spot", held_states=_HELD_LIVE_STATES,
    )
    assert "first_quoteless_at_utc" in d
    assert d["quoteless_span_seconds"] >= 0.0


def test_absent_evidence_is_labelled_not_silent():
    """Ang 478 na klase (bago ang #1177). Kung babalik ito, makikita natin."""
    le: dict = {}
    _no_bbo_run_observe(le, quote_reason="no_bbo", snapshot=None)
    d = _no_bbo_decline_detail(
        le, _Sess(), quote_reason="no_bbo", snapshot=None, seen=1, need=3,
        execution_family="alpaca_spot", held_states=_HELD_LIVE_STATES,
    )
    assert d["evidence_present"] is False
    assert d["unavailable_kind_histogram"] == {"evidence_absent": 1}
    assert not any(k.startswith("last_") for k in d), "walang snapshot => walang last_*"


def test_histogram_is_bounded():
    """Ang sesyong tumatagal ng oras ay hindi makakapagpalobo ng payload."""
    le: dict = {}
    for i in range(_NO_BBO_HISTOGRAM_MAX_KEYS + 8):
        _no_bbo_run_observe(le, quote_reason=f"reason_{i}", snapshot=None)
    hist = le["no_bbo_reason_counts"]
    assert len(hist) <= _NO_BBO_HISTOGRAM_MAX_KEYS + 1
    assert hist["(iba pa)"] == 8


def test_reset_clears_the_whole_run():
    """Ang 'persistent' ay SUNUD-SUNOD — hindi dapat dalhin ng bagong takbo
    ang ebidensya ng luma."""
    le: dict = {"no_bbo_consecutive_ticks": 2}
    _run(le, STALE_SNAPSHOT, "execution_bbo_unavailable", 2)
    _no_bbo_run_reset(le)
    for k in (
        "no_bbo_consecutive_ticks", "no_bbo_reason_counts",
        "no_bbo_kind_counts", "no_bbo_source_counts", "no_bbo_first_ts_utc",
    ):
        assert k not in le, k


def test_noisy_snapshot_fields_are_not_copied():
    """Ang capture sha at tape row id ay hindi nagsasabi kung BAKIT tahimik."""
    le: dict = {}
    _run(le, STALE_SNAPSHOT, "execution_bbo_unavailable", 1)
    d = _no_bbo_decline_detail(
        le, _Sess(), quote_reason="execution_bbo_unavailable",
        snapshot=STALE_SNAPSHOT, seen=1, need=3,
        execution_family="alpaca_spot", held_states=_HELD_LIVE_STATES,
    )
    assert "last_capture_event_sha256" not in d
    assert "last_tape_row_id" not in d
    # ...pero ang DALAWANG cap ay parehong dumadaan: ang nagpasya at ang tinatak.
    assert d["last_max_age_seconds"] == 10.0
    assert d["last_adapter_quote_max_age_seconds"] == 60.0


def test_phase_reflects_the_session_state():
    le: dict = {}
    _no_bbo_run_observe(le, quote_reason="no_bbo", snapshot=None)
    pre = _no_bbo_decline_detail(
        le, _Sess("watching_live"), quote_reason="no_bbo", snapshot=None,
        seen=1, need=3, execution_family="alpaca_spot",
        held_states=_HELD_LIVE_STATES,
    )
    assert pre["phase"] == "pre_entry"
    held_state = next(iter(_HELD_LIVE_STATES))
    held = _no_bbo_decline_detail(
        le, _Sess(held_state), quote_reason="no_bbo", snapshot=None,
        seen=1, need=3, execution_family="alpaca_spot",
        held_states=_HELD_LIVE_STATES,
    )
    assert held["phase"] == "held"


def test_observe_never_raises_on_garbage():
    """Instrumentation ay hindi kailanman dapat pumatay ng tick."""
    le: dict = {"no_bbo_reason_counts": "hindi-dict", "no_bbo_kind_counts": 7}
    _no_bbo_run_observe(le, quote_reason=None, snapshot={"source": None})
    assert isinstance(le["no_bbo_reason_counts"], dict)
    assert le["no_bbo_reason_counts"]["-"] == 1
