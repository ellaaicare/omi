"""Service authentication helpers for the canonical Ella event ledger."""

from __future__ import annotations

import os

CANONICAL_EVENT_SERVICE_HEADER = "X-Ella-Event-Ledger-Key"
CANONICAL_EVENT_SUBJECT_HEADER = "X-Ella-Subject-Uid"


def canonical_event_service_headers(subject_uid: str) -> dict[str, str]:
    """Return the configured internal ledger header without fallback credentials."""
    token = os.getenv("ELLA_EVENT_LEDGER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("canonical_event_service_auth_not_configured")
    normalized_subject = subject_uid.strip()
    if not normalized_subject:
        raise RuntimeError("canonical_event_service_subject_required")
    return {
        CANONICAL_EVENT_SERVICE_HEADER: token,
        CANONICAL_EVENT_SUBJECT_HEADER: normalized_subject,
    }
