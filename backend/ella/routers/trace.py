"""Authenticated, subject-scoped Ella routing telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ella.routers.resolve import _get_pool as _get_resolve_pool
from utils.ella.exact_firebase_auth import get_exact_firebase_uid, require_matching_firebase_uid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella/debug", tags=["ella-debug"])
_traces = deque(maxlen=200)
_pool: Optional[asyncpg.Pool] = None


async def _get_pool() -> asyncpg.Pool:
    """Get or create the trace pool, preferring the resolve router's pool."""
    global _pool
    if _pool is None:
        try:
            _pool = await _get_resolve_pool()
        except Exception:
            _pool = await asyncpg.create_pool(
                host="127.0.0.1",
                port=5433,
                user="postgres",
                password=os.getenv("ELLA_POSTGRES_PASSWORD", "postgres"),
                database="ella_ai",
                min_size=1,
                max_size=5,
            )
    return _pool


def _debug_reads_enabled() -> bool:
    return os.getenv("ELLA_DEBUG_ROUTES_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _require_debug_reads_enabled() -> None:
    if not _debug_reads_enabled():
        raise HTTPException(status_code=404, detail={"code": "ella_debug_routes_disabled"})


class RouteTrace:
    """A server-owned routing event; public responses expose only safe fields."""

    def __init__(self):
        self.trace_id = str(uuid.uuid4())[:8]
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.endpoint = ""
        self.method = ""
        self.client_ip = ""
        self.client_type = ""
        self.client_version = ""
        self.uid = ""
        self.debug_level = None
        self.resolved_agent = ""
        self.resolved_gateway = ""
        self.resolved_session_key = ""
        self.resolve_source = ""
        self.openclaw_status = None
        self.openclaw_latency_ms = 0
        self.response_status = None
        self.total_latency_ms = 0
        self.error = ""
        self.notes = []
        self.client_route = ""
        self.client_headers = {}

    def to_dict(self) -> dict:
        """Retain the internal interface used by existing diagnostics tests."""
        return {
            "traceId": self.trace_id,
            "timestamp": self.timestamp,
            "endpoint": self.endpoint,
            "method": self.method,
            "clientIp": self.client_ip,
            "clientType": self.client_type,
            "clientVersion": self.client_version,
            "uid": self.uid,
            "debugLevel": self.debug_level,
            "resolvedAgent": self.resolved_agent,
            "resolvedGateway": self.resolved_gateway,
            "resolvedSessionKey": self.resolved_session_key,
            "resolveSource": self.resolve_source,
            "openclawStatus": self.openclaw_status,
            "openclawLatencyMs": self.openclaw_latency_ms,
            "responseStatus": self.response_status,
            "totalLatencyMs": self.total_latency_ms,
            "error": self.error,
            "notes": self.notes,
            "clientRoute": self.client_route,
            "clientHeaders": self.client_headers,
        }

    def to_public_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "timestamp": self.timestamp,
            "endpoint": self.endpoint,
            "method": self.method,
            "clientType": self.client_type,
            "clientVersion": self.client_version,
            "debugLevel": self.debug_level,
            "responseStatus": self.response_status,
            "totalLatencyMs": self.total_latency_ms,
            "hasError": bool(self.error),
        }


async def _persist_trace(trace: RouteTrace) -> None:
    try:
        pool = await _get_pool()
        await pool.execute(
            """
            INSERT INTO routing_traces (
                trace_id, created_at, endpoint, method, client_ip,
                client_type, client_version, uid, debug_level,
                resolved_agent, resolved_gateway, resolved_session_key,
                resolve_source, openclaw_status, openclaw_latency_ms,
                response_status, total_latency_ms, error, notes,
                client_route, client_headers
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12, $13, $14, $15, $16, $17, $18, $19, $20, $21
            )
            """,
            trace.trace_id,
            datetime.fromisoformat(trace.timestamp),
            trace.endpoint or None,
            trace.method or None,
            trace.client_ip or None,
            trace.client_type or None,
            trace.client_version or None,
            trace.uid or None,
            trace.debug_level,
            trace.resolved_agent or None,
            trace.resolved_gateway or None,
            trace.resolved_session_key or None,
            trace.resolve_source or None,
            trace.openclaw_status,
            trace.openclaw_latency_ms or 0,
            trace.response_status,
            trace.total_latency_ms or 0,
            trace.error or None,
            json.dumps(trace.notes),
            trace.client_route or None,
            json.dumps(trace.client_headers),
        )
    except Exception:
        logger.warning("code=routing_trace_persist_failed classification=unexpected")


def record_trace(trace: RouteTrace) -> None:
    """Record a server-owned trace without logging runtime coordinates."""
    _traces.appendleft(trace)
    logger.info(
        "code=routing_trace_recorded endpoint=%s status=%s latency_ms=%s",
        trace.endpoint,
        trace.response_status,
        trace.total_latency_ms,
    )
    try:
        asyncio.get_running_loop().create_task(_persist_trace(trace))
    except RuntimeError:
        pass


