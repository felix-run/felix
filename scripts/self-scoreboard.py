#!/usr/bin/env python3
"""The scoreboard for Felix building Felix — numbers, not impressions.

Reads GitHub (read-only, through `gh api`) and, when `FELIX_BUILDER_URL` and
`FELIX_BUILDER_KEY` are set, the builder API's `/usage/summary`, and prints the metrics
`docs/SELF.md` defines as a markdown table with the graduation gate each rung waits on.
Nothing is posted; a person reads it and decides.

Stdlib only, like every script here, so it runs on a host with nothing but `gh` and Python.
The GitHub reads are one function, so a test can hand this file a fake and the metrics are
pure over its result. `ROWS` is the table `docs/SELF.md` carries; a test holds the two
together once both are on `main`.

    python3 scripts/self-scoreboard.py                 # last 28 days
    python3 scripts/self-scoreboard.py --days 7
    python3 scripts/self-scoreboard.py --json          # the same numbers for a machine
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

REPO = os.environ.get("FELIX_SELF_REPO_SLUG", "felix-run/felix")
BOT_LOGINS = frozenset({"felix-run-bot", "felix-run-bot[bot]"})
AUTHORED_LABEL = "felix:authored"
VERDICT_LABELS = frozenset({"felix:ready", "felix:needs-detail"})
# A person removing Felix's verdict counts as an override only this soon after it was given;
# a label cleaned up a month later is housekeeping.
OVERRIDE_WINDOW = timedelta(days=7)
# A smoke failure this soon after a Felix merge is charged to it.
REGRESSION_WINDOW = timedelta(hours=48)

# The accepted evidence shapes from docs/SELF.md, one regex each. The audit id is bounded by
# non-hex on both sides so a 40-hex commit sha does not contain one.
EVIDENCE_SHAPES = (
    re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])"),  # audit event id
    re.compile(r"github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+"),  # Actions run
    re.compile(r"\beval-run\s+[0-9a-f]{8,}"),  # eval run id
    re.compile(r"\bROADMAP\.md:\d+@[0-9a-f]{7,40}\b"),  # roadmap line at a commit
    re.compile(r"\busage:[\w-]+:\S+"),  # usage window
    re.compile(r"(?<![\w/])#\d+\b"),  # an issue a person filed
)
_EVIDENCE_HEADING = re.compile(r"(?im)^###\s+Evidence\s*$")
# The readiness check's comment opens with this line — docs/SELF.md, "The readiness check".
_READINESS_SCORE = re.compile(r"\breadiness\s+(\d)/8\b", re.I)
_THREAD = re.compile(r"(?im)^\s*Felix-Thread:\s*(\S+)\s*$")


def gh(path: str) -> Any:
    """One paginated read through `gh api`. The only thing here that touches the network."""
    cmd = ["gh", "api", path, "--header", "Accept: application/vnd.github+json", "--paginate", "--slurp"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return flatten_pages(json.loads(out) if out.strip() else [])


def flatten_pages(data: Any) -> Any:
    """`--slurp` yields a list of pages; a page is itself a list for list endpoints."""
    if isinstance(data, list) and data and isinstance(data[0], list):
        return [item for page in data for item in page]
    return data


def evidence_of(issue_body: str) -> str:
    """The text under the form's `### Evidence` heading, or the whole body when absent."""
    m = _EVIDENCE_HEADING.search(issue_body or "")
    if not m:
        return issue_body or ""
    rest = (issue_body or "")[m.end() :]
    return rest.split("\n###", 1)[0]


def has_evidence(issue_body: str) -> bool:
    text = evidence_of(issue_body)
    return any(p.search(text) for p in EVIDENCE_SHAPES)


def _labels(item: dict[str, Any]) -> set[str]:
    return {lb["name"] if isinstance(lb, dict) else str(lb) for lb in item.get("labels") or []}


def _login(item: dict[str, Any], key: str = "user") -> str:
    """The login under `user`, `actor` or `author` — GitHub names the same thing three ways."""
    return str((item.get(key) or {}).get("login") or "")


def _is_bot(item: dict[str, Any], key: str = "user") -> bool:
    return _login(item, key) in BOT_LOGINS


