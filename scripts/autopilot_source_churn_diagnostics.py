from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_coding_benchmark import (  # noqa: E402
    DEFAULT_OUTPUT as DEFAULT_SCORECARD,
    SOURCE_STABILITY_ROOTS,
    SOURCE_STABILITY_SKIP_DIRS,
    SOURCE_STABILITY_SUFFIXES,
)


SOURCE_CHURN_DIAGNOSTICS_SCHEMA_VERSION = "chili.source-churn-diagnostics.v1"
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "SOURCE_CHURN_DIAGNOSTICS.md"
DEFAULT_LEASE_PATH = REPO_ROOT / "project_ws" / "AgentOps" / "SOURCE_QUIET_BENCHMARK_LEASE.json"
DEFAULT_WATCH_SECONDS = 30.0
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_MAX_TABLE_ROWS = 200


@dataclass(frozen=True)
class SourceFileStat:
    path: str
    modified_ns: int
    size: int

    @property
    def modified_at(self) -> datetime:
        return datetime.fromtimestamp(self.modified_ns / 1_000_000_000, tz=timezone.utc)


@dataclass(frozen=True)
class WatchChange:
    path: str
    change: str
    before: SourceFileStat | None
    after: SourceFileStat | None


SnapshotFn = Callable[[Path], Mapping[str, SourceFileStat]]
SleepFn = Callable[[float], None]
ClockFn = Callable[[], float]
NowFn = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def scorecard_metadata(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    metadata: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        metadata[key.strip().lower()] = value.strip()
    return metadata


def lease_metadata(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "invalid_json"}
    if not isinstance(payload, Mapping):
        return {"status": "invalid_payload"}
    values: dict[str, str] = {}
    for key in (
        "status",
        "lease_id",
        "holder",
        "pid",
        "created_utc",
        "expires_utc",
        "released_utc",
        "quiet_seconds",
        "lease_seconds",
        "permission_boundary",
    ):
        value = payload.get(key)
        if value is not None:
            values[key] = str(value)
    return values


def metadata_text(metadata: Mapping[str, str], key: str, default: str = "") -> str:
    return str(metadata.get(key.lower()) or default).strip()


def metadata_int(metadata: Mapping[str, str], key: str, default: int = 0) -> int:
    value = metadata_text(metadata, key)
    if "/" in value:
        value = value.split("/", 1)[0].strip()
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def iter_source_files(repo_root: Path) -> Iterable[Path]:
    for root_rel in SOURCE_STABILITY_ROOTS:
        root = repo_root / Path(root_rel)
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in SOURCE_STABILITY_SKIP_DIRS and not name.startswith(".")
            ]
            for filename in filenames:
                if not filename.endswith(SOURCE_STABILITY_SUFFIXES):
                    continue
                yield Path(dirpath) / filename


def source_tree_snapshot(repo_root: Path = REPO_ROOT) -> dict[str, SourceFileStat]:
    snapshot: dict[str, SourceFileStat] = {}
    for path in iter_source_files(repo_root):
        try:
            stat = path.stat()
            rel_path = path.relative_to(repo_root).as_posix()
        except OSError:
            continue
        except ValueError:
            rel_path = path.as_posix()
        snapshot[rel_path] = SourceFileStat(
            path=rel_path,
            modified_ns=int(stat.st_mtime_ns),
            size=int(stat.st_size),
        )
    return snapshot


def compare_snapshots(
    before: Mapping[str, SourceFileStat],
    after: Mapping[str, SourceFileStat],
) -> list[WatchChange]:
    changes: list[WatchChange] = []
    for path in sorted(set(before) | set(after)):
        before_stat = before.get(path)
        after_stat = after.get(path)
        if before_stat == after_stat:
            continue
        if before_stat is None:
            change = "added"
        elif after_stat is None:
            change = "removed"
        else:
            change = "modified"
        changes.append(
            WatchChange(
                path=path,
                change=change,
                before=before_stat,
                after=after_stat,
            )
        )
    return changes


def files_newer_than(
    snapshot: Mapping[str, SourceFileStat],
    generated_at: datetime | None,
) -> list[SourceFileStat]:
    if generated_at is None:
        return []
    return sorted(
        (
            stat
            for stat in snapshot.values()
            if stat.modified_at > generated_at.astimezone(timezone.utc)
        ),
        key=lambda stat: (stat.modified_at, stat.path),
    )


