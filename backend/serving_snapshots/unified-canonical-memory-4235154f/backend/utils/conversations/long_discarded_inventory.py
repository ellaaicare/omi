from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, Sequence, Tuple

DEFAULT_STALE_PROCESSING_AFTER = timedelta(hours=6)
INVENTORY_STATUSES = {"completed", "failed", "processing"}


def iter_paginated_documents(
    fetch_page: Callable[[Any | None, int], Sequence[Any]], page_size: int
) -> Iterator[Tuple[Any, int]]:
    last_document = None
    page_number = 0

    while True:
        documents = list(fetch_page(last_document, page_size))
        if not documents:
            return

        page_number += 1
        for document in documents:
            yield document, page_number

        if len(documents) < page_size:
            return
        last_document = documents[-1]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def structured_is_empty(data: Dict[str, Any]) -> bool:
    structured = data.get("structured") or {}
    return not any(str(structured.get(field) or "").strip() for field in ("title", "overview"))


def classify_long_discarded_record(
    data: Dict[str, Any],
    *,
    transcript_chars: int,
    min_transcript_chars: int,
    now: datetime,
    stale_processing_after: timedelta = DEFAULT_STALE_PROCESSING_AFTER,
) -> Dict[str, Any]:
    status_value = data.get("status")
    status = str(getattr(status_value, "value", status_value) or "")
    result: Dict[str, Any] = {"candidate": False, "reason": "unsupported_status", "status": status}

    if data.get("discarded") is not True:
        result["reason"] = "not_discarded"
        return result
    if not structured_is_empty(data):
        result["reason"] = "structured_not_empty"
        return result
    if transcript_chars < min_transcript_chars:
        result["reason"] = "transcript_too_short"
        return result
    if status not in INVENTORY_STATUSES:
        return result
    if status in {"completed", "failed"}:
        result.update(candidate=True, reason=status)
        return result

    reference = _as_utc(data.get("finished_at") or data.get("created_at") or data.get("started_at"))
    if reference is None:
        result["reason"] = "processing_timestamp_missing"
        return result

    current = _as_utc(now) or now
    processing_age = max(timedelta(0), current - reference)
    result["processing_age_seconds"] = int(processing_age.total_seconds())
    if processing_age < stale_processing_after:
        result["reason"] = "active_processing"
        return result

    result.update(candidate=True, reason="stale_processing")
    return result
