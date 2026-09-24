"""`scripts/self-scoreboard.py` — the metrics are pure over what `gh api` returned.

The fetch is one function handed in, so these hand it a fake repository — matched on the
exact path, query string stripped, unknown paths raising — and read the numbers off. Each
row of the table in docs/SELF.md has a case that can go the wrong way.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests._scripts import load_script

board = load_script("self-scoreboard")
ROOT = Path(__file__).resolve().parents[2]
BOT = {"login": "felix-run-bot"}
APP = {"login": "felix-run-bot[bot]"}
HUMAN = {"login": "blakebauman"}
NOW = datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue(n: int, *, user: dict, labels: list[str], body: str) -> dict:
    return {"number": n, "user": user, "labels": [{"name": lb} for lb in labels], "body": body}


def _pr(
    n: int,
    *,
    user: dict,
    state: str,
    merged_at: str | None,
    body: str = "Felix-Thread: t\n",
    labels: list[str] | None = None,
    created_at: str | None = None,
) -> dict:
    return {
        "number": n,
        "user": user,
        "state": state,
        "merged_at": merged_at,
        "body": body,
        "labels": [{"name": lb} for lb in labels or []],
        "created_at": created_at or _iso(NOW - timedelta(days=1)),
    }


def _fake(repo: dict[str, object]):
    """Exact path match after stripping the query; an unknown path is a test bug, not a 404."""
    prefix = f"repos/{board.REPO}/"

    def fetch(path: str):
        key = path.split("?", 1)[0]
        assert key.startswith(prefix), path
        key = key[len(prefix) :]
        if key in repo:
            return repo[key]
        if key in {"issues", "pulls"} or key.endswith(("/commits", "/reviews", "/timeline", "/comments")):
            return []
        if key == "actions/workflows/smoke.yml/runs":
            return {"workflow_runs": []}
        raise AssertionError(f"the script fetched a path this fake does not know: {path}")

    return fetch


def _metrics(repo: dict[str, object], days: int = 28) -> dict:
    return board.metrics(board.collect(days, fetch=_fake(repo)))


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ("### Evidence\n\nROADMAP.md:214@3bc66e1\n\n### Outcome\n\nx", True),
        ("### Evidence\n\naudit 3f9c3f9c3f9c3f9c3f9c3f9c3f9c3f9c\n", True),
        ("### Evidence\n\nhttps://github.com/felix-run/felix/actions/runs/1234\n", True),
        ("### Evidence\n\neval-run 9b1d9b1d9b1d\n", True),
        ("### Evidence\n\nusage:contributor:2026-09-08..2026-09-14\n", True),
        ("### Evidence\n\n#124\n", True),
        ("### Evidence\n\nI read the file and it looked wrong\n", False),
        ("### Evidence\n\n_No response_\n\n### Outcome\n\nsee #12\n", False),
        # A 40-hex commit sha is not an audit id, though it contains 32 hex characters.
        ("### Evidence\n\ncommit 3bc66e13bc66e13bc66e13bc66e13bc66e13bc6\n", False),
    ],
)
def test_evidence_shapes(body: str, ok: bool) -> None:
    assert board.has_evidence(body) is ok


def test_evidence_cited_and_meta_ratio_count_only_the_bot() -> None:
    """A person's issue with evidence and `felix:meta` must move neither numerator."""
    repo = {
        "issues": [
            _issue(1, user=BOT, labels=["felix:task"], body="### Evidence\n\nROADMAP.md:1@abcdef1\n"),
            _issue(2, user=APP, labels=["felix:task", "felix:meta"], body="### Evidence\n\nI read it\n"),
            _issue(3, user=HUMAN, labels=["bug", "felix:meta"], body="### Evidence\n\n#12\n"),
            # The issues endpoint returns pull requests too; they are not issues.
            {**_issue(4, user=BOT, labels=[], body="### Evidence\n\n#1\n"), "pull_request": {"url": "x"}},
        ],
    }
    m = _metrics(repo)
    assert m["window_issues"] == 3
    assert m["bot_issues"] == 2, "both bot spellings, no person, no pull request"
    assert m["evidence_cited_pct"] == 50.0
    assert m["meta_work_pct"] == 50.0
    assert m["tasks"] == 2


