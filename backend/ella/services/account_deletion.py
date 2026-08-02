"""Resumable orchestration for the authenticated account-deletion route."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from database import account_deletion as account_deletion_db
from ella.services.ai_consent import build_account_deletion_receipt


@dataclass(frozen=True)
class AccountDeletionResponse:
    status_code: int
    body: dict[str, Any]


async def execute_account_deletion(
    uid: str,
    *,
    delete_firestore: Callable[[str], Any],
    delete_firebase: Callable[[str], Any],
) -> AccountDeletionResponse:
    """Advance every safe deletion stage once and return typed progress."""
    try:
        state = await account_deletion_db.quarantine_account_for_deletion(uid)
    except account_deletion_db.AccountDeletionUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "retryable": True},
        ) from exc

    remaining = set(state.external_cleanup_required)
    try:
        await run_in_threadpool(delete_firestore, uid)
    except Exception:
        remaining.add("firestore_data")

    if not remaining:
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
