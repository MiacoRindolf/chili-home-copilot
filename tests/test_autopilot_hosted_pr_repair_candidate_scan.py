from __future__ import annotations

import json
import subprocess
import sys

from scripts import autopilot_hosted_pr_repair_candidate_scan as scan


def test_candidate_scan_reports_negative_when_no_review_threads():
    prs = [
        {
            "number": 282,
            "url": "https://github.com/MiacoRindolf/chili-home-copilot/pull/282",
            "title": "Gate weak stock momentum under queue pressure",
            "state": "MERGED",
            "mergedAt": "2026-06-03T11:12:06Z",
            "headRefOid": "6160d0f82d749fc04d0f74ea7030d2fd482b3e6d",
            "review_threads": 0,
            "comments": 0,
            "reviews": 0,
        }
    ]

    probes = scan.probe_prs(prs, lookup=lambda item: item)
    markdown = scan.render_report(
        probes,
        repo="MiacoRindolf/chili-home-copilot",
        generated_utc="2026-07-03T12:00:00Z",
    )

    assert scan.scan_status(probes) == "no_review_thread_candidates"
    assert "- Review-thread candidates: 0" in markdown
    assert "- Promotion impact: blocked" in markdown
    assert "green CI run without review-thread detail is insufficient" in markdown


def test_candidate_scan_surfaces_review_thread_candidate():
    prs = [
        {
            "number": 900,
            "url": "https://github.com/MiacoRindolf/chili-home-copilot/pull/900",
            "title": "Repair hosted PR review feedback",
            "state": "MERGED",
            "mergedAt": "2026-07-03T12:00:00Z",
            "headRefOid": "abc123",
        }
    ]

    probes = scan.probe_prs(
        prs,
        lookup=lambda _item: {"review_threads": 2, "comments": 1, "reviews": 1},
    )
    markdown = scan.render_report(
        probes,
        repo="MiacoRindolf/chili-home-copilot",
        generated_utc="2026-07-03T12:00:00Z",
    )

    assert scan.scan_status(probes) == "candidate_found"
    assert "- Review-thread candidates: 1" in markdown
    assert "- Promotion impact: candidate_leads_only" in markdown
    assert "PR 900" in markdown


def test_candidate_scan_cli_accepts_input_pr_list(tmp_path, capsys):
    input_path = tmp_path / "prs.json"
    output_path = tmp_path / "scan.md"
    input_path.write_text(
        json.dumps(
            [
                {
                    "number": 1,
                    "url": "https://github.com/o/r/pull/1",
                    "title": "one",
                    "reviewThreads": 1,
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = scan.main(
        [
            "--repo",
            "o/r",
            "--input-pr-list",
            str(input_path),
            "--output",
            str(output_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "candidate_found"
    assert payload["review_thread_candidates"] == 1
    assert output_path.is_file()


def test_candidate_scan_uses_gh_metadata_runner():
    calls: list[tuple[str, ...]] = []

    def fake_runner(args, _timeout):
        calls.append(tuple(args))
        if args[:3] == ("gh", "pr", "list"):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    [
                        {
                            "number": 123,
                            "url": "https://github.com/o/r/pull/123",
                            "title": "threaded",
                            "state": "MERGED",
                            "mergedAt": "2026-07-03T12:00:00Z",
                            "headRefOid": "abc123",
                        }
                    ]
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {"totalCount": 3},
                                "comments": {"totalCount": 2},
                                "reviews": {"totalCount": 1},
                            }
                        }
                    }
                }
            ),
            stderr="",
        )

    probes = scan.scan_hosted_pr_repair_candidates(
        repo="o/r",
        limit=1,
        runner=fake_runner,
    )

    assert scan.scan_status(probes) == "candidate_found"
    assert probes[0].review_threads == 3
    assert calls[0][:3] == ("gh", "pr", "list")
    assert calls[1][:3] == ("gh", "api", "graphql")


def test_run_command_replaces_undecodable_gh_output():
    result = scan._run_command(
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'pr title: valid\\x9d tail')",
        ),
        10,
    )

    assert result.returncode == 0
    assert "pr title: valid" in result.stdout
    assert "\ufffd" in result.stdout