def test_the_window_excludes_old_pull_requests_and_ones_with_no_date() -> None:
    repo = {
        "pulls": [
            _pr(1, user=BOT, state="open", merged_at=None, created_at=_iso(NOW - timedelta(days=40))),
            _pr(2, user=BOT, state="open", merged_at=None, created_at=_iso(NOW - timedelta(days=2))),
            {**_pr(3, user=BOT, state="open", merged_at=None), "created_at": None},
        ],
    }
    assert _metrics(repo, days=28)["bot_pulls"] == 1
    assert _metrics(repo, days=60)["bot_pulls"] == 2


def test_a_priority_label_applied_by_the_bot_is_a_violation() -> None:
    t0 = NOW - timedelta(days=3)
    repo = {
        "issues": [_issue(1, user=BOT, labels=["felix:task"], body="")],
        "issues/1/timeline": [
            {"event": "labeled", "label": {"name": "p1"}, "actor": BOT, "created_at": _iso(t0)},
            {"event": "labeled", "label": {"name": "p3"}, "actor": APP, "created_at": _iso(t0)},
            {"event": "labeled", "label": {"name": "p2"}, "actor": HUMAN, "created_at": _iso(t0)},
        ],
    }
    assert _metrics(repo)["human_priority_violations"] == 2


def test_a_verdict_removed_by_a_person_within_a_week_is_an_override() -> None:
    t0 = NOW - timedelta(days=10)
    repo = {
        "issues": [
            _issue(1, user=HUMAN, labels=["felix:task"], body=""),
            _issue(2, user=HUMAN, labels=["felix:task"], body=""),
        ],
        "issues/1/timeline": [
            {"event": "labeled", "label": {"name": "felix:ready"}, "actor": BOT, "created_at": _iso(t0)},
            {
                "event": "unlabeled",
                "label": {"name": "felix:ready"},
                "actor": HUMAN,
                "created_at": _iso(t0 + timedelta(days=1)),
            },
            {
                "event": "labeled",
                "label": {"name": "felix:needs-detail"},
                "actor": BOT,
                "created_at": _iso(t0),
            },
            # The bot changing its own verdict is not an override.
            {
                "event": "unlabeled",
                "label": {"name": "felix:needs-detail"},
                "actor": BOT,
                "created_at": _iso(t0 + timedelta(days=1)),
            },
        ],
        "issues/2/timeline": [
            {"event": "labeled", "label": {"name": "felix:ready"}, "actor": BOT, "created_at": _iso(t0)},
            # Housekeeping a month later is not an override.
            {
                "event": "unlabeled",
                "label": {"name": "felix:ready"},
                "actor": HUMAN,
                "created_at": _iso(t0 + timedelta(days=30)),
            },
        ],
    }
    assert _metrics(repo)["verdict_overrides"] == 1


def test_readiness_score_is_read_from_the_bots_first_readiness_comment() -> None:
    repo = {
        "issues": [
            _issue(1, user=HUMAN, labels=["felix:task"], body=""),
            _issue(2, user=HUMAN, labels=["felix:task"], body=""),
        ],
        "issues/1/comments": [
            {"user": BOT, "body": "on it"},
            {"user": BOT, "body": "Readiness 5/8 — missing: acceptance"},
            {"user": BOT, "body": "Readiness 8/8"},
        ],
        "issues/2/comments": [{"user": HUMAN, "body": "ping"}, {"user": APP, "body": "readiness 7/8"}],
    }
    assert _metrics(repo)["readiness_first_check_median"] == 6


