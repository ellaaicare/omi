import uuid
from datetime import date, datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ella.routers.today_cards import create_today_cards_router
from ella.services.ai_consent import require_current_ai_consent
from ella.services.today_card import (
    TodayCardContent,
    TodayCardFeedbackAction,
    TodayCardKind,
    TodayCardMaterializationResult,
    TodayCardPresentation,
    TodayCardRecord,
    TodayCardSourceRef,
    TodayCardState,
    TodayCardUserContext,
    sha256_ref,
)

UID = "today-api-user"
OTHER_UID = "other-user"
CARD_ID = "2265689d-e0d7-4a26-bdeb-2c8c97e90b89"
NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def _card(state=TodayCardState.ready, version=2):
    source = TodayCardSourceRef(
        source_type="conversation_summary",
        source_id="conversation-a",
        source_version_id="summary-v2",
        occurred_at=datetime(2026, 7, 31, 18, tzinfo=timezone.utc),
        evidence_hash=sha256_ref("source"),
        conversation_id="conversation-a",
    )
    has_content = state in {TodayCardState.ready, TodayCardState.new_user}
    return TodayCardRecord(
        card_id=CARD_ID,
        uid=UID,
        local_date=date(2026, 8, 1),
        timezone="UTC",
        version=version,
        state=state,
        kind=(TodayCardKind.recap if state == TodayCardState.ready else TodayCardKind.welcome if has_content else None),
        content=(
            TodayCardContent(
                eyebrow="A NOTE FROM YESTERDAY" if state == TodayCardState.ready else "FOR YOU TODAY",
                headline="A grounded headline",
                body="A grounded body.",
                spoken_text="A grounded headline. A grounded body.",
                sentence_source_ids=["conversation-a"] if state == TodayCardState.ready else [],
            )
            if has_content
            else None
        ),
        source_refs=[source] if state == TodayCardState.ready else [],
        evidence_hash=sha256_ref([source.model_dump(mode="json")]) if state == TodayCardState.ready else sha256_ref([]),
        generated_at=NOW,
        updated_at=NOW,
        reason_code="no_safe_source" if state == TodayCardState.degraded else None,
        presentation=TodayCardPresentation(style="letter"),
    )


class Repository:
    def __init__(self, card):
        self.card = card
        self.feedback_ids = set()

    async def get_user_context(self, uid):
        return TodayCardUserContext(uid=uid, timezone="UTC", canonical_event_count=1) if uid == UID else None

    async def get_current(self, uid, _local_date):
        return self.card if uid == UID else None

    async def get_by_id(self, uid, card_id):
        return self.card if uid == UID and card_id == self.card.card_id else None

    async def sources_are_current(self, card):
        return card.invalidated_at is None

    async def invalidate_source(self, **_kwargs):
        return 1

    async def record_feedback(self, *, uid, card_id, expected_version, feedback_id, action):
        if uid != UID or card_id != self.card.card_id:
            raise LookupError("today_card_not_found")
        if expected_version != self.card.version:
            raise ValueError("today_card_version_stale")
        inserted = feedback_id not in self.feedback_ids
        self.feedback_ids.add(feedback_id)
        assert action == TodayCardFeedbackAction.helpful
        return self.card, inserted


class Materializer:
    def __init__(self, card):
        self.card = card

    async def materialize(self, uid, _local_date=None):
        if uid != UID:
            raise LookupError("today_card_user_not_found")
        return TodayCardMaterializationResult(card=self.card, created=False)

    async def materialize_due(self, _limit):
        return [TodayCardMaterializationResult(card=self.card, created=False)]


def _client(card, *, uid=UID):
    repository = Repository(card)
    app = FastAPI()
    app.include_router(create_today_cards_router(repository, Materializer(card)))
    app.dependency_overrides[require_current_ai_consent] = lambda: uid
    return TestClient(app), repository


