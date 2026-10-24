"""Guards for Ella conversation summary write-back."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

ELLA_PREFIX = "[Ella] "
MIN_OVERVIEW_CHARS = 40

_RAW_SCANNER_COUNT_RE = re.compile(
    r"\b(?:scanner\s+)?(?:picked\s+up|reported|flagged|found|detected)?\s*\d+\s+"
    r"(?:escalations?|events?|detections?|flags?|segments?)\b",
    re.IGNORECASE,
)
_RAW_SCANNER_CATEGORY_RE = re.compile(
    r"\b(?:categories?|category\s+hits?|signals?)\s*[:=]\s*[a-z][a-z\s,/+-]*(?:,\s*[a-z][a-z\s/+-]*){2,}",
    re.IGNORECASE,
)
_INTERNAL_JARGON_RE = re.compile(
    r"\b(?:qmd|write-?back|(?:internal|agent|model|tool|n8n|openclaw)\s+routing|"
    r"model[_ -]?runner|tool\s+(?:call|error|trace)|"
    r"metadata/conversations|scanner[_ -]?logs?|observer[_ -]?logs?|openclaw\s+workspace|n8n)\b",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


class SummarySanitizationError(ValueError):
    """Raised when a candidate summary is not safe to write to user-facing fields."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


@dataclass(frozen=True)
class SanitizedSummary:
    title: Optional[str] = None
    overview: Optional[str] = None
    emoji: Optional[str] = None
    category: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


def sanitize_summary_update(
    *,
    title: Optional[str] = None,
    overview: Optional[str] = None,
    emoji: Optional[str] = None,
    category: Optional[str] = None,
) -> SanitizedSummary:
    """Validate and normalize fields before writing an Ella summary to Firestore."""
    warnings: list[str] = []
    cleaned_overview = None

    if overview is not None:
        cleaned_overview = _sanitize_overview(overview, warnings)

    return SanitizedSummary(
        title=_clean_optional_text(title),
        overview=cleaned_overview,
        emoji=_clean_optional_text(emoji),
        category=_clean_optional_text(category),
        warnings=warnings,
    )


def _sanitize_overview(overview: str, warnings: list[str]) -> str:
    candidate = _normalize_text(overview)
    violations: list[str] = []

    if not candidate:
        violations.append("overview is empty")
    elif len(candidate) < MIN_OVERVIEW_CHARS:
        violations.append("overview is too short to replace an existing summary")

    if candidate and not candidate.startswith(ELLA_PREFIX):
        candidate = f"{ELLA_PREFIX}{candidate}"
        warnings.append("overview_missing_ella_prefix")

    candidate = _remove_raw_scanner_audit_sentences(candidate, warnings)

    if _RAW_SCANNER_COUNT_RE.search(candidate):
        violations.append("overview contains raw scanner counts")
    if _RAW_SCANNER_CATEGORY_RE.search(candidate):
        violations.append("overview contains raw scanner category lists")
    if _INTERNAL_JARGON_RE.search(candidate):
        violations.append("overview contains internal debug or routing jargon")
    if candidate and not candidate.startswith(ELLA_PREFIX):
        violations.append("overview must start with [Ella] prefix")
    if len(candidate) < MIN_OVERVIEW_CHARS:
        violations.append("overview is too short after sanitization")

    if violations:
        raise SummarySanitizationError(sorted(set(violations)))

    return candidate


def _remove_raw_scanner_audit_sentences(candidate: str, warnings: list[str]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", candidate)
    kept: list[str] = []
    removed = False

    for sentence in sentences:
        if _RAW_SCANNER_COUNT_RE.search(sentence) or _RAW_SCANNER_CATEGORY_RE.search(sentence):
            removed = True
            continue
        kept.append(sentence)

    if not removed:
        return candidate

    warnings.append("removed_raw_scanner_audit")
    return _normalize_text(" ".join(kept))


def _clean_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return _normalize_text(value)


def _normalize_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()
