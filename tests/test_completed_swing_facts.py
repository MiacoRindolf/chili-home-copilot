"""Standalone pure tests: python -B tests/test_completed_swing_facts.py.

Loads only the stdlib reducer file. No app package or pytest DB fixture imports.
"""
from dataclasses import replace
import hashlib
import importlib.util
from itertools import groupby
import json
from pathlib import Path
import sys
import unittest


MODULE = Path(__file__).resolve().parents[1] / "app/services/trading/momentum_neural/completed_swing_facts.py"
spec = importlib.util.spec_from_file_location("standalone_completed_swing_facts", MODULE)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

# Explicit test resource budgets, not market thresholds/defaults.
CAP = m.Capacities(100, 1000, 1000, 1000000)


def rows(prices, *, known=None, start=1, epoch=("fixture", 1)):
    return [m.Print(start+i, price, 1, start+i, start+i+100 if known is None else known,
                    start+i+100 if known is None else known, epoch)
            for i, price in enumerate(prices)]


def receipt(state, values):
    return m.Receipt(state.stream_key, state.segment_key, values[0].known_us, len(values),
                     values[-1].cursor, m.rows_sha256(values), m.state_sha256(state))


def run(values, *, chunk=None, restart=False):
    state, facts = m.State("leg", "segment"), []
    for _, group in groupby(values, key=lambda p: p.known_us):
        group = list(group)
        work = m.Frontier(state, receipt(state, group), CAP)
        before = m.dump_state(state)
        width = chunk or len(group)
        for i in range(0, len(group), width):
            work.feed(group[i:i+width])
            assert m.dump_state(state) == before
        result = work.finish()
        assert result.status == "applied", result.reason
        state = m.restore_state(m.dump_state(result.state)) if restart else result.state
        facts.extend(result.facts)
    return state, facts


