from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ella.routers.hermes_cloud_enrichment import (
    create_hermes_cloud_enrichment_router,
)


class FakeService:
    def __init__(self):
        self.calls = []

    async def enrich(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            conversation_id=kwargs["conversation_id"],
            runtime_binding_id="binding-a",
            runtime_interaction_id="interaction-a",
            active_summary_version_id="version-a",
            canonical_user_event_id="event-user",
            canonical_assistant_event_id="event-assistant",
            transcript_sha256="a" * 64,
            summary_sha256="b" * 64,
            provider_response_present=True,
            duplicate=False,
            client_interaction_id=(kwargs.get("expected_client_interaction_id") or "omi-enrichment:" + ("c" * 64)),
        )


def _client(service):
    async def factory():
        return service

    app = FastAPI()
    app.include_router(create_hermes_cloud_enrichment_router(factory))
    return TestClient(app)


def test_enrichment_router_requires_configured_service_token(monkeypatch):
    monkeypatch.delenv("ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN", raising=False)
    response = _client(FakeService()).post(
        "/v1/ella/internal/hermes-cloud/enrichment/run",
        json={"uid": "synthetic-user", "conversation_id": "conversation-a"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "hermes_cloud_enrichment_auth_not_configured"


def test_enrichment_router_rejects_wrong_service_token(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN", "x" * 32)
    response = _client(FakeService()).post(
        "/v1/ella/internal/hermes-cloud/enrichment/run",
        headers={"X-Ella-Hermes-Cloud-Enrichment-Token": "wrong"},
        json={"uid": "synthetic-user", "conversation_id": "conversation-a"},
    )

    assert response.status_code == 401


def test_enrichment_router_returns_content_free_receipt(monkeypatch):
    token = "x" * 32
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN", token)
    service = FakeService()
    response = _client(service).post(
        "/v1/ella/internal/hermes-cloud/enrichment/run",
        headers={"X-Ella-Hermes-Cloud-Enrichment-Token": token},
        json={"uid": "synthetic-user", "conversation_id": "conversation-a"},
    )

    assert response.status_code == 200
    assert response.json()["content_free"] is True
    assert "text" not in response.json()
    assert service.calls == [
        {
            "uid": "synthetic-user",
            "conversation_id": "conversation-a",
            "allow_shadow": False,
            "expected_client_interaction_id": None,
            "expected_transcript_sha256": None,
        }
    ]


def test_enrichment_router_binds_outbox_identity(monkeypatch):
    token = "x" * 32
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN", token)
    service = FakeService()
    job_id = "hce_" + ("d" * 64)
    client_id = "omi-enrichment:" + ("c" * 64)
    transcript_sha256 = "a" * 64
    response = _client(service).post(
        "/v1/ella/internal/hermes-cloud/enrichment/run",
        headers={"X-Ella-Hermes-Cloud-Enrichment-Token": token},
        json={
            "uid": "synthetic-user",
            "conversation_id": "conversation-a",
            "outbox_job_id": job_id,
            "client_interaction_id": client_id,
            "transcript_sha256": transcript_sha256,
        },
    )

    assert response.status_code == 200
    assert response.json()["outbox_job_id"] == job_id
    assert response.json()["client_interaction_id"] == client_id
    assert service.calls[0]["expected_client_interaction_id"] == client_id
    assert service.calls[0]["expected_transcript_sha256"] == transcript_sha256
