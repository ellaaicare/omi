"""Distributed serialization between authenticated content writes and deletion."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
import threading
import time
from typing import Any, AsyncIterator, Awaitable, Callable
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
_detached_writer: ContextVar["DetachedContentWriter | None"] = ContextVar(
    "detached_content_writer",
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


def content_writes_active(uid: str, *, db: Any = None) -> bool:
    """Return whether new UID-scoped durable work may be admitted.

    This is only a queue-selection hint. Callers must still acquire a durable
    writer registration before claiming or mutating work because deletion can
    begin immediately after this read.
    """
    firestore_db = _configured_firestore_db(db)
    data = _snapshot_data(_fence_reference(firestore_db, uid).get())
    return data.get("state", ACTIVE) == ACTIVE


def transaction_content_writer_current(
    transaction: Any,
    firestore_db: Any,
    uid: str,
    writer_token: str,
    *,
    allow_draining: bool,
) -> bool:
    """Check one durable writer token inside another Firestore transaction."""
    data = _snapshot_data(_fence_reference(firestore_db, uid).get(transaction=transaction))
    allowed_states = {ACTIVE, DRAINING} if allow_draining else {ACTIVE}
    return data.get("state", ACTIVE) in allowed_states and writer_token in _registered_writers(data)


def _snapshot_data(snapshot: Any) -> dict[str, Any]:
    if not getattr(snapshot, "exists", False):
        return {}
    return dict(snapshot.to_dict() or {})


def _registered_writers(data: dict[str, Any]) -> dict[str, datetime]:
    """Return every unreleased writer; timestamps never prove absence.

    A process crash or missed heartbeat must leave deletion pending. Expiry is
    diagnostic metadata only because a live writer can stall its event loop
    longer than the configured interval and still reach its datastore commit.
    """
    writers = data.get("writers")
    if writers is None:
        return {}
    if not isinstance(writers, dict):
        raise ContentWriteFenceError("account_content_fence_corrupt")
    registered: dict[str, datetime] = {}
    for token, expires_at in writers.items():
        if not isinstance(token, str) or not token or not isinstance(expires_at, datetime):
            raise ContentWriteFenceError("account_content_fence_corrupt")
        registered[token] = expires_at
    return registered


def _acquire_firestore_writer(db: Any, uid: str, token: str, lease_seconds: int) -> None:
    reference = _fence_reference(db, uid)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def acquire(transaction: Any) -> None:
        data = _snapshot_data(reference.get(transaction=transaction))
        if data.get("state", ACTIVE) != ACTIVE:
            raise ContentWriteFenceError("account_write_forbidden")
        writers = _registered_writers(data)
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


def _assert_firestore_writer_current(db: Any, uid: str, token: str) -> None:
    reference = _fence_reference(db, uid)

    @firestore.transactional
    def assert_current(transaction: Any) -> None:
        data = _snapshot_data(reference.get(transaction=transaction))
        writers = _registered_writers(data)
        if token not in writers or data.get("state", ACTIVE) not in {ACTIVE, DRAINING}:
            raise ContentWriteFenceError("account_write_forbidden")

    assert_current(db.transaction())


def _release_firestore_writer(db: Any, uid: str, token: str) -> None:
    reference = _fence_reference(db, uid)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def release(transaction: Any) -> None:
        snapshot = reference.get(transaction=transaction)
        data = _snapshot_data(snapshot)
        writers = _registered_writers(data)
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
        writers = _registered_writers(data)
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


async def _assert_postgres_owner_active(uid: str) -> None:
    """Briefly serialize admission with deletion, then release the pool slot.

    The Firestore writer registration is acquired only after this check. That
    ordering means deletion either observes the writer registration and drains
    it, or tombstones first and makes registration fail. No PostgreSQL
    connection or advisory lock is held across arbitrary route code.
    """
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
                    if str(status or "") != ACTIVE:
                        raise ContentWriteFenceError("account_write_forbidden")
                finally:
                    await _finish_even_if_cancelled(transaction.rollback())
                return
        if owner_missing:
            return
    except ContentWriteFenceError:
        raise
    except Exception as exc:
        raise ContentWriteFenceError("account_authority_unavailable") from exc


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

        try:
            yield
        finally:
            await _finish_even_if_cancelled(asyncio.to_thread(_release_firestore_writer, firestore_db, uid, token))

    if _authority_enabled():
        await _assert_postgres_owner_active(uid)
    async with firestore_fence():
        yield uid


@dataclass(frozen=True)
class DetachedContentWriter:
    """A writer registration transferred from a request to detached work."""

    uid: str
    token: str
    firestore_db: Any

    def assert_current(self) -> None:
        _assert_firestore_writer_current(self.firestore_db, self.uid, self.token)

    def release(self) -> None:
        _release_firestore_writer(self.firestore_db, self.uid, self.token)


@asynccontextmanager
async def detached_content_write_fence(uid: str, *, db: Any = None) -> AsyncIterator[DetachedContentWriter]:
    """Own and bind one writer registration inside a detached async task."""
    firestore_db = _configured_firestore_db(db)
    if _authority_enabled():
        await _assert_postgres_owner_active(uid)
    writer = DetachedContentWriter(uid=uid, token=uuid.uuid4().hex, firestore_db=firestore_db)
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                _acquire_firestore_writer,
                firestore_db,
                uid,
                writer.token,
                _lease_seconds(),
            ),
            timeout=_acquire_seconds(),
        )
    except ContentWriteFenceError:
        raise
    except Exception as exc:
        raise ContentWriteFenceError("account_content_fence_unavailable") from exc

    context_token = _detached_writer.set(writer)
    try:
        yield writer
    finally:
        _detached_writer.reset(context_token)
        await _finish_even_if_cancelled(asyncio.to_thread(writer.release))


def _prepare_detached_content_writer(uid: str) -> DetachedContentWriter:
    registry = _request_fence_registry.get()
    if registry is None or uid not in registry:
        parent = _detached_writer.get()
        if parent is None or parent.uid != uid:
            raise ContentWriteFenceError("account_content_fence_unavailable")
        parent.assert_current()
    firestore_db = _configured_firestore_db(None)
    token = uuid.uuid4().hex
    _acquire_firestore_writer(firestore_db, uid, token, _lease_seconds())
    return DetachedContentWriter(uid=uid, token=token, firestore_db=firestore_db)


def assert_detached_content_writer_current(uid: str) -> None:
    """Fail a detached worker at its datastore commit boundary if ownership is lost."""
    writer = _detached_writer.get()
    if writer is None or writer.uid != uid:
        raise ContentWriteFenceError("account_content_fence_unavailable")
    writer.assert_current()


def assert_content_writer_admitted(uid: str) -> None:
    """Require a live request or detached registration for a UID-linked write."""
    registry = _request_fence_registry.get()
    if registry is not None and uid in registry:
        return
    writer = _detached_writer.get()
    if writer is None or writer.uid != uid:
        raise ContentWriteFenceError("account_content_fence_unavailable")
    writer.assert_current()


async def finish_admitted_content_mutation(uid: str, awaitable: Awaitable[Any]) -> Any:
    """Shield an admitted mutation and join it before propagating cancellation.

    Cancellation is a request to stop waiting, not proof that an external
    mutation stopped. The durable writer therefore remains owned until the
    submitted operation has a known terminal outcome.
    """
    assert_content_writer_admitted(uid)
    task = asyncio.create_task(awaitable)
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            # The durable job/lease remains the resumable outcome. Shutdown
            # cancellation is still propagated only after the mutation ended.
            pass
        raise
    assert_content_writer_admitted(uid)
    return result


async def run_admitted_threaded_mutation(
    subject_uid: str,
    target: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a synchronous mutation without orphaning its actual thread."""
    return await finish_admitted_content_mutation(subject_uid, asyncio.to_thread(target, *args, **kwargs))