def test_today_card_ready_envelope_etag_and_stale_version_contract():
    client, _repository = _client(_card())

    response = client.get("/v1/ella/today-card")

    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == "ella.today_card.v1"
    assert body["state"] == "ready"
    assert body["card"]["kind"] == "recap"
    assert body["card"]["source_date"] == "2026-07-31"
    assert body["card"]["source_refs"][0]["source_version_id"] == "summary-v2"
    assert body["etag"] == response.headers["etag"]
    assert response.headers["cache-control"] == "private, max-age=60, must-revalidate"

    unchanged = client.get("/v1/ella/today-card", headers={"If-None-Match": response.headers["etag"]})
    assert unchanged.status_code == 304
    assert unchanged.content == b""

    stale = client.get(f"/v1/ella/today-cards/{CARD_ID}?expected_version=1")
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "today_card_version_stale"}


def test_typed_new_user_and_degraded_states_do_not_collapse_to_empty_array():
    new_user, _ = _client(_card(TodayCardState.new_user))
    degraded, _ = _client(_card(TodayCardState.degraded))
    preparing, _ = _client(_card(TodayCardState.preparing))

    new_body = new_user.get("/v1/ella/today-card").json()
    degraded_body = degraded.get("/v1/ella/today-card").json()
    preparing_body = preparing.get("/v1/ella/today-card").json()

    assert new_body["state"] == "new_user"
    assert new_body["card"]["kind"] == "welcome"
    assert degraded_body["state"] == "degraded"
    assert degraded_body["card"] is None
    assert degraded_body["reason_code"] == "no_safe_source"
    assert preparing_body["state"] == "preparing"
    assert preparing_body["card"] is None
    assert preparing_body["retry_after_seconds"] == 30


def test_cross_user_card_access_is_indistinguishable_from_missing():
    client, _repository = _client(_card(), uid=OTHER_UID)

    response = client.get(f"/v1/ella/today-cards/{CARD_ID}")

    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "today_card_not_found"}


def test_invalidated_source_is_not_returned_by_card_id():
    invalidated = _card().model_copy(update={"invalidated_at": NOW, "invalidation_reason": "source_deleted"})
    client, _repository = _client(invalidated)

    response = client.get(f"/v1/ella/today-cards/{CARD_ID}")

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "today_card_source_stale"}


def test_feedback_is_versioned_idempotent_and_rejects_extra_fields():
    client, _repository = _client(_card())
    feedback_id = str(uuid.uuid4())
    payload = {"feedback_id": feedback_id, "expected_version": 2, "action": "helpful"}

    first = client.post(f"/v1/ella/today-cards/{CARD_ID}/feedback", json=payload)
    duplicate = client.post(f"/v1/ella/today-cards/{CARD_ID}/feedback", json=payload)
    stale = client.post(
        f"/v1/ella/today-cards/{CARD_ID}/feedback",
        json={**payload, "expected_version": 1},
    )
    extra = client.post(
        f"/v1/ella/today-cards/{CARD_ID}/feedback",
        json={**payload, "prompt": "client-authored"},
    )

    assert first.json()["duplicate"] is False
    assert duplicate.json()["duplicate"] is True
    assert stale.status_code == 409
    assert extra.status_code == 422


def test_internal_materialization_fails_closed_without_exact_service_token(monkeypatch):
    client, _repository = _client(_card())
    monkeypatch.delenv("ELLA_TODAY_CARD_SERVICE_TOKEN", raising=False)

    missing_config = client.post("/v1/ella/internal/today-cards/materialize", json={"uid": UID})
    monkeypatch.setenv("ELLA_TODAY_CARD_SERVICE_TOKEN", "test-service-token")
    wrong = client.post(
        "/v1/ella/internal/today-cards/materialize",
        json={"uid": UID},
        headers={"Authorization": "Bearer wrong"},
    )
    accepted = client.post(
        "/v1/ella/internal/today-cards/materialize",
        json={"uid": UID},
        headers={"Authorization": "Bearer test-service-token"},
    )

    assert missing_config.status_code == 503
    assert wrong.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["state"] == "ready"
