"""Shared Hermes/Honcho session key helpers."""

from __future__ import annotations

import re


def safe_session_component(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(value or "")).strip("-")[:160] or "unknown"


def canonical_omi_session_key(uid: str) -> str:
    """Stable Hermes/Honcho long-term memory scope for one Ella profile."""

    return f"ella:omi:{safe_session_component(uid.lower())}:canonical"
