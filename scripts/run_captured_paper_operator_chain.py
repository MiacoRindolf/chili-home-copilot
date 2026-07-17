"""Operator convenience driver: fresh bundle + host snapshot + plan -> operator flow.

Bawat attempt ay bagong activation generation (append-only ang generations).
Kailangan: TEST_DATABASE_URL na *_test; production postgres sa 5433; IQFeed up
AT umaagos ang ticks (premarket / RTH / after-hours - lahat OK, hindi
market-hours-gated: ang certification symbol ay LIVE-DERIVED mula sa kung ano
ang aktwal na nagpi-print ngayon). Inner error codes ay tine-trace para hindi
mawala sa sanitized na operator JSON.
"""
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, r"D:\dev\chili-home-copilot-codex-broker")

REPO = Path(r"D:\dev\chili-home-copilot-codex-broker")
CP = Path(r"D:\CHILI-Docker\captured-paper")
_BENCH_ROOT = Path(r"D:\CHILI-Docker\chili-data\benchmarks")
BENCH_PATH = max(
    _BENCH_ROOT.glob("chili-replay-capture-benchmark-*/reports/*.json"),
    key=lambda p: p.stat().st_mtime,
)
print("BENCHMARK:", BENCH_PATH, flush=True)
ACCOUNT_ID = "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
GEN = str(uuid.uuid4())
print("GENERATION:", GEN, flush=True)

# The exact-print smoke certifies capture on ONE symbol and needs it to print
# within a bounded window. A hardcoded mega-cap (e.g. AAPL) is dead outside
# regular hours, so certify against whatever is actually flowing on the live
# feed right now - this makes the smoke session-agnostic (premarket / RTH /
# after-hours), which is the whole point of a capture-pipeline gate.
import re as _re
from sqlalchemy import create_engine as _ce, text as _t

_eng = _ce("postgresql://chili:chili@localhost:5433/chili")
with _eng.connect() as _c:
    _cands = _c.execute(
        _t(
            "SELECT symbol, count(*) n FROM iqfeed_trade_ticks "
            "WHERE observed_at > now() - interval '90 seconds' "
            "GROUP BY symbol ORDER BY n DESC LIMIT 40"
        )
    ).fetchall()
def _live_delay_is_zero(symbol: str, timeout_s: float = 4.0) -> bool:
    """Ask IQConnect directly whether this symbol's L1 feed is real-time.

    2026-07-17: the tape has NO delay/entitlement column, and a failed smoke
    writes its own DELAYED prints back to the tape, so a delayed symbol (SPY
    Delay=15 on this NASDAQ-realtime DTN entitlement) self-poisons the
    top-by-count pick.  A 15-minute-delayed quote can never satisfy the 2s
    authoritative freshness bound, so only Delay=0 symbols may certify
    capture.  NOTE: never include Symbol in SELECT UPDATE FIELDS
    (auto-prepended; including it silently voids the select).
    """
    import socket as _sk
    import time as _tm

    try:
        sock = _sk.create_connection(("127.0.0.1", 5009), timeout=timeout_s)
    except OSError:
        return False
    try:
        sock.settimeout(timeout_s)
        sock.sendall(b"S,SET PROTOCOL,6.2\r\n")
        sock.sendall(b"S,SELECT UPDATE FIELDS,Most Recent Trade,Delay\r\n")
        sock.sendall(f"w{symbol}\r\n".encode("ascii"))
        deadline = _tm.monotonic() + timeout_s
        buffer = b""
        while _tm.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            for line in buffer.decode("ascii", errors="replace").splitlines():
                parts = line.split(",")
                if len(parts) >= 4 and parts[0] in ("P", "Q") and parts[1] == symbol:
                    return (parts[3] or "").strip() in ("0", "")
        return False
    finally:
        try:
            sock.sendall(f"r{symbol}\r\n".encode("ascii"))
        except OSError:
            pass
        sock.close()