def _bot_authored(pr: dict[str, Any]) -> bool:
    """By login or by label: the label is what a person applies when the loop's identity changes."""
    return _is_bot(pr) or AUTHORED_LABEL in _labels(pr)


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _median(values: Iterable[float]) -> float | None:
    vals = list(values)
    return statistics.median(vals) if vals else None


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def collect(days: int, fetch: Callable[[str], Any] = gh) -> dict[str, Any]:
    """Everything the metrics need, fetched once. `fetch` is `gh` or a test's fake."""
    cutoff = _since(days)
    since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    issues = [
        i
        for i in fetch(f"repos/{REPO}/issues?state=all&since={since}&per_page=100")
        if "pull_request" not in i
    ]
    pulls = fetch(f"repos/{REPO}/pulls?state=all&sort=updated&direction=desc&per_page=100")
    bot_pulls = []
    for p in pulls:
        created = _parse(p.get("created_at"))
        if _bot_authored(p) and created is not None and created >= cutoff:
            bot_pulls.append(p)
    detail: dict[int, dict[str, Any]] = {}
    for p in bot_pulls:
        n = p["number"]
        detail[n] = {
            "commits": fetch(f"repos/{REPO}/pulls/{n}/commits"),
            "reviews": fetch(f"repos/{REPO}/pulls/{n}/reviews"),
        }
    timelines: dict[int, list[dict[str, Any]]] = {}
    comments: dict[int, list[dict[str, Any]]] = {}
    for i in issues:
        n = i["number"]
        is_task = "felix:task" in _labels(i)
        if is_task or _is_bot(i):
            timelines[n] = fetch(f"repos/{REPO}/issues/{n}/timeline")
        if is_task:
            comments[n] = fetch(f"repos/{REPO}/issues/{n}/comments")
    smoke = fetch("repos/" + REPO + "/actions/workflows/smoke.yml/runs?per_page=100")
    smoke_runs = smoke.get("workflow_runs", smoke) if isinstance(smoke, dict) else smoke
    return {
        "issues": issues,
        "bot_pulls": bot_pulls,
        "detail": detail,
        "timelines": timelines,
        "comments": comments,
        "smoke_runs": list(smoke_runs or []),
    }


def _label_events(timelines: dict[int, list[dict[str, Any]]]) -> tuple[int, int]:
    """(priority labels the bot applied, verdicts a person removed within the window).

    Priority is a person's: every `labeled p*` by the bot is a violation. A verdict the bot
    gave and a person removed within `OVERRIDE_WINDOW` is an override of the readiness check.
    """
    violations = 0
    overrides = 0
    for events in timelines.values():
        given: dict[str, datetime] = {}
        for ev in events:
            label = str((ev.get("label") or {}).get("name") or "")
            when = _parse(ev.get("created_at"))
            if ev.get("event") == "labeled":
                if re.fullmatch(r"p[123]", label) and _is_bot(ev, "actor"):
                    violations += 1
                if label in VERDICT_LABELS and _is_bot(ev, "actor") and when is not None:
                    given[label] = when
            elif ev.get("event") == "unlabeled" and label in VERDICT_LABELS and not _is_bot(ev, "actor"):
                gave = given.get(label)
                if gave is not None and when is not None and when - gave <= OVERRIDE_WINDOW:
                    overrides += 1
    return violations, overrides


def _first_readiness_scores(comments: dict[int, list[dict[str, Any]]]) -> list[int]:
    """The score in the bot's first *readiness* comment on each task — not its first comment."""
    scores: list[int] = []
    for cs in comments.values():
        for c in cs:
            if not _is_bot(c):
                continue
            m = _READINESS_SCORE.search(c.get("body") or "")
            if m:
                scores.append(int(m.group(1)))
                break
    return scores


def _every_commit_is_the_bots(commits: list[dict[str, Any]]) -> bool:
    return all(_is_bot(c, "author") for c in commits)


def _rounds(reviews: list[dict[str, Any]]) -> int:
    """Distinct commits that drew an approve or a changes-requested: one round per head reviewed."""
    return len({r.get("commit_id") for r in reviews if r.get("state") in {"CHANGES_REQUESTED", "APPROVED"}})


def _pull_outcomes(pulls: list[dict[str, Any]], detail: dict[int, dict[str, Any]]) -> dict[str, Any]:
    merged = [p for p in pulls if p.get("merged_at")]
    closed_unmerged = [p for p in pulls if p.get("state") == "closed" and not p.get("merged_at")]
    clean = [p for p in merged if _every_commit_is_the_bots(detail[p["number"]]["commits"])]
    rounds = [_rounds(detail[p["number"]]["reviews"]) for p in merged]
    with_thread = [p for p in pulls if _THREAD.search(p.get("body") or "")]
    return {
        "bot_pulls": len(pulls),
        "merged": len(merged),
        "merge_pct": _pct(len(merged), len(pulls)),
        "rework_pct": _pct(len(closed_unmerged), len(pulls)),
        "merged_without_human_commits_pct": _pct(len(clean), len(merged)),
        "review_rounds_median": _median(rounds),
        "pulls_with_thread": len(with_thread),
        "pulls_missing_contract": sorted(p["number"] for p in pulls if p not in with_thread),
    }


