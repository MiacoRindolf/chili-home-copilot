"""TIME-SHARE SUPERVISOR v3 — lease-holding supervisor na may Job Object.

Ayon sa v3 spec ni Codex (2026-08-14). Isang proseso ang gumagawa ng LAHAT:

  ACCEPT (habang HAWAK ang lease, hindi binibitawan):
    1. acquire ALPA/OWNR advisory lock (pg_try_advisory_lock; busy = exit 3).
    2. patunayan sa pg_locks na ANG SARILING backend ang may hawak.
    3. validahin ang counterparty artifacts nang TAHASAN: --rollback-artifact
       PATH na dapat umiiral at naglalaman ng terminal marker; opsyonal na
       --prepared-path + --prepared-sha256 na nire-rehash, sine-check ang
       schema + epoch. Walang glob, walang filename na sapat.
    4. buong census (6 counters + broker na naka-pin sa expected account +
       fail-closed producer probe kasama services/tasks/WSL/containers).
    5. isulat ang ACCEPTED receipt (O_EXCL + fsync + rehash; TUNAY na
       holder_pid/backend_pid; kasama ang env values at .env sha binding).
  RUN (lease pa rin ang hawak):
    6. gumawa ng Windows JOB OBJECT na may KILL_ON_JOB_CLOSE, i-spawn ang app
       (uvicorn) sa loob nito. Mamatay ang supervisor = mamatay ang app.
    7. monitor loop: sariling DB backend buhay + lock hawak pa (pg_locks) +
       app buhay. Lease/DB loss => TerminateJobObject agad (fail closed).
  PREPARE (lease pa rin ang hawak, PAGKATAPOS mamatay ang app):
    8. TerminateJobObject sa end time; buong census ulit; PREPARED receipt.
    9. saka lang bibitawan ang lock (koneksyon sarado) at lalabas.

Ang kabilang supervisor (sealed side) ang kukuha ng parehong lock, magva-
validate ng PREPARED (explicit path + full sha + schema + epoch), gagawa ng
ACCEPTED, at hahawak ng lock habang order-capable — walang standalone accept.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import psycopg2
import win32api
import win32con
import win32job

DB_URL = "postgresql://chili:chili@localhost:5433/chili"
LEASE_CLASSID = 0x414C5041  # 'ALPA'
LEASE_OBJID = 0x4F574E52    # 'OWNR'
RECEIPT_DIR = r"D:\dev\chili-home-copilot\project_ws\AgentOps\timeshare\receipts"
SCHEMA_PREPARED = "chili.timeshare-handoff-prepared.v3"
SCHEMA_ACCEPTED = "chili.timeshare-handoff-accepted.v3"
COMMIT_PIN = "18f1b90"
ENV_PATH = r"D:\dev\chili-home-copilot\.env"
LANE_ENV_PATH = r"E:\dev\wt-window2\.env"
POWERSHELL = r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe"
SCHTASKS = r"C:\WINDOWS\System32\schtasks.exe"
WSL = r"C:\WINDOWS\System32\wsl.exe"
DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"

BRIDGE_SOURCE = r"E:\dev\wt-window2\scripts\iqfeed_trade_bridge.py"
_BRIDGE_BUILD_FALLBACK = "iqfeed-l1-exact-print-provenance-v3+sha256:7e89282e868a40e2"


def _bridge_build_from_disk(path: str = BRIDGE_SOURCE) -> str:
    """Ang EKSAKTONG build id na isusulat ng bridge sa bawat tape row.

    2026-09-03 (RTH, 92 arm / 0 fill): ang pin sa ibaba ay HARDCODED na
    ``…7e89282e868a40e2`` mula pa Aug 26, samantalang ang bridge ay nagsulat
    ng ``…604de228f5c63fdd`` (Sep 1–2) at ``…2aa8d76326865c4c`` (Sep 2 13:00Z
    pataas). Sa ``alpaca_spot.py:938`` ang ``bridge_version != expected_build``
    ay nagbabalik ng ``None`` sa BAWAT row, kaya ang IQFeed-L1 authority leg ng
    ``get_best_bid_ask`` ay PATAY mula nang ma-edit ang bridge — at walang log
    line na nagsabi. Ang build id ay ``sha256(source)[:16]`` (tingnan ang
    ``_bridge_build_id`` sa bridge mismo), kaya ang tamang pin ay ang hash ng
    PAREHONG file na tatakbo, kinukuwenta sa oras ng launch — hindi tinitipa.
    """
    try:
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
        return f"iqfeed-l1-exact-print-provenance-v3+sha256:{digest}"
    except OSError as exc:
        print(f"[supervisor] BABALA: hindi mabasa ang bridge source ({exc}); "
              f"gagamitin ang lumang pin {_BRIDGE_BUILD_FALLBACK}")
        return _BRIDGE_BUILD_FALLBACK


WINDOW_ENV = {
    "CHILI_PORT": "8010",
    "CHILI_TLS": "0",
    "CHILI_SCHEDULER_ROLE": "momentum_exec_only",
    "CHILI_MOMENTUM_LEGACY_ALPACA_DISPATCH_ENABLED": "true",
    "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": "true",
    # v3.6 (08-17, H1 root-cause): sa LOOP mode, ang queued_live ay WALANG dispatch
    # path (_on_tick ay watching_live/pending/position lang; ang certified IQFeed
    # notify listener ay patay dahil blangko ang bridge-build pin) — kaya 0 admitted
    # kailanman. Lipat sa napatunayang BATCH scheduler driver: kinukuha nito ang
    # LAHAT ng runnable states (kasama queued_live) bawat interval. Loop OFF.
    "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": "false",
    "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED": "true",
    "CHILI_MOMENTUM_EXEC_LIVE_RUNNER_SCHEDULER_ENABLED": "true",
    "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_INTERVAL_SECONDS": "10",
    # v3.11 (08-18 ~08:00 PT, zero-trade autopsy): TILT OFF PANSAMANTALA — ang
    # Ortex batch-manifest admission ay naging TERMINAL gate sa placement:
    # 116/134 live_entry_pending_place ang namatay sa
    # ortex_batch_manifest_reference_mismatch/invalid (order_posted=false
    # LAHAT) dahil exact-match ang reference sa KASALUKUYANG hub manifest
    # (kasama decision_at/batch_sha256) na nagra-rotate bawat rebuild —
    # deterministic ang mismatch para sa anumang in-flight na desisyon.
    # Kapag false ang flag, ang admission check sa live_runner.py (~L24376)
    # ay nagre-return ng None agad = walang Ortex gate, walang credit spend.
    # v3.13 (08-18 ~10:15 PT): TILT ON ULIT — merged ang #1054 (tilt-hindi-
    # veto): sa ordinaryong session, ang operational na admission failure
    # (manifest rotation mismatch atbp.) ay nagpapatuloy sa NEUTRAL sizing sa
    # halip na ma-defer; sealed replay at captured-paper ay fail-closed pa
    # rin. Build sa E: ay e22257b na kasama ito. (Kasaysayan: v3.7 off nang
    # walang key; v3.8 on; v3.9 off sa 403; v3.10 on matapos ang top-up.)
    "CHILI_MOMENTUM_SQUEEZE_FUEL_TILT_ENABLED": "true",
    # v3.11 din: ang blangkong pin ay HARD-DISABLE sa IQFeed-L1 quote leg ng
    # alpaca adapter (kapareho ng blank-pin bug ng notify listener sa v3.6) —
    # kaya ang entry BBO ay napunta sa Alpaca-IEX cache (frozen 4.575 mid ng
    # PFSA buong umaga 08-18). Ang halagang ito ay ang BRIDGE_BUILD na
    # naka-stamp sa mga buhay na momentum_nbbo_spread_tape row (source=
    # iqfeed_l1, message_type=Q, basis=iqfeed_q_receive_trade_reference_fenced)
    # ng E:\dev\wt-window2 bridge. Kapag nag-iba ang bridge build, i-update
    # ito mula sa: SELECT DISTINCT bridge_version FROM momentum_nbbo_spread_tape
    # WHERE source='iqfeed_l1' ORDER BY 1 DESC LIMIT 1 (pinakabago).
    # 2026-09-03: KINUKUWENTA mula sa bridge source sa oras ng launch (tingnan
    # ang _bridge_build_from_disk sa itaas). Ang tinipang sha ay nabubulok sa
    # bawat edit ng bridge at PATAY ang IQFeed leg nang walang log line.
    "CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD": _bridge_build_from_disk(),
    # v3.12 (08-18 ~16:00Z): ang HULING gate ng funnel — ang submit-boundary
    # execution BBO ay humihingi ng DIREKTANG Alpaca-IEX quote na may provider
    # timestamp ≤2.0s (default). Ang manipis na small-caps ay bihirang mag-quote
    # sa IEX sa loob ng 2s → execution_bbo_unavailable ang pumatay kay IPST
    # nang paulit-ulit (at ×11 kaninang umaga). 30.0 = ang ceiling na tinatanggap
    # ng config validation (le=30.0); ang entry-quote gate ay nagpapatunay pa rin
    # ng sariwa at makitid na book bago pa ito, at marketable LIMIT ang order
    # kaya bounded ang presyo. PAPER lane lamang ito (window env override).
    "CHILI_MOMENTUM_ENTRY_BBO_MAX_AGE_SECONDS": "30",
    # v3.14 (08-19, Ortex burn audit): ang bawat successful call ay ~2 credits
    # pala (dynamic, kita sa response creditsUsed) — ang 242 calls noong 08-18
    # ay ~484 credits (1000→409 sa 2 araw). Ang top-12 ay nagbabayad para sa
    # ~7 simbolong halos hindi tina-trade (ang entries ay galing sa tuktok ng
    # board). Clamp sa 5 habang wala pang realized na ebidensya ng tilt value;
    # susukatin sa unang 10-20 fills (may squeeze evidence sa bawat entry log).
    # Kasabay nito ang DB-backed cache read (PR) para hindi na nagre-refetch
    # sa bawat window relaunch.
    "CHILI_MOMENTUM_SQUEEZE_FUEL_TOP_N": "5",
    # v3.15 (08-19, staged para sa SUSUNOD na launch): ang short-interest data
    # ay ~araw-araw lang nag-a-update — 72h TTL = 1 fetch kada simbolo kada 3
    # araw imbes na araw-araw. TANDAAN: kasama ang TTL sa quota policy sha,
    # kaya ang unang launch na may ganito ay magre-refetch ng board nang
    # minsan (~20-40 credits one-time), tapos 3-araw na ang dedupe.
    "CHILI_ORTEX_SUCCESS_CACHE_TTL_SECONDS": "259200",
    # ── v3.16 (08-19 gabi): ANG PINAKAMAHALAGANG PAGBABAGO NG ARAW ────────────
    # Ang buong huling milya ay nagpepresyo mula sa Alpaca-IEX, at ang IEX ay
    # MULTO para sa maliliit na pangalan. Sinukat sa parehong sandali:
    #   ZSTK 16:24  IEX 9.15/12.28 = 3,013 bps, 28s luma
    #               SIP 10.60/10.62 =    19 bps, 0.12s
    #   YJ   10:39  IEX 0 two-sided quote sa 60s | SIP 925 quotes 2.37/2.38
    #   ZNB  08:51  IEX 0 two-sided quote sa 60s | SIP 1,241 quotes 4.50/4.52
    # Bunga noong 08-19: 25 execution_bbo_unavailable, 14 above_planned_limit
    # (6 sa kanila ay MARKETABLE na sa tunay na NBBO ask — tinanggihan sa
    # presyong hindi totoo), 5 spread_risk_veto na 4 ay phantom — at ZERO
    # live_entry_submitted sa 53 pending_place. Bayad na ang SIP at naka-
    # whitelist na sa build_captured_paper_runtime_env.py; walang code change.
    # ⚠️ ASAHAN: TATAAS ang above_planned_limit at magiging load-bearing ang
    # spread_risk_veto sa mga pangalang hindi pa niya nagagate — pag-aalis iyon
    # ng takip, HINDI regression. HUWAG taasan ang
    # chili_momentum_entry_max_spread_fraction_of_risk bilang tugon.
    # ⛔ v3.16 BINAWI (08-19 gabi, bago pa ma-deploy). Ang SIP flip ay ISASAMA
    # SANA batay sa autopsy na nagsabing "SIP is already paid for, no entitlement
    # error". PINABULAANAN ito ng lokal na pagsubok gamit ang MISMONG susi ng lane:
    #   IEX  AZI bid=1.46 ask=1.49 spread_bps=201  ts=20:55:56Z   -> OK
    #   SIP  APIError: "subscription does not permit querying recent SIP data"
    # Ang plano natin ay nagpapahintulot lang ng SIP na >15 min ang tanda, HINDI
    # ang live quotes na kailangan ng entry path. Kung ito ay na-deploy, ang BAWAT
    # quote request ay magiging error — mas masahol pa kaysa sa multong IEX book.
    # ⇒ Ang lunas sa phantom IEX ay DESISYON NG OPERATOR: (a) i-upgrade ang Alpaca
    #   market-data plan, o (b) gamitin ang sarili nating IQFeed NBBO pati sa
    #   execution check (mag-iingat: pagpepresyo laban sa librong hindi natin
    #   matratrade). HUWAG ibalik ang "sip" hangga't hindi na-upgrade ang plano.
    # "CHILI_ALPACA_DATA_FEED": "sip",
    "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED": "true",
    "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED": "true",
    "CHILI_AUTOPILOT_PRICE_BUS_ENABLED": "true",
    # v3.3 (08-14, unang window autopsy): kung wala nito, ang equity arms ay
    # nakaturo sa CHILI_EQUITY_EXECUTION_RAIL=robinhood_agentic_mcp mula .env —
    # ang LIVE rail, hindi ang paper account, kaya zero arms buong umaga.
    # Ang flag na ito ang explicit paper-only topology (fail-closed papunta sa
    # alpaca_spot; hinding-hindi babagsak pabalik sa RH live). EQUITY_ONLY
    # naman ang nagtatanggal ng anumang Coinbase live-cash surface sa window.
    "CHILI_MOMENTUM_EQUITY_EXECUTION_VIA_ALPACA_PAPER": "true",
    "CHILI_MOMENTUM_AUTO_ARM_EQUITY_ONLY": "true",
    "CHILI_MOMENTUM_AUTO_ARM_CRYPTO_ONLY": "false",
    "PYTHONUNBUFFERED": "1",
}

SIX_COUNTERS_SQL = """
SELECT
    (SELECT count(*) FROM trading_automation_sessions
      WHERE mode = 'live' AND execution_family IN ('alpaca_spot','alpaca_short')
        AND state NOT IN ('cancelled','expired','error','archived','finished',
                          'live_finished','live_cancelled','live_error','live_arm_expired')
    ),
    (SELECT count(*) FROM broker_symbol_action_claims
      WHERE account_scope = 'alpaca:paper' AND phase <> 'resolved'),
    (SELECT count(*) FROM adaptive_risk_reservations
      WHERE account_scope = 'alpaca:paper' AND state NOT IN ('released','closed')),
    (SELECT count(*) FROM adaptive_risk_opportunity_claims
      WHERE account_scope = 'alpaca:paper' AND status = 'reserved'),
    (SELECT count(*) FROM captured_paper_post_commit_outbox
      WHERE account_scope = 'alpaca:paper' AND status <> 'completed'),
    (SELECT count(*) FROM captured_paper_completed_fill_watch
      WHERE state NOT IN ('terminal_zero_fill','fill_handoff_committed'))
