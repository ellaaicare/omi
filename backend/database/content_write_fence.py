"""Distributed serialization between authenticated content writes and deletion."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone
import hashlib
import os
import time
from typing import Any, AsyncIterator
import uuid

from google.cloud import firestore

from database import authority_advisory_lock, voice_canary

FENCE_COLLECTION = "_ella_account_deletion_fences"
ACTIVE = "ACTIVE"
DRAINING = "DRAINING"
TOMBSTONED = "TOMBSTONED"
DEFAULT_LEASE_SECONDS = 900
MAX_LEASE_SECONDS = 900
DEFAULT_ACQUIRE_SECONDS = 15.0
DEFAULT_DRAIN_SECONDS = 15.0
_firestore_db: Any = None
_request_fence_registry: ContextVar[dict[str, Any] | None] = ContextVar(
    "content_write_fence_registry",
    default=None,
)


class ContentWriteFenceError(RuntimeError):
    """A content mutation could not safely cross the deletion fence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def configure_firestore_db(db: Any) -> None:
    """Inject the production Firestore client without import-time credentials."""
    global _firestore_db
    _firestore_db = db


def _configured_firestore_db(db: Any) -> Any:
    configured = db if db is not None else _firestore_db
    if configured is None:
        raise ContentWriteFenceError("account_content_fence_unavailable")
    return configured


def _configured_seconds(name: str, default: float, *, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(max(value, 0.1), maximum)


def _lease_seconds() -> int:
    return max(
        1,
        int(
            _configured_seconds(
                "ELLA_CONTENT_WRITE_FENCE_LEASE_SECONDS",
                DEFAULT_LEASE_SECONDS,
                maximum=MAX_LEASE_SECONDS,
            )
        ),
    )


def _acquire_seconds() -> float:
    return _configured_seconds(
        "ELLA_CONTENT_WRITE_FENCE_ACQUIRE_SECONDS",
        DEFAULT_ACQUIRE_SECONDS,
        maximum=60.0,
    )


def _drain_seconds() -> float:
    return _configured_seconds(
        "ELLA_CONTENT_WRITE_FENCE_DRAIN_SECONDS",
        DEFAULT_DRAIN_SECONDS,
        maximum=60.0,
    )


def _authority_enabled() -> bool:
    configured = os.getenv("ELLA_POSTGRES_AUTHORITY_ENABLED")
    if configured is not None:
        return configured.strip().lower() != "false"
    return os.getenv("ELLA_ENABLED", "true").strip().lower() != "false"


def _fence_reference(db: Any, uid: str) -> Any:
    subject_hash = hashlib.sha256(uid.encode("utf-8")).hexdigest()
    return db.collection(FENCE_COLLECTION).document(subject_hash)


def _snapshot_data(snapshot: Any) -> dict[str, Any]:
    if not getattr(snapshot, "exists", False):
        return {}
    return dict(snapshot.to_dict() or {})


def _active_writers(data: dict[str, Any], now: datetime) -> dict[str, datetime]:
    writers = data.get("writers")
    if not isinstance(writers, dict):
        return {}
    return {
        token: expires_at
        for token, expires_at in writers.items()
        if isinstance(token, str) and isinstance(expires_at, datetime) and expires_at > now
    }


def _acquire_firestore_writer(db: Any, uid: str, token: str, lease_seconds: int) -> None:
    reference = _fence_reference(db, uid)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def acquire(transaction: Any) -> None:
        data = _snapshot_data(reference.get(transaction=transaction))
        if data.get("state", ACTIVE) != ACTIVE:
            raise ContentWriteFenceError("account_write_forbidden")
        writers = _active_writers(data, now)
        writers[token] = now + timedelta(seconds=lease_seconds)
        transaction.set(
            reference,
            {
                "state": ACTIVE,
                "writers": writers,
                "updated_at": now,
            },
        )

    acquire(db.transaction())


def _renew_firestore_writer(db: Any, uid: str, token: str, lease_seconds: int) -> bool:
    reference = _fence_reference(db, uid)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def renew(transaction: Any) -> bool:
        data = _snapshot_data(reference.get(transaction=transaction))
        writers = _active_writers(data, now)
        if token not in writers or data.get("state", ACTIVE) == TOMBSTONED:
            return False
        writers[token] = now + timedelta(seconds=lease_seconds)
        transaction.set(
            reference,
            {
                "state": data.get("state", ACTIVE),
                "writers": writers,
                "updated_at": now,
            },
        )
        return True

    return bool(renew(db.transaction()))


def _release_firestore_writer(db: Any, uid: str, token: str) -> None:
    reference = _fence_reference(db, uid)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def release(transaction: Any) -> None:
        snapshot = reference.get(transaction=transaction)
        data = _snapshot_data(snapshot)
        writers = _active_writers(data, now)
        writers.pop(token, None)
        state = data.get("state", ACTIVE)
        if state == ACTIVE and not writers:
            if getattr(snapshot, "exists", False):
                transaction.delete(reference)
            return
        transaction.set(
            reference,
            {
                "state": state,
                "writers": writers,
                "updated_at": now,
            },
        )

    release(db.transaction())


def _advance_firestore_tombstone(db: Any, uid: str) -> bool:
    """Block new writers and tombstone once every admitted writer is absent."""
    reference = _fence_reference(db, uid)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def advance(transaction: Any) -> bool:
        data = _snapshot_data(reference.get(transaction=transaction))
        if data.get("state") == TOMBSTONED:
            return True
        writers = _active_writers(data, now)
        state = DRAINING if writers else TOMBSTONED
        transaction.set(
            reference,
            {
                "state": state,
                "writers": writers,
                "deletion_requested_at": data.get("deletion_requested_at") or now,
                "tombstoned_at": now if state == TOMBSTONED else None,
                "updated_at": now,
            },
        )
        return state == TOMBSTONED

    return bool(advance(db.transaction()))


async def _finish_even_if_cancelled(awaitable: Any) -> None:
    task = asyncio.create_task(awaitable)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@asynccontextmanager
async def _postgres_owner_fence(uid: str) -> AsyncIterator[None]:
    """Hold the shared owner advisory lock and ACTIVE proof through the write."""
    try:
        pool = await voice_canary.get_pool()
        owner_missing = False
        async with pool.acquire() as connection:
            try:
                owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            except authority_advisory_lock.AuthorityLockError as exc:
                if exc.code == "authority_lock_owner_missing":
                    owner_missing = True
                else:
                    raise
            if not owner_missing:
                transaction = connection.transaction()
                await transaction.start()
                try:
                    proof = await asyncio.wait_for(
                        authority_advisory_lock.acquire_authority_lock(connection, owner=owner),
                        timeout=_acquire_seconds(),
                    )
                    user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                        connection,
                        uid=uid,
                        owner=owner,
                        proof=proof,
                    )
                    status = await connection.fetchval("SELECT status FROM users WHERE id = $1", user_id)
                    if str(status or "") in {"DELETION_PENDING", "DELETED"}:
                        raise ContentWriteFenceError("account_write_forbidden")
                    yield
                finally:
                    await _finish_even_if_cancelled(transaction.rollback())
                return
        if owner_missing:
            yield
    except ContentWriteFenceError:
        raise
    except Exception as exc:
        raise ContentWriteFenceError("account_authority_unavailable") from exc