class ClientTracePayload(BaseModel):
    """Bounded, content-free client telemetry."""

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
    uid = require_matching_firebase_uid(authenticated_uid, payload.uid, feature="Client trace")
    trace = RouteTrace()
    trace.trace_id = f"c-{str(uuid.uuid4())[:6]}"
    trace.endpoint = "client-reported"
    trace.method = "CLIENT"
    trace.client_type = payload.clientType if payload.clientType in {"ios", "dashboard", "web"} else "unknown"
    trace.client_version = payload.clientVersion
    trace.uid = uid
    trace.debug_level = payload.debugLevel if payload.debugLevel >= 0 else None
    trace.response_status = payload.status if payload.status > 0 else None
    trace.total_latency_ms = payload.latencyMs
    trace.notes = ["client-telemetry"]
    record_trace(trace)
    return {"ok": True, "traceId": trace.trace_id}


async def _traces_from_db(
    limit: int,
    uid: str,
    hours: int,
    client_type: Optional[str],
    errors_only: bool,
) -> dict:
    pool = await _get_pool()
    conditions = ["created_at > NOW() - $1::interval", "uid = $2"]
    params = [timedelta(hours=hours), uid]
    if client_type:
        conditions.append(f"client_type = ${len(params) + 1}")
        params.append(client_type)
    if errors_only:
        conditions.append("error IS NOT NULL AND error != ''")
    rows = await pool.fetch(
        f"""
        SELECT trace_id, created_at, endpoint, method, client_type,
               client_version, debug_level, response_status, total_latency_ms,
               CASE WHEN error IS NULL OR error = '' THEN FALSE ELSE TRUE END AS has_error
        FROM routing_traces
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at DESC
        LIMIT {limit}
        """,
        *params,
    )
    return {
        "source": "database",
        "count": len(rows),
        "lookbackHours": hours,
        "traces": [
            {
                "traceId": row["trace_id"],
                "timestamp": row["created_at"].isoformat() if row["created_at"] else None,
                "endpoint": row["endpoint"],
                "method": row["method"],
                "clientType": row["client_type"],
                "clientVersion": row["client_version"],
                "debugLevel": row["debug_level"],
                "responseStatus": row["response_status"],
                "totalLatencyMs": row["total_latency_ms"],
                "hasError": row["has_error"],
            }
            for row in rows
        ],
    }


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
    exact_uid = require_matching_firebase_uid(authenticated_uid, uid, feature="Routing traces")
    if source == "db" or hours > 0:
        return await _traces_from_db(limit, exact_uid, hours or 24, client_type, errors_only)
    traces = [trace for trace in _traces if trace.uid == exact_uid]
    if client_type:
        traces = [trace for trace in traces if trace.client_type == client_type]
    if errors_only:
        traces = [trace for trace in traces if trace.error]
    return {
        "source": "memory",
        "count": len(traces[:limit]),
        "total": len(traces),
        "traces": [trace.to_public_dict() for trace in traces[:limit]],
    }


@router.get("/trace/{uid}")
async def get_user_traces(
    uid: str,
    limit: int = Query(20, ge=1, le=100),
    source: Optional[str] = Query(None),
    hours: int = Query(0, ge=0, le=168),
    authenticated_uid: str = Depends(get_exact_firebase_uid),
    _debug_enabled: None = Depends(_require_debug_reads_enabled),
):
    exact_uid = require_matching_firebase_uid(authenticated_uid, uid, feature="Routing traces")
    if source == "db" or hours > 0:
        return await _traces_from_db(limit, exact_uid, hours or 24, None, False)
    traces = [trace for trace in _traces if trace.uid == exact_uid]
    return {
        "uid": exact_uid,
        "source": "memory",
        "count": len(traces[:limit]),
        "traces": [trace.to_public_dict() for trace in traces[:limit]],
    }


@router.get("/stats")
async def trace_stats(
    hours: int = Query(24, ge=1, le=168),
    authenticated_uid: str = Depends(get_exact_firebase_uid),
    _debug_enabled: None = Depends(_require_debug_reads_enabled),
):
    pool = await _get_pool()
    row = await pool.fetchrow(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE error IS NOT NULL AND error != '') AS errors,
               AVG(total_latency_ms) FILTER (WHERE total_latency_ms > 0) AS avg_latency_ms,
               MAX(total_latency_ms) AS max_latency_ms,
               COUNT(*) FILTER (WHERE method = 'CLIENT') AS client_reported
        FROM routing_traces
        WHERE created_at > NOW() - $1::interval AND uid = $2
        """,
        timedelta(hours=hours),
        authenticated_uid,
    )
    return {
        "lookbackHours": hours,
        "total": row["total"],
        "errors": row["errors"],
        "avgLatencyMs": round(float(row["avg_latency_ms"] or 0), 1),
        "maxLatencyMs": row["max_latency_ms"] or 0,
        "clientReported": row["client_reported"],
    }


@router.get("/status")
async def debug_status(
    authenticated_uid: str = Depends(get_exact_firebase_uid),
    _debug_enabled: None = Depends(_require_debug_reads_enabled),
):
    pool = await _get_pool()
    row = await pool.fetchrow(
        """
        SELECT COUNT(*) AS cnt
        FROM routing_traces
        WHERE created_at > NOW() - $1::interval AND uid = $2
        """,
        timedelta(hours=24),
        authenticated_uid,
    )
    return {"status": "ok", "dbTraces24h": row["cnt"] if row else 0}


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
