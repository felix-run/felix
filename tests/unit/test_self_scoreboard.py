"""`scripts/self-scoreboard.py` — the metrics are pure over what `gh api` returned.

The fetch is one function handed in, so these hand it a fake repository and read the
numbers off. Each case is one row of the table in docs/SELF.md.
"""

from __future__ import annotations

import pytest

from tests._scripts import load_script

board = load_script("self-scoreboard")
BOT = {"login": "felix-bot"}
HUMAN = {"login": "blakebauman"}


def _issue(n: int, *, user: dict, labels: list[str], body: str) -> dict:
    return {"number": n, "user": user, "labels": [{"name": lb} for lb in labels], "body": body}


def _pr(n: int, *, user: dict, state: str, merged_at: str | None, body: str = "Felix-Thread: t\n") -> dict:
    return {
        "number": n,
        "user": user,
        "state": state,
        "merged_at": merged_at,
        "body": body,
        "created_at": "2099-01-01T00:00:00Z",
    }


def _fake(repo: dict):
    def fetch(path: str, **_: object):
        for key, value in repo.items():
            if key in path:
                return value
        return []

    return fetch


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ("### Evidence\n\nROADMAP.md:214@3bc66e1\n\n### Outcome\n\nx", True),
        ("### Evidence\n\naudit 3f9c3f9c3f9c3f9c3f9c3f9c3f9c3f9c\n", True),
        ("### Evidence\n\nhttps://github.com/felix-run/felix/actions/runs/1234\n", True),
        ("### Evidence\n\nusage:contributor:2026-09-08..2026-09-14\n", True),
        ("### Evidence\n\n#124\n", True),
        ("### Evidence\n\nI read the file and it looked wrong\n", False),
        ("### Evidence\n\n_No response_\n\n### Outcome\n\nsee #12\n", False),
    ],
)
def test_evidence_shapes(body: str, ok: bool) -> None:
    assert board.has_evidence(body) is ok


def test_evidence_cited_and_meta_ratio_count_only_the_bot() -> None:
    repo = {
        "/issues?": [
            _issue(1, user=BOT, labels=["felix:task"], body="### Evidence\n\nROADMAP.md:1@abcdef1\n"),
            _issue(2, user=BOT, labels=["felix:task", "felix:meta"], body="### Evidence\n\nI read it\n"),
            _issue(3, user=HUMAN, labels=["bug"], body="nothing"),
        ],
        "/pulls?": [],
    }
    m = board.metrics(board.collect(28, fetch=_fake(repo)))
    assert m["bot_issues"] == 2
    assert m["evidence_cited_pct"] == 50.0
    assert m["meta_work_pct"] == 50.0
    assert m["tasks"] == 2


def test_a_priority_label_applied_by_the_bot_is_a_violation() -> None:
    repo = {
        "/issues?": [_issue(1, user=BOT, labels=["felix:task"], body="")],
        "/issues/1/timeline": [
            {"event": "labeled", "label": {"name": "p1"}, "actor": BOT},
            {"event": "labeled", "label": {"name": "felix:ready"}, "actor": BOT},
            {"event": "labeled", "label": {"name": "p2"}, "actor": HUMAN},
            {"event": "unlabeled", "label": {"name": "felix:ready"}, "actor": HUMAN},
        ],
        "/pulls?": [],
    }
    m = board.metrics(board.collect(28, fetch=_fake(repo)))
    assert m["human_priority_violations"] == 1
    assert m["verdict_overrides"] == 1


def test_readiness_score_is_read_from_the_bots_first_comment() -> None:
    repo = {
        "/issues?": [
            _issue(1, user=HUMAN, labels=["felix:task"], body=""),
            _issue(2, user=HUMAN, labels=["felix:task"], body=""),
        ],
        "/issues/1/comments": [
            {"user": BOT, "body": "Readiness 5/8 — missing: acceptance"},
            {"user": BOT, "body": "Readiness 8/8"},
        ],
        "/issues/2/comments": [{"user": HUMAN, "body": "ping"}, {"user": BOT, "body": "readiness 7/8"}],
        "/pulls?": [],
    }
    m = board.metrics(board.collect(28, fetch=_fake(repo)))
    assert m["readiness_first_check_median"] == 6


def test_pull_request_metrics() -> None:
    repo = {
        "/issues?": [],
        "/pulls?": [
            _pr(10, user=BOT, state="closed", merged_at="2099-01-02T00:00:00Z"),
            _pr(11, user=BOT, state="closed", merged_at="2099-01-02T00:00:00Z"),
            _pr(12, user=BOT, state="closed", merged_at=None),
            _pr(13, user=BOT, state="open", merged_at=None, body="no trailer"),
            _pr(14, user=HUMAN, state="closed", merged_at="2099-01-02T00:00:00Z"),
        ],
        "/pulls/10/commits": [{"author": BOT}],
        "/pulls/11/commits": [{"author": BOT}, {"author": HUMAN}],
        "/pulls/10/reviews": [
            {"state": "CHANGES_REQUESTED", "commit_id": "a"},
            {"state": "APPROVED", "commit_id": "b"},
        ],
        "/pulls/11/reviews": [{"state": "APPROVED", "commit_id": "c"}],
    }
    m = board.metrics(board.collect(28, fetch=_fake(repo)))
    assert m["bot_pulls"] == 4, "a person's PR is not the bot's"
    assert m["merged"] == 2 and m["merge_pct"] == 50.0
    assert m["rework_pct"] == 25.0
    assert m["merged_without_human_commits_pct"] == 50.0
    assert m["review_rounds_median"] == 1.5
    assert m["pulls_missing_contract"] == [13]


def test_render_names_every_row_and_its_gate() -> None:
    m = board.metrics(board.collect(28, fetch=_fake({"/issues?": [], "/pulls?": []})))
    out = board.render(m, None, 28)
    for label, _key, _threshold, _gate in board.ROWS:
        assert label in out
    assert "rung 0" in out and "rung 3" in out
    assert "n/a" in out, "an empty window renders as n/a, not as 0 %"
