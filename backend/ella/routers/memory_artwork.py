"""Authenticated API for the versioned memory-artwork lifecycle."""

from __future__ import annotations

import os
import re
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from ella.services.memory_artwork import (
    ARTWORK_CONSENT_VERSION,
    DEFAULT_STYLE_VERSION,
    MemoryArtworkError,
    MemoryArtworkService,
    MemoryArtworkWorker,
)
from ella.services.memory_artwork_recovery import claim_memory_artwork_enrichment_recovery
from ella.services.summary_recovery import recover_failed_conversation_summary
from utils.ella.exact_firebase_auth import (
    ELLA_SUBJECT_UID_HEADER,
    EllaRequestAuthority,
    get_exact_firebase_uid,
    get_exact_service_authority,
)

router = APIRouter(prefix="/v1/ella", tags=["Ella Memory Artwork"])
MEMORY_ARTWORK_SERVICE_HEADER = "X-Ella-Memory-Artwork-Service-Key"
RECONCILIATION_CURSOR_PREFIX = "reconciliation:"
RECONCILIATION_CURSOR_RE = re.compile(r"^reconciliation:([0-9a-f]{64})$")


class MemoryArtworkPreferencesUpdate(BaseModel):
    consent: Literal["accepted", "declined"]
    consent_version: str = ARTWORK_CONSENT_VERSION
    style_version: str = DEFAULT_STYLE_VERSION


class MemoryArtworkBackfillRequest(BaseModel):
    cursor: Optional[str] = Field(default=None, min_length=1, max_length=256, pattern=r"^[^/]+$")


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
    background_tasks: BackgroundTasks,
    uid: str = Depends(get_exact_firebase_uid),
):
    service = MemoryArtworkService()
    try:
        return await service.enqueue(uid, memory_id)
    except MemoryArtworkError as exc:
        if exc.code == "memory_artwork_enrichment_not_terminal":
            claim = await claim_memory_artwork_enrichment_recovery(uid, memory_id)
            outcome = str(claim.get("outcome") or "")
            if outcome == "completed":
                return await service.enqueue(uid, memory_id)
            if outcome == "claimed":
                background_tasks.add_task(
                    recover_failed_conversation_summary,
                    uid=uid,
                    conversation_id=memory_id,
                    request_id=str(claim["request_id"]),
                    attempt_count=int(claim.get("attempt_count") or 1),
                )
            if outcome in {"claimed", "processing", "busy"}:
                return {
                    "schema_version": "ella.memory_artwork.v1",
                    "outcome": "enrichment_queued" if outcome == "claimed" else "enrichment_in_progress",
                    "status": "generating",
                }
        raise _http_error(exc) from exc


@router.post("/memory-artwork/backfill")
async def backfill_memory_artwork(
    background_tasks: BackgroundTasks,
    payload: Optional[MemoryArtworkBackfillRequest] = None,
    uid: str = Depends(get_exact_firebase_uid),
):
    try:
        service = MemoryArtworkService()
        cursor = payload.cursor if payload else None
        reconciliation_match = RECONCILIATION_CURSOR_RE.fullmatch(cursor or "")
        if cursor is None or reconciliation_match:
            requested_job_id = reconciliation_match.group(1) if reconciliation_match else None
            reconciliation = (
                await service.start_reconciliation(uid) if cursor is None else await service.reconciliation_status(uid)
            )
            if requested_job_id is not None and reconciliation.get("job_id") != requested_job_id:
                raise MemoryArtworkError("memory_artwork_backfill_cursor_invalid")
            if reconciliation.get("status") == "failed":
                raise MemoryArtworkError(
                    str(reconciliation.get("failure_code") or "memory_artwork_reconciliation_failed"),
                    retryable=True,
                )
            active = reconciliation.get("status") in {"pending", "processing"}
            return {
                "schema_version": "ella.memory_artwork.v1",
                "limit": 10,
                "scan_limit": 50,
                "queued": int(reconciliation.get("queued") or 0),
                "existing": int(reconciliation.get("existing") or 0),
                "skipped": int(reconciliation.get("skipped") or 0),
                "memory_ids": [],
                "next_cursor": (f"{RECONCILIATION_CURSOR_PREFIX}{reconciliation.get('job_id')}" if active else None),
                "has_more": active,
                "reconciliation_status": reconciliation.get("status"),
                "pages_processed": int(reconciliation.get("pages_processed") or 0),
                "scanned": int(reconciliation.get("scanned") or 0),
            }
        result = await service.backfill(uid, cursor_memory_id=cursor)
        recovery_memory_ids = result.pop("_recovery_memory_ids", [])
        recovery_queued = 0
        for memory_id in recovery_memory_ids:
            claim = await claim_memory_artwork_enrichment_recovery(uid, memory_id)
            outcome = str(claim.get("outcome") or "")
            if outcome == "completed":
                await service.enqueue(uid, memory_id)
            elif outcome == "claimed":
                background_tasks.add_task(
                    recover_failed_conversation_summary,
                    uid=uid,
                    conversation_id=memory_id,
                    request_id=str(claim["request_id"]),
                    attempt_count=int(claim.get("attempt_count") or 1),
                )
                recovery_queued += 1
        result["enrichment_recovery_queued"] = recovery_queued
        return result
    except MemoryArtworkError as exc:
        raise _http_error(exc) from exc


@router.post("/memory-artwork/reconciliation", status_code=202)
async def start_memory_artwork_reconciliation(uid: str = Depends(get_exact_firebase_uid)):
    try:
        return await MemoryArtworkService().start_reconciliation(uid)
    except MemoryArtworkError as exc:
        raise _http_error(exc) from exc


@router.get("/memory-artwork/reconciliation")
async def get_memory_artwork_reconciliation(uid: str = Depends(get_exact_firebase_uid)):
    try:
        return await MemoryArtworkService().reconciliation_status(uid)
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
