"""Optional Presidio PII redaction (``felix-harness[pii]``) with regex fallback."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("felix.governance.pii")

_REGEX_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "email"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "card"),
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
    global _analyzer, _anonymizer, _presidio_checked
    if _presidio_checked:
        return _analyzer is not None
    _presidio_checked = True
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine
    except ImportError:
        logger.debug("presidio extras not installed; using regex PII fallback")
        return False
    # AnalyzerEngine downloads spaCy models by default — only enable when a
    # model is already present so CI / lean images stay on the regex path.
    if not _spacy_model_ready():
        logger.debug("presidio present but no spaCy English model; regex fallback")
        return False
    try:
        _analyzer = AnalyzerEngine()
        _anonymizer = AnonymizerEngine()
        return True
    except Exception:
        logger.debug("presidio engine init failed", exc_info=True)
        _analyzer = None
        _anonymizer = None
        return False


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
