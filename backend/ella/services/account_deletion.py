"""Resumable orchestration for the authenticated account-deletion route."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from database import account_deletion as account_deletion_db
from database import content_write_fence
from ella.services.ai_consent import build_account_deletion_receipt


@dataclass(frozen=True)
class AccountDeletionResponse:
    status_code: int
    body: dict[str, Any]


def ella_authority_persistence_enabled() -> bool:
    """Return the explicit deletion-authority boundary without probing schema.

    A missing/invalid enabled configuration remains enabled so database
    failures cannot silently downgrade deletion to the legacy destructive path.
    """
    configured = os.getenv("ELLA_POSTGRES_AUTHORITY_ENABLED")
    if configured is not None:
        return configured.strip().lower() != "false"
    return os.getenv("ELLA_ENABLED", "true").strip().lower() != "false"


async def execute_account_deletion(
    uid: str,
    *,
    delete_firestore: Callable[[str], Any],
    delete_firebase: Callable[[str], Any],
) -> AccountDeletionResponse:
    """Advance every safe deletion stage once and return typed progress."""
    authority_enabled = ella_authority_persistence_enabled()
    if authority_enabled:
        try:
            state = await account_deletion_db.quarantine_account_for_deletion(uid)
        except account_deletion_db.AccountDeletionUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "retryable": True},
            ) from exc
    else:
        state = account_deletion_db.AccountDeletionState(
            user_found=False,
            capacity_released=False,
            authority_quarantined=False,
            external_cleanup_required=(),
            external_cleanup_references=(),
            counts={},
        )

    remaining = set(state.external_cleanup_required)
    try:
        writers_drained = await content_write_fence.tombstone_content_writes(uid)
    except content_write_fence.ContentWriteFenceError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "retryable": True},
        ) from exc
    if not writers_drained:
        remaining.add("firestore_data")
        ordered_remaining = tuple(sorted(remaining))
        return AccountDeletionResponse(
            status_code=202,
            body={
                "status": "deletion_pending",
                "code": "account_deletion_cleanup_pending",
                "authority_quarantined": state.authority_quarantined,
                "capacity_released": state.capacity_released,
                "retryable": True,
                "deletion_receipt": build_account_deletion_receipt(
                    status="pending",
                    remaining=ordered_remaining,
                    external_cleanup_references=state.external_cleanup_references,
                ),
            },
        )

    try:
        await run_in_threadpool(delete_firestore, uid)
    except Exception:
        remaining.add("firestore_data")
    else:
        remaining.discard("firestore_data")

    if authority_enabled and not remaining:
        try:
            await account_deletion_db.finalize_account_deletion(uid)
        except account_deletion_db.AccountDeletionUnavailable as exc:
            if exc.code == "account_deletion_external_cleanup_incomplete":
                remaining.update({"hermes_profile", "honcho_tenancy", "runtime_registry"})
            else:
                raise HTTPException(
                    status_code=503,
                    detail={"code": exc.code, "retryable": True},
                ) from exc

    if not remaining:
        try:
            await run_in_threadpool(delete_firebase, uid)
        except Exception:
            remaining.add("firebase_identity")

    if remaining:
        ordered_remaining = tuple(sorted(remaining))
        return AccountDeletionResponse(
            status_code=202,
            body={
                "status": "deletion_pending",
                "code": "account_deletion_cleanup_pending",
                "authority_quarantined": state.authority_quarantined,
                "capacity_released": state.capacity_released,
                "retryable": True,
                "deletion_receipt": build_account_deletion_receipt(
                    status="pending",
                    remaining=ordered_remaining,
                    external_cleanup_references=state.external_cleanup_references,
                ),
            },
        )

    return AccountDeletionResponse(
        status_code=200,
        body={
            "status": "ok",
            "message": "Account deleted successfully",
            "deletion_receipt": build_account_deletion_receipt(),
        },
    )