class CompletedSwingFactsTests(unittest.TestCase):
    def test_no_application_or_database_import(self):
        self.assertNotIn("app.db", sys.modules)
        self.assertNotIn("app.services.trading.momentum_neural.pipeline", sys.modules)

    def test_rising_and_flat_streams_never_invent_a_low(self):
        for prices in ([1, 2, 3, 4], [2, 2, 2, 2]):
            self.assertEqual(run(rows(prices))[1], [])

    def test_unfinished_decline_and_plateau_remain_unconfirmed(self):
        state, facts = run(rows([4, 3, 2, 2]))
        self.assertEqual(facts, [])
        self.assertEqual((state.direction, state.plateau.first.id, state.plateau.last.id), (-1, 3, 4))

    def test_micro_touch_and_strict_are_separate(self):
        state, facts = run(rows([9, 10, 9, 9, 9.5, 10]))
        by_variant = {f.variant: f for f in facts}
        self.assertEqual(by_variant[m.MICRO].confirmation.id, 5)
        self.assertEqual(by_variant[m.TOUCH].confirmation.id, 6)
        self.assertNotIn(m.STRICT, by_variant)
        self.assertEqual(state.pending[0].variants, (m.STRICT,))
        values = rows([10.1], start=7)
        result = m.reduce_frontier(state, receipt(state, values), values, CAP)
        self.assertEqual(result.facts[0].variant, m.STRICT)
        self.assertEqual(result.facts[0].candidate.low.first.id, 3)

    def test_fixed_low_is_not_replaced_or_resurrected(self):
        _, facts = run(rows([9, 10, 9, 9.5, 8, 11]))
        old = [f for f in facts if f.candidate.low.first.id == 3 and f.variant != m.MICRO]
        self.assertEqual(len(old), 2)
        self.assertTrue(all(f.status == "undercut_before_reclaim" and f.invalidating_print.id == 5 for f in old))
        self.assertTrue(any(f.candidate.low.first.id == 5 and f.variant == m.STRICT for f in facts))

    def test_same_frontier_break_then_recovery_is_not_intact(self):
        _, facts = run(rows([9, 10, 9, 11, 8, 12], known=100))
        old = [f for f in facts if f.candidate.low.first.id == 3]
        self.assertEqual(len(old), 3)
        self.assertTrue(all(f.status == "confirmed_broken_by_frontier" for f in old))
        self.assertTrue(all(f.first_break_in_frontier.id == 5 for f in old))

    def test_transport_chunks_cannot_publish_intermediate_facts(self):
        values = rows([9, 10, 9, 11, 8, 12], known=100)
        baseline = run(values)
        for width in range(1, len(values)+1):
            self.assertEqual(run(values, chunk=width), baseline)
        initial = m.State("leg", "segment")
        work = m.Frontier(initial, receipt(initial, values), CAP)
        work.feed(values[:4])
        result = work.finish()
        self.assertEqual(result.status, "unresolved")
        self.assertIs(result.state, initial)
        self.assertEqual(result.facts, ())

    def test_different_actual_frontiers_change_visibility_not_identity(self):
        prices = [9, 10, 9, 11, 8, 12]
        _, separate = run(rows(prices))
        _, together = run(rows(prices, known=100))
        self.assertEqual([(f.candidate.candidate_id, f.variant) for f in separate],
                         [(f.candidate.candidate_id, f.variant) for f in together])
        self.assertTrue(all(f.status == "confirmed_intact_at_frontier" for f in separate))
        self.assertTrue(any(f.status == "confirmed_broken_by_frontier" for f in together))

    def test_left_boundary_does_not_invent_prior_rise(self):
        state, facts = run(rows([10, 9, 11]))
        self.assertEqual([f.status for f in facts], ["confirmed_intact_at_frontier",
                                                   "unknown_peak_at_boundary", "unknown_peak_at_boundary"])
        self.assertEqual(state.pending, ())

    def test_plateau_identity_and_gap_are_exact(self):
        _, facts = run(rows([9, 10, 10, 9, 9, 9, 11], known=100))
        candidate = facts[0].candidate
        self.assertEqual((candidate.peak.first.id, candidate.peak.last.id, candidate.peak.count), (2, 3, 2))
        self.assertEqual((candidate.low.first.id, candidate.low.last.id, candidate.low.count), (4, 6, 3))
        self.assertEqual(candidate.path_max_gap_us, 1)

    def test_all_nested_fixed_identities_survive_until_reclaim(self):
        _, facts = run(rows([1, 10, 2, 9, 3, 8, 4, 11]))
        strict = [f for f in facts if f.variant == m.STRICT]
        self.assertEqual([f.candidate.low.first.id for f in strict], [3, 5, 7])
        self.assertTrue(all(f.confirmation.id == 8 for f in strict))

    def test_repeated_touches_keep_distinct_strict_candidates(self):
        state, _ = run(rows([1, 2, 1, 2, 1, 2, 1, 2]))
        self.assertEqual([p.candidate.low.first.id for p in state.pending], [3, 5, 7])
        self.assertTrue(all(p.variants == (m.STRICT,) for p in state.pending))

    def test_each_resource_capacity_failure_is_atomic(self):
        initial = m.State("leg", "segment")
        values = rows([1, 2, 1, 2, 1, 2], known=100)
        for cap in (replace(CAP, pending_candidates=1), replace(CAP, frontier_facts=1),
                    replace(CAP, frontier_prints=len(values)-1),
                    replace(CAP, state_bytes=len(m.dump_state(initial).encode())+1)):
            result = m.reduce_frontier(initial, receipt(initial, values), values, cap)
            self.assertEqual(result.reason, "resource_capacity_unresolved")
            self.assertIs(result.state, initial)
            self.assertEqual(result.facts, ())

    def test_capacity_failure_keeps_prior_candidates_and_can_replay(self):
        state, _ = run(rows([1, 2, 1, 2]))
        values = rows([1, 2], known=200, start=5)
        rec = receipt(state, values)
        result = m.reduce_frontier(state, rec, values, replace(CAP, pending_candidates=1))
        self.assertIs(result.state, state)
        self.assertEqual([p.candidate.low.first.id for p in state.pending], [3])
        result = m.reduce_frontier(state, rec, values, CAP)
        self.assertEqual([p.candidate.low.first.id for p in result.state.pending], [3, 5])

    def test_restart_after_every_frontier_matches_uninterrupted(self):
        values = rows([1, 10, 2, 9, 3, 8, 4, 11, 3, 12])
        self.assertEqual(run(values), run(values, restart=True))

    def test_interrupted_work_can_be_discarded_and_replayed(self):
        initial = m.State("leg", "segment")
        values = rows([1, 2, 1, 3], known=100)
        rec = receipt(initial, values)
        work = m.Frontier(initial, rec, CAP)
        work.feed(values[:3])
        before = m.dump_state(initial)
        del work
        restored = m.restore_state(before)
        self.assertEqual(m.reduce_frontier(initial, rec, values, CAP),
                         m.reduce_frontier(restored, rec, values, CAP))

    def test_exact_receipt_replay_is_idempotent_but_payload_is_verified(self):
        initial = m.State("leg", "segment")
        values = rows([1, 2, 1, 3], known=100)
        rec = receipt(initial, values)
        first = m.reduce_frontier(initial, rec, values, CAP)
        again = m.reduce_frontier(first.state, rec, values, CAP)
        self.assertEqual(again.status, "already_applied")
        self.assertIs(again.state, first.state)
        self.assertEqual(again.facts, ())
        bad = [replace(p, price=p.price+.1) for p in values]
        self.assertEqual(m.reduce_frontier(first.state, rec, bad, CAP).status, "unresolved")

    def test_late_epoch_and_frontier_conflicts_do_not_mutate_state(self):
        state, _ = run(rows([1, 2]))
        variants = [rows([3], start=1, known=200), rows([3], start=3, known=200, epoch=("other", 2)),
                    rows([3], start=3, known=101)]
        for values in variants:
            result = m.reduce_frontier(state, receipt(state, values), values, CAP)
            self.assertEqual(result.status, "unresolved")
            self.assertIs(result.state, state)
            self.assertEqual(result.facts, ())
        fresh = m.State("leg", "explicit-new-segment")
        values = rows([10, 9, 11], known=200, epoch=("other", 2))
        result = m.reduce_frontier(fresh, receipt(fresh, values), values, CAP)
        self.assertEqual(result.facts[-1].status, "unknown_peak_at_boundary")

    def test_wrong_identity_previous_state_and_membership_are_rejected(self):
        state = m.State("leg", "segment")
        values = rows([1, 2], known=100)
        rec = receipt(state, values)
        for wrong in (replace(rec, stream_key="other"), replace(rec, segment_key="other"),
                      replace(rec, previous_state_sha256="0"*64), replace(rec, rows_sha256="0"*64),
                      replace(rec, known_us=101), replace(rec, last_cursor=(9, 9))):
            result = m.reduce_frontier(state, wrong, values, CAP)
            self.assertEqual(result.status, "unresolved")
            self.assertIs(result.state, state)

    def test_unknown_version_corruption_and_invalid_direction_reject_restart(self):
        state, _ = run(rows([1, 2, 1, 2]))
        original = json.loads(m.dump_state(state))
        for field, value in (("version", "future"), ("direction", 7), ("stream_key", "other")):
            payload = {**original["payload"], field: value}
            envelope = {"payload": payload, "sha256": hashlib.sha256(m._json(payload)).hexdigest()}
            with self.assertRaises(ValueError):
                m.restore_state(json.dumps(envelope))
        original["sha256"] = "0"*64
        with self.assertRaises(ValueError):
            m.restore_state(json.dumps(original))

    def test_invalid_prints_and_capacities_never_default_to_valid_evidence(self):
        for bad in (None, True, float("nan"), float("inf"), 0, -1, "1"):
            with self.assertRaises(ValueError):
                m.Print(1, bad, 1, 1, 2, 2, ("fixture",))
        with self.assertRaises(ValueError):
            m.Print(1, 1, 1, 1, 3, 2, ("fixture",))
        with self.assertRaises(ValueError):
            m.Capacities(True, 1, 1, 1)

    def test_restart_does_not_default_missing_version_or_nested_fields(self):
        state, _ = run(rows([1, 2, 1, 2]))
        for field in ("version", "direction", "pending"):
            envelope = json.loads(m.dump_state(state))
            envelope["payload"].pop(field)
            envelope["sha256"] = hashlib.sha256(m._json(envelope["payload"])).hexdigest()
            with self.assertRaises(ValueError):
                m.restore_state(json.dumps(envelope))
        envelope = json.loads(m.dump_state(state))
        envelope["payload"]["last_receipt"].pop("contract")
        envelope["sha256"] = hashlib.sha256(m._json(envelope["payload"])).hexdigest()
        with self.assertRaises(ValueError):
            m.restore_state(json.dumps(envelope))

    def test_mutable_state_containers_cannot_break_committed_immutability(self):
        with self.assertRaises(ValueError):
            m.State("leg", "segment", pending=[])
        state, _ = run(rows([1, 2, 1, 2]))
        with self.assertRaises(ValueError):
            m.Pending(state.pending[0].candidate, [m.STRICT])
        with self.assertRaises(ValueError):
            replace(state.last_receipt, last_cursor=list(state.last_receipt.last_cursor))

    def test_restart_rejects_decline_peak_from_another_epoch(self):
        # Root's exact witness: a valid checksum cannot repair contradictory history.
        state, _ = run(rows([9, 10, 9], epoch=("run", 1)))
        envelope = json.loads(m.dump_state(state))
        for end in ("first", "last"):
            envelope["payload"]["decline_peak"][end]["epoch"] = ["different-run", 9]
        envelope["sha256"] = hashlib.sha256(m._json(envelope["payload"])).hexdigest()
        with self.assertRaises(ValueError):
            m.restore_state(json.dumps(envelope))

    def test_restart_rejects_mixed_epoch_plateaus_and_fixed_candidates(self):
        state, _ = run(rows([9, 10, 10, 9, 9, 10], epoch=("run", 1)))
        for field, ends in (("peak", ("first",)), ("low", ("last",)),
                            ("peak", ("first", "last")), ("low", ("first", "last"))):
            with self.subTest(field=field, ends=ends):
                envelope = json.loads(m.dump_state(state))
                candidate = envelope["payload"]["pending"][0]["candidate"]
                for end in ends:
                    candidate[field][end]["epoch"] = ["different-run", 9]
                envelope["sha256"] = hashlib.sha256(m._json(envelope["payload"])).hexdigest()
                with self.assertRaises(ValueError):
                    m.restore_state(json.dumps(envelope))

    def test_restart_requires_json_arrays_without_epoch_coercion(self):
        state, _ = run(rows([9, 10, 9, 10]))
        for wrong in ("fixture", {"fixture": 1}, 1, None):
            with self.subTest(wrong=wrong):
                envelope = json.loads(m.dump_state(state))
                def replace_epochs(value):
                    if isinstance(value, dict):
                        for key, child in value.items():
                            if key == "epoch":
                                value[key] = wrong
                            else:
                                replace_epochs(child)
                    elif isinstance(value, list):
                        for child in value:
                            replace_epochs(child)
                replace_epochs(envelope["payload"])
                envelope["sha256"] = hashlib.sha256(m._json(envelope["payload"])).hexdigest()
                with self.assertRaises(ValueError):
                    m.restore_state(json.dumps(envelope))
        empty, _ = run(rows([1, 2]))
        envelope = json.loads(m.dump_state(empty))
        envelope["payload"]["pending"] = {}
        envelope["sha256"] = hashlib.sha256(m._json(envelope["payload"])).hexdigest()
        with self.assertRaises(ValueError):
            m.restore_state(json.dumps(envelope))
        envelope = json.loads(m.dump_state(state))
        envelope["payload"]["pending"][0]["variants"] = {m.STRICT: True}
        envelope["sha256"] = hashlib.sha256(m._json(envelope["payload"])).hexdigest()
        with self.assertRaises(ValueError):
            m.restore_state(json.dumps(envelope))

    def test_restart_rejects_witness_clocks_outside_committed_frontier(self):
        state, _ = run(rows([9, 10, 9]))
        for field in ("plateau", "decline_peak"):
            with self.subTest(field=field):
                envelope = json.loads(m.dump_state(state))
                for end in ("first", "last"):
                    envelope["payload"][field][end]["published_us"] += 1000
                envelope["sha256"] = hashlib.sha256(m._json(envelope["payload"])).hexdigest()
                with self.assertRaises(ValueError):
                    m.restore_state(json.dumps(envelope))
        pending, _ = run(rows([9, 10, 10, 9, 10]))
        for field in ("peak", "low", "micro_confirmation"):
            with self.subTest(pending_field=field):
                envelope = json.loads(m.dump_state(pending))
                candidate = envelope["payload"]["pending"][0]["candidate"]
                if field == "micro_confirmation":
                    candidate[field]["published_us"] += 1000
                else:
                    candidate[field]["first"]["published_us"] += 1000
                envelope["sha256"] = hashlib.sha256(m._json(envelope["payload"])).hexdigest()
                with self.assertRaises(ValueError):
                    m.restore_state(json.dumps(envelope))


if __name__ == "__main__":
    unittest.main(verbosity=2)
