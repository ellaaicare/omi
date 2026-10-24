"""Authenticated, content-free Ella routing telemetry.

Durable ``routing_traces`` writes are intentionally disabled.  The table has
no reviewed account-deletion cascade, so request, header, route, provider, and
runtime material must never be copied into it.  The remaining recorder emits
one fixed-schema log line synchronously and retains no per-user trace state.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from utils.ella.exact_firebase_auth import get_exact_firebase_uid, require_matching_firebase_uid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella/debug", tags=["ella-debug"])


def _debug_reads_enabled() -> bool:
    return os.getenv("ELLA_DEBUG_ROUTES_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _require_debug_reads_enabled() -> None:
    if not _debug_reads_enabled():
        raise HTTPException(status_code=404, detail={"code": "ella_debug_routes_disabled"})


class RouteTrace:
    """Minimal server-owned observability fields; no user or runtime material."""

    __slots__ = (
        "trace_id",
        "endpoint_class",
        "method",
        "debug_level",
        "response_status",
        "total_latency_ms",
        "has_error",
    )

    def __init__(self):
        self.trace_id = str(uuid.uuid4())[:8]
        self.endpoint_class = "ella"
        self.method = ""
        self.debug_level = None
        self.response_status = None
        self.total_latency_ms = 0
        self.has_error = False

    def to_public_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "endpointClass": self.endpoint_class,
            "method": self.method,
            "debugLevel": self.debug_level,
            "responseStatus": self.response_status,
            "totalLatencyMs": self.total_latency_ms,
            "hasError": self.has_error,
        }

    def to_dict(self) -> dict:
        """Compatibility alias exposing the same fixed, content-free schema."""
        return self.to_public_dict()


def record_trace(trace: RouteTrace) -> None:
    """Synchronously log fixed fields without enqueueing or retaining a trace."""
    logger.info(
        "code=routing_trace endpoint_class=%s method=%s status=%s latency_ms=%s has_error=%s",
        trace.endpoint_class,
        trace.method,
        trace.response_status,
        trace.total_latency_ms,
        trace.has_error,
    )


class ClientTracePayload(BaseModel):
    """Bounded client telemetry used only for authenticated request validation."""

    uid: str = Field(default="", max_length=128)
    clientType: str = Field(default="", max_length=24)
    clientVersion: str = Field(default="", max_length=32)
    debugLevel: int = Field(default=-1, ge=-1, le=4)
    latencyMs: int = Field(default=0, ge=0, le=3_600_000)
    status: int = Field(default=0, ge=0, le=599)


@router.post("/client-trace")
async def ingest_client_trace(
    payload: ClientTracePayload,
    authenticated_uid: str = Depends(get_exact_firebase_uid),
):
    require_matching_firebase_uid(authenticated_uid, payload.uid, feature="Client trace")
    trace = RouteTrace()
    trace.trace_id = f"c-{str(uuid.uuid4())[:6]}"
    trace.endpoint_class = "client-reported"
    trace.method = "CLIENT"
    trace.debug_level = payload.debugLevel if payload.debugLevel >= 0 else None
    trace.response_status = payload.status if payload.status > 0 else None
    trace.total_latency_ms = payload.latencyMs
    record_trace(trace)
    return {"ok": True, "traceId": trace.trace_id, "retained": False}


def _empty_trace_response() -> dict:
    return {"source": "disabled", "count": 0, "total": 0, "traces": []}


@router.get("/traces")
async def get_traces(
    limit: int = Query(50, ge=1, le=500),
    uid: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    hours: int = Query(0, ge=0, le=168),
    client_type: Optional[str] = Query(None),
    errors_only: bool = Query(False),
    authenticated_uid: str = Depends(get_exact_firebase_uid),
    _debug_enabled: None = Depends(_require_debug_reads_enabled),
):
    del limit, source, hours, client_type, errors_only
    require_matching_firebase_uid(authenticated_uid, uid, feature="Routing traces")
    return _empty_trace_response()


@router.get("/trace/{uid}")
async def get_user_traces(
    uid: str,
    limit: int = Query(20, ge=1, le=100),
    source: Optional[str] = Query(None),
    hours: int = Query(0, ge=0, le=168),
    authenticated_uid: str = Depends(get_exact_firebase_uid),
    _debug_enabled: None = Depends(_require_debug_reads_enabled),
):
    del limit, source, hours
    exact_uid = require_matching_firebase_uid(authenticated_uid, uid, feature="Routing traces")
    return {"uid": exact_uid, **_empty_trace_response()}


@router.get("/stats")
async def trace_stats(
    hours: int = Query(24, ge=1, le=168),
    _authenticated_uid: str = Depends(get_exact_firebase_uid),
    _debug_enabled: None = Depends(_require_debug_reads_enabled),
):
    return {
        "lookbackHours": hours,
        "storage": "disabled",
        "total": 0,
        "errors": 0,
        "avgLatencyMs": 0.0,
        "maxLatencyMs": 0,
        "clientReported": 0,
    }


@router.get("/status")
async def debug_status(
    _authenticated_uid: str = Depends(get_exact_firebase_uid),
    _debug_enabled: None = Depends(_require_debug_reads_enabled),
):
    return {"status": "ok", "traceStorage": "disabled"}


@router.get("/console", response_class=HTMLResponse)
async def debug_console(
    _authenticated_uid: str = Depends(get_exact_firebase_uid),
    _debug_enabled: None = Depends(_require_debug_reads_enabled),
):
    console_path = "/var/www/ella-ai-care.com/debug-console.html"
    try:
        with open(console_path, "r", encoding="utf-8") as handle:
            return HTMLResponse(content=handle.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Debug console not found</h1>", status_code=404)
