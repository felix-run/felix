"""A heuristic security scan of a skill bundle: pass, advisory, or fail, plus a score.

Six families of finding — embedded credentials, risky script constructs, prompt-injection
phrasing, obfuscation, URLs to executables or piped remote content, and oversized files —
and a low-severity note of any egress a `plugin.json` declares. Any critical or high
finding fails the scan; a medium one makes it advisory.

The patterns are this module's own rather than `felix.secrets` or
`felix.governance.content_screening`: the first matches a whole value (is this setting a
literal credential?) rather than searching text, and the second is tuned for tool output,
where `system prompt:` is an attack — in a skill about prompts it is a heading.

The scan's cost is bounded by construction: a file over `MAX_FILE_CHARS` is reported for
its size and not pattern-matched, and no pattern has an unbounded wildcard, so a long line
cannot make one backtrack quadratically. The original's `curl .* | sh` became
`curl [^\\n]{0,500} | sh`: a pipe further than 500 characters along the line is missed.
Ported from Skillist's `skill-format` package (MIT); see NOTICE.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from felix.skills.binary import is_binary_asset_path
from felix.skills.plugin import parse_plugin_manifest

Severity = Literal["low", "medium", "high", "critical"]
ScanStatus = Literal["pass", "advisory", "fail"]


@dataclass(slots=True, frozen=True)
class SecurityIssue:
    severity: Severity
    path: str
    message: str
    rule_id: str | None = None


@dataclass(slots=True, frozen=True)
class SecurityScanResult:
    status: ScanStatus
    issues: list[SecurityIssue]
    score: int


@dataclass(slots=True, frozen=True)
class _Rule:
    rule_id: str
    pattern: re.Pattern[str]
    message: str
    severity: Severity


# Every family is (rule id, pattern, message, severity); the severity is the family's
# except for scripts, where it varies by construct.
CREDENTIAL_RULES: tuple[_Rule, ...] = (
    _Rule("cred-aws", re.compile(r"AKIA[0-9A-Z]{16}"), "Possible AWS access key", "critical"),
    _Rule("cred-stripe", re.compile(r"sk_live_[a-zA-Z0-9]+"), "Possible Stripe live secret", "critical"),
    _Rule(
        "cred-ghp", re.compile(r"ghp_[a-zA-Z0-9]{36}"), "Possible GitHub personal access token", "critical"
    ),
    _Rule("cred-slack", re.compile(r"xox[baprs]-[a-zA-Z0-9-]+"), "Possible Slack token", "critical"),
    _Rule(
        "cred-pem",
        re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "Private key material detected",
        "critical",
    ),
    _Rule("cred-openai", re.compile(r"sk-[a-zA-Z0-9]{20,}"), "Possible OpenAI-style API key", "critical"),
)

SCRIPT_RULES: tuple[_Rule, ...] = (
    _Rule("script-eval", re.compile(r"\beval\s*\("), "eval() usage", "high"),
    _Rule("script-child-process", re.compile(r"child_process"), "child_process usage", "medium"),
    _Rule("script-exec", re.compile(r"\bexec\s*\("), "exec() usage", "medium"),
    _Rule("script-rm-rf", re.compile(r"rm\s+-rf\s+/"), "Destructive rm -rf / pattern", "critical"),
    _Rule(
        "script-pipe-bash",
        re.compile(r"curl\s+[^\n]{0,500}\|\s*(ba)?sh"),
        "Remote script piped to shell",
        "critical",
    ),
    _Rule(
        "script-wget-sh",
        re.compile(r"wget\s+[^\n]{0,500}\|\s*(ba)?sh"),
        "Remote script piped to shell",
        "critical",
    ),
    _Rule(
        "script-base64-exec",
        re.compile(r"base64\s+(-d|--decode)[^\n]{0,500}\|"),
        "Base64-decoded payload execution",
        "high",
    ),
)

PROMPT_INJECTION_RULES: tuple[_Rule, ...] = (
    _Rule(
        "pi-ignore",
        re.compile(r"ignore (all )?(previous|prior) instructions", re.IGNORECASE),
        "Possible prompt-injection: ignore previous instructions",
        "high",
    ),
    _Rule(
        "pi-disregard",
        re.compile(r"disregard (your|the) (system|safety)", re.IGNORECASE),
        "Possible prompt-injection: disregard system/safety",
        "high",
    ),
    _Rule(
        "pi-roleplay",
        re.compile(r"you are now (in )?(DAN|developer mode|unrestricted)", re.IGNORECASE),
        "Possible prompt-injection: role override",
        "high",
    ),
    _Rule(
        "pi-exfil",
        re.compile(r"send (all |the )?(secrets?|credentials?|api keys?) (to|via)", re.IGNORECASE),
        "Possible data-exfiltration instruction",
        "high",
    ),
)

OBFUSCATION_RULES: tuple[_Rule, ...] = (
    _Rule(
        "obf-long-hex",
        re.compile(r"\\x[0-9a-f]{2}(\\x[0-9a-f]{2}){20,}", re.IGNORECASE),
        "Long hex-escaped sequence (possible obfuscation)",
        "high",
    ),
    _Rule(
        "obf-fromcharcode",
        re.compile(r"String\.fromCharCode\s*\(\s*\d+(\s*,\s*\d+){10,}"),
        "String.fromCharCode obfuscation pattern",
        "high",
    ),
)

_SCRIPT_EXT_RE = re.compile(r"\.(sh|bash|py|js|mjs|cjs|ts)\Z")
_URL_RE = re.compile(r"https?://[^\s)\"']+")
_LOCAL_URL_RE = re.compile(r"localhost|127\.0\.0\.1|0\.0\.0\.0")
_EXECUTABLE_URL_RE = re.compile(r"\.(zip|exe|dmg|pkg|sh|bat)(\?|\Z)", re.IGNORECASE)
_RAW_CONTENT_HOST_RE = re.compile(r"raw\.githubusercontent\.com|gist\.githubusercontent\.com", re.IGNORECASE)
MAX_FILE_CHARS = 512_000
_PENALTY: dict[Severity, int] = {"critical": 40, "high": 25, "medium": 10, "low": 3}


def _matches(rules: tuple[_Rule, ...], path: str, content: str) -> list[SecurityIssue]:
    return [
        SecurityIssue(severity=r.severity, path=path, message=r.message, rule_id=r.rule_id)
        for r in rules
        if r.pattern.search(content)
    ]


def _url_issues(path: str, content: str) -> list[SecurityIssue]:
    issues: list[SecurityIssue] = []
    for url in _URL_RE.findall(content):
        if _LOCAL_URL_RE.search(url):
            continue
        if _EXECUTABLE_URL_RE.search(url):
            issues.append(
                SecurityIssue("medium", path, f"External executable URL: {url[:80]}", "url-executable")
            )
        if _RAW_CONTENT_HOST_RE.search(url) and "|" in content:
            issues.append(
                SecurityIssue(
                    "medium",
                    path,
                    "Fetches remote content that may be piped to a shell",
                    "url-remote-pipe-risk",
                )
            )
    return issues


def _file_issues(path: str, content: str) -> list[SecurityIssue]:
    if len(content) > MAX_FILE_CHARS:
        # Reported, not pattern-matched: the size is the finding, and bounding the scan's
        # cost by the file's matters more than what a regex would find in it.
        return [
            SecurityIssue("medium", path, "File exceeds 512KB — unusually large for a skill", "size-large")
        ]
    issues = _matches(CREDENTIAL_RULES, path, content)
    if path.startswith("scripts/") or _SCRIPT_EXT_RE.search(path):
        issues += _matches(SCRIPT_RULES, path, content)
    issues += _matches(PROMPT_INJECTION_RULES, path, content)
    issues += _matches(OBFUSCATION_RULES, path, content)
    issues += _url_issues(path, content)
    return issues


def scan_skill_security(files: Mapping[str, str]) -> SecurityScanResult:
    """The heuristic baseline scorer — always available, no external dependency."""
    issues: list[SecurityIssue] = []
    for path, content in files.items():
        # Base64 asset text is not source; pattern-matching it only finds false positives.
        if not is_binary_asset_path(path):
            issues += _file_issues(path, content)

    # A declared egress allowlist is surfaced, never blocking: whether a skill should reach
    # a host is a reviewer's judgement, not a mechanical fail.
    plugin_raw = files.get("plugin.json")
    manifest = parse_plugin_manifest(plugin_raw) if plugin_raw else None
    hosts = (manifest.network.allowed_hosts if manifest and manifest.network else None) or []
    if hosts:
        issues.append(
            SecurityIssue(
                "low",
                "plugin.json",
                f"Declares outbound network access to: {', '.join(hosts)}",
                "network-egress-declared",
            )
        )

    severities = {i.severity for i in issues}
    status: ScanStatus = (
        "fail" if severities & {"critical", "high"} else "advisory" if "medium" in severities else "pass"
    )
    score = 100 - sum(_PENALTY[i.severity] for i in issues)
    return SecurityScanResult(status=status, issues=issues, score=max(0, min(100, score)))


__all__ = [
    "CREDENTIAL_RULES",
    "OBFUSCATION_RULES",
    "PROMPT_INJECTION_RULES",
    "SCRIPT_RULES",
    "SecurityIssue",
    "SecurityScanResult",
    "scan_skill_security",
]
