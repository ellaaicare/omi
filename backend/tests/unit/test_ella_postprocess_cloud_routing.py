from datetime import datetime, timezone
from types import SimpleNamespace

from utils.ella import postprocess


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def _conversation():
    return SimpleNamespace(
        id="conversation-a",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        language="en",
        status="completed",
        structured=SimpleNamespace(
            title="Synthetic",
            overview="Synthetic summary",
            emoji="",
            category="other",
        ),
        transcript_segments=[SimpleNamespace(is_user=True, speaker="SPEAKER_0", text="Synthetic input")],
    )


def _configure_cloud(monkeypatch):
    monkeypatch.setattr(postprocess, "POSTPROCESS_ENABLED", True)
    monkeypatch.setattr(
        postprocess,
        "HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS",
        frozenset({"synthetic-user"}),
    )


def test_cloud_selected_uid_queues_and_skips_all_legacy_webhooks(
    monkeypatch,
):
    _configure_cloud(monkeypatch)
    calls = []
    queued = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(
        postprocess,
        "enqueue_cloud_enrichment",
        lambda uid, conversation: queued.append((uid, conversation.id)) or {"status": "pending"},
    )
    monkeypatch.setattr(postprocess.requests, "post", fake_post)

    postprocess.fire_postprocess_webhook("synthetic-user", _conversation())

    assert queued == [("synthetic-user", "conversation-a")]
    assert calls == []


def test_cloud_selected_uid_queues_even_when_generic_postprocess_is_disabled(
    monkeypatch,
):
    _configure_cloud(monkeypatch)
    monkeypatch.setattr(postprocess, "POSTPROCESS_ENABLED", False)
    calls = []
    queued = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(
        postprocess,
        "enqueue_cloud_enrichment",
        lambda uid, conversation: queued.append((uid, conversation.id)) or {"status": "pending"},
    )
    monkeypatch.setattr(postprocess.requests, "post", fake_post)

    postprocess.fire_postprocess_webhook("synthetic-user", _conversation())

    assert queued == [("synthetic-user", "conversation-a")]
    assert calls == []


def test_cloud_selected_uid_outbox_failure_never_falls_back(monkeypatch):
    _configure_cloud(monkeypatch)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    def fail_enqueue(uid, conversation):
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr(
        postprocess,
        "enqueue_cloud_enrichment",
        fail_enqueue,
    )
    monkeypatch.setattr(postprocess.requests, "post", fake_post)

    postprocess.fire_postprocess_webhook("synthetic-user", _conversation())

    assert calls == []
    assert postprocess.CONVERSATION_READY_WEBHOOK_URL not in calls


def test_cloud_selected_uid_never_serializes_or_posts_legacy_payload(monkeypatch):
    _configure_cloud(monkeypatch)
    calls = []

    class BoundaryConversation:
        id = "conversation-a"

        @property
        def structured(self):
            raise AssertionError("selected cloud path read legacy structured content")

        @property
        def transcript_segments(self):
            raise AssertionError("selected cloud path read legacy transcript content")

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(
        postprocess,
        "enqueue_cloud_enrichment",
        lambda uid, conversation: {"status": "pending"},
    )
    monkeypatch.setattr(postprocess.requests, "post", fake_post)

    postprocess.fire_postprocess_webhook("synthetic-user", BoundaryConversation())

    assert calls == []


def test_non_selected_uid_preserves_both_legacy_webhooks(monkeypatch):
    monkeypatch.setattr(postprocess, "POSTPROCESS_ENABLED", True)
    monkeypatch.setattr(
        postprocess,
        "HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS",
        frozenset({"synthetic-user"}),
    )
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return FakeResponse()

    class ImmediateThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(postprocess.requests, "post", fake_post)
    monkeypatch.setattr(postprocess.threading, "Thread", ImmediateThread)

    postprocess.fire_postprocess_webhook("legacy-user", _conversation())

    assert [url for url, _ in calls] == [
        postprocess.POSTPROCESS_WEBHOOK_URL,
        postprocess.CONVERSATION_READY_WEBHOOK_URL,
    ]
    assert calls[0][1]["structured"]["overview"] == "Synthetic summary"
    assert calls[1][1]["transcript"] == "User: Synthetic input"