CERT_SYMBOL = None
for _s, _n in _cands:
    _sym = str(_s)
    if not _re.fullmatch(r"[A-Z]{1,6}", _sym):
        continue
    if _live_delay_is_zero(_sym):
        CERT_SYMBOL = _sym
        break
    print(f"cert-candidate {_sym}: DELAYED/unprobeable - skipped", flush=True)
if CERT_SYMBOL is None:
    print("FATAL: no real-time (Delay=0) cert symbol on the live tape", flush=True)
    raise SystemExit(3)
print("CERT_SYMBOL (live-derived, Delay=0 verified):", CERT_SYMBOL, flush=True)

# ---- error tracing (surface inner cutover codes) ----
from scripts import captured_paper_host_cutover as hc

_orig_hc = hc.CapturedPaperHostCutoverError.__init__


def _dbg_hc(self, code, message, *a, **k):
    print(f"[CUTOVER-ERR] {code}: {message}", file=sys.stderr, flush=True)
    return _orig_hc(self, code, message, *a, **k)


hc.CapturedPaperHostCutoverError.__init__ = _dbg_hc

import traceback as _tb
from scripts import run_captured_paper_preactivation_probes as probes

_orig_probe = probes.CapturedPaperPreactivationProbeError.__init__


def _dbg_probe(self, code, message, *a, **k):
    ctx = sys.exc_info()[1]
    print(f"[PROBE-ERR] {code}: {message} | inner: {type(ctx).__name__ if ctx else None}: {str(ctx)[:400]}", file=sys.stderr, flush=True)
    if ctx is not None:
        _tb.print_exception(type(ctx), ctx, ctx.__traceback__, limit=6, file=sys.stderr)
    return _orig_probe(self, code, message, *a, **k)


probes.CapturedPaperPreactivationProbeError.__init__ = _dbg_probe

# ---- 1) bootstrap bundle under the new generation ----
from scripts import build_iqfeed_capture_bootstrap_bundle as builder

bench_raw = BENCH_PATH.read_bytes()
bench_sha = hashlib.sha256(bench_raw).hexdigest()
source_hashes = {
    role: hashlib.sha256((REPO / relative).read_bytes()).hexdigest()
    for role, relative in builder._SOURCE_RELATIVE_PATHS.items()
}
from dotenv import dotenv_values
import urllib.request

vals = dotenv_values(r"D:\dev\chili-home-copilot\.env")
req = urllib.request.Request(
    "https://paper-api.alpaca.markets/v2/account",
    headers={
        "APCA-API-KEY-ID": (vals.get("CHILI_ALPACA_API_KEY") or "").strip(),
        "APCA-API-SECRET-KEY": (vals.get("CHILI_ALPACA_API_SECRET") or "").strip(),
    },
)
received_at = datetime.now(UTC)
with urllib.request.urlopen(req, timeout=15) as resp:
    acct = json.loads(resp.read())
available_at = datetime.now(UTC)
assert acct.get("id") == ACCOUNT_ID and acct.get("status") == "ACTIVE"

now = datetime.now(UTC)
request_doc = {
    "schema_version": builder.BUILD_REQUEST_SCHEMA_VERSION,
    "repo_root": str(REPO),
    "artifact_root": str(CP / "bootstrap" / "artifacts"),
    "capture_store_root": str(CP / "capture-store"),
    "resource_benchmark": {"path": str(BENCH_PATH), "sha256": bench_sha},
    "source_sha256": source_hashes,
    "expected_account_id": ACCOUNT_ID,
    "account_risk_snapshot": {
        "equity": str(acct.get("equity")),
        "buying_power": str(acct.get("buying_power")),
    },
    "account_query": {
        "endpoint": "/v2/account",
        "environment": "paper",
        "account_id": ACCOUNT_ID,
    },
    "account_received_at": builder._iso(received_at),
    "account_available_at": builder._iso(available_at),
    "effective_config": {"capture_profile": "diagnostic_only_bootstrap"},
    "bridge_configuration": {
        "iqfeed_l1": {
            "schema_version": "chili.iqfeed-l1-bridge-capture-config.v3",
            "protocol_version": "6.2",
            "port": 5009,
        },
        "iqfeed_l2": {
            "schema_version": "chili.iqfeed-depth-bridge.capture-config.v1",
            "protocol_version": "6.2",
            "port": 9200,
        },
    },
    "activation_generation": GEN,
    "generated_at": builder._iso(now),
    "generation": 1,
}
raw = builder._canonical_json_bytes(request_doc)
request_sha = hashlib.sha256(raw).hexdigest()
request_path = CP / "bootstrap" / "inputs" / f"{request_sha}.request.json"
request_path.write_bytes(raw)
built = builder.build_iqfeed_capture_bootstrap_bundle_from_request(
    request_path=request_path,
    request_sha256=request_sha,
    allowed_read_roots=(REPO, CP, BENCH_PATH.parents[1]),
    allowed_write_roots=(CP,),
)
print("BUNDLE OK:", built.manifest_sha256, flush=True)

