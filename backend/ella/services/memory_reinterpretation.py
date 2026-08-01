"""Durable post-session reinterpretation for memory-scoped voice sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator

import database.conversations as conversations_db
from database.memory_reinterpretations import canonical_transcript_hash
from ella.services.hermes_session import canonical_omi_session_key
from ella.services.summary_recovery import (
    extract_json_object,
    resolve_summary_provider_send,
    summary_provider_config_for_uid,
)
from ella.services.summary_writeback import ConcurrentConversationSummaryChangeError

logger = logging.getLogger(__name__)

_WORKER_RUNTIME_METRICS: dict[str, int] = {
    "loop_failures_total": 0,
    "lease_renewal_failures_total": 0,
}


class ReinterpretationSummary(BaseModel):
    title: str = ""
    overview: str
    emoji: str = ""
    category: str = "other"
    ella_tags: list[str] = Field(default_factory=lambda: ["omi", "reinterpretation"])
    ella_signal: dict[str, Any] = Field(default_factory=dict)


class ReinterpretationProposal(BaseModel):
    kind: Literal["factual_correction", "ambiguous_reinterpretation"]
    certainty: Literal["confirmed", "ambiguous"] = "ambiguous"
    correction_text: str = Field(min_length=1, max_length=4000)
    evidence_event_ids: list[str] = Field(default_factory=list)
    evidence_quote: str = Field(default="", max_length=1000)
    corrected_summary: Optional[ReinterpretationSummary] = None

    @field_validator("correction_text", "evidence_quote")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


class ReinterpretationPlan(BaseModel):
    outcome: Literal["no_change", "proposals"]
    proposals: list[ReinterpretationProposal] = Field(default_factory=list)

    @field_validator("proposals")
    @classmethod
    def _bound_proposals(cls, value: list[ReinterpretationProposal]) -> list[ReinterpretationProposal]:
        if len(value) > 20:
            raise ValueError("at most 20 reinterpretation proposals are allowed")
        return value


class ReinterpretationWorkerError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.retryable = retryable
        self.detail = detail


@dataclass(frozen=True)
class ApplyResult:
    correction_id: str
    active_summary_version_id: str
    idempotent_replay: bool = False


def _model_dump(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model.dict()


def _validate_plan(value: dict[str, Any]) -> ReinterpretationPlan:
    plan = ReinterpretationPlan(**value)
    if plan.outcome == "no_change" and plan.proposals:
        raise ValueError("no_change cannot include proposals")
    if plan.outcome == "proposals" and not plan.proposals:
        raise ValueError("proposals outcome requires at least one proposal")
    return plan


def _ordered_transcript(rows: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{row.get('event_id')}] {str(row.get('role') or 'unknown').upper()}: {str(row.get('text') or '').strip()}"
        for row in rows
        if str(row.get("text") or "").strip()
    )


def _current_summary(conversation: dict[str, Any]) -> dict[str, Any]:
    structured = conversation.get("structured") if isinstance(conversation.get("structured"), dict) else {}
    return {
        "title": str(structured.get("title") or conversation.get("title") or ""),
        "overview": str(structured.get("overview") or conversation.get("overview") or ""),
        "emoji": str(structured.get("emoji") or conversation.get("emoji") or ""),
        "category": str(structured.get("category") or conversation.get("category") or "other"),
    }


def _proposal_id(job_id: str, transcript_revision: int, index: int) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"ella:{job_id}:revision:{transcript_revision}:proposal:{index}",
        )
    )


def _correction_id(job_id: str, transcript_revision: int, index: int) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"ella:{job_id}:revision:{transcript_revision}:correction:{index}",
        )
    )


def _evidence_is_explicit(
    proposal: ReinterpretationProposal,
    rows_by_event_id: dict[str, dict[str, Any]],
) -> bool:
    if (
        proposal.kind != "factual_correction"
        or proposal.certainty != "confirmed"
        or proposal.corrected_summary is None
        or not proposal.evidence_event_ids
        or not proposal.evidence_quote
    ):
        return False
    quote = " ".join(proposal.evidence_quote.casefold().split())
    if not quote:
        return False
    matched_user_event = False
    for event_id in proposal.evidence_event_ids:
        row = rows_by_event_id.get(event_id)
        if row is None:
            return False
        text = " ".join(str(row.get("text") or "").casefold().split())
        if quote in text and str(row.get("role") or "").lower() == "user":
            matched_user_event = True
    return matched_user_event


def _validate_rows(job: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ReinterpretationWorkerError("canonical_session_empty", retryable=True)
    scope_kinds = {str(row.get("scope_kind") or "") for row in rows}
    if len(scope_kinds) != 1 or not scope_kinds.issubset({"memory", "daily_card"}):
        raise ReinterpretationWorkerError("canonical_scope_mismatch", retryable=False)
    for row in rows:
        if row.get("uid") != job["uid"] or row.get("session_id") != job["logical_session_id"]:
            raise ReinterpretationWorkerError("canonical_owner_mismatch", retryable=False)
        if (
            row.get("conversation_id") != job["conversation_id"]
            or row.get("active_summary_version_id") != job["starting_summary_version_id"]
        ):
            raise ReinterpretationWorkerError("canonical_scope_mismatch", retryable=False)
    if canonical_transcript_hash(rows) != job["transcript_hash"]:
        raise ReinterpretationWorkerError("canonical_transcript_changed", retryable=True)
    expected_refs = [
        (str(ref.get("event_id") or ""), str(ref.get("source_identity") or ""))
        for ref in job.get("canonical_refs") or []
    ]
    actual_refs = [(str(row.get("event_id") or ""), str(row.get("source_identity") or "")) for row in rows]
    if expected_refs != actual_refs:
        raise ReinterpretationWorkerError("canonical_reference_mismatch", retryable=True)


def build_reinterpretation_prompt(
    *,
    transcript: str,
    current_summary: dict[str, Any],
    event_ids: list[str],
) -> str:
    return f"""You are Ella's read-only memory reinterpretation analyst.

