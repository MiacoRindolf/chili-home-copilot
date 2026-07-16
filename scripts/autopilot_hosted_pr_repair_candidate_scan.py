from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_REPO = "MiacoRindolf/chili-home-copilot"
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "HOSTED_PR_REPAIR_CANDIDATE_SCAN.md"
HOSTED_PR_REPAIR_CANDIDATE_SCAN_SCHEMA_VERSION = "chili.hosted-pr-repair-candidate-scan.v1"
DEFAULT_LIMIT = 50


class HostedPrRepairCandidateScanError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class HostedPrProbe:
    number: int
    url: str
    title: str
    state: str
    merged_at: str
    head_sha: str
    review_threads: int
    comments: int = 0
    reviews: int = 0

    @property
    def has_review_thread_inventory(self) -> bool:
        return self.review_threads > 0


CommandRunner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]
ThreadLookup = Callable[[Mapping[str, object]], Mapping[str, object]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _int_value(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _run_command(args: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )


def _repo_owner_name(repo: str) -> tuple[str, str]:
    parts = [part for part in str(repo or "").strip().split("/") if part]
    if len(parts) != 2:
        raise HostedPrRepairCandidateScanError("repo must be in owner/name form")
    return parts[0], parts[1]


def _load_json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HostedPrRepairCandidateScanError(f"{path}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise HostedPrRepairCandidateScanError(f"{path}: {exc}") from exc


def merged_prs_from_gh(
    repo: str,
    *,
    limit: int = DEFAULT_LIMIT,
    runner: CommandRunner = _run_command,
) -> list[Mapping[str, object]]:
    limit = max(1, int(limit))
    result = runner(
        (
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--limit",
            str(limit),
            "--json",
            "number,title,url,state,mergedAt,headRefOid,headRefName,baseRefName",
        ),
        60,
    )
    if result.returncode != 0:
        raise HostedPrRepairCandidateScanError(
            "gh pr list failed: " + (result.stderr or result.stdout or "unknown error").strip()
        )
    payload = json.loads(result.stdout or "[]")
    if not isinstance(payload, list):
        raise HostedPrRepairCandidateScanError("gh pr list returned non-list JSON")
    return [item for item in payload if isinstance(item, Mapping)]


def review_thread_counts_from_gh(
    repo: str,
    pr_number: int,
    *,
    runner: CommandRunner = _run_command,
) -> Mapping[str, object]:
    owner, name = _repo_owner_name(repo)
    query = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      reviewThreads(first: 1) { totalCount }
      comments(first: 1) { totalCount }
      reviews(first: 1) { totalCount }
    }
  }
}
""".strip()
    result = runner(
        (
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={int(pr_number)}",
        ),
        60,
    )
    if result.returncode != 0:
        raise HostedPrRepairCandidateScanError(
            f"gh graphql reviewThreads query failed for PR {pr_number}: "
            + (result.stderr or result.stdout or "unknown error").strip()
        )
    payload = json.loads(result.stdout or "{}")
    pull = (
        payload.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        if isinstance(payload, Mapping)
        else {}
    )
    if not isinstance(pull, Mapping):
        return {}
    return {
        "review_threads": _int_value((pull.get("reviewThreads") or {}).get("totalCount")),
        "comments": _int_value((pull.get("comments") or {}).get("totalCount")),
        "reviews": _int_value((pull.get("reviews") or {}).get("totalCount")),
    }


def probe_prs(
    prs: Sequence[Mapping[str, object]],
    *,
    lookup: ThreadLookup,
) -> list[HostedPrProbe]:
    probes: list[HostedPrProbe] = []
    for item in prs:
        number = _int_value(item.get("number"))
        if number <= 0:
            continue
        counts = dict(lookup(item))
        probes.append(
            HostedPrProbe(
                number=number,
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                state=str(item.get("state") or "MERGED"),
                merged_at=str(item.get("mergedAt") or item.get("merged_at") or ""),
                head_sha=str(item.get("headRefOid") or item.get("head_sha") or ""),
                review_threads=_int_value(counts.get("review_threads")),
                comments=_int_value(counts.get("comments")),
                reviews=_int_value(counts.get("reviews")),
            )
        )
    return probes


def scan_status(probes: Sequence[HostedPrProbe]) -> str:
    if not probes:
        return "no_prs_scanned"
    if any(probe.has_review_thread_inventory for probe in probes):
        return "candidate_found"
    return "no_review_thread_candidates"


def render_report(
    probes: Sequence[HostedPrProbe],
    *,
    repo: str,
    generated_utc: str | None = None,
) -> str:
    generated_utc = generated_utc or _utc_now()
    candidates = [probe for probe in probes if probe.has_review_thread_inventory]
    status = scan_status(probes)
    lines = [
        "# Hosted PR Repair Candidate Scan",
        "",
        f"- Schema: {HOSTED_PR_REPAIR_CANDIDATE_SCAN_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_utc}",
        f"- Status: {status}",
        f"- Repository: {repo}",
        f"- PRs scanned: {len(probes)}",
        f"- Review-thread candidates: {len(candidates)}",
        "- Promotion impact: blocked" if not candidates else "- Promotion impact: candidate_leads_only",
        (
            "- Next action: Find or create a hosted repair PR with review-thread line detail, "
            "publication/current-head proof, and a green post-repair check receipt."
            if not candidates
            else "- Next action: Collect transcript-bound evidence for the candidate PRs, then run the hosted repair artifact validator."
        ),
        "- Permission boundary: read-only PR metadata scan; no git/PR mutation, runtime restart, deploy, database, broker, or live-trading action.",
        "",
        "| PR | Title | Review threads | Comments | Reviews | Head SHA | Merged UTC |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for probe in probes:
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(probe.url or f"#{probe.number}"),
                    _escape_cell(probe.title),
                    str(probe.review_threads),
                    str(probe.comments),
                    str(probe.reviews),
                    _escape_cell(probe.head_sha),
                    _escape_cell(probe.merged_at),
                ]
            )
            + " |"
        )
    lines.append("")
    if candidates:
        lines.extend(
            [
                "## Candidate Leads",
                "",
            ]
        )
        for probe in candidates:
            lines.append(
                f"- PR {probe.number}: {probe.url or 'missing URL'} has "
                f"{probe.review_threads} review-thread(s)."
            )
    else:
        lines.extend(
            [
                "## Negative Finding",
                "",
                "- No scanned merged PR exposed review-thread inventory.",
                "- A green CI run without review-thread detail is insufficient for `real_inventory` promotion.",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def scan_hosted_pr_repair_candidates(
    *,
    repo: str = DEFAULT_REPO,
    limit: int = DEFAULT_LIMIT,
    input_pr_list: Path | None = None,
    runner: CommandRunner = _run_command,
) -> list[HostedPrProbe]:
    if input_pr_list is not None:
        payload = _load_json_file(input_pr_list)
        if not isinstance(payload, list):
            raise HostedPrRepairCandidateScanError("input PR list JSON must be a list")
        prs = [item for item in payload if isinstance(item, Mapping)]

        def lookup_from_item(item: Mapping[str, object]) -> Mapping[str, object]:
            return {
                "review_threads": item.get("reviewThreads")
                or item.get("review_threads")
                or (item.get("reviewThreadCount") if "reviewThreadCount" in item else 0),
                "comments": item.get("comments") or item.get("comment_count") or 0,
                "reviews": item.get("reviews") or item.get("review_count") or 0,
            }

        return probe_prs(prs, lookup=lookup_from_item)

    prs = merged_prs_from_gh(repo, limit=limit, runner=runner)

    def lookup_from_gh(item: Mapping[str, object]) -> Mapping[str, object]:
        return review_thread_counts_from_gh(repo, _int_value(item.get("number")), runner=runner)

    return probe_prs(prs, lookup=lookup_from_gh)


def write_report(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan merged PRs for hosted repair review-thread candidate inventory."
    )
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--input-pr-list", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        probes = scan_hosted_pr_repair_candidates(
            repo=args.repo,
            limit=args.limit,
            input_pr_list=args.input_pr_list,
        )
    except HostedPrRepairCandidateScanError as exc:
        print(f"hosted PR repair candidate scan error: {exc}", file=sys.stderr)
        return 2
    markdown = render_report(probes, repo=args.repo)
    if not args.no_write:
        write_report(markdown, args.output)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": HOSTED_PR_REPAIR_CANDIDATE_SCAN_SCHEMA_VERSION,
                    "status": scan_status(probes),
                    "repo": args.repo,
                    "prs_scanned": len(probes),
                    "review_thread_candidates": sum(
                        1 for probe in probes if probe.has_review_thread_inventory
                    ),
                    "path": str(args.output),
                    "written": not args.no_write,
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif args.no_write:
        print(markdown)
    else:
        print(f"Wrote {args.output}")
    return 0 if scan_status(probes) != "no_prs_scanned" else 1


if __name__ == "__main__":
    raise SystemExit(main())
