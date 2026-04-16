"""Canonical learning-cycle spec: uniqueness, graph sync, no drift in run_learning_cycle."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.services.trading.learning_cycle_architecture import (
    SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID,
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
    TRADING_BRAIN_ROOT_METADATA,
    apply_learning_cycle_step_status,
    apply_learning_cycle_step_status_progress,
    get_cycle_step,
)
from app.trading_brain.stage_catalog import STAGE_KEYS, TOTAL_STAGES


def _assert_io_tuple(t: tuple[str, ...]) -> None:
    assert isinstance(t, tuple)
    for x in t:
        assert isinstance(x, str)


def test_cycle_cluster_and_step_ids_unique() -> None:
    seen: set[tuple[str, str]] = set()
    assert TRADING_BRAIN_ROOT_METADATA.description.strip()
    assert TRADING_BRAIN_ROOT_METADATA.remarks.strip()
    _assert_io_tuple(TRADING_BRAIN_ROOT_METADATA.inputs)
    _assert_io_tuple(TRADING_BRAIN_ROOT_METADATA.outputs)
    for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS:
        assert c.description.strip()
        assert c.remarks.strip()
        _assert_io_tuple(c.inputs)
        _assert_io_tuple(c.outputs)
        for s in c.steps:
            key = (c.id, s.sid)
            assert key not in seen, f"duplicate step key: {key}"
            seen.add(key)
            assert s.description.strip()
            assert s.remarks.strip()
            _assert_io_tuple(s.inputs)
            _assert_io_tuple(s.outputs)
        assert get_cycle_step(c.id, c.steps[0].sid).label


def test_architecture_node_count_consistent() -> None:
    """Cluster + step count should be consistent across the architecture definition."""
    clusters = TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
    n_steps = sum(len(c.steps) for c in clusters)
    assert len(clusters) == 13
    assert n_steps == 32


def test_snapshot_learning_for_brain_worker_status_file_has_stable_keys() -> None:
    from app.services.trading.learning import (
        _BRAIN_WORKER_STATUS_LEARNING_KEYS,
        snapshot_learning_for_brain_worker_status_file,
    )

    snap = snapshot_learning_for_brain_worker_status_file()
    assert set(snap.keys()) == set(_BRAIN_WORKER_STATUS_LEARNING_KEYS)


def test_apply_learning_cycle_step_status_sets_graph_node_fields() -> None:
    st: dict = {}
    apply_learning_cycle_step_status(st, "c_discovery", "mine")
    assert st["graph_node_id"] == "nm_lc_mine"
    assert st["current_cluster_id"] == "c_discovery"
    assert st["current_step_sid"] == "mine"
    ci = next(i for i, c in enumerate(TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS) if c.id == "c_discovery")
    assert st["current_cluster_index"] == ci
    mine_i = next(
        i for i, s in enumerate(TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS[ci].steps) if s.sid == "mine"
    )
    assert st["current_step_index"] == mine_i

    st2: dict = {}
    apply_learning_cycle_step_status_progress(st2, "c_state", "snapshots_daily", 3, 100)
    assert st2["graph_node_id"] == "nm_lc_snapshots_daily"
    assert st2["current_cluster_id"] == "c_state"
    assert st2["current_step_sid"] == "snapshots_daily"
    assert st2["current_step"] == "Taking daily market snapshots (3/100)"


def test_apply_learning_cycle_step_status_preceded_by_graph_node_comment() -> None:
    """Each apply_learning_cycle_step_status in learning.py must be preceded by # graph-node: cid/sid."""
    path = Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "learning.py"
    lines = path.read_text(encoding="utf-8").splitlines()
    apply_re = re.compile(
        r"apply_learning_cycle_step_status\s*\(\s*_learning_status\s*,\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*\)"
    )
    graph_re = re.compile(r"^\s*#\s*graph-node:\s*([\w_]+)/([\w_]+)")
    for i, line in enumerate(lines):
        m = apply_re.search(line)
        if not m:
            continue
        prev = lines[i - 1] if i > 0 else ""
        gm = graph_re.match(prev)
        assert gm is not None, f"line {i + 1}: expected # graph-node: cluster/step above apply call"
        assert gm.group(1) == m.group(1) and gm.group(2) == m.group(2), (
            f"line {i + 1}: graph-node {gm.group(1)}/{gm.group(2)} does not match "
            f"apply_learning_cycle_step_status({m.group(1)}, {m.group(2)})"
        )


