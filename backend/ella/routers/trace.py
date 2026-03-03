"""
Ella Request Tracing — Persistent + In-Memory

Every routing decision is captured:
- In-memory ring buffer (200 entries) for instant dashboard polling
- Postgres `routing_traces` table for historical queries, analytics, retention

Endpoints:
  GET  /v1/ella/debug/traces        — recent traces (from memory or DB)
  GET  /v1/ella/debug/trace/{uid}   — traces for a specific user
  GET  /v1/ella/debug/status        — routing config health
  POST /v1/ella/debug/client-trace  — iOS/client-reported trace (ingest)
  GET  /v1/ella/debug/stats         — aggregate stats (from DB)
"""

import json
import logging
import os
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella/debug", tags=["ella-debug"])

# In-memory ring buffer for fast recent queries
_traces = deque(maxlen=200)

# Database pool (lazy-initialized, shared via resolve.py's pool)
_pool: Optional[asyncpg.Pool] = None


async def _get_pool() -> asyncpg.Pool:
    """Get or create the asyncpg connection pool for traces."""
    global _pool
    if _pool is None:
        # Try to reuse resolve.py's pool first
        try:
            from ella.routers.resolve import _get_pool as _get_resolve_pool
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
        self.client_type = ""       # "ios", "dashboard", "curl", "e2e-debugger"
        self.client_version = ""    # "1.2.3"
        self.uid = ""
        self.debug_level = None
        self.resolved_agent = ""
        self.resolved_gateway = ""
        self.resolved_session_key = ""
        self.resolve_source = ""    # "database", "fallback", "header_override"
        self.openclaw_status = None
        self.openclaw_latency_ms = 0
        self.response_status = None
        self.total_latency_ms = 0
        self.error = ""
        self.notes = []
        self.client_route = ""      # route the client thinks it's using
        self.client_headers = {}    # raw X-Ella-* headers from client

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
    """Write trace to postgres. Fire-and-forget — errors logged, not raised."""
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


def record_trace(trace: RouteTrace):
    """Add a trace to the in-memory buffer, log it, and queue DB persist."""
    _traces.appendleft(trace)
    logger.info(
        f"[TRACE {trace.trace_id}] {trace.endpoint} uid={trace.uid} "
        f"-> agent={trace.resolved_agent} via={trace.resolve_source} "
        f"gateway={trace.resolved_gateway} "
        f"openclaw={trace.openclaw_status} "
        f"total={trace.total_latency_ms}ms"
    )
    # Fire-and-forget DB persist via background task
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist_trace(trace))
    except RuntimeError:
        # No running loop (shouldn't happen in FastAPI, but safety net)
        pass


# --------------- Client Trace Ingestion ---------------

class ClientTracePayload(BaseModel):
    """Trace data reported by iOS/web clients."""
    uid: str
    route: str = ""                # e.g. "/v1/ella/chat/stream"
    resolvedAgent: str = ""
    resolvedGateway: str = ""
    sessionKey: str = ""
    resolveSource: str = ""        # "cached", "api_resolve", "hardcoded"
    clientType: str = ""           # "ios", "dashboard"
    clientVersion: str = ""
    debugLevel: int = -1
    latencyMs: int = 0
    status: int = 0
    error: str = ""
    notes: list = []
    headers: dict = {}


@router.post("/client-trace")
async def ingest_client_trace(payload: ClientTracePayload, request: Request):
    """Ingest a trace reported by a client (iOS app, dashboard, etc).

    Clients call this after completing a chat request to report their
    view of the routing — which endpoint they hit, what agent they resolved,
    timing from their perspective.
    """
    trace = RouteTrace()
    trace.trace_id = f"c-{str(uuid.uuid4())[:6]}"  # "c-" prefix = client-reported
    trace.endpoint = payload.route or "client-reported"
    trace.method = "CLIENT"
    trace.client_ip = request.client.host if request.client else ""
    trace.client_type = payload.clientType
    trace.client_version = payload.clientVersion
    trace.uid = payload.uid
    trace.debug_level = payload.debugLevel if payload.debugLevel >= 0 else None
    trace.resolved_agent = payload.resolvedAgent
    trace.resolved_gateway = payload.resolvedGateway
    trace.resolved_session_key = payload.sessionKey
    trace.resolve_source = payload.resolveSource
    trace.openclaw_status = payload.status if payload.status > 0 else None
    trace.total_latency_ms = payload.latencyMs
    trace.error = payload.error
    trace.notes = payload.notes or ["client-reported"]
    trace.client_route = payload.route
    trace.client_headers = payload.headers

    record_trace(trace)

    return {"ok": True, "traceId": trace.trace_id}


