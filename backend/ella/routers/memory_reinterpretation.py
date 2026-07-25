"""Authenticated status and operator endpoints for memory reinterpretation."""

from __future__ import annotations

import hmac
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException

import database.conversations as conversations_db
from database.memory_reinterpretations import (
    PostgresMemoryReinterpretationRepository,
    public_job,
)
from ella.routers.canonical_events import _get_pool
from ella.services.memory_reinterpretation import (
    MemoryReinterpretationWorker,
    worker_runtime_metrics,
)
from utils.other import endpoints as auth


def _operator_token() -> str:
    return os.getenv("ELLA_MEMORY_REINTERPRETATION_OPERATOR_TOKEN", "").strip()


def _require_operator(presented: Optional[str]) -> None:
    expected = _operator_token()
    if not expected:
        raise HTTPException(status_code=503, detail={"code": "operator_auth_not_configured"})
    if not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail={"code": "invalid_operator_token"})


def create_memory_reinterpretation_router(
    repository: Any = None,
    worker: Optional[MemoryReinterpretationWorker] = None,
) -> APIRouter:
    repository = repository or PostgresMemoryReinterpretationRepository(_get_pool)
    worker = worker or MemoryReinterpretationWorker(repository)
    router = APIRouter(tags=["memory-reinterpretation"])

    async def _owned_job(
        *,
        uid: str,
        conversation_id: str,
        job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        # Missing and non-owned conversations intentionally have identical
        # responses so the endpoint cannot be used for existence probing.
        conversation = conversations_db.get_conversation(uid, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail={"code": "reinterpretation_not_found"})
        job = await repository.get_for_user(
            uid=uid,
            conversation_id=conversation_id,
            job_id=job_id,
        )
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "reinterpretation_not_found"})
        return job

    @router.get("/v1/ella/conversations/{conversation_id}/reinterpretations/latest")
    async def latest_reinterpretation(
        conversation_id: str,
        uid: str = Depends(auth.get_current_user_uid),
    ) -> dict[str, Any]:
        job = await _owned_job(uid=uid, conversation_id=conversation_id)
        return {"ok": True, "reinterpretation": public_job(job)}

    @router.get("/v1/ella/conversations/{conversation_id}/reinterpretations/{job_id}")
    async def get_reinterpretation(
        conversation_id: str,
        job_id: str,
        uid: str = Depends(auth.get_current_user_uid),
    ) -> dict[str, Any]:
        job = await _owned_job(uid=uid, conversation_id=conversation_id, job_id=job_id)
        return {"ok": True, "reinterpretation": public_job(job)}

    @router.post("/v1/ella/internal/memory-reinterpretations/run-once")
    async def run_once(
        x_operator_token: Optional[str] = Header(
            default=None,
            alias="X-Ella-Reinterpretation-Token",
        ),
    ) -> dict[str, Any]:
        _require_operator(x_operator_token)
        result = await worker.run_once("operator-run-once")
        return {"ok": True, "result": result}

    @router.get("/v1/ella/internal/memory-reinterpretations/metrics")
    async def metrics(
        x_operator_token: Optional[str] = Header(
            default=None,
            alias="X-Ella-Reinterpretation-Token",
        ),
    ) -> dict[str, Any]:
        _require_operator(x_operator_token)
        metrics_value = await repository.metrics()
        metrics_value["worker_runtime"] = worker_runtime_metrics()
        return {"ok": True, "metrics": metrics_value}

    return router