def watch_source_tree(
    repo_root: Path,
    *,
    watch_seconds: float,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    snapshot_fn: SnapshotFn = source_tree_snapshot,
    sleep_fn: SleepFn = time.sleep,
    clock_fn: ClockFn = time.monotonic,
) -> dict[str, object]:
    watch_seconds = max(float(watch_seconds), 0.0)
    poll_seconds = max(float(poll_seconds), 0.01)
    initial = dict(snapshot_fn(repo_root))
    if watch_seconds <= 0:
        return {
            "status": "not_required",
            "watch_seconds": watch_seconds,
            "source_changes_during_watch": 0,
            "changes": [],
            "scanned_files": len(initial),
        }

    previous = initial
    observed: dict[str, WatchChange] = {}
    deadline = clock_fn() + watch_seconds
    while True:
        remaining = deadline - clock_fn()
        if remaining <= 0:
            break
        sleep_fn(min(poll_seconds, remaining))
        current = dict(snapshot_fn(repo_root))
        for change in compare_snapshots(previous, current):
            observed[change.path] = change
        previous = current

    final_changes = compare_snapshots(initial, previous)
    for change in final_changes:
        observed[change.path] = change
    changes = [observed[path] for path in sorted(observed)]
    return {
        "status": "stable" if not changes else "changed",
        "watch_seconds": watch_seconds,
        "source_changes_during_watch": len(changes),
        "changes": changes,
        "scanned_files": len(previous),
    }


def _diagnostic_state(
    *,
    scorecard_present: bool,
    scorecard_status: str,
    scorecard_generated_raw: str,
    scorecard_generated_at: datetime | None,
    scorecard_source_stability: str,
    current_source_freshness: str,
    newer_count: int,
    watch_status: str,
    watch_change_count: int,
) -> tuple[str, str, str, str]:
    benchmark_command = (
        "python scripts/autopilot_coding_benchmark.py "
        "--require-source-quiet-seconds 30"
    )
    if not scorecard_present:
        return (
            "blocked",
            "blocked",
            "scorecard_missing",
            (
                "Generate the coding benchmark scorecard with a source quiet "
                f"preflight: {benchmark_command}."
            ),
        )
    if scorecard_generated_raw and scorecard_generated_at is None:
        return (
            "blocked",
            "blocked",
            "scorecard_generated_utc_invalid",
            "Repair or regenerate the coding benchmark scorecard; its Generated UTC is invalid.",
        )
    if watch_change_count:
        return (
            "warning",
            "blocked",
            "wait_for_source_quiet",
            (
                "Source/test files changed during this diagnostic window; wait "
                "for churn to settle and rerun this diagnostic before benchmarking."
            ),
        )
    if current_source_freshness == "stale" or scorecard_source_stability == "changed":
        if watch_status in {"stable", "not_required"}:
            return (
                "warning",
                "blocked",
                "ready_for_benchmark_rerun",
                (
                    "The tree was quiet during this diagnostic window; rerun the "
                    "full coding benchmark with a source quiet preflight."
                ),
            )
    if scorecard_status == "passed" and current_source_freshness == "current":
        return (
            "passed",
            "clear",
            "not_needed_scorecard_current",
            "Scorecard is source-current; proceed to frontier model evidence gates.",
        )
    return (
        "warning",
        "blocked",
        "benchmark_needs_attention",
        "Review the coding benchmark scorecard result and rerun it after source/test churn settles.",
    )