def _regressions(pulls: list[dict[str, Any]], smoke_runs: list[dict[str, Any]]) -> int:
    """Smoke failures that started within `REGRESSION_WINDOW` after a Felix merge."""
    merges = [m for m in (_parse(p.get("merged_at")) for p in pulls) if m is not None]
    count = 0
    for run in smoke_runs:
        if run.get("conclusion") != "failure":
            continue
        started = _parse(run.get("run_started_at") or run.get("created_at"))
        if started is None:
            continue
        if any(timedelta(0) <= started - m <= REGRESSION_WINDOW for m in merges):
            count += 1
    return count


def metrics(data: dict[str, Any]) -> dict[str, Any]:
    issues = data["issues"]
    bot_issues = [i for i in issues if _is_bot(i)]
    with_evidence = [i for i in bot_issues if has_evidence(i.get("body") or "")]
    meta = [i for i in bot_issues if "felix:meta" in _labels(i)]
    violations, overrides = _label_events(data["timelines"])
    return {
        "window_issues": len(issues),
        "bot_issues": len(bot_issues),
        "evidence_cited_pct": _pct(len(with_evidence), len(bot_issues)),
        "meta_work_pct": _pct(len(meta), len(bot_issues)),
        "human_priority_violations": violations,
        "readiness_first_check_median": _median(_first_readiness_scores(data["comments"])),
        "verdict_overrides": overrides,
        "tasks": sum(1 for i in issues if "felix:task" in _labels(i)),
        **_pull_outcomes(data["bot_pulls"], data["detail"]),
        "regressions": _regressions(data["bot_pulls"], data.get("smoke_runs") or []),
    }


# (label, key, threshold text, gate) — the table docs/SELF.md carries. Edit both.
ROWS = (
    ("Evidence-cited issues (%)", "evidence_cited_pct", "≥ 90", "rung 0"),
    ("Meta-work ratio (%)", "meta_work_pct", "≤ 20", ""),
    ("Human-priority violations", "human_priority_violations", "0", ""),
    ("Readiness at first check (median /8)", "readiness_first_check_median", "≥ 6 human / 8 Felix", ""),
    ("Verdict overrides", "verdict_overrides", "≤ 2 in 10", "rung 1"),
    ("Felix PRs opened", "bot_pulls", "—", ""),
    ("Merge rate (%)", "merge_pct", "≥ 60 over 10", "rung 3"),
    ("Rework rate (% closed unmerged)", "rework_pct", "≤ 30", ""),
    ("Merged without human commits (%)", "merged_without_human_commits_pct", "≥ 50", ""),
    ("Review rounds (median)", "review_rounds_median", "≤ 2", ""),
    ("Regressions (smoke failures within 48 h of a Felix merge)", "regressions", "0", "rung 3"),
    ("PRs missing the contract", "pulls_missing_contract", "none", ""),
)


def builder_cost(days: int) -> dict[str, Any] | None:
    """Cost per manifest from the builder API, when a person pointed this at one."""
    base = os.environ.get("FELIX_BUILDER_URL", "").rstrip("/")
    key = os.environ.get("FELIX_BUILDER_KEY", "")
    if not base or not key:
        return None
    since_ms = int(_since(days).timestamp() * 1000)
    url = f"{base}/usage/summary?since_ms={since_ms}"
    req = urllib.request.Request(url, headers={"authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _usage(t: dict[str, Any]) -> str:
    return (
        f"cost_usd={t.get('cost_usd')} tokens_in={t.get('tokens_input')} tokens_out={t.get('tokens_output')}"
    )


def _shown(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, list):
        return ", ".join(f"#{n}" for n in v) if v else "none"
    return str(v)


def render(m: dict[str, Any], cost: dict[str, Any] | None, days: int) -> str:
    head = f"## Self-build scoreboard — last {days} days"
    lines = [head, "", "| Metric | Value | First threshold | Gate |", "|---|---|---|---|"]
    for label, key, threshold, gate in ROWS:
        lines.append(f"| {label} | {_shown(m.get(key))} | {threshold} | {gate} |")
    if cost:
        lines += ["", f"Builder usage since {cost.get('since_ms')}: {_usage(cost.get('totals') or {})}"]
    tail = f"{m['bot_issues']} issues and {m['bot_pulls']} pull requests by the bot in the window"
    lines += ["", f"{tail}; {m['tasks']} felix:task issues total."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--json", action="store_true", help="print the metrics as JSON instead of a table")
    args = ap.parse_args(argv)
    data = collect(args.days)
    m = metrics(data)
    cost = None
    try:
        cost = builder_cost(args.days)
    except Exception as exc:  # the builder is optional; say so rather than fail the board
        print(f"builder usage unavailable: {exc}", file=sys.stderr)
    if args.json:
        print(json.dumps({"days": args.days, "metrics": m, "builder": cost}, indent=2, default=str))
    else:
        print(render(m, cost, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