"""
COUNTER_NAMES = [
    "active_sessions", "active_action_claims", "active_reservations",
    "reserved_opportunities", "active_outbox_rows", "active_fill_watches",
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _env_read():
    vals = {}
    for line in open(ENV_PATH, encoding="utf-8", errors="replace"):
        m = re.match(
            r"^(CHILI_ALPACA_API_KEY|CHILI_ALPACA_API_SECRET|CHILI_ALPACA_EXPECTED_ACCOUNT_ID)\s*=\s*(.+?)\s*$",
            line,
        )
        if m and m.group(1) not in vals:
            vals[m.group(1)] = m.group(2).strip().strip('"')
    return vals


def _preshutdown_flatten(deadline_s=90):
    """v3.7 (COIW 2026-08-21): bago patayin ang app sa window_end, i-flatten ang
    anumang bukas na posisyon sa paper account — noong 08-21, pinatay ng Job
    Object ang app habang bukas ang COIW 177sh (nakansela pa ang deadman stop sa
    teardown) at alam ng PREPARED census na clean=False pero wala itong ginawa;
    kung weekend iyon sa totoong pera, nakalutang ang posisyon nang walang stop.

    Ginagawa: kanselahin ang open orders → marketable-limit SELL (extended hours,
    1.5% sa ilalim ng current) bawat long → i-poll hanggang flat o deadline. Sa
    deadline na hindi flat: TULOY ang shutdown (huwag kailanman mag-hang) pero
    isinisigaw sa receipt/stdout. Fail-open sa census errors (ang dating gawi)."""
    keys = _env_read()
    hdr = {"APCA-API-KEY-ID": keys.get("CHILI_ALPACA_API_KEY", ""),
           "APCA-API-SECRET-KEY": keys.get("CHILI_ALPACA_API_SECRET", ""),
           "Content-Type": "application/json"}
    base = "https://paper-api.alpaca.markets"
    report = {"attempted": False, "flat": None, "placed": [], "errors": []}

    def _req(path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(base + path, data=data, headers=hdr, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
        return json.loads(body) if body else None

    try:
        positions = _req("/v2/positions") or []
        if not positions:
            report["flat"] = True
            return report
        report["attempted"] = True
        try:
            _req("/v2/orders", method="DELETE")  # kanselahin lahat ng open orders
        except Exception as exc:
            report["errors"].append(f"cancel_all: {exc}")
        for pos in positions:
            try:
                qty = int(float(pos.get("qty") or 0))
                if qty <= 0:
                    continue
                cur = float(pos.get("current_price") or pos.get("avg_entry_price") or 0)
                limit = round(cur * 0.985, 2) if cur > 0 else None
                if limit is None:
                    report["errors"].append(f"{pos.get('symbol')}: walang presyo")
                    continue
                od = _req("/v2/orders", method="POST", payload={
                    "symbol": pos.get("symbol"), "qty": str(qty), "side": "sell",
                    "type": "limit", "limit_price": str(limit),
                    "time_in_force": "day", "extended_hours": True,
                    "client_order_id": f"chili_supervisor_eod_{pos.get('symbol')}_{int(time.time())}",
                })
                report["placed"].append(f"{pos.get('symbol')} x{qty} @ {limit} ({od.get('status')})")
            except Exception as exc:
                report["errors"].append(f"{pos.get('symbol')}: {exc}")
        t0 = time.time()
        while time.time() - t0 < deadline_s:
            time.sleep(5)
            try:
                if not (_req("/v2/positions") or []):
                    report["flat"] = True
                    return report
            except Exception as exc:
                report["errors"].append(f"poll: {exc}")
        report["flat"] = False
    except Exception as exc:
        report["errors"].append(f"census: {exc}")
        report["flat"] = None
    return report


def _broker_census(expected_account_id):
    out = {"endpoint": "https://paper-api.alpaca.markets", "at_utc": _now(),
           "expected_account_id": expected_account_id}
    keys = _env_read()
    ok = True
    for name, path in (("account", "/v2/account"),
                       ("orders", "/v2/orders?status=open&limit=200"),
                       ("positions", "/v2/positions")):
        req = urllib.request.Request(
            out["endpoint"] + path,
            headers={"APCA-API-KEY-ID": keys.get("CHILI_ALPACA_API_KEY", ""),
                     "APCA-API-SECRET-KEY": keys.get("CHILI_ALPACA_API_SECRET", "")},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
        out[f"{name}_sha256"] = hashlib.sha256(body).hexdigest()
        out[f"{name}_at_utc"] = _now()
        data = json.loads(body)
        if name == "account":
            out["account_id"] = str(data.get("id", ""))
            out["account_status"] = str(data.get("status", ""))
            if out["account_id"] != expected_account_id or out["account_status"] != "ACTIVE":
                ok = False
        else:
            out[f"{name}_count"] = len(data)
            if data:
                ok = False
    out["clean"] = ok
    return out


def _probe(cmd, name, failures):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        failures.append({"probe": name, "error": repr(exc)})
        return None
    if p.returncode != 0:
        failures.append({"probe": name, "rc": p.returncode, "stderr": (p.stderr or "")[:300]})
        return None
    return p.stdout or ""


# v3.2 (08-14): tinanggal ang iqfeed_trade_bridge/iqfeed_depth_bridge sa
# order-capable set — market-DATA-only ang mga bridge (IQConnect -> Postgres
# ticks, walang broker client, walang order path), at kailangan ng operator na
# TULOY ang capture habang may window. Ang census na ito ay para sa ORDER
# surface absence, hindi data-producer absence.
ORDER_CAPABLE_RE = (
    "trading_scheduler|live_runner|uvicorn|app.main|one-shot|captured_alpaca|"
    "start_scheduler|alpaca"
)
SEARCH_TOOL_RE = (
    r'(?i)(?:^|[\s"\\/])(?:rg|grep|findstr)(?:\.exe)?(?=\s|$)'
)


def _producer_census(self_pid=None, app_pid=None):
    """Fail-CLOSED census ng lahat ng order-capable surface."""
    out = {"at_utc": _now(), "probes": {}, "order_capable_total": 0, "probe_failures": []}
    exempt = {p for p in (self_pid, app_pid, os.getpid()) if p}

    so = _probe([
        POWERSHELL, "-NoProfile", "-Command",
        "$ErrorActionPreference='Stop';"
        "$rows = Get-CimInstance Win32_Process | Where-Object { $_.Name -in "
        "@('python.exe','pythonw.exe','powershell.exe','pwsh.exe','cmd.exe','wscript.exe','cscript.exe') };"
        # ⚠️ DALAWANG PASS (2026-08-26). Ang isang $null na CommandLine ay HINDI
        # ebidensya ng kalaban -- normal ito sa Windows: isang prosesong lumabas sa
        # gitna ng enumeration, o isang protektadong proseso ng ibang user. Ang
        # dating isang-pass ay nagbibilang ng ANUMANG $null bilang probe failure,
        # kaya ISANG hilera lang ang nagpapabagsak sa buong census => fail-closed
        # => walang lane.
        #
        # NASUKAT, DALAWANG MAGKASUNOD NA ARAW:
        #   08-25  premarket -- lane hindi bumukas, unreadable_cmdlines=1
        #   08-26  07:55:06Z -- receipt clean=False, order_capable_total=0,
        #                       broker_census clean, unreadable_cmdlines=1
        #          Nabigo ang lane nang 2h47m nang tahimik bago ito napansin.
        #
        # ⚠️ NANANATILING FAIL-CLOSED ANG BANTAY. Hindi nito ipinapasa ang $null.
        # Muli lang nitong binabasa ang MISMONG PID:
        #   * wala na  => tiyak na hindi kalaban (lumabas na ito)
        #   * ibang may-ari => hindi ito lane ng operator na ito
        #   * $null pa rin at buhay at atin => BINIBILANG, gaya ng dati
        "$classify = { param($nm, $cl)"
        f"  $searchShell = ($nm -in @('powershell.exe','pwsh.exe','cmd.exe')) -and "
        f"($cl -match '{SEARCH_TOOL_RE}');"
        f"  ($cl -match '{ORDER_CAPABLE_RE}') -and (-not $searchShell) -and "
        "  ($cl -notmatch 'timeshare_supervisor|Get-CimInstance') };"
        "$hits=@(); $unread=0; $nullPids=@();"
        "foreach ($p in $rows) { $cl=$p.CommandLine;"
        " if ($null -eq $cl) { $nullPids += $p.ProcessId; continue }"
        " if (& $classify $p.Name $cl)"
        "{ $hits += @{pid=$p.ProcessId; name=$p.Name; cmd=$cl.Substring(0,[Math]::Min(160,$cl.Length))} } }"
        "$me = $env:USERNAME;"
        "foreach ($np in $nullPids) {"
        " $p2 = Get-CimInstance Win32_Process -Filter \"ProcessId=$np\" -ErrorAction SilentlyContinue;"
        " if ($null -eq $p2) { continue }"
        " $own = $null; try { $own = (Invoke-CimMethod -InputObject $p2 -MethodName GetOwner).User } catch {};"
        " if ($own -and $own -ne $me) { continue }"
        " $cl2 = $p2.CommandLine;"
        " if ($null -eq $cl2) { $unread++; continue }"
        " if (& $classify $p2.Name $cl2)"
        "{ $hits += @{pid=$p2.ProcessId; name=$p2.Name; cmd=$cl2.Substring(0,[Math]::Min(160,$cl2.Length))} } }"
        # ⚠️ 09-10: ang $null na CommandLine ng SARILING proseso ng user ay halos palaging
        # ELEVATION — hindi kayang basahin ng limited na reader ang elevated na proseso.
        # Iniuulat ang antas ng READER para masabi ng receipt KUNG BAKIT, hindi lang na
        # hindi nababasa. Nananatiling fail-closed; ang lunas ay `RunLevel=Highest` sa task.
        "$elev = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
        ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);"
        "@{hits=@($hits); unreadable=$unread; null_first_pass=$nullPids.Count; reader_elevated=$elev} | ConvertTo-Json -Depth 4",
    ], "processes", out["probe_failures"])
    if so is not None:
        data = json.loads(so or "{}")
        hits = data.get("hits") or []
        if isinstance(hits, dict):
            hits = [hits]
        hits = [h for h in hits if int(h.get("pid") or 0) not in exempt]
        out["probes"]["processes"] = hits
        out["probes"]["unreadable_cmdlines"] = int(data.get("unreadable") or 0)
        # Ilan ang $null sa UNANG pass at nabawi ng pangalawa -- ito ang
        # nagpapakita sa operator na gumana ang recovery sa halip na tahimik itong
        # magmukhang walang nangyari.
        out["probes"]["unreadable_recovered"] = max(
            0,
            int(data.get("null_first_pass") or 0) - int(data.get("unreadable") or 0),
        )
        out["order_capable_total"] += len(hits)
        out["probes"]["reader_elevated"] = bool(data.get("reader_elevated"))
        if out["probes"]["unreadable_cmdlines"]:
            # 09-10: isang elevated na Claude shell (anak ng claude.exe) + Limited na task
            # = 4.5 oras na walang lane. Pangalanan ang sanhi sa mismong dahilan ng pagbagsak.
            _why = ("unreadable_command_lines_reader_not_elevated"
                    if not out["probes"]["reader_elevated"] else "unreadable_command_lines")
            out["probe_failures"].append({"probe": "processes", "reason": _why})

    so = _probe([
        POWERSHELL, "-NoProfile", "-Command",
        "$ErrorActionPreference='Stop';"
        "$svc = Get-Service | Where-Object { $_.Name -match 'chili|alpaca|iqfeed' -and $_.Status -ne 'Stopped' };"
        "@($svc | ForEach-Object { $_.Name }) | ConvertTo-Json",
    ], "services", out["probe_failures"])
    if so is not None:
        svcs = json.loads(so or "[]")
        svcs = [svcs] if isinstance(svcs, str) else (svcs or [])
        out["probes"]["running_services"] = svcs
        out["order_capable_total"] += len(svcs)

    so = _probe([
        POWERSHELL, "-NoProfile", "-Command",
        "$ErrorActionPreference='Stop';"
        "$t = Get-ScheduledTask | Where-Object { $_.State -ne 'Disabled' -and "
        "($_.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join ' ' -match "
        "'captured_alpaca|iqfeed|chili.*(paper|momentum)|alpaca' };"
        "@($t | ForEach-Object { @{name=$_.TaskName; state=[string]$_.State} }) | ConvertTo-Json -Depth 3",
    ], "scheduled_tasks", out["probe_failures"])
    if so is not None:
        tasks = json.loads(so or "[]")
        tasks = [tasks] if isinstance(tasks, dict) else (tasks or [])
        out["probes"]["enabled_order_capable_tasks"] = tasks
        running = [t for t in tasks if str(t.get("state")).lower() == "running"]
        out["order_capable_total"] += len(running)

    so = _probe([DOCKER, "ps", "--format", "{{.Names}}"], "docker", out["probe_failures"])
    if so is not None:
        risky = [n for n in so.splitlines() if re.search(r"exec|paper|runner", n, re.I)]
        out["probes"]["order_capable_containers"] = risky
        out["order_capable_total"] += len(risky)

    so = _probe([WSL, "-l", "--running", "-q"], "wsl", out["probe_failures"])
    if so is not None:
        distros = [x.strip() for x in so.replace("\x00", "").splitlines() if x.strip()]
        out["probes"]["running_wsl_distros"] = distros
        foreign = [d for d in distros if d.lower() not in ("docker-desktop", "docker-desktop-data")]
        if foreign:
            out["probes"]["foreign_wsl"] = foreign
            out["order_capable_total"] += len(foreign)

    # Ang orphan reconciler ay APScheduler job sa loob ng trading_scheduler
    # process — ang kawalan ng anumang order-capable process (sa itaas) ang
    # nagpapatunay ng kawalan nito. Dokumentado, hindi string-match na hiwalay.
    out["orphan_reconciler_state"] = "absent_via_process_census"
    out["clean"] = out["order_capable_total"] == 0 and not out["probe_failures"]
    return out


def _write_receipt(doc):
    body = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    sha = hashlib.sha256(body).hexdigest()
    os.makedirs(RECEIPT_DIR, exist_ok=True)
    phase = doc["schema"].split(".")[1].split("-")[-1]
    path = os.path.join(
        RECEIPT_DIR,
        f"{phase}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{sha[:12]}.json",
    )
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        raise SystemExit("receipt write failed — fail closed")
    rehash = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if rehash != sha:
        raise SystemExit("post-write rehash mismatch — fail closed")
    return path, sha


class Lease:
    """Session-scoped na hawak; pinapatunayan sa pg_locks bawat check."""

    def __init__(self):
        self.conn = psycopg2.connect(DB_URL)
        self.conn.autocommit = True
        self.cur = self.conn.cursor()
        self.cur.execute("SELECT pg_backend_pid()")
        self.backend_pid = int(self.cur.fetchone()[0])

    def acquire(self):
        self.cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (LEASE_CLASSID, LEASE_OBJID))
        return bool(self.cur.fetchone()[0])

    def held_by_me(self):
        try:
            self.cur.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted "
                "AND classid=%s AND objid=%s AND pid=%s",
                (LEASE_CLASSID, LEASE_OBJID, self.backend_pid),
            )
            return int(self.cur.fetchone()[0]) == 1
        except Exception:
            return False

    def six_counters(self):
        self.cur.execute(SIX_COUNTERS_SQL)
        return dict(zip(COUNTER_NAMES, self.cur.fetchone()))


def _base_doc(schema, epoch, lease):
    return {
        "schema": schema,
        "at_utc": _now(),
        "window_epoch": epoch,
        "lease_key": {"classid": LEASE_CLASSID, "objid": LEASE_OBJID},
        "lease_backend_pid": lease.backend_pid,
        "lease_held_verified": lease.held_by_me(),
        "holder_pid": os.getpid(),
        "holder_identity": f"claude-legacy-supervisor@{os.environ.get('COMPUTERNAME','?')}",
        "commit_pin": COMMIT_PIN,
        "script_sha256": _sha_file(os.path.abspath(__file__)),
        "env_file_sha256": _sha_file(ENV_PATH),
        "window_env_values": dict(WINDOW_ENV),
        "paper_order_submission_authorized": False,
    }


def _validate_rollback_artifact(path):
    if not path or not os.path.isfile(path):
        return False, "rollback_artifact_missing"
    body = open(path, "rb").read()
    if b"rollback_completed" not in body:
        return False, "rollback_artifact_not_terminal"
    return True, hashlib.sha256(body).hexdigest()


def _validate_prepared(path, expected_sha, epoch):
    if not path:
        return True, "walang_prepared_na_hinihingi"  # unang window: rollback artifact ang basehan
    if not os.path.isfile(path):
        return False, "prepared_missing"
    body = open(path, "rb").read()
    if hashlib.sha256(body).hexdigest() != (expected_sha or "").lower():
        return False, "prepared_sha_mismatch"
    doc = json.loads(body)
    if doc.get("schema") not in (SCHEMA_PREPARED, "chili.timeshare-handoff-prepared.v2"):
        return False, "prepared_schema_mismatch"
    if doc.get("window_epoch") and epoch and doc["window_epoch"] > epoch:
        return False, "prepared_epoch_future"
    if doc.get("clean") is not True:
        return False, "prepared_not_clean"
    return True, "ok"


def supervision_policy(args, *, lane_env=None, supervisor_env=None, config=None):
    """Explicit PAPER continuity, distinct from a timed equity window.

    Account/producer/lease checks below still run. No hidden HHMM sentinel is
    accepted, and the stock-only fallback cannot execute for this mode.
    """
    continuous = bool(getattr(args, "continuous_native_paper", False))
    if not continuous:
        end = getattr(args, "end_utc", None)
        if not isinstance(end, str) or not re.fullmatch(r"[0-2][0-9][0-5][0-9]", end) or int(end[:2]) > 23:
            raise ValueError("supervisor_valid_utc_cutoff_required")
        return {"mode": "timed_window", "end_hhmm": int(end), "shutdown_orders": "legacy_equity_cleanup"}
    if getattr(args, "end_utc", None) is not None or not getattr(args, "operator_authorized", False):
        raise ValueError("supervisor_continuous_operator_scope_required")
    if lane_env is None or supervisor_env is None:
        from dotenv import dotenv_values
        lane_env = dotenv_values(LANE_ENV_PATH)
        supervisor_env = dotenv_values(ENV_PATH)
    yes = lambda value: str(value).lower() in ("true", "1")
    if (not yes(lane_env.get("CHILI_ALPACA_PAPER")) or
            not yes(supervisor_env.get("CHILI_ALPACA_PAPER")) or
            not yes(lane_env.get("CHILI_MOMENTUM_CRYPTO_EXECUTION_VIA_ALPACA_PAPER"))):
        raise ValueError("supervisor_continuous_native_paper_required")
    account = lane_env.get("CHILI_ALPACA_EXPECTED_ACCOUNT_ID", "")
    if str(UUID(account)) != account or account != supervisor_env.get("CHILI_ALPACA_EXPECTED_ACCOUNT_ID"):
        raise ValueError("supervisor_continuous_account_binding_changed")
    config_path = lane_env.get("CHILI_MOMENTUM_NATIVE_CRYPTO_HOST_CONFIG_PATH", "")
    if not Path(config_path).is_absolute():
        raise ValueError("supervisor_native_config_required")
    if config is None:
        with Path(config_path).open('rb') as handle:
            raw = handle.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("supervisor_native_config_capacity")
        config = json.loads(raw)
        if config.get("env_sha256") != _sha_file(LANE_ENV_PATH):
            raise ValueError("supervisor_native_environment_changed")
    if (config.get("contract") != "native_paper_host_v1" or
            Path(config.get("supervisor_path", "")).resolve() != Path(__file__).resolve() or
            Path(config.get("env_path", "")).resolve() != Path(LANE_ENV_PATH).resolve() or
            Path(config.get("supervisor_env_path", "")).resolve() != Path(ENV_PATH).resolve()):
        raise ValueError("supervisor_native_host_binding_changed")
    return {"mode": "continuous_native_paper", "end_hhmm": None,
            "shutdown_orders": "native_owner_reconciliation_only",
            "account_identity_sha256": hashlib.sha256(account.encode()).hexdigest(),
            "native_config_path": config_path}


def supervision_stop(policy, *, now_hhmm, app_returncode, lease_held):
    if app_returncode is not None:
        return "app_died"
    if not lease_held:
        return "lease_lost"
    if policy["end_hhmm"] is not None and now_hhmm >= policy["end_hhmm"]:
        return "window_end"
    return None


def shutdown_cleanup(policy):
    if policy["mode"] == "continuous_native_paper":
        # An unexpected stop needs the native durable owner to reconcile exact
        # fractional quantities and pending orders. Never cancel all account
        # orders or submit the old stock-specific emergency orders here.
        return {"attempted": False, "flat": None, "placed": [], "errors": [],
                "policy": "native_owner_reconciliation_only", "recovery_required": True}
    return _preshutdown_flatten()


def cmd_window(args):
    policy = supervision_policy(args)
    lease = Lease()
    if not lease.acquire():
        print("LEASE_BUSY — fail closed, hindi tatakbo")
        return 3
    print(f"LEASE_HELD backend_pid={lease.backend_pid} supervisor_pid={os.getpid()}")

    if args.rollback_artifact:
        ok_rb, rb_info = _validate_rollback_artifact(args.rollback_artifact)
    elif args.operator_authorized:
        # Operator override (2026-08-14 Option C): walang artifact mula sa
        # counterparty; ang kapalit na patunay ay ang producer census sa ibaba
        # (order_capable_total==0 — kung may tumatakbong Codex service, hindi
        # magiging clean ang ACCEPT at hindi tatakbo). Desisyon ng operator,
        # nakatala sa receipt.
        ok_rb, rb_info = True, "operator_authorized_census_gated"
    else:
        ok_rb, rb_info = False, "walang_artifact_at_walang_operator_authorization"
    ok_prep, prep_info = _validate_prepared(args.prepared_path, args.prepared_sha256, args.epoch)
    if not (ok_rb and ok_prep):
        print(f"ACCEPT FAILED: rollback={rb_info} prepared={prep_info} — fail closed")
        return 4

    expected_acct = _env_read().get("CHILI_ALPACA_EXPECTED_ACCOUNT_ID", "")
    doc = _base_doc(SCHEMA_ACCEPTED, args.epoch, lease)
    doc["supervision_policy"] = policy
    doc["counterparty_rollback_artifact"] = {"path": args.rollback_artifact, "sha256_or_reason": rb_info}
    doc["operator_authorized_override"] = bool(getattr(args, "operator_authorized", False))
    doc["counterparty_prepared"] = {"path": args.prepared_path, "verdict": prep_info}
    doc["prestart_counters"] = lease.six_counters()
    doc["broker_census"] = _broker_census(expected_acct)
    doc["producer_census"] = _producer_census()
    doc["clean"] = (
        doc["lease_held_verified"]
        and all(v == 0 for v in doc["prestart_counters"].values())
        and doc["broker_census"]["clean"]
        and doc["producer_census"]["clean"]
    )
    path, sha = _write_receipt(doc)
    print(f"ACCEPTED receipt: {path} sha={sha} clean={doc['clean']}")
    if not doc["clean"]:
        print("ACCEPT hindi malinis — fail closed")
        return 4

    # ---- RUN: Job Object + app ----
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)

    env = dict(os.environ)
    env.update(WINDOW_ENV)
    if policy["mode"] == "continuous_native_paper":
        env["CHILI_ALPACA_PAPER"] = "true"
        env["CHILI_MOMENTUM_CRYPTO_EXECUTION_VIA_ALPACA_PAPER"] = "true"
        env["CHILI_MOMENTUM_NATIVE_CRYPTO_HOST_CONFIG_PATH"] = policy["native_config_path"]
    # 2026-09-03: ilathala ang pin laban sa pinakabagong build sa tape para
    # makita AGAD kung patay ang IQFeed leg (dating tahimik na kamatayan).
    try:
        _pin = env["CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD"]
        with psycopg2.connect(DB_URL, connect_timeout=8) as _c, _c.cursor() as _cur:
            _cur.execute("SET statement_timeout='10s'")
            _cur.execute(
                "SELECT bridge_version FROM momentum_nbbo_spread_tape "
                "WHERE source='iqfeed_l1' AND observed_at > "
                "(now() at time zone 'utc') - interval '10 minutes' "
                "ORDER BY observed_at DESC LIMIT 1"
            )
            _row = _cur.fetchone()
        _tape = _row[0] if _row else "(walang iqfeed_l1 row sa huling 10 min)"
        _mark = "TUGMA" if _tape == _pin else "HINDI TUGMA — PATAY ANG IQFEED LEG"
        print(f"IQFEED BRIDGE PIN {_mark}: pin={_pin} tape={_tape}")
    except Exception as _exc:
        print(f"IQFEED BRIDGE PIN: hindi ma-verify laban sa tape ({_exc})")
    app = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8010"],
        # v3.5 (08-16): sira ang D: git (tahimik na partial writes) — ang window
        # app ay tumatakbo na mula sa malinis na E: worktree ng merged main.
        cwd=r"E:\dev\wt-window2",
        env=env,
        stdout=open(args.app_log, "ab"),
        stderr=subprocess.STDOUT,
        creationflags=0x00000004,  # CREATE_SUSPENDED (wala sa subprocess constants)
    )
    handle = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, app.pid)
    win32job.AssignProcessToJobObject(job, handle)
    # Sinimulan nang CREATE_SUSPENDED para ang assignment sa Job Object ay
    # MAUNA sa unang instruction ng app — walang puwang na tumakbo ito sa
    # labas ng kill-on-close na sakop.
    _resume_process(app.pid)
    print(f"APP LAUNCHED pid={app.pid} sa loob ng Job Object (KILL_ON_JOB_CLOSE)")

    while True:
        now_hhmm = int(datetime.now(timezone.utc).strftime("%H%M"))
        reason = supervision_stop(policy, now_hhmm=now_hhmm,
                                  app_returncode=app.poll(), lease_held=lease.held_by_me())
        if reason is not None:
            break
        time.sleep(20)
    print(f"SHUTDOWN trigger: {reason} @ {_now()}")
    win32job.TerminateJobObject(job, 0)
    # v3.4: hintayin ang TUNAY na pagkamatay ng app bago ang census — noong
    # W20260814-01B, ang 5s fixed sleep ay kulang para sa 1.7GB uvicorn teardown
    # kaya na-detect ng PREPARE census ang SARILI NATING app bilang "order-capable
    # producer" at naging clean=False ang receipt nang walang tunay na dahilan.
    for _ in range(60):
        if app.poll() is not None:
            break
        time.sleep(2)
    time.sleep(3)

    if reason == "lease_lost":
        print("LEASE LOST — pinatay ang app (fail closed); WALANG prepared receipt na isusulat")
        return 5

    # v3.7 (COIW 08-21): FLATTEN PAGKATAPOS ng app kill, BAGO ang census/lease
    # release — noong 08-21, namatay ang app habang bukas ang COIW 177sh
    # (nakansela pa ang deadman stop sa teardown), clean=False ang receipt pero
    # walang gumawa ng aksyon; sa totoong pera at weekend = hubad na posisyon.
    # Ang pagkakasunod ay app-muna-bago-flatten para hindi lumaban ang deadman/
    # broker-sync machinery ng app sa cancel+sell (segundong hubad lang).
    flatten_report = None
    try:
        flatten_report = shutdown_cleanup(policy)
        print(f"POST-KILL FLATTEN: flat={flatten_report.get('flat')} "
              f"placed={flatten_report.get('placed')} "
              f"errors={flatten_report.get('errors')}")
    except Exception as exc:
        print(f"POST-KILL FLATTEN failed (tuloy ang receipt): {exc}")

    doc = _base_doc(SCHEMA_PREPARED, args.epoch, lease)
    doc["supervision_policy"] = policy
    doc["shutdown_reason"] = reason
    if flatten_report is not None:
        doc["post_kill_flatten"] = flatten_report
    doc["prestart_counters"] = lease.six_counters()
    doc["broker_census"] = _broker_census(expected_acct)
    doc["producer_census"] = _producer_census(app_pid=app.pid)
    doc["clean"] = (
        doc["lease_held_verified"]
        and all(v == 0 for v in doc["prestart_counters"].values())
        and doc["broker_census"]["clean"]
        and doc["producer_census"]["clean"]
    )
    path, sha = _write_receipt(doc)
    print(f"PREPARED receipt: {path} sha={sha} clean={doc['clean']}")
    lease.conn.close()  # dito lang bumibitaw ang lock
    print("LEASE RELEASED — tapos ang window")
    return 0 if doc["clean"] else 1


def _resume_process(pid):
    import ctypes
    k32 = ctypes.windll.kernel32
    TH32CS_SNAPTHREAD = 0x4
    THREAD_SUSPEND_RESUME = 0x0002

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
            ("th32ThreadID", ctypes.c_ulong), ("th32OwnerProcessID", ctypes.c_ulong),
            ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
        ]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(THREADENTRY32)
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            h = k32.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
            if h:
                k32.ResumeThread(h)
                k32.CloseHandle(h)
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)


def cmd_status(_args):
    lease = Lease()
    got = lease.acquire()
    if got:
        print("lease: MALAYA (nakuha at binitawan sa status probe)")
        lease.conn.close()
    else:
        print("lease: HAWAK NG IBA")
    if os.path.isdir(RECEIPT_DIR):
        for f in sorted(os.listdir(RECEIPT_DIR))[-6:]:
            print(" receipt:", f)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("window")
    w.add_argument("--epoch", required=True)
    lifetime = w.add_mutually_exclusive_group(required=True)
    lifetime.add_argument("--end-utc", help="HHMM UTC na katapusan ng window")
    lifetime.add_argument("--continuous-native-paper", action="store_true",
                          help="Explicit continuous native crypto PAPER ownership; no UTC cutoff")
    w.add_argument("--rollback-artifact", default=None, help="tahasang path ng terminal rollback artifact ni Codex")
    w.add_argument("--operator-authorized", action="store_true", help="operator override: ang counterparty-absence ay pinapatunayan ng producer census sa halip na artifact")
    w.add_argument("--prepared-path", default=None)
    w.add_argument("--prepared-sha256", default=None)
    w.add_argument("--app-log", default=r"D:\dev\chili-home-copilot\project_ws\AgentOps\timeshare\window_app.log")
    sub.add_parser("status")
    args = ap.parse_args()
    return cmd_window(args) if args.cmd == "window" else cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