async def _renew_until_released(db: Any, uid: str, token: str, lease_seconds: int) -> None:
    interval = max(1.0, lease_seconds / 3)
    while True:
        await asyncio.sleep(interval)
        renewed = await asyncio.to_thread(_renew_firestore_writer, db, uid, token, lease_seconds)
        if not renewed:
            return


@asynccontextmanager
async def content_write_fence(uid: str, *, db: Any = None) -> AsyncIterator[str]:
    """Hold every distributed deletion fence through the caller's mutation."""
    firestore_db = _configured_firestore_db(db)
    lease_seconds = _lease_seconds()
    token = uuid.uuid4().hex

    @asynccontextmanager
    async def firestore_fence() -> AsyncIterator[None]:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_acquire_firestore_writer, firestore_db, uid, token, lease_seconds),
                timeout=_acquire_seconds(),
            )
        except ContentWriteFenceError:
            raise
        except Exception as exc:
            raise ContentWriteFenceError("account_content_fence_unavailable") from exc

        renew_task = asyncio.create_task(_renew_until_released(firestore_db, uid, token, lease_seconds))
        try:
            yield
        finally:
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass
            await _finish_even_if_cancelled(asyncio.to_thread(_release_firestore_writer, firestore_db, uid, token))

    if _authority_enabled():
        async with _postgres_owner_fence(uid):
            async with firestore_fence():
                yield uid
    else:
        async with firestore_fence():
            yield uid


@asynccontextmanager
async def request_content_write_fence(uid: str) -> AsyncIterator[str]:
    """Hold one fence per UID until the entire ASGI request has completed."""
    registry = _request_fence_registry.get()
    if registry is None:
        async with content_write_fence(uid):
            yield uid
        return

    await admit_request_content_writer(uid)
    yield uid


async def admit_request_content_writer(uid: str) -> str:
    """Register a post-authentication writer with the request-lifetime fence."""
    registry = _request_fence_registry.get()
    if registry is None:
        raise ContentWriteFenceError("account_content_fence_unavailable")
    if uid not in registry:
        manager = content_write_fence(uid)
        await manager.__aenter__()
        registry[uid] = manager
    return uid


class ContentWriteFenceMiddleware:
    """Release registered fences after response streams and background tasks."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        registry: dict[str, Any] = {}
        context_token: Token[dict[str, Any] | None] = _request_fence_registry.set(registry)
        try:
            await self.app(scope, receive, send)
        finally:
            try:
                for manager in reversed(tuple(registry.values())):
                    await _finish_even_if_cancelled(manager.__aexit__(None, None, None))
            finally:
                _request_fence_registry.reset(context_token)


async def tombstone_content_writes(uid: str, *, db: Any = None) -> bool:
    """Drain admitted writers, deny new writers, and durably tombstone the UID."""
    firestore_db = _configured_firestore_db(db)
    deadline = time.monotonic() + _drain_seconds()
    while True:
        try:
            tombstoned = await asyncio.to_thread(_advance_firestore_tombstone, firestore_db, uid)
        except Exception as exc:
            raise ContentWriteFenceError("account_content_fence_unavailable") from exc
        if tombstoned:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
