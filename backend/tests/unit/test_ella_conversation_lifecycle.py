from datetime import datetime, timedelta, timezone

from ella.services import conversation_lifecycle as lifecycle


def test_resolve_max_duration_uses_default(monkeypatch):
    monkeypatch.setattr(lifecycle.ELLA_CONFIG, "conversation_max_duration_enabled", True)
    monkeypatch.setattr(lifecycle.ELLA_CONFIG, "conversation_max_duration_seconds", 1800)

    assert lifecycle.resolve_max_duration_seconds({}) == 1800


def test_resolve_max_duration_user_override(monkeypatch):
    monkeypatch.setattr(lifecycle.ELLA_CONFIG, "conversation_max_duration_enabled", True)
    monkeypatch.setattr(lifecycle.ELLA_CONFIG, "conversation_max_duration_seconds", 1800)

    assert lifecycle.resolve_max_duration_seconds({"conversation_max_duration_seconds": 2700}) == 2700


def test_resolve_max_duration_user_disable(monkeypatch):
    monkeypatch.setattr(lifecycle.ELLA_CONFIG, "conversation_max_duration_enabled", True)
    monkeypatch.setattr(lifecycle.ELLA_CONFIG, "conversation_max_duration_seconds", 1800)

    assert lifecycle.resolve_max_duration_seconds({"conversation_max_duration_enabled": False}) is None


def test_should_split_for_max_duration_when_elapsed_exceeds_limit(monkeypatch):
    monkeypatch.setattr(lifecycle.ELLA_CONFIG, "conversation_max_duration_enabled", True)
    monkeypatch.setattr(lifecycle.ELLA_CONFIG, "conversation_max_duration_seconds", 1800)

    now = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)
    conversation = {"started_at": now - timedelta(seconds=1801)}

    decision = lifecycle.should_split_for_max_duration(conversation, now)

    assert decision.should_split is True
    assert decision.reason == "max_duration"
    assert decision.limit_seconds == 1800


def test_should_not_split_before_limit(monkeypatch):
    monkeypatch.setattr(lifecycle.ELLA_CONFIG, "conversation_max_duration_enabled", True)
    monkeypatch.setattr(lifecycle.ELLA_CONFIG, "conversation_max_duration_seconds", 1800)

    now = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)
    conversation = {"started_at": now - timedelta(seconds=1200)}

    decision = lifecycle.should_split_for_max_duration(conversation, now)

    assert decision.should_split is False
    assert decision.elapsed_seconds == 1200