def test_scheduler_only_cluster_excluded_from_stage_keys() -> None:
    assert SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID == "c_universe"
    for sid in ("batch_prescreen_scan", "brain_market_snapshots"):
        assert sid not in STAGE_KEYS
    first = TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS[0]
    assert first.id == SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID
    for sid in (s.sid for s in first.steps):
        assert sid not in STAGE_KEYS


def test_stage_keys_catalog_invariants() -> None:
    assert len(STAGE_KEYS) == len(set(STAGE_KEYS)), "progress stage sids must be unique"
    assert TOTAL_STAGES == len(STAGE_KEYS)
    non_sched = [
        step.sid
        for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
        if c.id != SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID
        for step in c.steps
    ]
    assert list(STAGE_KEYS) == non_sched


def _literal_apply_learning_pair(call: ast.Call) -> tuple[str, str] | None:
    if not isinstance(call.func, ast.Name) or call.func.id != "apply_learning_cycle_step_status":
        return None
    if len(call.args) < 3:
        return None
    st = call.args[0]
    if not isinstance(st, ast.Name) or st.id not in ("_learning_status", "learning_status"):
        return None
    c_arg, s_arg = call.args[1], call.args[2]
    if not isinstance(c_arg, ast.Constant) or not isinstance(c_arg.value, str):
        return None
    if not isinstance(s_arg, ast.Constant) or not isinstance(s_arg.value, str):
        return None
    return (c_arg.value, s_arg.value)


def _block_has_call_named(stmts: list[ast.stmt], name: str) -> bool:
    for stmt in stmts:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            fn = stmt.value.func
            if isinstance(fn, ast.Name) and fn.id == name:
                return True
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            fn = stmt.value.func
            if isinstance(fn, ast.Name) and fn.id == name:
                return True
        if isinstance(stmt, ast.Try):
            if _block_has_call_named(stmt.body, name):
                return True
            for h in stmt.handlers:
                if _block_has_call_named(h.body, name):
                    return True
            if _block_has_call_named(stmt.orelse, name):
                return True
            if _block_has_call_named(stmt.finalbody, name):
                return True
        elif isinstance(stmt, ast.If):
            if _block_has_call_named(stmt.body, name) or _block_has_call_named(stmt.orelse, name):
                return True
        elif isinstance(stmt, ast.With):
            if _block_has_call_named(stmt.body, name):
                return True
        elif isinstance(stmt, (ast.For, ast.While)):
            if _block_has_call_named(stmt.body, name) or _block_has_call_named(stmt.orelse, name):
                return True
    return False


def _find_main_cycle_try(fn: ast.FunctionDef) -> ast.Try:
    for stmt in fn.body:
        if isinstance(stmt, ast.Try) and _block_has_call_named(stmt.body, "apply_learning_cycle_step_status"):
            return stmt
    raise AssertionError("run_learning_cycle: no try block containing apply_learning_cycle_step_status")


