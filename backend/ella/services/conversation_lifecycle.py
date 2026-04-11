"""
Ella conversation lifecycle policy.

This module keeps Ella-owned conversation split rules out of upstream-managed
websocket code. The upstream hook should call these pure helpers and remain
small enough to re-apply after Basehardware syncs.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ella.config import ELLA_CONFIG


MIN_MAX_DURATION_SECONDS = 10 * 60
MAX_MAX_DURATION_SECONDS = 4 * 60 * 60


@dataclass(frozen=True)
class ConversationSplitDecision:
    should_split: bool
    reason: Optional[str] = None
    elapsed_seconds: float = 0.0
    limit_seconds: Optional[int] = None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _coerce_enabled(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None


def _coerce_duration(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        duration = int(value)
    except (TypeError, ValueError):
        return None
    if duration < MIN_MAX_DURATION_SECONDS or duration > MAX_MAX_DURATION_SECONDS:
        return None
    return duration


def resolve_max_duration_seconds(user_preferences: Optional[dict] = None) -> Optional[int]:
    """
    Resolve max conversation duration from user preferences and Ella defaults.

    User preference semantics:
    - enabled=None: use server default
    - enabled=False: disable for this user
    - seconds=None: use server default seconds
    """
    prefs = user_preferences or {}
    enabled = _coerce_enabled(prefs.get("conversation_max_duration_enabled"))
    if enabled is False:
        return None

    default_enabled = bool(ELLA_CONFIG.conversation_max_duration_enabled)
    if enabled is None and not default_enabled:
        return None

    user_seconds = _coerce_duration(prefs.get("conversation_max_duration_seconds"))
    if user_seconds is not None:
        return user_seconds

    return _coerce_duration(ELLA_CONFIG.conversation_max_duration_seconds)


def should_split_for_max_duration(
    conversation: dict,
    now: Optional[datetime] = None,
    user_preferences: Optional[dict] = None,
) -> ConversationSplitDecision:
    limit_seconds = resolve_max_duration_seconds(user_preferences)
    if limit_seconds is None:
        return ConversationSplitDecision(should_split=False)

    started_at = _parse_datetime(conversation.get("started_at") or conversation.get("created_at"))
    if started_at is None:
        return ConversationSplitDecision(should_split=False, limit_seconds=limit_seconds)

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    elapsed_seconds = max(0.0, (now_utc - started_at).total_seconds())
    if elapsed_seconds >= limit_seconds:
        return ConversationSplitDecision(
            should_split=True,
            reason="max_duration",
            elapsed_seconds=elapsed_seconds,
            limit_seconds=limit_seconds,
        )

    return ConversationSplitDecision(
        should_split=False,
        elapsed_seconds=elapsed_seconds,
        limit_seconds=limit_seconds,
    )
