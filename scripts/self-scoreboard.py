#!/usr/bin/env python3
"""The scoreboard for Felix building Felix — numbers, not impressions.

Reads GitHub (read-only, through `gh api`) and, when `FELIX_BUILDER_URL` and
`FELIX_BUILDER_KEY` are set, the builder API's `/usage/summary`, and prints the metrics
`docs/SELF.md` defines as a markdown table with the graduation gate each rung waits on.
Nothing is posted; a person reads it and decides.

Stdlib only, like every script here, so it runs on a host with nothing but `gh` and Python.
The GitHub reads are one function, so a test can hand this file a fake and the metrics are
pure over its result.

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
BOT_LOGINS = frozenset({"felix-bot", "felix-bot[bot]"})

# The accepted evidence shapes from docs/SELF.md, one regex each.
EVIDENCE_SHAPES = (
    re.compile(r"\b[0-9a-f]{32}\b"),  # audit event id
    re.compile(r"github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+"),  # Actions run
    re.compile(r"\beval-run\s+[0-9a-f]{8,}"),  # eval run id
    re.compile(r"\bROADMAP\.md:\d+@[0-9a-f]{7,40}\b"),  # roadmap line at a commit
    re.compile(r"\busage:[\w-]+:\S+"),  # usage window
    re.compile(r"(?<![\w/])#\d+\b"),  # an issue a person filed
)
_EVIDENCE_HEADING = re.compile(r"(?im)^###\s+Evidence\s*$")
_READINESS_SCORE = re.compile(r"\breadiness\s+(\d)/8\b", re.I)
_THREAD = re.compile(r"(?im)^\s*Felix-Thread:\s*(\S+)\s*$")


def gh(path: str, *, paginate: bool = True) -> Any:
    """One read through `gh api`. The only thing here that touches the network."""
    cmd = ["gh", "api", path, "--header", "Accept: application/vnd.github+json"]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    data = json.loads(out) if out.strip() else []
    if paginate and isinstance(data, list) and data and isinstance(data[0], list):
        data = [item for page in data for item in page]
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


def _login(item: dict[str, Any]) -> str:
    return str((item.get("user") or {}).get("login") or "")


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


def collect(days: int, fetch: Callable[..., Any] = gh) -> dict[str, Any]:
    """Everything the metrics need, fetched once. `fetch` is `gh` or a test's fake."""
    since = _since(days).strftime("%Y-%m-%dT%H:%M:%SZ")
    issues = [
        i
        for i in fetch(f"repos/{REPO}/issues?state=all&since={since}&per_page=100")
        if "pull_request" not in i
    ]
    pulls = fetch(f"repos/{REPO}/pulls?state=all&sort=updated&direction=desc&per_page=100")
    bot_pulls = [
        p
        for p in pulls
        if _login(p) in BOT_LOGINS and (_parse(p.get("created_at")) or _since(0)) >= _since(days)
    ]
    detail: dict[int, dict[str, Any]] = {}
    for p in bot_pulls:
        n = p["number"]
        detail[n] = {
            "commits": fetch(f"repos/{REPO}/pulls/{n}/commits"),
            "reviews": fetch(f"repos/{REPO}/pulls/{n}/reviews"),
        }
    timelines: dict[int, list[dict[str, Any]]] = {}
    for i in issues:
        if "felix:task" in _labels(i) or _login(i) in BOT_LOGINS:
            timelines[i["number"]] = fetch(f"repos/{REPO}/issues/{i['number']}/timeline")
    comments: dict[int, list[dict[str, Any]]] = {}
    for i in issues:
        if "felix:task" in _labels(i):
            comments[i["number"]] = fetch(f"repos/{REPO}/issues/{i['number']}/comments")
    return {
        "issues": issues,
        "bot_pulls": bot_pulls,
        "detail": detail,
        "timelines": timelines,
        "comments": comments,
    }


