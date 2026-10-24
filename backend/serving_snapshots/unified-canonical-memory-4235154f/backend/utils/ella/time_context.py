"""User-local time annotations for Ella timeline/context payloads."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_USER_TIMEZONE = os.getenv("ELLA_DEFAULT_USER_TIMEZONE", "America/Los_Angeles")


def user_timezone(tz_name: str | None = None) -> ZoneInfo:
    name = (tz_name or DEFAULT_USER_TIMEZONE or "UTC").strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def timezone_name(tz_name: str | None = None) -> str:
    return user_timezone(tz_name).key


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def relative_human(dt: datetime, now: datetime | None = None) -> str:
    now_utc = (now or utc_now()).astimezone(timezone.utc)
    delta_seconds = int((now_utc - dt.astimezone(timezone.utc)).total_seconds())
    future = delta_seconds < 0
    seconds = abs(delta_seconds)
    if seconds < 90:
        value = f"{seconds} seconds"
    elif seconds < 90 * 60:
        value = f"{round(seconds / 60)} minutes"
    elif seconds < 36 * 3600:
        value = f"{round(seconds / 3600, 1):g} hours"
    else:
        value = f"{round(seconds / 86400, 1):g} days"
    return f"in {value}" if future else f"{value} ago"


def local_time_fields(
    value: Any,
    *,
    tz_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    dt = parse_datetime(value)
    tz = user_timezone(tz_name)
    if dt is None:
        return {
            "utc": None,
            "timezone": tz.key,
            "local": None,
            "local_date": None,
            "local_time": None,
            "relative_to_now": None,
            "seconds_from_now": None,
        }
    local = dt.astimezone(tz)
    now_utc = (now or utc_now()).astimezone(timezone.utc)
    return {
        "utc": iso_utc(dt),
        "timezone": tz.key,
        "local": local.isoformat(),
        "local_date": local.strftime("%Y-%m-%d"),
        "local_time": local.strftime("%H:%M:%S %Z"),
        "relative_to_now": relative_human(dt, now_utc),
        "seconds_from_now": int((dt - now_utc).total_seconds()),
    }


def build_time_context(tz_name: str | None = None, *, now: datetime | None = None) -> dict[str, str]:
    tz = user_timezone(tz_name)
    now_utc = (now or utc_now()).astimezone(timezone.utc)
    now_local = now_utc.astimezone(tz)
    return {
        "now_utc": iso_utc(now_utc),
        "user_timezone": tz.key,
        "now_local": now_local.isoformat(),
        "now_local_date": now_local.strftime("%Y-%m-%d"),
        "now_local_time": now_local.strftime("%H:%M:%S %Z"),
    }


def annotate_event_time(
    event: dict[str, Any],
    *,
    tz_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    annotated = dict(event)
    started_at = (
        annotated.get("started_at")
        or annotated.get("created_at")
        or annotated.get("timestamp")
        or annotated.get("finished_at")
    )
    fields = local_time_fields(started_at, tz_name=tz_name, now=now)
    annotated["started_at_utc"] = fields["utc"]
    annotated["started_at_local"] = fields["local"]
    annotated["started_at_local_date"] = fields["local_date"]
    annotated["started_at_local_time"] = fields["local_time"]
    annotated["started_at_timezone"] = fields["timezone"]
    annotated["relative_to_now"] = fields["relative_to_now"]
    annotated["seconds_from_now"] = fields["seconds_from_now"]
    annotated["time"] = fields
    return annotated