# --------------- Query Endpoints ---------------

@router.get("/traces")
async def get_traces(
    limit: int = Query(50, ge=1, le=500),
    uid: Optional[str] = Query(None),
    source: Optional[str] = Query(None, description="memory or db"),
    hours: int = Query(0, ge=0, le=168, description="DB lookback hours (0=use memory)"),
    client_type: Optional[str] = Query(None),
    errors_only: bool = Query(False),
):
    """Get routing traces.

    By default returns from in-memory buffer (fast, last 200).
    Set source=db or hours>0 to query postgres for historical data.
    """
    if source == "db" or hours > 0:
        return await _traces_from_db(limit, uid, hours or 24, client_type, errors_only)

    # In-memory fast path
    traces = list(_traces)
    if uid:
        traces = [t for t in traces if t.uid == uid]
    if client_type:
        traces = [t for t in traces if t.client_type == client_type]
    if errors_only:
        traces = [t for t in traces if t.error]
    return {
        "source": "memory",
        "count": len(traces[:limit]),
        "total": len(traces),
        "traces": [t.to_dict() for t in traces[:limit]],
    }


async def _traces_from_db(
    limit: int, uid: Optional[str], hours: int,
    client_type: Optional[str], errors_only: bool,
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
):
    """Get routing traces for a specific user UID."""
    if source == "db" or hours > 0:
        return await _traces_from_db(limit, uid, hours or 24, None, False)

    traces = [t for t in _traces if t.uid == uid]
    return {
        "uid": uid,
        "source": "memory",
        "count": len(traces[:limit]),
        "traces": [t.to_dict() for t in traces[:limit]],
    }


@router.get("/stats")
async def trace_stats(hours: int = Query(24, ge=1, le=168)):
    """Aggregate trace statistics from the database."""
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
        """,
        timedelta(hours=hours),
    )
    # Dynamic client type breakdown
    client_rows = await pool.fetch(
        """
        SELECT COALESCE(NULLIF(client_type, ''), 'unknown') AS ctype, COUNT(*) AS cnt
        FROM routing_traces
        WHERE created_at > NOW() - $1::interval
        GROUP BY ctype
        ORDER BY cnt DESC
        """,
        timedelta(hours=hours),
    )
    # Dynamic resolve source breakdown
    source_rows = await pool.fetch(
        """
        SELECT COALESCE(NULLIF(resolve_source, ''), 'unknown') AS src, COUNT(*) AS cnt
        FROM routing_traces
        WHERE created_at > NOW() - $1::interval
        GROUP BY src
        ORDER BY cnt DESC
        """,
        timedelta(hours=hours),
    )
    # Top agents
    agent_rows = await pool.fetch(
        """
        SELECT resolved_agent, COUNT(*) as cnt
        FROM routing_traces
        WHERE created_at > NOW() - $1::interval
          AND resolved_agent IS NOT NULL AND resolved_agent != ''
        GROUP BY resolved_agent
        ORDER BY cnt DESC
        LIMIT 10
        """,
        timedelta(hours=hours),
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
async def debug_status():
    """Current routing configuration and health."""
    from ella.config import ELLA_CONFIG

    # Quick DB count
    db_count = 0
    try:
        pool = await _get_pool()
        row = await pool.fetchrow(
            "SELECT COUNT(*) AS cnt FROM routing_traces WHERE created_at > NOW() - $1::interval",
            timedelta(hours=24),
        )
        db_count = row["cnt"] if row else 0
    except Exception:
        pass

    return {
        "debugLevel": ELLA_CONFIG.debug_level,
        "openclawUrl": os.getenv("OPENCLAW_URL", "NOT SET"),
        "openclawTokenSet": bool(os.getenv("OPENCLAW_GATEWAY_TOKEN")),
        "traceBufferSize": len(_traces),
        "traceBufferMax": 200,
        "dbTraces24h": db_count,
        "recentErrors": len([t for t in _traces if t.error]),
        "uniqueUids": len(set(t.uid for t in _traces)),
        "lastTrace": _traces[0].to_dict() if _traces else None,
    }
