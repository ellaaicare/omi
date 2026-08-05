"""Service authentication helpers for the canonical Ella event ledger."""

from __future__ import annotations

import os

CANONICAL_EVENT_SERVICE_HEADER = "X-Ella-Event-Ledger-Key"


def canonical_event_service_headers() -> dict[str, str]:
    """Return the configured internal ledger header without fallback credentials."""
    token = os.getenv("ELLA_EVENT_LEDGER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("canonical_event_service_auth_not_configured")
    return {CANONICAL_EVENT_SERVICE_HEADER: token}