def render_diagnostics(
    *,
    generated_at: datetime,
    scorecard_path: Path,
    scorecard_present: bool,
    scorecard_metadata_values: Mapping[str, str],
    lease_metadata_values: Mapping[str, str],
    source_snapshot: Mapping[str, SourceFileStat],
    newer_files: Sequence[SourceFileStat],
    watch: Mapping[str, object],
    max_table_rows: int = DEFAULT_MAX_TABLE_ROWS,
) -> tuple[str, dict[str, object]]:
    scorecard_status = (
        metadata_text(scorecard_metadata_values, "status", "missing")
        if scorecard_present
        else "missing"
    ).lower()
    scorecard_generated_raw = metadata_text(scorecard_metadata_values, "generated utc")
    scorecard_generated_at = parse_utc(scorecard_generated_raw)
    scorecard_source_stability = metadata_text(
        scorecard_metadata_values,
        "source stability",
        "unknown" if not scorecard_present else "",
    ).lower()
    scorecard_source_change_preview = metadata_text(scorecard_metadata_values, "source change preview", "none")
    source_changes_during_scorecard = metadata_int(
        scorecard_metadata_values,
        "source changes during run",
        0,
    )
    current_source_freshness = (
        "scorecard_missing"
        if not scorecard_present
        else "invalid_scorecard_generated_utc"
        if scorecard_generated_raw and scorecard_generated_at is None
        else "current"
        if not newer_files
        else "stale"
    )
    watch_status = str(watch.get("status") or "").strip().lower()
    watch_changes = list(watch.get("changes") or [])
    watch_change_count = int(watch.get("source_changes_during_watch") or 0)
    status, promotion_impact, rerun_readiness, next_action = _diagnostic_state(
        scorecard_present=scorecard_present,
        scorecard_status=scorecard_status,
        scorecard_generated_raw=scorecard_generated_raw,
        scorecard_generated_at=scorecard_generated_at,
        scorecard_source_stability=scorecard_source_stability,
        current_source_freshness=current_source_freshness,
        newer_count=len(newer_files),
        watch_status=watch_status,
        watch_change_count=watch_change_count,
    )
    lease_status = metadata_text(lease_metadata_values, "status", "missing")
    lease_id = metadata_text(lease_metadata_values, "lease_id", "")
    lease_released = metadata_text(lease_metadata_values, "released_utc", "")
    if (
        source_changes_during_scorecard > 0
        and watch_status in {"stable", "not_required"}
        and scorecard_source_change_preview
        and scorecard_source_change_preview != "none"
    ):
        lease_hint = f" Lease {lease_id or 'unknown'} is {lease_status}"
        if lease_released:
            lease_hint += f" and released at {lease_released}"
        next_action = (
            f"{next_action} Last benchmark source-change preview: "
            f"{scorecard_source_change_preview}. Confirm peer source-writing lanes honored the quiet lease before rerunning."
            f"{lease_hint}."
        )
    max_table_rows = max(int(max_table_rows), 0)
    newer_rows = list(newer_files[:max_table_rows]) if max_table_rows else []
    watch_rows = list(watch_changes[:max_table_rows]) if max_table_rows else []
    scorecard_rel = scorecard_path
    try:
        scorecard_rel = scorecard_path.relative_to(REPO_ROOT)
    except ValueError:
        pass
    lines = [
        "# CHILI Source Churn Diagnostics",
        "",
        f"- Schema: {SOURCE_CHURN_DIAGNOSTICS_SCHEMA_VERSION}",
        f"- Generated UTC: {iso_utc(generated_at)}",
        f"- Status: {status}",
        f"- Promotion impact: {promotion_impact}",
        f"- Rerun readiness: {rerun_readiness}",
        f"- Scorecard: {scorecard_rel.as_posix()}",
        f"- Scorecard status: {scorecard_status}",
        f"- Scorecard generated UTC: {scorecard_generated_raw}",
        f"- Scorecard source stability: {scorecard_source_stability}",
        f"- Source changes during scorecard: {source_changes_during_scorecard}",
        f"- Scorecard source change preview: {scorecard_source_change_preview}",
        f"- Benchmark lease status: {lease_status}",
        f"- Benchmark lease id: {lease_id or 'missing'}",
        f"- Benchmark lease holder: {metadata_text(lease_metadata_values, 'holder', 'missing')}",
        f"- Benchmark lease created UTC: {metadata_text(lease_metadata_values, 'created_utc', '')}",
        f"- Benchmark lease released UTC: {lease_released}",
        f"- Current source freshness: {current_source_freshness}",
        f"- Source files scanned: {len(source_snapshot)}",
        f"- Source changes after scorecard: {len(newer_files)}",
        f"- Files newer table rows: {len(newer_rows)}/{len(newer_files)}",
        f"- Watch status: {watch_status}",
        f"- Watch seconds: {float(watch.get('watch_seconds') or 0.0):.1f}",
        f"- Source changes during watch: {watch_change_count}",
        f"- Watch table rows: {len(watch_rows)}/{watch_change_count}",
        f"- Next action: {next_action}",
        "- Safety: read-only source/test diagnostics only; no git, deployment, runtime restart, broker, migration, or live-trading action.",
        "",
        "## Files Newer Than Scorecard",
        "",
        "| Path | Modified UTC | Seconds after scorecard | Size |",
        "| --- | --- | ---: | ---: |",
    ]
    if newer_rows and scorecard_generated_at is not None:
        for stat in newer_rows:
            delta = stat.modified_at - scorecard_generated_at
            lines.append(
                "| "
                + " | ".join(
                    [
                        escape_cell(stat.path),
                        iso_utc(stat.modified_at),
                        f"{delta.total_seconds():.3f}",
                        str(stat.size),
                    ]
                )
                + " |"
            )
    else:
        lines.append("| none |  |  |  |")
    lines.extend(
        [
            "",
            "## Files Changed During Watch",
            "",
            "| Path | Change | Before UTC | After UTC | Before size | After size |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    if watch_rows:
        for change in watch_rows:
            assert isinstance(change, WatchChange)
            before = change.before
            after = change.after
            lines.append(
                "| "
                + " | ".join(
                    [
                        escape_cell(change.path),
                        escape_cell(change.change),
                        iso_utc(before.modified_at if before else None),
                        iso_utc(after.modified_at if after else None),
                        str(before.size if before else ""),
                        str(after.size if after else ""),
                    ]
                )
                + " |"
            )
    else:
        lines.append("| none |  |  |  |  |  |")
    markdown = "\n".join(lines) + "\n"
    payload = {
        "schema": SOURCE_CHURN_DIAGNOSTICS_SCHEMA_VERSION,
        "generated_utc": iso_utc(generated_at),
        "status": status,
        "promotion_impact": promotion_impact,
        "rerun_readiness": rerun_readiness,
        "scorecard": scorecard_rel.as_posix(),
        "scorecard_status": scorecard_status,
        "scorecard_generated_utc": scorecard_generated_raw,
        "scorecard_source_stability": scorecard_source_stability,
        "source_changes_during_scorecard": source_changes_during_scorecard,
        "scorecard_source_change_preview": scorecard_source_change_preview,
        "benchmark_lease_status": lease_status,
        "benchmark_lease_id": lease_id,
        "benchmark_lease_holder": metadata_text(lease_metadata_values, "holder", ""),
        "benchmark_lease_released_utc": lease_released,
        "current_source_freshness": current_source_freshness,
        "source_files_scanned": len(source_snapshot),
        "source_changes_after_scorecard": len(newer_files),
        "files_newer_table_rows": len(newer_rows),
        "watch_status": watch_status,
        "watch_seconds": float(watch.get("watch_seconds") or 0.0),
        "source_changes_during_watch": watch_change_count,
        "watch_table_rows": len(watch_rows),
        "next_action": next_action,
        "files_newer_than_scorecard": [stat.path for stat in newer_rows],
        "files_changed_during_watch": [change.path for change in watch_rows],
    }
    return markdown, payload


def run_diagnostics(
    *,
    repo_root: Path = REPO_ROOT,
    scorecard_path: Path = DEFAULT_SCORECARD,
    lease_path: Path = DEFAULT_LEASE_PATH,
    output_path: Path = DEFAULT_OUTPUT,
    watch_seconds: float = DEFAULT_WATCH_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    max_table_rows: int = DEFAULT_MAX_TABLE_ROWS,
    write: bool = True,
    snapshot_fn: SnapshotFn = source_tree_snapshot,
    sleep_fn: SleepFn = time.sleep,
    clock_fn: ClockFn = time.monotonic,
    now_fn: NowFn = utc_now,
) -> tuple[str, dict[str, object], Path]:
    scorecard_present = scorecard_path.is_file()
    metadata = scorecard_metadata(scorecard_path)
    lease = lease_metadata(lease_path)
    generated_at = parse_utc(metadata_text(metadata, "generated utc"))
    snapshot = dict(snapshot_fn(repo_root))
    newer = files_newer_than(snapshot, generated_at)
    watch = watch_source_tree(
        repo_root,
        watch_seconds=watch_seconds,
        poll_seconds=poll_seconds,
        snapshot_fn=snapshot_fn,
        sleep_fn=sleep_fn,
        clock_fn=clock_fn,
    )
    markdown, payload = render_diagnostics(
        generated_at=now_fn(),
        scorecard_path=scorecard_path,
        scorecard_present=scorecard_present,
        scorecard_metadata_values=metadata,
        lease_metadata_values=lease,
        source_snapshot=snapshot,
        newer_files=newer,
        watch=watch,
        max_table_rows=max_table_rows,
    )
    if write:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    payload = {**payload, "path": output_path.as_posix(), "written": bool(write)}
    return markdown, payload, output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explain whether CHILI coding benchmark evidence is source-current."
    )
    parser.add_argument("--scorecard", type=Path, default=DEFAULT_SCORECARD)
    parser.add_argument("--lease", type=Path, default=DEFAULT_LEASE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--watch-seconds", type=float, default=DEFAULT_WATCH_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--max-table-rows", type=int, default=DEFAULT_MAX_TABLE_ROWS)
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    parser.add_argument("--no-write", action="store_true", help="Print without writing the report.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    markdown, payload, _ = run_diagnostics(
        scorecard_path=args.scorecard,
        lease_path=args.lease,
        output_path=args.output,
        watch_seconds=args.watch_seconds,
        poll_seconds=args.poll_seconds,
        max_table_rows=args.max_table_rows,
        write=not args.no_write,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