Review a completed memory-scoped voice conversation. You may reason and propose,
but you may not write memory. Return JSON only.

Rules:
- Ordinary reminiscing, questions, reactions, and unsupported guesses are no_change.
- A factual_correction must be an explicit factual clarification spoken by the user.
- evidence_quote must be an exact contiguous quote from a cited USER event.
- If intent or fact is ambiguous, use ambiguous_reinterpretation.
- For a confirmed factual_correction, corrected_summary must be a complete replacement
  for the current summary, preserve non-conflicting details, start overview with
  "[Ella] ", and never invent unsupported facts.
- Preserve proposal order as it appears in the conversation.
- Never propose caregiver, scanner, notification, or profile mutations.

Current memory summary:
{json.dumps(current_summary, ensure_ascii=False, sort_keys=True)}

Allowed evidence event IDs:
{json.dumps(event_ids)}

Canonical session transcript:
{transcript}

Return one of:
{{"outcome":"no_change","proposals":[]}}

or:
{{
  "outcome":"proposals",
  "proposals":[
    {{
      "kind":"factual_correction|ambiguous_reinterpretation",
      "certainty":"confirmed|ambiguous",
      "correction_text":"concise requested interpretation",
      "evidence_event_ids":["exact event id"],
      "evidence_quote":"exact user quote",
      "corrected_summary":{{
        "title":"short title",
        "overview":"[Ella] complete grounded summary",
        "emoji":"one emoji",
        "category":"category",
        "ella_tags":["omi","reinterpretation"],
        "ella_signal":{{}}
      }}
    }}
  ]
}}
"""


class HermesReinterpretationClient:
    async def propose(
        self,
        *,
        job: dict[str, Any],
        transcript: str,
        current_summary: dict[str, Any],
        event_ids: list[str],
    ) -> ReinterpretationPlan:
        config = await summary_provider_config_for_uid(job["uid"])
        if config.provider != "hermes-api":
            raise ReinterpretationWorkerError("hermes_required", retryable=False)
        if config.cloud_authority is None and not config.hermes_api_key:
            raise ReinterpretationWorkerError("hermes_api_key_missing", retryable=True)
        trace_id = f"memory-reinterpretation:{job['id']}"
        prompt = build_reinterpretation_prompt(
            transcript=transcript,
            current_summary=current_summary,
            event_ids=event_ids,
        )
        try:
            async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
                hermes_url, hermes_model, hermes_api_key = await resolve_summary_provider_send(config)
                headers = {
                    "Authorization": f"Bearer {hermes_api_key}",
                    "Content-Type": "application/json",
                    "X-Hermes-Session-Id": job["id"],
                    "X-Hermes-Session-Key": canonical_omi_session_key(job["uid"]),
                    "X-Trace-Id": trace_id,
                }
                response = await client.post(
                    hermes_url,
                    headers=headers,
                    json={
                        "model": hermes_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                        "max_tokens": 1800,
                    },
                )
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            return _validate_plan(extract_json_object(content))
        except ReinterpretationWorkerError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ReinterpretationWorkerError(
                "hermes_unavailable",
                retryable=True,
                detail=type(exc).__name__,
            ) from exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code >= 500 or exc.response.status_code == 429
            raise ReinterpretationWorkerError(
                f"hermes_http_{exc.response.status_code}",
                retryable=retryable,
            ) from exc
        except Exception as exc:
            raise ReinterpretationWorkerError(
                "hermes_invalid_response",
                retryable=True,
                detail=type(exc).__name__,
            ) from exc


async def _load_conversation(uid: str, conversation_id: str) -> Optional[dict[str, Any]]:
    return await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)


async def _create_pending_proposal(
    *,
    job: dict[str, Any],
    proposal: ReinterpretationProposal,
    proposal_id: str,
    proposal_index: int,
) -> str:
    from ella.services import proposal_ingest

    revision_trace = f"memory-reinterpretation:{job['id']}:revision:{job['transcript_revision']}"
    claims = {
        "sub": f"omi-worker:{job['uid']}",
        "profile_uid": job["uid"],
        "role": "system",
        "external_provider": "memory-reinterpretation-worker",
        "grant_id": "memory-reinterpretation-worker",
        "trace_id": f"{revision_trace}:proposal:{proposal_index}",
        "scopes": ["proposals:write"],
        "allowed_tools": ["memory_reinterpretation_propose"],
    }
    result = await asyncio.to_thread(
        proposal_ingest.create_proposal,
        session_claims=claims,
        tool_name="memory_reinterpretation_propose",
        proposal_type="summary_correction",
        payload={
            "title": f"Memory reinterpretation for conversation {job['conversation_id']}",
            "description": proposal.correction_text,
            "target": {
                "kind": "omi_conversation_summary",
                "conversation_id": job["conversation_id"],
                "active_summary_version_id": job["starting_summary_version_id"],
            },
            "evidence": {
                "canonical_event_ids": proposal.evidence_event_ids,
                "transcript_hash": job["transcript_hash"],
            },
            "proposal_index": proposal_index,
            "source": "memory-scoped-voice-session",
            "write_policy": "proposal_only",
        },
        idempotency_key=f"{revision_trace}:proposal:{proposal_index}",
    )
    created = result.get("proposal") or {}
    return str(created.get("proposal_id") or proposal_id)


async def _apply_proposal(
    *,
    job: dict[str, Any],
    proposal: ReinterpretationProposal,
    correction_id: str,
    proposal_index: int,
    expected_version_id: str,
) -> ApplyResult:
    from ella.routers.corrections import apply_memory_reinterpretation_correction

    result = await apply_memory_reinterpretation_correction(
        uid=job["uid"],
        conversation_id=job["conversation_id"],
        correction_id=correction_id,
        trace_id=(
            f"memory-reinterpretation:{job['id']}:" f"revision:{job['transcript_revision']}:proposal:{proposal_index}"
        ),
        active_summary_version_id=expected_version_id,
        correction_text=proposal.correction_text,
        corrected_summary=_model_dump(proposal.corrected_summary),
        evidence_event_ids=proposal.evidence_event_ids,
        source_session_id=job["logical_session_id"],
    )
    return ApplyResult(
        correction_id=correction_id,
        active_summary_version_id=str(result["active_summary_version_id"]),
        idempotent_replay=bool(result.get("idempotent_replay")),
    )


class MemoryReinterpretationWorker:
    def __init__(
        self,
        repository,
        *,
        hermes_client: Optional[HermesReinterpretationClient] = None,
        conversation_loader: Callable[[str, str], Awaitable[Optional[dict[str, Any]]]] = _load_conversation,
        pending_proposal_writer: Callable[..., Awaitable[str]] = _create_pending_proposal,
        correction_writer: Callable[..., Awaitable[ApplyResult]] = _apply_proposal,
        lease_seconds: Optional[int] = None,
    ):
        self.repository = repository
        self.hermes_client = hermes_client or HermesReinterpretationClient()
        self.conversation_loader = conversation_loader
        self.pending_proposal_writer = pending_proposal_writer
        self.correction_writer = correction_writer
        configured_lease = (
            lease_seconds
            if lease_seconds is not None
            else int(os.getenv("ELLA_MEMORY_REINTERPRETATION_LEASE_SECONDS", "120"))
        )
        self.lease_seconds = max(30, configured_lease)

    async def _require_current_lease(self, job: dict[str, Any]) -> None:
        try:
            current = await self.repository.renew_lease(
                job,
                lease_seconds=self.lease_seconds,
            )
        except Exception as exc:
            _WORKER_RUNTIME_METRICS["lease_renewal_failures_total"] += 1
            raise ReinterpretationWorkerError(
                "lease_renewal_failed",
                retryable=True,
                detail=type(exc).__name__,
            ) from exc
        if not current:
            raise ReinterpretationWorkerError("lease_lost", retryable=True)

    async def _heartbeat_lease(self, job: dict[str, Any]) -> None:
        interval = max(5.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.repository.renew_lease(
                    job,
                    lease_seconds=self.lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _WORKER_RUNTIME_METRICS["lease_renewal_failures_total"] += 1
                logger.exception(
                    "[FLOW:MEMORY-REINTERPRETATION] lease heartbeat failed",
                    extra={"job_id": job["id"]},
                )
                continue
            if not renewed:
                return

    async def run_once(self, worker_id: str) -> Optional[dict[str, Any]]:
        job = await self.repository.claim_due(
            worker_id,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return None
        started = time.monotonic()
        metrics: dict[str, Any] = {
            "attempt": job["attempt_count"],
            "transcript_revision": job["transcript_revision"],
        }
        heartbeat_task = asyncio.create_task(self._heartbeat_lease(job))
        try:
            try:
                rows = await self.repository.load_canonical_rows(job)
            except ValueError as exc:
                if str(exc) == "session_owner_collision":
                    raise ReinterpretationWorkerError(
                        "canonical_owner_collision",
                        retryable=False,
                    ) from exc
                raise
            _validate_rows(job, rows)
            metrics["event_count"] = len(rows)
            conversation = await self.conversation_loader(job["uid"], job["conversation_id"])
            if conversation is None:
                raise ReinterpretationWorkerError("conversation_not_found", retryable=False)

            plan_value = job.get("proposal_plan")
            if plan_value:
                plan = _validate_plan(plan_value)
            else:
                active_version_id = str(conversation.get("active_summary_version_id") or "")
                if active_version_id != job["starting_summary_version_id"]:
                    await self._require_current_lease(job)
                    finished = await self.repository.finish(
                        job,
                        status="conflict",
                        outcome="stale_version",
                        proposal_ids=[],
                        correction_ids=[],
                        receipt_refs=[],
                        metrics={**metrics, "latency_ms": int((time.monotonic() - started) * 1000)},
                    )
                    if not finished:
                        raise ReinterpretationWorkerError("lease_lost", retryable=True)
                    return {"job_id": job["id"], "status": "conflict"}
                plan = await self.hermes_client.propose(
                    job=job,
                    transcript=_ordered_transcript(rows),
                    current_summary=_current_summary(conversation),
                    event_ids=[str(row["event_id"]) for row in rows],
                )
                await self._require_current_lease(job)
                if not await self.repository.record_plan(job, _model_dump(plan)):
                    raise ReinterpretationWorkerError("lease_lost", retryable=True)

            if plan.outcome == "no_change":
                await self._require_current_lease(job)
                finished = await self.repository.finish(
                    job,
                    status="no_change",
                    outcome="no_change",
                    proposal_ids=[],
                    correction_ids=[],
                    receipt_refs=[],
                    metrics={**metrics, "latency_ms": int((time.monotonic() - started) * 1000)},
                )
                if not finished:
                    raise ReinterpretationWorkerError("lease_lost", retryable=True)
                return {"job_id": job["id"], "status": "no_change"}

            progress = dict(job.get("progress") or {})
            completed_indexes = {int(value) for value in progress.get("completed_indexes") or []}
            pending_indexes = {int(value) for value in progress.get("pending_indexes") or []}
            applied_indexes = {int(value) for value in progress.get("applied_indexes") or []}
            proposal_ids = list(job.get("proposal_ids") or [])
            correction_ids = list(job.get("correction_ids") or [])
            receipt_refs = list(job.get("receipt_refs") or [])
            expected_version_id = str(progress.get("active_summary_version_id") or job["starting_summary_version_id"])
            rows_by_event_id = {str(row["event_id"]): row for row in rows}
            has_pending = bool(pending_indexes)
            has_applied = bool(applied_indexes or correction_ids)

            for index, proposal in enumerate(plan.proposals):
                if index in completed_indexes:
                    continue
                if _evidence_is_explicit(proposal, rows_by_event_id):
                    correction_id = _correction_id(
                        job["id"],
                        job["transcript_revision"],
                        index,
                    )
                    await self._require_current_lease(job)
                    try:
                        applied = await self.correction_writer(
                            job=job,
                            proposal=proposal,
                            correction_id=correction_id,
                            proposal_index=index,
                            expected_version_id=expected_version_id,
                        )
                    except ConcurrentConversationSummaryChangeError:
                        await self._require_current_lease(job)
                        finished = await self.repository.finish(
                            job,
                            status="conflict",
                            outcome="stale_version",
                            proposal_ids=proposal_ids,
                            correction_ids=correction_ids,
                            receipt_refs=receipt_refs,
                            metrics={
                                **metrics,
                                "latency_ms": int((time.monotonic() - started) * 1000),
                            },
                        )
                        if not finished:
                            raise ReinterpretationWorkerError("lease_lost", retryable=True)
                        return {"job_id": job["id"], "status": "conflict"}
                    expected_version_id = applied.active_summary_version_id
                    if correction_id not in correction_ids:
                        correction_ids.append(correction_id)
                        receipt_refs.append(
                            {
                                "conversation_id": job["conversation_id"],
                                "correction_id": correction_id,
                                "status": "applied",
                            }
                        )
                    applied_indexes.add(index)
                    has_applied = True
                else:
                    fallback_id = _proposal_id(
                        job["id"],
                        job["transcript_revision"],
                        index,
                    )
                    await self._require_current_lease(job)
                    proposal_id = await self.pending_proposal_writer(
                        job=job,
                        proposal=proposal,
                        proposal_id=fallback_id,
                        proposal_index=index,
                    )
                    if proposal_id not in proposal_ids:
                        proposal_ids.append(proposal_id)
                    pending_indexes.add(index)
                    has_pending = True
                completed_indexes.add(index)
                progress = {
                    "completed_indexes": sorted(completed_indexes),
                    "pending_indexes": sorted(pending_indexes),
                    "applied_indexes": sorted(applied_indexes),
                    "active_summary_version_id": expected_version_id,
                }
                if not await self.repository.record_progress(
                    job,
                    progress=progress,
                    proposal_ids=proposal_ids,
                    correction_ids=correction_ids,
                    receipt_refs=receipt_refs,
                ):
                    raise ReinterpretationWorkerError("lease_lost", retryable=True)

            status = "pending_review" if has_pending else "applied"
            outcome = "applied_with_pending" if has_applied and has_pending else status
            await self._require_current_lease(job)
            finished = await self.repository.finish(
                job,
                status=status,
                outcome=outcome,
                proposal_ids=proposal_ids,
                correction_ids=correction_ids,
                receipt_refs=receipt_refs,
                metrics={
                    **metrics,
                    "proposal_count": len(plan.proposals),
                    "latency_ms": int((time.monotonic() - started) * 1000),
                },
            )
            if not finished:
                raise ReinterpretationWorkerError("lease_lost", retryable=True)
            return {"job_id": job["id"], "status": status}
        except ReinterpretationWorkerError as exc:
            status = await self.repository.fail_or_retry(
                job,
                error_code=exc.code,
                error_detail=exc.detail,
                retryable=exc.retryable,
                metrics={**metrics, "latency_ms": int((time.monotonic() - started) * 1000)},
            )
            logger.warning(
                "[FLOW:MEMORY-REINTERPRETATION] job=%s status=%s error=%s",
                job["id"],
                status,
                exc.code,
            )
            return {"job_id": job["id"], "status": status, "error_code": exc.code}
        except Exception as exc:
            status = await self.repository.fail_or_retry(
                job,
                error_code="worker_unhandled_error",
                error_detail=type(exc).__name__,
                retryable=True,
                metrics={**metrics, "latency_ms": int((time.monotonic() - started) * 1000)},
            )
            logger.exception(
                "[FLOW:MEMORY-REINTERPRETATION] unhandled worker failure",
                extra={"job_id": job["id"]},
            )
            return {
                "job_id": job["id"],
                "status": status,
                "error_code": "worker_unhandled_error",
            }
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass


_worker_task: Optional[asyncio.Task] = None


def worker_enabled() -> bool:
    return os.getenv("ELLA_MEMORY_REINTERPRETATION_WORKER_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def worker_runtime_metrics() -> dict[str, int]:
    return dict(_WORKER_RUNTIME_METRICS)


async def run_worker_loop(
    worker: MemoryReinterpretationWorker,
    *,
    max_iterations: Optional[int] = None,
    sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    worker_id = os.getenv("ELLA_MEMORY_REINTERPRETATION_WORKER_ID") or f"omi-{uuid.uuid4()}"
    idle_seconds = max(1.0, float(os.getenv("ELLA_MEMORY_REINTERPRETATION_IDLE_SECONDS", "2")))
    failure_backoff = 1.0
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            result = await worker.run_once(worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _WORKER_RUNTIME_METRICS["loop_failures_total"] += 1
            logger.exception(
                "[FLOW:MEMORY-REINTERPRETATION] worker loop iteration failed",
                extra={"worker_id": worker_id},
            )
            await sleep_func(failure_backoff)
            failure_backoff = min(30.0, failure_backoff * 2)
            continue
        failure_backoff = 1.0
        if result is None:
            await sleep_func(idle_seconds)


async def start_worker(worker: MemoryReinterpretationWorker) -> None:
    global _worker_task
    if not worker_enabled() or (_worker_task and not _worker_task.done()):
        return
    _worker_task = asyncio.create_task(run_worker_loop(worker))


async def stop_worker() -> None:
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    _worker_task = None