def metrics(data: dict[str, Any]) -> dict[str, Any]:
    issues = data["issues"]
    bot_issues = [i for i in issues if _login(i) in BOT_LOGINS]
    tasks = [i for i in issues if "felix:task" in _labels(i)]
    with_evidence = [i for i in bot_issues if has_evidence(i.get("body") or "")]
    meta = [i for i in bot_issues if "felix:meta" in _labels(i)]

    # Priority is a person's. Every `labeled p*` event by the bot is a violation.
    violations = 0
    overrides = 0
    for events in data["timelines"].values():
        for ev in events:
            if ev.get("event") == "labeled" and re.fullmatch(
                r"p[123]", (ev.get("label") or {}).get("name") or ""
            ):
                if str((ev.get("actor") or {}).get("login") or "") in BOT_LOGINS:
                    violations += 1
            # A person removing Felix's verdict is an override of the readiness check.
            if ev.get("event") == "unlabeled" and (ev.get("label") or {}).get("name") in {
                "felix:ready",
                "felix:needs-detail",
            }:
                if str((ev.get("actor") or {}).get("login") or "") not in BOT_LOGINS:
                    overrides += 1

    # Readiness at first check: the score in the bot's first comment on each task.
    first_scores: list[int] = []
    for cs in data["comments"].values():
        for c in cs:
            if _login(c) in BOT_LOGINS:
                m = _READINESS_SCORE.search(c.get("body") or "")
                if m:
                    first_scores.append(int(m.group(1)))
                break

    pulls = data["bot_pulls"]
    merged = [p for p in pulls if p.get("merged_at")]
    closed_unmerged = [p for p in pulls if p.get("state") == "closed" and not p.get("merged_at")]
    without_human_commits = [
        p
        for p in merged
        if all(
            str(((c.get("author") or {}) or {}).get("login") or "") in BOT_LOGINS
            for c in data["detail"][p["number"]]["commits"]
        )
    ]
    review_rounds = [
        len(
            {
                r.get("commit_id")
                for r in data["detail"][p["number"]]["reviews"]
                if r.get("state") in {"CHANGES_REQUESTED", "APPROVED"}
            }
        )
        for p in merged
    ]
    with_thread = [p for p in pulls if _THREAD.search(p.get("body") or "")]
    contract_missing = [p["number"] for p in pulls if not _THREAD.search(p.get("body") or "")]

    return {
        "window_issues": len(issues),
        "bot_issues": len(bot_issues),
        "evidence_cited_pct": _pct(len(with_evidence), len(bot_issues)),
        "meta_work_pct": _pct(len(meta), len(bot_issues)),
        "human_priority_violations": violations,
        "readiness_first_check_median": _median(first_scores),
        "verdict_overrides": overrides,
        "tasks": len(tasks),
        "bot_pulls": len(pulls),
        "merged": len(merged),
        "merge_pct": _pct(len(merged), len(pulls)),
        "rework_pct": _pct(len(closed_unmerged), len(pulls)),
        "merged_without_human_commits_pct": _pct(len(without_human_commits), len(merged)),
        "review_rounds_median": _median(review_rounds),
        "pulls_with_thread": len(with_thread),
        "pulls_missing_contract": contract_missing,
    }


# (label, key, threshold text, gate) — the table docs/SELF.md carries, in one place a person edits.
ROWS = (
    ("Evidence-cited issues %", "evidence_cited_pct", "≥ 90", "rung 0"),
    ("Meta-work ratio %", "meta_work_pct", "≤ 20", ""),
    ("Human-priority violations", "human_priority_violations", "0", ""),
    ("Readiness at first check (median /8)", "readiness_first_check_median", "≥ 6 human / 8 Felix", ""),
    ("Verdict overrides", "verdict_overrides", "≤ 2 in 10", "rung 1"),
    ("Felix PRs opened", "bot_pulls", "—", ""),
    ("Merge %", "merge_pct", "≥ 60 over 10", "rung 3"),
    ("Rework % (closed unmerged)", "rework_pct", "≤ 30", ""),
    ("Merged without human commits %", "merged_without_human_commits_pct", "≥ 50", ""),
    ("Review rounds (median)", "review_rounds_median", "≤ 2", ""),
    ("PRs missing the contract", "pulls_missing_contract", "none", "rung 3"),
)


def builder_cost(days: int) -> dict[str, Any] | None:
    """Cost per manifest from the builder API, when a person pointed this at one."""
    base = os.environ.get("FELIX_BUILDER_URL", "").rstrip("/")
    key = os.environ.get("FELIX_BUILDER_KEY", "")
    if not base or not key:
        return None
    since_ms = int(_since(days).timestamp() * 1000)
    req = urllib.request.Request(
        f"{base}/usage/summary?since_ms={since_ms}", headers={"authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _usage(t: dict[str, Any]) -> str:
    return (
        f"cost_usd={t.get('cost_usd')} tokens_in={t.get('tokens_input')} tokens_out={t.get('tokens_output')}"
    )


def render(m: dict[str, Any], cost: dict[str, Any] | None, days: int) -> str:
    lines = [
        f"## Self-build scoreboard — last {days} days",
        "",
        "| Metric | Value | First threshold | Gate |",
        "|---|---|---|---|",
    ]
    for label, key, threshold, gate in ROWS:
        v = m.get(key)
        shown = (
            "n/a"
            if v is None
            else (
                ", ".join(f"#{n}" for n in v)
                if isinstance(v, list) and v
                else ("none" if isinstance(v, list) else str(v))
            )
        )
        lines.append(f"| {label} | {shown} | {threshold} | {gate} |")
    if cost:
        totals = cost.get("totals") or {}
        lines += [
            "",
            f"Builder usage since {cost.get('since_ms')}: {_usage(totals)}",
        ]
    lines += [
        "",
        f"{m['bot_issues']} issues and {m['bot_pulls']} pull requests by the bot in the window; "
        f"{m['tasks']} felix:task issues total.",
    ]
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
