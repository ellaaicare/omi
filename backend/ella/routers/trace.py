"""
Ella Request Tracing — Durable, deletion-fenced telemetry

Every routing decision is captured:
- Postgres `routing_traces` table for historical queries, analytics, retention

Endpoints:
  GET  /v1/ella/debug/traces        — recent traces (from DB)
  GET  /v1/ella/debug/trace/{uid}   — traces for a specific user
  GET  /v1/ella/debug/status        — routing config health
  POST /v1/ella/debug/client-trace  — iOS/client-reported trace (ingest)
  GET  /v1/ella/debug/stats         — aggregate stats (from DB)
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from database import content_write_fence
from ella.config import ELLA_CONFIG
from ella.routers.resolve import _get_pool as _get_resolve_pool
from utils.other import endpoints as auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella/debug", tags=["ella-debug"])

# Database pool (lazy-initialized, shared via resolve.py's pool)
_pool: Optional[asyncpg.Pool] = None


async def _get_pool() -> asyncpg.Pool:
    """Get or create the asyncpg connection pool for traces."""
    global _pool
    if _pool is None:
        # Try to reuse resolve.py's pool first
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


class RouteTrace:
    """A single routing event through the system."""

    def __init__(self):
        self.trace_id = str(uuid.uuid4())[:8]
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.endpoint = ""
        self.method = ""
        self.client_ip = ""
        self.client_type = ""  # "ios", "dashboard", "curl", "e2e-debugger"
        self.client_version = ""  # "1.2.3"
        self.uid = ""
        self.debug_level = None
        self.resolved_agent = ""
        self.resolved_gateway = ""
        self.resolved_session_key = ""
        self.resolve_source = ""  # "database", "fallback", "header_override"
        self.openclaw_status = None
        self.openclaw_latency_ms = 0
        self.response_status = None
        self.total_latency_ms = 0
        self.error = ""
        self.notes = []
        self.client_route = ""  # route the client thinks it's using
        self.client_headers = {}  # raw X-Ella-* headers from client

    def to_dict(self):
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


async def _persist_trace(trace: RouteTrace):
    """Write one trace while its transferred deletion fence remains live."""
    try:
        content_write_fence.assert_detached_content_writer_current(trace.uid)
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
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11, $12,
                $13, $14, $15,
                $16, $17, $18, $19,
                $20, $21
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
    except Exception as e:
        logger.warning(f"[TRACE] Failed to persist trace {trace.trace_id}: {e}")


async def record_trace(trace: RouteTrace) -> asyncio.Task:
    """Transfer a writer registration before detached durable persistence."""
    if not trace.uid:
        raise content_write_fence.ContentWriteFenceError("account_content_fence_unavailable")
    content_write_fence.assert_content_writer_admitted(trace.uid)
    logger.info(
        f"[TRACE {trace.trace_id}] {trace.endpoint} "
        f"-> agent={trace.resolved_agent} via={trace.resolve_source} "
        f"gateway={trace.resolved_gateway} "
        f"openclaw={trace.openclaw_status} "
        f"total={trace.total_latency_ms}ms"
    )
    return await content_write_fence.start_content_writer_task(
        trace.uid,
        lambda: _persist_trace(trace),
        name=f"routing-trace-{trace.trace_id}",
    )


# --------------- Client Trace Ingestion ---------------


class ClientTracePayload(BaseModel):
    """Bounded, content-free client telemetry; extra legacy fields are ignored."""

    uid: str = Field(default="", max_length=128)
    clientType: str = Field(default="", max_length=24)
    debugLevel: int = Field(default=-1, ge=-1, le=4)
    latencyMs: int = Field(default=0, ge=0, le=3_600_000)
    status: int = Field(default=0, ge=0, le=599)


@router.post("/client-trace")
async def ingest_client_trace(
    payload: ClientTracePayload,
    authenticated_uid: str = Depends(auth.get_writable_user_uid),
):
    """Ingest bounded telemetry for the authenticated writable subject."""
    if payload.uid and payload.uid != authenticated_uid:
        raise HTTPException(status_code=403, detail={"code": "ownership_mismatch"})

    trace = RouteTrace()
    trace.trace_id = f"c-{str(uuid.uuid4())[:6]}"  # "c-" prefix = client-reported
    trace.endpoint = "client-reported"
    trace.method = "CLIENT"
    trace.client_type = payload.clientType if payload.clientType in {"ios", "dashboard", "web"} else "unknown"
    trace.uid = authenticated_uid
    trace.debug_level = payload.debugLevel if payload.debugLevel >= 0 else None
    trace.response_status = payload.status if payload.status > 0 else None
    trace.total_latency_ms = payload.latencyMs
    trace.notes = ["client-telemetry"]

    await record_trace(trace)

    return {"ok": True, "traceId": trace.trace_id}


# --------------- Query Endpoints ---------------


@router.get("/traces")
async def get_traces(
    limit: int = Query(50, ge=1, le=500),
    uid: Optional[str] = Query(None),
    source: Optional[str] = Query(None, description="legacy parameter; storage is always db"),
    hours: int = Query(0, ge=0, le=168, description="DB lookback hours (0=24 hours)"),
    client_type: Optional[str] = Query(None),
    errors_only: bool = Query(False),
    authenticated_uid: str = Depends(auth.get_current_user_uid),
):
    """Get durable routing traces for the authenticated subject only."""
    del source
    if uid and uid != authenticated_uid:
        raise HTTPException(status_code=403, detail={"code": "ownership_mismatch"})
    return await _traces_from_db(limit, authenticated_uid, hours or 24, client_type, errors_only)


async def _traces_from_db(
    limit: int,
    uid: Optional[str],
    hours: int,
    client_type: Optional[str],
    errors_only: bool,
):
    """Query traces from postgres."""
    pool = await _get_pool()
    conditions = ["created_at > NOW() - $1::interval"]
    params = [timedelta(hours=hours)]
    idx = 2

    if uid:
        conditions.append(f"uid = ${idx}")
        params.append(uid)
        idx += 1
    if client_type:
        conditions.append(f"client_type = ${idx}")
        params.append(client_type)
        idx += 1
    if errors_only:
        conditions.append("error IS NOT NULL AND error != ''")

    where = " AND ".join(conditions)
    rows = await pool.fetch(
        f"""
        SELECT trace_id, created_at, endpoint, method, client_ip,
               client_type, client_version, uid, debug_level,
               resolved_agent, resolved_gateway, resolved_session_key,
               resolve_source, openclaw_status, openclaw_latency_ms,
               response_status, total_latency_ms, error, notes,
               client_route, client_headers
        FROM routing_traces
        WHERE {where}
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
                "traceId": r["trace_id"],
                "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
                "endpoint": r["endpoint"],
                "method": r["method"],
                "clientIp": r["client_ip"],
                "clientType": r["client_type"],
                "clientVersion": r["client_version"],
                "uid": r["uid"],
                "debugLevel": r["debug_level"],
                "resolvedAgent": r["resolved_agent"],
                "resolvedGateway": r["resolved_gateway"],
                "resolvedSessionKey": r["resolved_session_key"],
                "resolveSource": r["resolve_source"],
                "openclawStatus": r["openclaw_status"],
                "openclawLatencyMs": r["openclaw_latency_ms"],
                "responseStatus": r["response_status"],
                "totalLatencyMs": r["total_latency_ms"],
                "error": r["error"],
                "notes": json.loads(r["notes"]) if r["notes"] else [],
                "clientRoute": r["client_route"],
                "clientHeaders": json.loads(r["client_headers"]) if r["client_headers"] else {},
            }
            for r in rows
        ],
    }