def start_content_writer_thread(
    uid: str,
    target: Callable[..., Any],
    *,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    name: str | None = None,
    daemon: bool = True,
) -> threading.Thread:
    """Transfer a durable writer registration before starting a raw thread."""
    writer = _prepare_detached_content_writer(uid)

    def run() -> None:
        context_token = _detached_writer.set(writer)
        try:
            target(*args, **(kwargs or {}))
        finally:
            try:
                writer.release()
            finally:
                _detached_writer.reset(context_token)

    thread = threading.Thread(target=run, name=name, daemon=daemon)
    try:
        thread.start()
    except Exception:
        writer.release()
        raise
    return thread


async def start_content_writer_task(
    uid: str,
    factory: Callable[[], Awaitable[Any]],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Transfer a durable writer registration before starting an async task."""
    writer = await asyncio.to_thread(_prepare_detached_content_writer, uid)

    async def run() -> Any:
        context_token = _detached_writer.set(writer)
        try:
            return await factory()
        finally:
            try:
                await _finish_even_if_cancelled(asyncio.to_thread(writer.release))
            finally:
                _detached_writer.reset(context_token)

    try:
        return asyncio.create_task(run(), name=name)
    except Exception:
        await asyncio.to_thread(writer.release)
        raise


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
                registry.clear()
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