# ---- 2) FRESH host snapshot (age-gated by the baseline validator) + plan ----
from scripts import collect_captured_paper_host_snapshot as collector

snap_root = CP / f"host_snapshot_{GEN[:8]}"
snap_root.mkdir(parents=True, exist_ok=True)
collection = collector.collect_host_snapshot(
    probe=collector.WindowsReadOnlyHostProbe(),
    legacy_root=Path(r"D:\dev\chili-home-copilot"),
    captured_at=datetime.now(UTC),
)
persisted = collector.persist_host_snapshot(collection, output_root=snap_root)
print("SNAPSHOT:", collection.verdict, collection.reason_code, flush=True)
_paths = dict(persisted.artifact_paths)
task = Path(_paths["task_snapshot"])
proc = Path(_paths["process_snapshot"])
rest = Path(_paths["restore_plan"])


def sha_of(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()
plan = {
    "schema_version": "chili.captured-paper-operator-plan.v1",
    "activation_generation": GEN,
    "expected_account_id": ACCOUNT_ID,
    "candidate_root": str(REPO),
    "operator_output_root": str(CP / "operator"),
    "preactivation_output_root": str(CP / "preactivation"),
    "activation_artifact_root": str(CP / "activation"),
    "capture_store_root": str(CP / "capture-store"),
    "runtime_env_path": str(CP / "runtime" / "captured-paper-v3.env"),
    "runtime_env_sha256": "688185dbd7b8999bb42852ba425a3c84fc55717bce00c38213cf1d03108a0ca8",
    "iqfeed_bootstrap_manifest_path": str(built.manifest_path),
    "iqfeed_bootstrap_manifest_sha256": built.manifest_sha256,
    "python_executable": r"C:\Users\rindo\miniconda3\envs\chili-env\python.exe",
    "python_dependency_root": r"C:\Users\rindo\miniconda3\envs\chili-env\Lib\site-packages",
    "no_order_receipt_output": str(CP / "receipts" / f"no-order-receipt-{GEN}.json"),
    "powershell_executable": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    "host_principal_user_id": "rindo",
    "task_snapshot_path": str(task),
    "task_snapshot_sha256": sha_of(task),
    "process_snapshot_path": str(proc),
    "process_snapshot_sha256": sha_of(proc),
    "restore_plan_path": str(rest),
    "restore_plan_sha256": sha_of(rest),
    "capture_certification_symbol": CERT_SYMBOL,
    "allowed_read_roots": [
        str(REPO),
        str(CP),
        r"D:\CHILI-Docker\chili-data\benchmarks",
        r"C:\Users\rindo\miniconda3\envs\chili-env",
        r"D:\dev\chili-home-copilot",
        r"C:\Windows\System32",
    ],
}
plan_raw = json.dumps(
    plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
).encode()
plan_sha = hashlib.sha256(plan_raw).hexdigest()
plan_path = CP / "operator" / f"{plan_sha}.plan.json"
plan_path.write_bytes(plan_raw)
print("PLAN:", plan_sha, flush=True)

# ---- 3) operator flow ----
from scripts import captured_paper_operator_flow as flow

rc = flow.main(["--plan", str(plan_path), "--plan-sha256", plan_sha])
print("FLOW RC:", rc, flush=True)