@router.get("/trace/{uid}")
async def get_user_traces(
    uid: str,
    limit: int = Query(20, ge=1, le=100),
    source: Optional[str] = Query(None),
    hours: int = Query(0, ge=0, le=168),
    authenticated_uid: str = Depends(auth.get_current_user_uid),
):
    """Get durable routing traces for the exact authenticated subject."""
    del source
    if uid != authenticated_uid:
        raise HTTPException(status_code=403, detail={"code": "ownership_mismatch"})
    return await _traces_from_db(limit, authenticated_uid, hours or 24, None, False)


@router.get("/stats")
async def trace_stats(
    hours: int = Query(24, ge=1, le=168),
    authenticated_uid: str = Depends(auth.get_current_user_uid),
):
    """Aggregate trace statistics for the authenticated subject."""
    pool = await _get_pool()
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(DISTINCT uid) AS unique_users,
            COUNT(*) FILTER (WHERE error IS NOT NULL AND error != '') AS errors,
            AVG(total_latency_ms) FILTER (WHERE total_latency_ms > 0) AS avg_latency_ms,
            MAX(total_latency_ms) AS max_latency_ms,
            COUNT(*) FILTER (WHERE method = 'CLIENT') AS client_reported
        FROM routing_traces
        WHERE created_at > NOW() - $1::interval
          AND uid = $2
        """,
        timedelta(hours=hours),
        authenticated_uid,
    )
    # Dynamic client type breakdown
    client_rows = await pool.fetch(
        """
        SELECT COALESCE(NULLIF(client_type, ''), 'unknown') AS ctype, COUNT(*) AS cnt
        FROM routing_traces
        WHERE created_at > NOW() - $1::interval
          AND uid = $2
        GROUP BY ctype
        ORDER BY cnt DESC
        """,
        timedelta(hours=hours),
        authenticated_uid,
    )
    # Dynamic resolve source breakdown
    source_rows = await pool.fetch(
        """
        SELECT COALESCE(NULLIF(resolve_source, ''), 'unknown') AS src, COUNT(*) AS cnt
        FROM routing_traces
        WHERE created_at > NOW() - $1::interval
          AND uid = $2
        GROUP BY src
        ORDER BY cnt DESC
        """,
        timedelta(hours=hours),
        authenticated_uid,
    )
    # Top agents
    agent_rows = await pool.fetch(
        """
        SELECT resolved_agent, COUNT(*) as cnt
        FROM routing_traces
        WHERE created_at > NOW() - $1::interval
          AND uid = $2
          AND resolved_agent IS NOT NULL AND resolved_agent != ''
        GROUP BY resolved_agent
        ORDER BY cnt DESC
        LIMIT 10
        """,
        timedelta(hours=hours),
        authenticated_uid,
    )
    return {
        "lookbackHours": hours,
        "total": row["total"],
        "uniqueUsers": row["unique_users"],
        "errors": row["errors"],
        "avgLatencyMs": round(float(row["avg_latency_ms"] or 0), 1),
        "maxLatencyMs": row["max_latency_ms"] or 0,
        "byClientType": {r["ctype"]: r["cnt"] for r in client_rows},
        "clientReported": row["client_reported"],
        "byResolveSource": {r["src"]: r["cnt"] for r in source_rows},
        "topAgents": [{"agent": r["resolved_agent"], "count": r["cnt"]} for r in agent_rows],
    }


@router.get("/status")
async def debug_status(authenticated_uid: str = Depends(auth.get_current_user_uid)):
    """Current routing configuration and subject-scoped durable health."""
    # Quick DB count
    db_count = 0
    try:
        pool = await _get_pool()
        row = await pool.fetchrow(
            """
            SELECT COUNT(*) AS cnt
            FROM routing_traces
            WHERE created_at > NOW() - $1::interval
              AND uid = $2
            """,
            timedelta(hours=24),
            authenticated_uid,
        )
        db_count = row["cnt"] if row else 0
    except Exception:
        pass

    return {
        "debugLevel": ELLA_CONFIG.debug_level,
        "openclawUrl": os.getenv("OPENCLAW_URL", "NOT SET"),
        "openclawTokenSet": bool(os.getenv("OPENCLAW_GATEWAY_TOKEN")),
        "traceStorage": "database",
        "dbTraces24h": db_count,
    }


# --------------- Debug Console (HTML) ---------------


@router.get("/console", response_class=HTMLResponse)
async def debug_console(_authenticated_uid: str = Depends(auth.get_current_user_uid)):
    """Serve the Routing Debug Console HTML page."""
    console_path = "/var/www/ella-ai-care.com/debug-console.html"
    try:
        with open(console_path, "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Debug console not found</h1>", status_code=404)