def _collect_literal_applies_from_cycle_stmts(
    stmts: list[ast.stmt],
    *,
    secondary_pairs: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for stmt in stmts:
        if isinstance(stmt, ast.FunctionDef):
            continue
        if isinstance(stmt, ast.Try):
            out.extend(_collect_literal_applies_from_cycle_stmts(stmt.body, secondary_pairs=secondary_pairs))
            for h in stmt.handlers:
                out.extend(_collect_literal_applies_from_cycle_stmts(h.body, secondary_pairs=secondary_pairs))
            out.extend(_collect_literal_applies_from_cycle_stmts(stmt.orelse, secondary_pairs=secondary_pairs))
            out.extend(_collect_literal_applies_from_cycle_stmts(stmt.finalbody, secondary_pairs=secondary_pairs))
            continue
        if isinstance(stmt, ast.If):
            out.extend(_collect_literal_applies_from_cycle_stmts(stmt.body, secondary_pairs=secondary_pairs))
            out.extend(_collect_literal_applies_from_cycle_stmts(stmt.orelse, secondary_pairs=secondary_pairs))
            continue
        if isinstance(stmt, ast.With):
            out.extend(_collect_literal_applies_from_cycle_stmts(stmt.body, secondary_pairs=secondary_pairs))
            continue
        if isinstance(stmt, ast.For):
            out.extend(_collect_literal_applies_from_cycle_stmts(stmt.body, secondary_pairs=secondary_pairs))
            out.extend(_collect_literal_applies_from_cycle_stmts(stmt.orelse, secondary_pairs=secondary_pairs))
            continue
        if isinstance(stmt, ast.While):
            out.extend(_collect_literal_applies_from_cycle_stmts(stmt.body, secondary_pairs=secondary_pairs))
            out.extend(_collect_literal_applies_from_cycle_stmts(stmt.orelse, secondary_pairs=secondary_pairs))
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            fn = call.func
            if isinstance(fn, ast.Name) and fn.id == "run_secondary_miners_phase":
                out.extend(secondary_pairs)
                continue
            pair = _literal_apply_learning_pair(call)
            if pair:
                out.append(pair)
    return out


def _extract_secondary_literal_applies() -> list[tuple[str, str]]:
    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "trading"
        / "learning_cycle_steps"
        / "secondary_bundle.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_secondary_miners_phase"
    )
    return _collect_literal_applies_from_cycle_stmts(fn.body, secondary_pairs=[])


def _extract_run_learning_cycle_literal_applies() -> list[tuple[str, str]]:
    path = Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "learning.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_learning_cycle")
    main_try = _find_main_cycle_try(fn)
    secondary = _extract_secondary_literal_applies()
    return _collect_literal_applies_from_cycle_stmts(main_try.body, secondary_pairs=secondary)


def test_run_learning_cycle_apply_order_matches_stage_keys() -> None:
    """Literal ``apply_learning_cycle_step_status`` (+ secondary bundle) calls must be
    a subsequence of STAGE_KEYS (some steps are scheduler-only or called from sub-services)."""
    pairs = _extract_run_learning_cycle_literal_applies()
    runtime_sids = [sid for _c, sid in pairs]
    stage_list = list(STAGE_KEYS)
    for sid in runtime_sids:
        assert sid in stage_list, f"runtime sid {sid!r} not in STAGE_KEYS"
    idx = -1
    for sid in runtime_sids:
        pos = stage_list.index(sid)
        assert pos > idx, (
            f"runtime ordering mismatch: {sid!r} at STAGE_KEYS[{pos}] but previous was [{idx}]"
        )
        idx = pos


def test_decisioning_architecture_lists_pattern_engine_before_proposals() -> None:
    dec = next(c for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS if c.id == "c_decisioning")
    sids = [s.sid for s in dec.steps]
    assert sids.index("pattern_engine") < sids.index("proposals")


def test_run_learning_cycle_split_meta_apply_order_matches_architecture() -> None:
    """Runtime apply_learning_cycle_step_status calls for the split c_meta clusters
    match their canonical definitions (c_meta_learning, c_decisioning, c_control)."""
    path = Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "learning.py"
    text = path.read_text(encoding="utf-8")
    for cluster_id in ("c_meta_learning", "c_decisioning", "c_control"):
        cdef = next(c for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS if c.id == cluster_id)
        expected = [s.sid for s in cdef.steps]
        pat = re.compile(
            rf'apply_learning_cycle_step_status\(_learning_status,\s*"{re.escape(cluster_id)}",\s*"(\w+)"\)'
        )
        found = pat.findall(text)
        assert found == expected, f"cluster={cluster_id} runtime={found!r} architecture={expected!r}"


def test_run_learning_cycle_no_literal_current_step_assignments() -> None:
    """Forbid `_learning_status["current_step"] = "..."` in learning.py (use architecture helpers)."""
    path = Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "learning.py"
    text = path.read_text(encoding="utf-8")
    bad = re.findall(
        r'_learning_status\s*\[\s*["\']current_step["\']\s*\]\s*=\s*("[^"]*"|\'[^\']*\')',
        text,
    )
    allowed_empty = {'""', "''"}
    suspicious = [b for b in bad if b not in allowed_empty]
    assert not suspicious, (
        "Use apply_learning_cycle_step_status / _progress from learning_cycle_architecture; "
        f"found literal assignments: {suspicious}"
    )
