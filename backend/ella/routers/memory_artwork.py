"""Authenticated API for the versioned memory-artwork lifecycle."""

from __future__ import annotations

import os
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from ella.services.memory_artwork import (
    ARTWORK_CONSENT_VERSION,
    DEFAULT_STYLE_VERSION,
    MemoryArtworkError,
    MemoryArtworkService,
    MemoryArtworkWorker,
)
from utils.ella.exact_firebase_auth import (
    ELLA_SUBJECT_UID_HEADER,
    EllaRequestAuthority,
    get_exact_firebase_uid,
    get_exact_service_authority,
)

router = APIRouter(prefix="/v1/ella", tags=["Ella Memory Artwork"])
MEMORY_ARTWORK_SERVICE_HEADER = "X-Ella-Memory-Artwork-Service-Key"


class MemoryArtworkPreferencesUpdate(BaseModel):
    consent: Literal["accepted", "declined"]
    consent_version: str = ARTWORK_CONSENT_VERSION
    style_version: str = DEFAULT_STYLE_VERSION


class MemoryArtworkGenerationRequest(BaseModel):
    request_mode: Literal["manual", "automatic"] = "manual"


def require_memory_artwork_service(
    service_key: Optional[str] = Header(default=None, alias=MEMORY_ARTWORK_SERVICE_HEADER),
    subject_uid: Optional[str] = Header(default=None, alias=ELLA_SUBJECT_UID_HEADER),
) -> EllaRequestAuthority:
    return get_exact_service_authority(
        provided_service_key=service_key,
        configured_service_key=os.getenv("ELLA_MEMORY_ARTWORK_SERVICE_KEY", "").strip(),
        service_subject_uid=subject_uid,
        service="memory_artwork",
    )


def _http_error(exc: MemoryArtworkError) -> HTTPException:
    if exc.code == "memory_artwork_memory_not_found":
        status_code = 404
    elif exc.code.endswith("_invalid") or exc.code.endswith("_stale"):
        status_code = 422
    elif exc.code in {
        "memory_artwork_authority_changed",
        "memory_artwork_source_changed",
        "memory_artwork_prompt_changed",
        "memory_artwork_preference_authority_stale",
    }:
        status_code = 409
    else:
        status_code = 503 if exc.retryable or "disabled" in exc.code or "not_ready" in exc.code else 409
    return HTTPException(status_code=status_code, detail={"code": exc.code, "retryable": exc.retryable})


@router.get("/memory-artwork/preferences")
async def get_memory_artwork_preferences(uid: str = Depends(get_exact_firebase_uid)):
    try:
        return await MemoryArtworkService().preferences(uid)
    except MemoryArtworkError as exc:
        raise _http_error(exc) from exc


@router.put("/memory-artwork/preferences")
async def put_memory_artwork_preferences(
    payload: MemoryArtworkPreferencesUpdate,
    uid: str = Depends(get_exact_firebase_uid),
):
    try:
        return await MemoryArtworkService().set_preferences(
            uid,
            consent=payload.consent,
            consent_version=payload.consent_version,
            style_version=payload.style_version,
        )
    except MemoryArtworkError as exc:
        raise _http_error(exc) from exc


@router.get("/memories/{memory_id}/artwork")
async def get_memory_artwork(memory_id: str, uid: str = Depends(get_exact_firebase_uid)):
    try:
        return await MemoryArtworkService().signed_url(uid, memory_id)
    except MemoryArtworkError as exc:
        raise _http_error(exc) from exc


@router.post("/memories/{memory_id}/artwork")
async def retry_memory_artwork(
    memory_id: str,
    payload: Optional[MemoryArtworkGenerationRequest] = None,
    uid: str = Depends(get_exact_firebase_uid),
):
    try:
        return await MemoryArtworkService().enqueue(
            uid,
            memory_id,
            request_mode=payload.request_mode if payload is not None else "manual",
        )
    except MemoryArtworkError as exc:
        raise _http_error(exc) from exc


@router.post("/memory-artwork/backfill")
async def backfill_memory_artwork(uid: str = Depends(get_exact_firebase_uid)):
    try:
        return await MemoryArtworkService().backfill(uid)
    except MemoryArtworkError as exc:
        raise _http_error(exc) from exc


@router.post("/internal/memory-artwork/{memory_id}/process")
async def process_memory_artwork(
    memory_id: str,
    uid: Optional[str] = None,
    service: EllaRequestAuthority = Depends(require_memory_artwork_service),
):
    bound_uid = service.require_uid(uid, feature="Memory artwork worker")
    try:
        worker = MemoryArtworkWorker()
        conversation = worker.repository.get_conversation(bound_uid, memory_id) or {}
        generation_key = str(((conversation.get("artwork") or {}).get("generation_key") or ""))
        return await worker.run_job(bound_uid, memory_id, generation_key, raise_errors=True)
    except MemoryArtworkError as exc:
        raise _http_error(exc) from exc
