"""THE bench receipt payload projection -- one function for BOTH sides of the Ross bench.

WHY THIS FILE EXISTS ([E] review, 2026-09-11). The replay side (scripts/replay_v3_fsm_window.py
``_bench_payload``) and the RECORDED side (scripts/rossbench_export_recorded_events.py) must
grade one payload shape. The exporter used to re-derive it by reading ``_BENCH_PAYLOAD_KEYS``
out of the driver SOURCE with ``ast``. That allow-list was deleted on purpose on 2026-09-07
(the whole-payload contract below), so ``bench_payload_keys()`` raised ``SystemExit`` before a
single case was exported: the recorded column that step 4 of docs/ROSS_REPLAY_BENCH.md
(``rossbench_report.py``) reads had no producer again -- the exact defect the exporter was
written to close. A second copy of a contract drifts; this module is the only copy.

App-free (stdlib only) and import-safe: the driver cannot be imported outside a ``_test``
environment, the exporter must import without a database, and both import THIS.

THE CONTRACT: the WHOLE payload, bounded -- not a whitelist.

The whitelist was deleted (2026-09-07), and the operator was right to ask why it existed at
all. Measured before deciding: the runner writes 364 distinct keys into event payloads; the
whitelist passed 19 and DROPPED 353. Receipts are 1.38 MB median / 1.63 MB largest, the whole
rossbench corpus is 0.7 GB, and recording everything costs ~1.1x -- ten percent of disk.

Ten percent of disk against entire diagnoses. In ONE day the filter silently swallowed
``frontside_size_tilt`` (the only record of the six inputs behind the multiplier that sized a
winner leg 169 sh vs 87 sh), then ``anchor_bid``/``posted``, then ``depth_frac`` (without which
the pullback-add depth band cannot be re-derived from its own distribution, which is what the
no-magic-numbers doctrine requires). Each miss costs a full bench re-run -- hours -- not ten
percent of a gigabyte.

And the filter was in the WRONG PLACE. "Keep the receipt stable across releases" is a
READ-time concern: project the fields you want when you diff. Filtering at WRITE time destroys
the information permanently. An instrument that decides in advance what you are allowed to
measure is not an instrument.

What remains is a BOUND, not a policy about meaning: no single value may exceed
``BENCH_VALUE_CHARS_MAX`` serialized chars and no payload may carry more than
``BENCH_PAYLOAD_KEYS_MAX`` keys. Anything trimmed says so in-band (``_bench_trimmed``), so a
reader is never silently lied to -- which is exactly what the whitelist did.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

#: Serialization GUARD RAILS for receipt payloads -- deliberately sized so they never bind on
#: real data.
#:
#: WHY THESE VALUES, AND WHY THEY ARE NOT THRESHOLDS. Measured over the 58,205
#: ``trading_automation_events`` payloads written in the three days to 2026-09-09:
#:     keys per payload   p50 4    p99 18    p99.99 27    MAX 33
#:     bytes per payload  p50 168  p99 658   p99.99 2454  MAX 7910
#: The key bound is set at ~4x the observed maximum and the per-VALUE character bound above
#: the largest whole payload ever observed, so neither trims anything in the measured
#: population. This is a bound against pathology (a stack trace, an unbounded list), not a
#: decision about what may be measured. If either ever binds, that is a finding to
#: investigate -- not a value to raise.
BENCH_PAYLOAD_KEYS_MAX = 128
BENCH_VALUE_CHARS_MAX = 8192

#: The name the exporter's meta records for this projection.
PAYLOAD_CONTRACT = "whole_payload_bounded"


def payload_contract() -> dict[str, Any]:
    """What a recorded-events meta says about the projection its payloads went through."""
    return {
        "contract": PAYLOAD_CONTRACT,
        "keys_max": BENCH_PAYLOAD_KEYS_MAX,
        "value_chars_max": BENCH_VALUE_CHARS_MAX,
        "load_bearing": "export_replay_v3_parity_fixtures._load_bearing_payload wins on key "
                        "collisions",
        "producer": "scripts/replay_bench_payload.bench_payload",
    }


def bench_payload(
    event_type: str,
    payload: Any,
    *,
    load_bearing: Callable[[str, dict], Mapping[str, Any]],
) -> dict:
    """The WHOLE payload, bounded, with the parity fixture's load-bearing projection on top.

    ``load_bearing`` is ``export_replay_v3_parity_fixtures._load_bearing_payload`` in both
    callers -- REQUIRED, so neither side can silently skip it (injected so this module never
    imports psycopg2)."""
    p = payload or {}
    if not isinstance(p, dict):
        return {"_payload_not_a_dict": str(type(p).__name__)}
    keep: dict = {}
    trimmed: list[str] = []
    for i, (k, v) in enumerate(p.items()):
        if i >= BENCH_PAYLOAD_KEYS_MAX:
            trimmed.append(f"+{len(p) - BENCH_PAYLOAD_KEYS_MAX} more keys")
            break
        try:
            if isinstance(v, (str, bytes)) and len(v) > BENCH_VALUE_CHARS_MAX:
                keep[str(k)] = str(v[:BENCH_VALUE_CHARS_MAX])
                trimmed.append(str(k))
                continue
            s = json.dumps(v, default=str)
            if len(s) > BENCH_VALUE_CHARS_MAX:
                keep[str(k)] = s[:BENCH_VALUE_CHARS_MAX]
                trimmed.append(str(k))
                continue
            keep[str(k)] = v
        except Exception:
            keep[str(k)] = str(v)[:BENCH_VALUE_CHARS_MAX]
    # the load-bearing projection still wins on key collisions -- it is the parity contract
    keep.update(dict(load_bearing(str(event_type), p)))
    if trimmed:
        keep["_bench_trimmed"] = trimmed
    return keep
