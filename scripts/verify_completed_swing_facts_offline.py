"""Exact all-27-leg oracle for pure swing facts; local pinned artifacts only.

Run --help for explicit resource capacities. Imports the reducer file directly,
never the application package (whose __init__ imports the trading pipeline).
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
from itertools import groupby
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "app/services/trading/momentum_neural/completed_swing_facts.py"
STUDY_SHA = "552e7f635ff36eebbb58598dd3d43682aec223db26ad6fd5a40d320b377c333a"
LEDGER_SHA = "151dab7fb5803cb7fc352439314182ad61434163efa25881332920c1e8365e12"
UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def clock_us(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        # The existing export explicitly records naive event timestamps as UTC.
        value = value.replace(tzinfo=timezone.utc)
    delta = value.astimezone(timezone.utc) - UTC_EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def load_module():
    spec = importlib.util.spec_from_file_location("offline_completed_swing_facts", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert "app.db" not in sys.modules
    return module


def expected_witness(value):
    return None if value is None else (
        value["id"], value["price"], clock_us(value["event_utc"]),
        clock_us(value["received_utc"]), clock_us(value["publication_marker_utc"]),
        clock_us(value["recorded_known_bound_utc"]),
    )


def actual_witness(value):
    return None if value is None else (value.id, value.price, value.event_us,
                                      value.received_us, value.published_us, value.known_us)


def run_leg(m, leg, prefix, capacity, *, chunk, restart):
    state = m.State(leg["leg_id"], "pinned_recorded_export")
    begin = clock_us(leg["entry_broker_at_utc"])
    found, excluded = {}, []
    maxima = Counter()
    for _, grouped in groupby(prefix, key=lambda p: p.known_us):
        values = list(grouped)
        rec = m.Receipt(state.stream_key, state.segment_key, values[0].known_us,
                        len(values), values[-1].cursor, m.rows_sha256(values), m.state_sha256(state))
        working = m.Frontier(state, rec, capacity)
        width = chunk or len(values)
        for offset in range(0, len(values), width):
            working.feed(values[offset:offset+width])
        result = working.finish()
        assert result.status == "applied", (leg["leg_id"], rec.known_us, result.reason)
        state = m.restore_state(m.dump_state(result.state)) if restart else result.state
        maxima["frontiers"] += 1
        maxima["pending"] = max(maxima["pending"], len(state.pending))
        maxima["facts"] = max(maxima["facts"], len(result.facts))
        maxima["state_bytes"] = max(maxima["state_bytes"], len(m.dump_state(state).encode()))
        for fact in result.facts:
            candidate = fact.candidate
            if candidate.low.first.event_us < begin:
                if fact.variant == m.MICRO and candidate.micro_confirmation.known_us >= begin:
                    excluded.append((candidate.low.first.id, candidate.micro_confirmation.id,
                                     candidate.low.first.event_us, candidate.micro_confirmation.known_us))
                continue
            key = candidate.candidate_id
            item = found.setdefault(key, {"candidate": candidate, "variants": {}})
            assert item["candidate"] == candidate
            assert fact.variant not in item["variants"], (key, fact.variant)
            item["variants"][fact.variant] = fact
    for pending in state.pending:
        candidate = pending.candidate
        if candidate.low.first.event_us >= begin:
            item = found[candidate.candidate_id]
            for variant in pending.variants:
                assert variant not in item["variants"]
                item["variants"][variant] = None
    return found, excluded, state, dict(maxima)


def compare(m, actual, expected):
    assert set(actual) == set(expected), ("identity_difference", set(actual) ^ set(expected))
    counts = Counter()
    for key in sorted(expected):
        item, ref = actual[key], expected[key]
        candidate = item["candidate"]
        for value, field in ((candidate.peak.first, "preceding_local_peak_first"),
                             (candidate.peak.last, "preceding_local_peak_last"),
                             (candidate.low.first, "low_first"),
                             (candidate.low.last, "low_last_plateau"),
                             (candidate.micro_confirmation, "micro_confirmation")):
            assert actual_witness(value) == expected_witness(ref[field]), (key, field)
        assert candidate.low.count == ref["low_plateau_print_count"], key
        assert candidate.peak_has_prior_rise == ref["preceding_peak_has_observed_prior_rise"], key
        assert candidate.path_max_gap_us == round(ref["peak_to_micro_confirmation_max_event_gap_s"] * 1000000), key
        assert not ref["peak_to_micro_confirmation_epoch_change"], key
        assert set(item["variants"]) == set(ref["variants"]), key
        for variant, oracle in ref["variants"].items():
            fact = item["variants"][variant]
            if oracle["status"] == "pending_at_broker_close":
                assert fact is None, (key, variant)
                counts[variant + ":pending"] += 1
                continue
            assert fact is not None, (key, variant)
            assert actual_witness(fact.confirmation) == expected_witness(oracle.get("confirmation")), (key, variant, "confirmation")
            assert actual_witness(fact.invalidating_print) == expected_witness(oracle.get("invalidating_print")), (key, variant, "invalidation")
            if oracle["status"] == "confirmed":
                broken = oracle["followup"]["already_broken_by_confirmation_known_bound"]
                expected_status = "confirmed_broken_by_frontier" if broken else "confirmed_intact_at_frontier"
                assert fact.status == expected_status, (key, variant, fact.status, expected_status)
                assert actual_witness(fact.first_break_in_frontier) == (
                    expected_witness(oracle["followup"]["first_strict_low_break"]) if broken else None
                ), (key, variant, "frontier_break")
                counts[variant + ":confirmed"] += 1
                counts[variant + ":intact"] += not broken
            else:
                assert fact.status == oracle["status"], (key, variant, fact.status)
                counts[variant + ":" + fact.status] += 1
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-pending", type=int, required=True)
    parser.add_argument("--max-facts", type=int, required=True)
    parser.add_argument("--max-prints", type=int, required=True)
    parser.add_argument("--state-bytes", type=int, required=True)
    parser.add_argument("--chunk-prints", type=int, required=True)
    args = parser.parse_args()
    assert args.chunk_prints > 0
    def no_network(event, arguments):
        if event in ("socket.connect", "socket.getaddrinfo"):
            raise RuntimeError("offline oracle forbids network")
    sys.addaudithook(no_network)
    start = time.monotonic()
    module_pin, script_pin = sha(MODULE), sha(__file__)
    m = load_module()
    assert sha(MODULE) == module_pin, "module_changed_while_loading"
    cap = m.Capacities(args.max_pending, args.max_facts, args.max_prints, args.state_bytes)
    folder = args.artifacts.resolve()
    study_path = folder / "ASTRA_SEP10_COMPLETED_SWING_COMPARISON.json"
    ledger_path = folder / "ASTRA_SEP10_COMPLETED_SWING_CANDIDATES.jsonl.gz"
    assert sha(study_path) == STUDY_SHA and sha(ledger_path) == LEDGER_SHA
    study = json.loads(study_path.read_text(encoding="utf-8"))
    checked = {str(study_path): STUDY_SHA, str(ledger_path): LEDGER_SHA,
               str(MODULE): module_pin, str(Path(__file__).resolve()): script_pin}
    for name, digest in study["source_hashes"].items():
        assert sha(folder / name) == digest, name
        checked[str(folder / name)] = digest
    cohort = json.loads((folder / "ASTRA_SEP10_CLOSED_COHORT.json").read_text(encoding="utf-8"))["legs"]
    expected = {}
    with gzip.open(ledger_path, "rt", encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            assert row["candidate_id"] not in expected
            expected[row["candidate_id"]] = row
    assert len(expected) == 19173 and len(cohort) == 27
    leg_results, total = [], Counter()
    for stream in study["streams"]:
        path = Path(stream["path"])
        assert sha(path) == stream["sha256_gzip"], path
        checked[str(path)] = stream["sha256_gzip"]
        values = []
        with gzip.open(path, "rt", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                values.append(m.Print(row["id"], float(row["price"]), float(row["size"]),
                                      clock_us(row["observed_at"]), clock_us(row["received_at"]),
                                      clock_us(row["available_at"]), tuple(row.get(k) for k in
                                      ("source", "bridge_run_id", "connection_generation", "bridge_version"))))
        values.sort(key=lambda p: p.cursor)
        assert len(values) == stream["rows"] and not stream["invalid_rows"]
        assert all(a.known_us <= b.known_us for a, b in zip(values, values[1:]))
        for leg in (leg for leg in cohort if leg["session_id"] == stream["session_id"]):
            begin, end = clock_us(leg["entry_broker_at_utc"]), clock_us(leg["exit_broker_at_utc"])
            prefix = [p for p in values if p.known_us <= end]
            baseline = run_leg(m, leg, prefix, cap, chunk=None, restart=False)
            restarted = run_leg(m, leg, prefix, cap, chunk=args.chunk_prints, restart=True)
            assert baseline == restarted, (leg["leg_id"], "chunk_restart_difference")
            found, excluded, state, maxima = baseline
            wanted = {key: value for key, value in expected.items() if value["leg_id"] == leg["leg_id"]}
            counts = compare(m, found, wanted)
            ref_leg = next(x for x in study["legs"] if x["leg_id"] == leg["leg_id"])
            excluded_ref = [(x["low_id"], x["confirmation_id"], clock_us(x["low_event_utc"]),
                             clock_us(x["confirmation_known_bound_utc"]))
                            for x in ref_leg["formed_before_entry_confirmed_during_hold_excluded"]]
            assert excluded == excluded_ref, (leg["leg_id"], "pre_entry_population")
            unfinished = state.plateau.first if state.direction == -1 and state.plateau.first.event_us >= begin else None
            assert actual_witness(unfinished) == expected_witness(ref_leg["unfinished_terminal_decline"])
            eligible = sum(p.event_us >= begin for p in prefix)
            assert eligible == ref_leg["eligible_held_prints"]
            result = {"leg_id": leg["leg_id"], "status": "PASS", "coverage": ref_leg["coverage"],
                      "eligible_held_prints": eligible, "prefix_prints": len(prefix), "candidates": len(found),
                      "excluded_pre_entry": len(excluded), "unfinished_decline": unfinished is not None,
                      "counts": counts, "resource_observations": maxima,
                      "chunk_and_every_frontier_restart_equal": True}
            leg_results.append(result)
            total.update(counts)
            total.update(candidates=len(found), eligible_held_prints=eligible,
                         excluded_pre_entry=len(excluded), unfinished_declines=int(unfinished is not None))
            print(json.dumps({"leg": leg["leg_id"], "candidates": len(found), "status": "PASS"}), flush=True)
    assert len(leg_results) == 27 and total["candidates"] == 19173
    for path, digest in checked.items():
        assert sha(path) == digest, (path, "input_changed_during_run")
    output = {"schema": "completed_swing_facts_all27_oracle_v1", "status": "PASS",
              "generated_at_utc": datetime.now(timezone.utc).isoformat(), "module_sha256": module_pin,
              "oracle_script_sha256": script_pin, "checked_input_sha256": checked,
              "capacities": asdict(cap), "chunk_prints": args.chunk_prints,
              "elapsed_seconds": time.monotonic()-start, "totals": dict(total), "legs": leg_results,
              "limits": "Recorded-clock geometry only; complete supplied frontier membership is caller authority. "
                        "Not captured visibility, provider completeness, stop selection, execution or profitability."}
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": "PASS", "legs": len(leg_results), "totals": dict(total),
                      "elapsed_seconds": output["elapsed_seconds"]}), flush=True)


if __name__ == "__main__":
    main()
