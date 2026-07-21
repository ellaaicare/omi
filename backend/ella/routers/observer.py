"""Admin Observer runner endpoints.

These endpoints are intended for the Hermes cron/no-agent job and operational
smoke tests. They are token-gated and default to dry-run behavior.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ella.routers.canonical_events import CanonicalEventStore, PostgresCanonicalEventStore
from ella.services.observer_apply import apply_pending_observer_memory_proposals
from ella.services.observer import observer_log_to_dict, run_observer
from ella.services.observer_extractor import build_extraction_result, combined_extractor, normalize_extractor_mode
from ella.services.observer_logs import ObserverRunLogStore, PostgresObserverRunLogStore

DEFAULT_OBSERVER_LIMIT = 100
MAX_OBSERVER_LIMIT = 500


class ObserverRunRequest(BaseModel):
    uid: str
    canonical_identity: str = ""
    since: Optional[datetime | str] = None
    cursor_before: str = ""
    dry_run: bool = True
    limit: int = DEFAULT_OBSERVER_LIMIT
    channels: list[str] = Field(default_factory=list)
    extractor_mode: str = ""
    extractor_limit: int = 60
    extractor_timeout_seconds: float = 45.0
    model_metadata: dict[str, Any] = Field(default_factory=dict)


class ObserverApplyRequest(BaseModel):
    uid: str
    dry_run: bool = True
    limit: int = 20
    min_confidence: float = 0.9
    proposal_types: list[str] = Field(default_factory=list)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _observer_token() -> str:
    return _env("ELLA_OBSERVER_ADMIN_TOKEN", _env("ELLA_ADMIN_TOKEN", ""))


def _token_from_authorization(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    value = authorization.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _authenticate(authorization: Optional[str], x_ella_observer_token: Optional[str]) -> None:
    configured = _observer_token()
    if not configured:
        raise HTTPException(status_code=503, detail="Observer admin token is not configured")
    supplied = (x_ella_observer_token or "").strip() or _token_from_authorization(authorization)
    if supplied != configured:
        raise HTTPException(status_code=401, detail="Invalid observer admin token")


def _parse_datetime(value: datetime | str | None) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid since timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sanitize_limit(limit: int) -> int:
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be >= 1")
    return min(limit, MAX_OBSERVER_LIMIT)


def create_observer_router(
    *,
    event_store: Optional[CanonicalEventStore] = None,
    log_store: Optional[ObserverRunLogStore] = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/ella/observer", tags=["Ella Observer"])
    events = event_store or PostgresCanonicalEventStore()
    logs = log_store or PostgresObserverRunLogStore()

    @router.post("/run")
    async def run(
        request: ObserverRunRequest,
        authorization: Optional[str] = Header(default=None),
        x_ella_observer_token: Optional[str] = Header(default=None, alias="X-Ella-Observer-Token"),
    ):
        _authenticate(authorization, x_ella_observer_token)
        if not request.uid:
            raise HTTPException(status_code=400, detail="uid is required")
        source_events = await events.timeline(
            uid=request.uid,
            since=_parse_datetime(request.since),
            limit=_sanitize_limit(request.limit),
            channels=request.channels or None,
        )
        extractor_mode = normalize_extractor_mode(request.extractor_mode)
        extraction = await build_extraction_result(
            source_events,
            mode=extractor_mode,
            uid=request.uid,
            timeout_seconds=max(5.0, min(float(request.extractor_timeout_seconds or 45.0), 120.0)),
            limit=max(1, min(int(request.extractor_limit or 60), 100)),
        )
        log = run_observer(
            profile_uid=request.uid,
            canonical_identity=request.canonical_identity,
            cursor_before=request.cursor_before,
            dry_run=request.dry_run,
            events=source_events,
            extractor=combined_extractor(extraction),
            model_metadata={
                "extractor_mode": extractor_mode,
                **(extraction.metadata or {}),
                **(request.model_metadata or {}),
            },
        )
        await logs.save(log)
        return {"ok": True, "observer_run": observer_log_to_dict(log)}

    @router.post("/apply-pending")
    async def apply_pending(
        request: ObserverApplyRequest,
        authorization: Optional[str] = Header(default=None),
        x_ella_observer_token: Optional[str] = Header(default=None, alias="X-Ella-Observer-Token"),
    ):
        _authenticate(authorization, x_ella_observer_token)
        if not request.uid:
            raise HTTPException(status_code=400, detail="uid is required")
        return await apply_pending_observer_memory_proposals(
            profile_uid=request.uid,
            event_store=events,
            dry_run=request.dry_run,
            limit=_sanitize_limit(request.limit),
            min_confidence=max(0.0, min(float(request.min_confidence), 1.0)),
            proposal_types={str(item) for item in request.proposal_types if str(item)} or None,
        )

    @router.get("/runs/{run_id}")
    async def get_run(
        run_id: str,
        authorization: Optional[str] = Header(default=None),
        x_ella_observer_token: Optional[str] = Header(default=None, alias="X-Ella-Observer-Token"),
    ):
        _authenticate(authorization, x_ella_observer_token)
        log = await logs.get(run_id)
        if not log:
            raise HTTPException(status_code=404, detail="Observer run not found")
        return {"ok": True, "observer_run": log}

    @router.get("/health")
    async def health(
        authorization: Optional[str] = Header(default=None),
        x_ella_observer_token: Optional[str] = Header(default=None, alias="X-Ella-Observer-Token"),
    ):
        _authenticate(authorization, x_ella_observer_token)
        return {
            "ok": True,
            "mode": "proposal_only",
            "default_dry_run": True,
            "default_extractor_mode": normalize_extractor_mode(""),
        }

    return router


router = create_observer_router()
