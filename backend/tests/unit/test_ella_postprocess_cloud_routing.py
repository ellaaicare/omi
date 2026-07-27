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


def _configure_cloud(monkeypatch, *, enabled=True, url=None):
    monkeypatch.setattr(postprocess, "POSTPROCESS_ENABLED", True)
    monkeypatch.setattr(
        postprocess,
        "HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS",
        frozenset({"synthetic-user"}),
    )
    monkeypatch.setattr(
        postprocess,
        "HERMES_CLOUD_ENRICHMENT_ENABLED",
        enabled,
    )
    monkeypatch.setattr(
        postprocess,
        "HERMES_CLOUD_ENRICHMENT_TOKEN",
        "x" * 32,
    )
    monkeypatch.setattr(
        postprocess,
        "HERMES_CLOUD_ENRICHMENT_URL",
        url or "http://127.0.0.1:8000/v1/ella/internal/hermes-cloud/enrichment/run",
    )


def test_cloud_selected_uid_uses_loopback_and_never_legacy_ready(monkeypatch):
    _configure_cloud(monkeypatch)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == postprocess.HERMES_CLOUD_ENRICHMENT_URL:
            return FakeResponse(
                body={
                    "ok": True,
                    "status": "applied",
                    "content_free": True,
                    "duplicate": False,
                }
            )
        return FakeResponse()

    monkeypatch.setattr(postprocess.requests, "post", fake_post)

    postprocess.fire_postprocess_webhook("synthetic-user", _conversation())

    assert [url for url, _ in calls] == [
        postprocess.POSTPROCESS_WEBHOOK_URL,
        postprocess.HERMES_CLOUD_ENRICHMENT_URL,
    ]
    assert postprocess.CONVERSATION_READY_WEBHOOK_URL not in [url for url, _ in calls]
    cloud_headers = calls[1][1]["headers"]
    assert cloud_headers["X-Ella-Hermes-Cloud-Enrichment-Token"] == "x" * 32


def test_cloud_selected_uid_fails_closed_when_gate_is_disabled(monkeypatch):
    _configure_cloud(monkeypatch, enabled=False)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(postprocess.requests, "post", fake_post)

    postprocess.fire_postprocess_webhook("synthetic-user", _conversation())

    assert calls == [postprocess.POSTPROCESS_WEBHOOK_URL]
    assert postprocess.CONVERSATION_READY_WEBHOOK_URL not in calls


def test_cloud_selected_uid_rejects_non_loopback_adapter_without_fallback(
    monkeypatch,
):
    _configure_cloud(
        monkeypatch,
        url="https://example.test/v1/ella/internal/hermes-cloud/enrichment/run",
    )
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(postprocess.requests, "post", fake_post)

    postprocess.fire_postprocess_webhook("synthetic-user", _conversation())

    assert calls == [postprocess.POSTPROCESS_WEBHOOK_URL]
    assert postprocess.CONVERSATION_READY_WEBHOOK_URL not in calls


def test_cloud_selected_uid_provider_failure_never_falls_back(monkeypatch):
    _configure_cloud(monkeypatch)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if url == postprocess.HERMES_CLOUD_ENRICHMENT_URL:
            return FakeResponse(status_code=503)
        return FakeResponse()

    monkeypatch.setattr(postprocess.requests, "post", fake_post)

    postprocess.fire_postprocess_webhook("synthetic-user", _conversation())

    assert calls == [
        postprocess.POSTPROCESS_WEBHOOK_URL,
        postprocess.HERMES_CLOUD_ENRICHMENT_URL,
    ]
    assert postprocess.CONVERSATION_READY_WEBHOOK_URL not in calls
