"""Authenticated app and operator routes for ``ella.today_card.v1``."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from database.honcho_attestation import authority_credential
from ella.services.ai_consent import require_current_ai_consent
from ella.services.today_card import (
    TODAY_CARD_CONTRACT_VERSION,
    TodayCardFeedbackAction,
    TodayCardKind,
    TodayCardMaterializer,
    TodayCardRecord,
    TodayCardState,
)
from ella.services.today_card_postgres import PostgresTodayCardRepository
from utils.ella.exact_firebase_auth import ELLA_SUBJECT_UID_HEADER, EllaRequestAuthority, get_exact_service_authority


class TodayCardPublicSource(BaseModel):
    source_type: str
    source_id: str
    source_version_id: str | None = None
    occurred_at: datetime | None = None
    conversation_id: str | None = None


class TodayCardPublic(BaseModel):
    card_id: str
    version: int
    local_date: date
    timezone: str
    kind: TodayCardKind
    eyebrow: str
    headline: str
    body: str
    spoken_text: str
    source_date: date | None = None
    source_refs: list[TodayCardPublicSource]
    evidence_hash: str | None = None
    generated_at: datetime | None = None
    presentation: dict[str, Any] = Field(default_factory=dict)


class TodayCardEnvelope(BaseModel):
    contract_version: Literal["ella.today_card.v1"] = TODAY_CARD_CONTRACT_VERSION
    state: TodayCardState
    card: TodayCardPublic | None = None
    reason_code: str | None = None
    retry_after_seconds: int | None = None
    server_time: datetime
    etag: str


class TodayCardFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback_id: uuid.UUID
    expected_version: int = Field(ge=1)
    action: TodayCardFeedbackAction


class TodayCardFeedbackResponse(BaseModel):
    ok: bool = True
    card_id: str
    version: int
    duplicate: bool


class TodayCardMaterializeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uid: str = Field(min_length=1, max_length=256)
    local_date: date | None = None


class TodayCardInvalidateSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uid: str = Field(min_length=1, max_length=256)
    source_id: str = Field(min_length=1, max_length=256)
    reason: Literal["source_deleted", "source_version_changed", "source_retracted"]


def _etag(card: TodayCardRecord) -> str:
    material = f"{card.card_id}:{card.version}:{card.evidence_hash or ''}:{card.updated_at.isoformat()}"
    return '"' + hashlib.sha256(material.encode("utf-8")).hexdigest() + '"'


def _public_card(card: TodayCardRecord) -> TodayCardPublic | None:
    if card.content is None or card.kind is None:
        return None
    return TodayCardPublic(
        card_id=card.card_id,
        version=card.version,
        local_date=card.local_date,
        timezone=card.timezone,
        kind=card.kind.value,
        eyebrow=card.content.eyebrow,
        headline=card.content.headline,
        body=card.content.body,
        spoken_text=card.content.spoken_text,
        source_date=(
            card.source_refs[0].occurred_at.astimezone(ZoneInfo(card.timezone)).date()
            if card.source_refs and card.source_refs[0].occurred_at
            else None
        ),
        source_refs=[
            TodayCardPublicSource(
                source_type=source.source_type,
                source_id=source.source_id,
                source_version_id=source.source_version_id,
                occurred_at=source.occurred_at,
                conversation_id=source.conversation_id,
            )
            for source in card.source_refs
        ],
        evidence_hash=card.evidence_hash,
        generated_at=card.generated_at,
        presentation=card.presentation.model_dump(mode="json"),
    )


def _envelope(card: TodayCardRecord) -> TodayCardEnvelope:
    return TodayCardEnvelope(
        state=card.state,
        card=_public_card(card),
        reason_code=card.reason_code,
        retry_after_seconds=30 if card.state == TodayCardState.preparing else None,
        server_time=datetime.now(timezone.utc),
        etag=_etag(card),
    )


def _service_token(request: Request) -> tuple[str, str]:
    expected = authority_credential("ELLA_TODAY_CARD_SERVICE_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail={"code": "today_card_service_auth_not_configured"})
    authorization = str(request.headers.get("Authorization") or "")
    scheme, _, presented = authorization.partition(" ")
    if (
        scheme.lower() != "bearer"
        or not presented
        or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(status_code=403, detail={"code": "today_card_service_auth_invalid"})
    return presented, expected


def _require_service_auth(request: Request) -> None:
    _service_token(request)


def _require_subject_bound_service_auth(request: Request) -> EllaRequestAuthority:
    presented, expected = _service_token(request)
    return get_exact_service_authority(
        provided_service_key=presented,
        configured_service_key=expected,
        service_subject_uid=request.headers.get(ELLA_SUBJECT_UID_HEADER),
        service="today_card",
    )


def create_today_cards_router(
    repository: PostgresTodayCardRepository,
    materializer: TodayCardMaterializer,
) -> APIRouter:
    router = APIRouter(tags=["Ella Today Card"])

    async def load_owned_card(uid: str, card_id: str) -> TodayCardRecord:
        try:
            card = await repository.get_by_id(uid, card_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "today_card_unavailable"}) from exc
        if card is None:
            raise HTTPException(status_code=404, detail={"code": "today_card_not_found"})
        try:
            if not await repository.sources_are_current(card):
                raise HTTPException(status_code=409, detail={"code": "today_card_source_stale"})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "today_card_unavailable"}) from exc
        return card

    @router.get("/v1/ella/today-card", response_model=TodayCardEnvelope)
    async def get_today_card(
        request: Request,
        response: Response,
        uid: str = Depends(require_current_ai_consent),
    ):
        try:
            result = await materializer.materialize(uid)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail={"code": "today_card_user_not_found"}) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "today_card_unavailable"}) from exc
        envelope = _envelope(result.card)
        response.headers["ETag"] = envelope.etag
        response.headers["Cache-Control"] = "private, max-age=60, must-revalidate"
        if str(request.headers.get("If-None-Match") or "") == envelope.etag:
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": envelope.etag})
        return envelope

    @router.get("/v1/ella/today-card/health")
    async def get_today_card_health(uid: str = Depends(require_current_ai_consent)):
        try:
            user = await repository.get_user_context(uid)
            if user is None:
                raise HTTPException(status_code=404, detail={"code": "today_card_user_not_found"})
            local_date = datetime.now(timezone.utc).astimezone(ZoneInfo(user.timezone)).date()
            card = await repository.get_current(uid, local_date)
            sources_current = await repository.sources_are_current(card) if card else False
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "today_card_health_unavailable"}) from exc
        return {
            "ok": bool(card and sources_current and card.state in {TodayCardState.ready, TodayCardState.new_user}),
            "contract_version": TODAY_CARD_CONTRACT_VERSION,
            "local_date": local_date,
            "state": card.state.value if card else "missing",
            "version": card.version if card else None,
            "sources_current": sources_current,
            "generated_at": card.generated_at if card else None,
            "invalidated": bool(card and card.invalidated_at),
        }

    @router.get("/v1/ella/today-cards/{card_id}", response_model=TodayCardEnvelope)
    async def get_today_card_by_id(
        card_id: uuid.UUID,
        response: Response,
        expected_version: int | None = Query(default=None, ge=1),
        uid: str = Depends(require_current_ai_consent),
    ):
        card = await load_owned_card(uid, str(card_id))
        if expected_version is not None and card.version != expected_version:
            raise HTTPException(status_code=409, detail={"code": "today_card_version_stale"})
        envelope = _envelope(card)
        response.headers["ETag"] = envelope.etag
        response.headers["Cache-Control"] = "private, max-age=60, must-revalidate"
        return envelope

    @router.post("/v1/ella/today-cards/{card_id}/feedback", response_model=TodayCardFeedbackResponse)
    async def submit_today_card_feedback(
        card_id: uuid.UUID,
        body: TodayCardFeedbackRequest,
        uid: str = Depends(require_current_ai_consent),
    ):
        try:
            card, inserted = await repository.record_feedback(
                uid=uid,
                card_id=str(card_id),
                expected_version=body.expected_version,
                feedback_id=str(body.feedback_id),
                action=body.action,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail={"code": "today_card_not_found"}) from exc
        except ValueError as exc:
            if str(exc) == "today_card_version_stale":
                raise HTTPException(status_code=409, detail={"code": "today_card_version_stale"}) from exc
            if str(exc) == "today_card_feedback_conflict":
                raise HTTPException(status_code=409, detail={"code": "today_card_feedback_conflict"}) from exc
            if str(exc) == "today_card_source_stale":
                raise HTTPException(status_code=409, detail={"code": "today_card_source_stale"}) from exc
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "today_card_feedback_unavailable"}) from exc
        return TodayCardFeedbackResponse(
            card_id=card.card_id,
            version=card.version,
            duplicate=not inserted,
        )

    @router.post("/v1/ella/internal/today-cards/materialize")
    async def materialize_today_card(body: TodayCardMaterializeRequest, request: Request):
        authority = _require_subject_bound_service_auth(request)
        body.uid = authority.require_uid(body.uid, feature="Today-card materialization")
        try:
            result = await materializer.materialize(body.uid, body.local_date)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail={"code": "today_card_user_not_found"}) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "today_card_materialization_failed"}) from exc
        return {
            "ok": True,
            "card_id": result.card.card_id,
            "version": result.card.version,
            "state": result.card.state.value,
            "created": result.created,
        }

    @router.post("/v1/ella/internal/today-cards/materialize-due")
    async def materialize_due_today_cards(request: Request, limit: int = Query(default=100, ge=1, le=500)):
        _require_service_auth(request)
        try:
            results = await materializer.materialize_due(limit)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "today_card_sweep_failed"}) from exc
        return {
            "ok": True,
            "processed": len(results),
            "states": {
                state.value: sum(1 for result in results if result.card.state == state) for state in TodayCardState
            },
        }

    @router.post("/v1/ella/internal/today-cards/invalidate-source")
    async def invalidate_today_card_source(body: TodayCardInvalidateSourceRequest, request: Request):
        authority = _require_subject_bound_service_auth(request)
        body.uid = authority.require_uid(body.uid, feature="Today-card invalidation")
        try:
            invalidated = await repository.invalidate_source(
                uid=body.uid,
                source_id=body.source_id,
                reason=body.reason,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "today_card_invalidation_failed"}) from exc
        return {"ok": True, "invalidated": invalidated}

    return router
