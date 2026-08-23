"""Optional Presidio PII redaction (``felix-harness[pii]``) with regex fallback."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from felix.observability.metrics import record_counter

logger = logging.getLogger("felix.governance.pii")

_REGEX_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "email"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
    # Was r"\b(?:\d[ -]*?){13,19}\b". That shape *looks* like catastrophic
    # backtracking — a lazy quantifier inside a bounded repetition — but it is not:
    # every repetition must consume a digit, so the search space stays small, and no
    # payload I could construct made it slow. This form is kept because it is easier to
    # read and obviously linear, not because the old one was exploitable. Detection is
    # equivalent on real card numbers, bare digit runs, and dash/space separated forms.
    (re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), "card"),
)

_analyzer = None
_anonymizer = None
_presidio_checked = False


@dataclass(slots=True)
class PiiResult:
    matched: bool
    text: str
    engine: str  # "presidio" | "regex"


def _spacy_model_ready() -> bool:
    try:
        import spacy

        for name in ("en_core_web_sm", "en_core_web_md", "en_core_web_lg"):
            if spacy.util.is_package(name):
                return True
    except Exception:
        return False
    return False


def _try_load_presidio() -> bool:
    """Load Presidio once, or report that the regex fallback is in force.

    Two things were wrong here. The degradation was announced at ``logger.debug`` —
    invisible at the INFO default — so an operator who configured
    ``guardrails: {providers: [pii]}`` on the lean image (which ships neither Presidio
    nor a spaCy model) silently got three regexes instead. And ``_presidio_checked`` was
    a permanent latch, so one *transient* init failure pinned the process to the regex
    path for its entire lifetime.
    """
    global _analyzer, _anonymizer, _presidio_checked
    if _presidio_checked:
        return _analyzer is not None
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine
    except ImportError:
        # Deterministic: the package is absent and will not appear at runtime, so this
        # result is safe to latch.
        _presidio_checked = True
        _warn_degraded("presidio extras not installed")
        return False
    # AnalyzerEngine downloads spaCy models by default — only enable when a
    # model is already present so CI / lean images stay on the regex path.
    if not _spacy_model_ready():
        _presidio_checked = True
        _warn_degraded("presidio present but no spaCy English model")
        return False
    try:
        _analyzer = AnalyzerEngine()
        _anonymizer = AnonymizerEngine()
        _presidio_checked = True
        return True
    except Exception:
        # Do NOT latch: engine init can fail transiently (memory pressure, a partial
        # model download). Latching here pins the process to regex for its lifetime.
        logger.error("presidio engine init failed; will retry on next use", exc_info=True)
        record_counter("felix_control_degraded", {"control": "pii", "reason": "init_failed"})
        _analyzer = None
        _anonymizer = None
        return False


def _warn_degraded(reason: str) -> None:
    """Announce the regex fallback once, loudly enough to be seen at INFO."""
    logger.warning(
        "PII guardrail degraded to the regex fallback (%s). Only email, US SSN, and "
        "card-like digit runs are detected. Install felix-harness[pii] plus a spaCy "
        "English model for full coverage.",
        reason,
    )
    record_counter("felix_control_degraded", {"control": "pii", "reason": reason[:40]})


def presidio_active() -> bool:
    """Whether full PII analysis is in force (diagnostics, `felix doctor`)."""
    return _analyzer is not None


def _regex_redact(text: str) -> PiiResult:
    matched = False
    out = text
    for rx, kind in _REGEX_PATTERNS:
        if rx.search(out):
            matched = True
            out = rx.sub(f"[REDACTED:{kind}]", out)
    return PiiResult(matched=matched, text=out, engine="regex")


def redact_pii(text: str) -> PiiResult:
    """Redact PII from text. Prefer Presidio when available; always regex residual."""
    if not text:
        return PiiResult(matched=False, text=text, engine="regex")
    engine = "regex"
    out = text
    matched = False
    if _try_load_presidio() and _analyzer is not None and _anonymizer is not None:
        try:
            from presidio_anonymizer.entities import OperatorConfig

            results = _analyzer.analyze(text=text, language="en")
            if results:
                anonymized = _anonymizer.anonymize(
                    text=text,
                    analyzer_results=results,
                    operators={
                        "DEFAULT": OperatorConfig(
                            "replace",
                            {"new_value": "[REDACTED]"},
                        )
                    },
                )
                out = anonymized.text
                matched = True
                engine = "presidio"
        except Exception:
            logger.debug("presidio redact failed; falling back to regex", exc_info=True)
    residual = _regex_redact(out)
    if residual.matched:
        matched = True
        out = residual.text
        if engine == "presidio":
            engine = "presidio+regex"
    return PiiResult(matched=matched, text=out, engine=engine)


def reset_pii_engines_for_tests() -> None:
    """Clear cached engines (unit tests only)."""
    global _analyzer, _anonymizer, _presidio_checked
    _analyzer = None
    _anonymizer = None
    _presidio_checked = False


__all__ = ["PiiResult", "redact_pii", "reset_pii_engines_for_tests"]
