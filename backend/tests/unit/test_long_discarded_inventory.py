from datetime import datetime, timedelta, timezone

from utils.conversations.long_discarded_inventory import classify_long_discarded_record, iter_paginated_documents

NOW = datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc)


def _record(status: str, *, age: timedelta = timedelta(days=1)) -> dict:
    return {
        "status": status,
        "discarded": True,
        "structured": {"title": "", "overview": ""},
        "finished_at": NOW - age,
    }


def _classify(data: dict, transcript_chars: int = 25_775) -> dict:
    return classify_long_discarded_record(
        data,
        transcript_chars=transcript_chars,
        min_transcript_chars=25_000,
        now=NOW,
        stale_processing_after=timedelta(hours=6),
    )


def test_completed_and_failed_records_are_candidates():
    assert _classify(_record("completed"))["reason"] == "completed"
    assert _classify(_record("failed"))["reason"] == "failed"


def test_stale_processing_record_is_candidate():
    result = _classify(_record("processing", age=timedelta(hours=7)))

    assert result["candidate"] is True
    assert result["reason"] == "stale_processing"
    assert result["processing_age_seconds"] == 7 * 60 * 60


def test_active_processing_record_is_not_candidate():
    result = _classify(_record("processing", age=timedelta(minutes=5)))

    assert result["candidate"] is False
    assert result["reason"] == "active_processing"


def test_processing_record_without_timestamp_is_not_candidate():
    data = _record("processing")
    data.pop("finished_at")

    assert _classify(data)["reason"] == "processing_timestamp_missing"


def test_non_empty_or_short_records_are_not_candidates():
    non_empty = _record("completed")
    non_empty["structured"]["title"] = "Existing summary"

    assert _classify(non_empty)["reason"] == "structured_not_empty"
    assert _classify(_record("completed"), transcript_chars=100)["reason"] == "transcript_too_short"


def test_paginated_inventory_reads_every_page_without_truncation():
    documents = [type("Document", (), {"id": str(index)})() for index in range(5)]
    calls = []

    def fetch_page(last_document, page_size):
        start = int(last_document.id) + 1 if last_document else 0
        calls.append((start, page_size))
        return documents[start : start + page_size]

    results = list(iter_paginated_documents(fetch_page, page_size=2))

    assert [document.id for document, _ in results] == ["0", "1", "2", "3", "4"]
    assert [page for _, page in results] == [1, 1, 2, 2, 3]
    assert calls == [(0, 2), (2, 2), (4, 2)]
