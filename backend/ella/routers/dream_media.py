"""Firebase-authenticated dream metadata and private media delivery."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from ella.services.dream_media import DreamMediaError, DreamMediaService, DreamUpload, get_dream_media_service
from utils.ella.exact_firebase_auth import (
    ELLA_SUBJECT_UID_HEADER,
    EllaRequestAuthority,
    get_exact_firebase_uid,
    get_exact_service_authority,
)
from utils.ella.private_media_storage import MAX_DREAM_MEDIA_BYTES

router = APIRouter(prefix="/v1/ella", tags=["Ella Dream Media"])
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
DREAM_PIPELINE_HEADER = "X-Ella-Dream-Pipeline-Key"


class DreamUploadMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=8, max_length=128)
    title: str = Field(default="", max_length=300)
    narrative: str = Field(default="", max_length=12000)
    captions: list[str] = Field(default_factory=list, max_length=100)
    source_memory_ids: list[str] = Field(default_factory=list, max_length=100)
    created_at: Optional[datetime] = None

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", normalized):
            raise ValueError("invalid request id")
        return normalized

    @field_validator("title", "narrative")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return " ".join(value.replace("\x00", " ").split())

    @field_validator("captions")
    @classmethod
    def normalize_captions(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.replace("\x00", " ").split())[:1000] for value in values]
        return [value for value in normalized if value]

    @field_validator("source_memory_ids")
    @classmethod
    def validate_source_ids(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            candidate = value.strip()
            if not _ID_RE.fullmatch(candidate):
                raise ValueError("invalid source memory id")
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized


def _validate_dream_id(dream_id: str) -> str:
    if not _ID_RE.fullmatch(dream_id):
        raise HTTPException(status_code=404, detail={"code": "dream_media_not_found"})
    return dream_id


def _private_response_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"


def _raise_api_error(exc: DreamMediaError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "retryable": exc.retryable},
    ) from exc


def require_dream_pipeline_authority(
    x_pipeline_key: Optional[str] = Header(default=None, alias=DREAM_PIPELINE_HEADER),
    subject_uid: Optional[str] = Header(default=None, alias=ELLA_SUBJECT_UID_HEADER),
) -> EllaRequestAuthority:
    return get_exact_service_authority(
        provided_service_key=x_pipeline_key,
        configured_service_key=os.getenv("ELLA_DREAM_PIPELINE_SERVICE_KEY", ""),
        service_subject_uid=subject_uid,
        service="dream_pipeline",
    )


async def _read_bounded_upload(file: UploadFile) -> bytes:
    chunks = bytearray()
    while len(chunks) <= MAX_DREAM_MEDIA_BYTES:
        chunk = await file.read(min(1024 * 1024, MAX_DREAM_MEDIA_BYTES + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
    if not chunks or len(chunks) > MAX_DREAM_MEDIA_BYTES:
        chunks.clear()
        raise HTTPException(
            status_code=413,
            detail={"code": "dream_media_payload_size_invalid", "retryable": False},
        )
    payload = bytes(chunks)
    chunks.clear()
    return payload


@router.get("/dreams")
async def list_dreams(
    response: Response,
    uid: str = Depends(get_exact_firebase_uid),
    service: DreamMediaService = Depends(get_dream_media_service),
):
    _private_response_headers(response)
    try:
        dreams = await run_in_threadpool(service.list_dreams, uid)
    except DreamMediaError as exc:
        _raise_api_error(exc)
    return {"dreams": dreams}


@router.get("/dreams/{dream_id}/media")
async def get_dream_media(
    dream_id: str,
    response: Response,
    uid: str = Depends(get_exact_firebase_uid),
    service: DreamMediaService = Depends(get_dream_media_service),
):
    _private_response_headers(response)
    dream_id = _validate_dream_id(dream_id)
    try:
        return await run_in_threadpool(service.get_media, uid, dream_id)
    except DreamMediaError as exc:
        _raise_api_error(exc)


@router.delete("/dreams/{dream_id}")
async def delete_dream(
    dream_id: str,
    response: Response,
    uid: str = Depends(get_exact_firebase_uid),
    service: DreamMediaService = Depends(get_dream_media_service),
):
    _private_response_headers(response)
    dream_id = _validate_dream_id(dream_id)
    try:
        deleted = await run_in_threadpool(service.delete_dream, uid, dream_id)
    except DreamMediaError as exc:
        _raise_api_error(exc)
    if not deleted:
        raise HTTPException(status_code=404, detail={"code": "dream_media_not_found"})
    return {"status": "deleted", "dream_id": dream_id}


@router.post("/internal/dreams/{dream_id}/media", status_code=201)
async def upload_dream_media(
    dream_id: str,
    response: Response,
    metadata: str = Form(...),
    file: UploadFile = File(...),
    authority: EllaRequestAuthority = Depends(require_dream_pipeline_authority),
    service: DreamMediaService = Depends(get_dream_media_service),
):
    _private_response_headers(response)
    dream_id = _validate_dream_id(dream_id)
    uid = authority.require_uid(None, feature="dream_pipeline")
    try:
        parsed = DreamUploadMetadata.model_validate(json.loads(metadata))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "dream_media_metadata_invalid", "retryable": False},
        ) from exc
    payload = await _read_bounded_upload(file)
    upload = DreamUpload(
        request_id=parsed.request_id,
        title=parsed.title,
        narrative=parsed.narrative,
        captions=tuple(parsed.captions),
        source_memory_ids=tuple(parsed.source_memory_ids),
        created_at=parsed.created_at,
    )
    try:
        asset = await run_in_threadpool(
            service.upload,
            uid=uid,
            dream_id=dream_id,
            upload=upload,
            payload=payload,
            claimed_content_type=file.content_type or "",
        )
    except DreamMediaError as exc:
        _raise_api_error(exc)
    finally:
        del payload
    return {"dream_id": dream_id, "asset": asset}