def test_pull_request_metrics() -> None:
    merged = _iso(NOW - timedelta(days=2))
    repo = {
        "pulls": [
            _pr(10, user=BOT, state="closed", merged_at=merged),
            _pr(11, user=APP, state="closed", merged_at=merged),
            # Labelled `felix:authored` by a person after the loop's identity changed.
            _pr(12, user=HUMAN, state="closed", merged_at=None, labels=["felix:authored"]),
            _pr(13, user=BOT, state="open", merged_at=None, body="no trailer"),
            _pr(14, user=HUMAN, state="closed", merged_at=merged),
        ],
        "pulls/10/commits": [
            {"author": BOT, "parents": [{"sha": "a"}]},
            # GitHub's "update branch" merge from main, pressed by a person: no change of its own.
            {"author": HUMAN, "parents": [{"sha": "b"}, {"sha": "c"}]},
        ],
        "pulls/11/commits": [
            {"author": APP, "parents": [{"sha": "d"}]},
            {"author": HUMAN, "parents": [{"sha": "e"}]},
        ],
        "pulls/10/reviews": [
            {"state": "COMMENTED", "commit_id": "a"},
            {"state": "CHANGES_REQUESTED", "commit_id": "a"},
            {"state": "APPROVED", "commit_id": "a"},
            {"state": "APPROVED", "commit_id": "b"},
        ],
        "pulls/11/reviews": [{"state": "APPROVED", "commit_id": "c"}],
    }
    m = _metrics(repo)
    assert m["bot_pulls"] == 4, "a person's unlabelled PR is not the bot's"
    assert m["merged"] == 2 and m["merge_pct"] == 50.0
    assert m["rework_pct"] == 25.0
    assert m["merged_without_human_commits_pct"] == 50.0, "a person's merge-from-main is not a human change"
    assert m["review_rounds_median"] == 1.5, "a round is a reviewed commit, not a comment"
    assert m["pulls_with_thread"] == 3
    assert m["pulls_missing_contract"] == [13]


def test_a_smoke_failure_soon_after_a_felix_merge_is_a_regression() -> None:
    merged = NOW - timedelta(days=3)
    repo = {
        "pulls": [_pr(10, user=BOT, state="closed", merged_at=_iso(merged))],
        "actions/workflows/smoke.yml/runs": {
            "workflow_runs": [
                {"conclusion": "failure", "run_started_at": _iso(merged + timedelta(hours=6))},
                {"conclusion": "success", "run_started_at": _iso(merged + timedelta(hours=12))},
                {"conclusion": "failure", "run_started_at": _iso(merged + timedelta(hours=72))},
                {"conclusion": "failure", "run_started_at": _iso(merged - timedelta(hours=1))},
            ]
        },
    }
    assert _metrics(repo)["regressions"] == 1


def test_flatten_pages_is_what_gh_slurp_needs() -> None:
    assert board.flatten_pages([[{"a": 1}], [{"b": 2}]]) == [{"a": 1}, {"b": 2}]
    assert board.flatten_pages([{"workflow_runs": []}]) == [{"workflow_runs": []}]
    assert board.flatten_pages([]) == []


def test_gh_reads_through_subprocess_and_flattens(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    class _Done:
        stdout = json.dumps([[{"n": 1}], [{"n": 2}]])

    def run(cmd, **kw):
        seen.append(cmd)
        return _Done()

    monkeypatch.setattr(board.subprocess, "run", run)
    assert board.gh("repos/x/y/issues") == [{"n": 1}, {"n": 2}]
    assert seen[0][:3] == ["gh", "api", "repos/x/y/issues"] and "--paginate" in seen[0]


def test_render_names_every_row_its_gate_and_the_list_branch() -> None:
    m = _metrics({})
    out = board.render(m, None, 28)
    for label, _key, _threshold, _gate in board.ROWS:
        assert label in out
    assert "rung 0" in out and "rung 1" in out and "rung 3" in out
    assert "| Merge rate (%) | n/a |" in out, "an empty window renders as n/a, not as 0 %"
    assert "| PRs missing the contract | none |" in out
    m["pulls_missing_contract"] = [13, 15]
    assert "| PRs missing the contract | #13, #15 |" in board.render(
        m, {"since_ms": 1, "totals": {"cost_usd": 2}}, 7
    )


def test_the_table_is_the_one_docs_self_md_carries() -> None:
    """`ROWS` and the scoreboard table in docs/SELF.md are two copies; this holds them equal.

    The spec lands in its own pull request; until it is on this branch the test is inert
    and says so, rather than asserting against a file that is not there.
    """
    spec = ROOT / "docs" / "SELF.md"
    if not spec.exists():
        pytest.skip("docs/SELF.md is not on this branch yet (it lands with the self-build spec)")
    text = spec.read_text(encoding="utf-8")
    section = text.split("## Scoreboard", 1)[1].split("\n## ", 1)[0]
    doc_rows = [
        re.split(r"\s*\|\s*", line.strip("| "))[0]
        for line in section.splitlines()
        if line.startswith("| ") and "|---" not in line
    ]
    missing = [label for label, *_ in board.ROWS if not any(label.split(" (")[0] in row for row in doc_rows)]
    assert missing == [], f"rows the script prints and docs/SELF.md does not define: {missing}"
