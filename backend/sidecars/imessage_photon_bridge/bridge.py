"""Crash-safe transport orchestration for Ella's Photon iMessage bridge.

This module contains no Photon credentials and no owner/runtime selectors. The
provider implementation is injected by the executable; all owner and model
authority remains in the Ella backend.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol
from urllib.parse import urlparse

import httpx
from aiohttp import web

E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
PROOF_RE = re.compile(r"^[0-9]{6}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
NO_STORE_HEADERS = {"Cache-Control": "no-store"}
TERMINAL_RECEIPT_STATES = {"delivered", "failed", "uncertain", "quarantined"}
PROVIDER_OUTBOUND_MAX_CHARS = 8_000


class BridgeError(RuntimeError):
    """Typed, content-free bridge failure."""

    def __init__(self, code: str, *, status_code: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class BridgeConfig:
    backend_base_url: str
    registrar_bind: str
    registrar_port: int
    state_directory: Path
    project_id: str
    transport_token: str
    registrar_token: str
    heartbeat_seconds: float = 30.0
    backend_timeout_seconds: float = 90.0

    @classmethod
    def from_environment(cls) -> "BridgeConfig":
        return cls(
            backend_base_url=os.getenv("ELLA_IMESSAGE_BACKEND_URL", "").strip(),
            registrar_bind=os.getenv("ELLA_IMESSAGE_REGISTRAR_BIND", "127.0.0.1").strip(),
            registrar_port=_integer_environment("ELLA_IMESSAGE_REGISTRAR_PORT", 8796),
            state_directory=Path(os.getenv("ELLA_IMESSAGE_BRIDGE_STATE_DIR", "").strip()),
            project_id=os.getenv("PHOTON_PROJECT_ID", "").strip(),
            transport_token=os.getenv("ELLA_IMESSAGE_TRANSPORT_TOKEN", ""),
            registrar_token=os.getenv("ELLA_IMESSAGE_REGISTRAR_TOKEN", ""),
            heartbeat_seconds=_float_environment("ELLA_IMESSAGE_HEARTBEAT_SECONDS", 30.0),
            backend_timeout_seconds=_float_environment("ELLA_IMESSAGE_BACKEND_TIMEOUT_SECONDS", 90.0),
        ).validated()

    def validated(self) -> "BridgeConfig":
        parsed = urlparse(self.backend_base_url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BridgeError("bridge_backend_url_invalid")
        is_loopback_http = parsed.scheme == "http" and _is_loopback(parsed.hostname)
        if parsed.scheme != "https" and not is_loopback_http:
            raise BridgeError("bridge_backend_url_invalid")
        if not _is_loopback(self.registrar_bind):
            raise BridgeError("bridge_registrar_must_be_loopback")
        if not 1 <= self.registrar_port <= 65_535:
            raise BridgeError("bridge_registrar_port_invalid")
        if not str(self.state_directory) or not self.state_directory.is_absolute():
            raise BridgeError("bridge_state_directory_invalid")
        if not PROJECT_ID_RE.fullmatch(self.project_id):
            raise BridgeError("bridge_project_id_invalid")
        _require_secret(self.transport_token, "bridge_transport_token_invalid")
        _require_secret(self.registrar_token, "bridge_registrar_token_invalid")
        if hmac.compare_digest(self.transport_token, self.registrar_token):
            raise BridgeError("bridge_service_tokens_must_be_distinct")
        if not 10.0 <= self.heartbeat_seconds <= 60.0:
            raise BridgeError("bridge_heartbeat_interval_invalid")
        if not 30.0 <= self.backend_timeout_seconds <= 180.0:
            raise BridgeError("bridge_backend_timeout_invalid")
        return self


@dataclass(frozen=True)
class ProviderRegistration:
    project_id: str
    registration_id: str
    provider_user_id: str
    handset_e164: str
    assigned_destination: str


@dataclass(frozen=True)
class ProviderInbound:
    provider_message_id: str
    sender_e164: str
    text: str
    occurred_at: str
    raw_event: dict[str, Any]


class PhotonProvider(Protocol):
    async def connect(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> bool: ...

    async def disconnect(self) -> None: ...

    async def healthy(self) -> bool: ...

    async def list_users(self) -> list[dict[str, Any]]: ...

    async def register_user(self, handset_e164: str) -> dict[str, Any]: ...

    async def send_text(self, handset_e164: str, text: str) -> Optional[str]: ...


class EllaBackend(Protocol):
    async def proof(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def inbound(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def delivery_start(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def delivery_ack(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def delivery_uncertain(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def deregister(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class HttpEllaBackend:
    """Bounded transport-only client for the first-party Ella backend."""

    def __init__(self, config: BridgeConfig) -> None:
        self.base_url = config.backend_base_url.rstrip("/")
        self.token = config.transport_token
        self.timeout = httpx.Timeout(config.backend_timeout_seconds)

    async def close(self) -> None:
        return None

    async def proof(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/ella/internal/imessage/proof", payload)

    async def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/ella/internal/imessage/heartbeat", payload)

    async def inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/ella/internal/imessage/inbound", payload)

    async def delivery_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/ella/internal/imessage/delivery/start", payload)

    async def delivery_ack(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/ella/internal/imessage/delivery/ack", payload)

    async def delivery_uncertain(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/ella/internal/imessage/delivery/uncertain", payload)

    async def deregister(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/ella/internal/imessage/deregister", payload)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                response = await client.post(
                    f"{self.base_url}{path}",
                    headers={
                        "X-Ella-Imessage-Transport-Token": self.token,
                        "Content-Type": "application/json",
                    },
                    content=json.dumps(payload, separators=(",", ":")),
                )
        except httpx.HTTPError as exc:
            raise BridgeError("bridge_backend_transport_failed") from exc
        if response.status_code != 200:
            code = "bridge_backend_rejected"
            try:
                detail = response.json().get("detail")
                if isinstance(detail, dict) and re.fullmatch(r"[a-z0-9_]+", str(detail.get("code") or "")):
                    code = str(detail["code"])
            except (TypeError, ValueError):
                pass
            raise BridgeError(code, status_code=response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            raise BridgeError("bridge_backend_response_invalid") from exc
        if not isinstance(body, dict):
            raise BridgeError("bridge_backend_response_invalid")
        return body


class SingletonLease:
    """Host-local lifetime lease; the kernel releases it if the process dies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Optional[Any] = None

    def acquire(self) -> None:
        if self.handle is not None:
            return
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, stat.S_IRUSR | stat.S_IWUSR)
        handle = os.fdopen(fd, "r+", encoding="ascii")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise BridgeError("bridge_singleton_already_running") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        self.handle = handle

    def close(self) -> None:
        if self.handle is None:
            return
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


class BridgeJournal:
    """Protected SQLite journal for registration and transport crash boundaries."""

    def __init__(self, state_directory: Path) -> None:
        self.state_directory = state_directory
        self._prepare_directory()
        self.path = state_directory / "bridge.sqlite3"
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _prepare_directory(self) -> None:
        if self.state_directory.exists() and self.state_directory.is_symlink():
            raise BridgeError("bridge_state_directory_symlink_refused")
        self.state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = stat.S_IMODE(self.state_directory.stat().st_mode)
        if mode != 0o700 or self.state_directory.stat().st_uid != os.getuid():
            raise BridgeError("bridge_state_directory_insecure")

    def _migrate(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS registrations (
                provider_request_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                handset_e164 TEXT NOT NULL UNIQUE,
                registration_id TEXT,
                provider_user_id TEXT,
                assigned_destination TEXT,
                status TEXT NOT NULL CHECK (status IN ('attempting','pending','ready','uncertain','disabled')),
                provider_attempt_started INTEGER NOT NULL DEFAULT 0 CHECK (provider_attempt_started IN (0,1)),
                proof_accepted INTEGER NOT NULL DEFAULT 0 CHECK (proof_accepted IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbound_events (
                project_id TEXT NOT NULL,
                provider_message_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                event_json TEXT,
                status TEXT NOT NULL CHECK (status IN ('pending','processing','terminal','quarantined')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, provider_message_id)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                receipt_id TEXT PRIMARY KEY,
                delivery_idempotency_key TEXT NOT NULL UNIQUE,
                binding_generation INTEGER NOT NULL,
                line_identity TEXT NOT NULL,
                contact_identity TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                handset_e164 TEXT NOT NULL,
                reply_text TEXT,
                provider_send_started INTEGER NOT NULL DEFAULT 0 CHECK (provider_send_started IN (0,1)),
                provider_message_id TEXT,
                status TEXT NOT NULL CHECK (status IN ('prepared','sending','ack_pending','delivered','uncertain')),
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS inbound_events_status_idx ON inbound_events(status, created_at);
            CREATE INDEX IF NOT EXISTS deliveries_status_idx ON deliveries(status, created_at);
            """)

    def begin_registration(self, provider_request_id: str, project_id: str, handset_e164: str) -> dict[str, Any]:
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT * FROM registrations WHERE provider_request_id = ?",
                (provider_request_id,),
            ).fetchone()
            by_handset = self.connection.execute(
                "SELECT * FROM registrations WHERE handset_e164 = ? AND status != 'disabled'",
                (handset_e164,),
            ).fetchone()
            if existing and (existing["project_id"] != project_id or existing["handset_e164"] != handset_e164):
                raise BridgeError("bridge_registration_idempotency_conflict", status_code=409)
            if by_handset and by_handset["provider_request_id"] != provider_request_id:
                existing = by_handset
            if not existing:
                self.connection.execute(
                    """
                    INSERT INTO registrations (
                        provider_request_id, project_id, handset_e164, status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'attempting', ?, ?)
                    """,
                    (provider_request_id, project_id, handset_e164, now, now),
                )
                existing = self.connection.execute(
                    "SELECT * FROM registrations WHERE provider_request_id = ?", (provider_request_id,)
                ).fetchone()
            self.connection.execute("COMMIT")
            return dict(existing)
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def mark_registration_attempt_started(self, provider_request_id: str) -> None:
        self.connection.execute(
            """
            UPDATE registrations
            SET provider_attempt_started = 1, status = 'pending', updated_at = ?
            WHERE provider_request_id = ?
            """,
            (_utc_now(), provider_request_id),
        )

    def mark_registration_uncertain(self, provider_request_id: str) -> None:
        self.connection.execute(
            "UPDATE registrations SET status = 'uncertain', updated_at = ? WHERE provider_request_id = ?",
            (_utc_now(), provider_request_id),
        )

    def complete_registration(self, provider_request_id: str, registration: ProviderRegistration) -> dict[str, Any]:
        self.connection.execute(
            """
            UPDATE registrations
            SET registration_id = ?, provider_user_id = ?, assigned_destination = ?,
                status = 'ready', updated_at = ?
            WHERE provider_request_id = ? AND project_id = ? AND handset_e164 = ?
            """,
            (
                registration.registration_id,
                registration.provider_user_id,
                registration.assigned_destination,
                _utc_now(),
                provider_request_id,
                registration.project_id,
                registration.handset_e164,
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM registrations WHERE provider_request_id = ?", (provider_request_id,)
        ).fetchone()
        if not row or row["status"] != "ready":
            raise BridgeError("bridge_registration_commit_failed")
        return dict(row)

    def registration_for_handset(self, handset_e164: str) -> Optional[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM registrations WHERE handset_e164 = ? AND status = 'ready'",
            (handset_e164,),
        ).fetchall()
        if len(rows) > 1:
            raise BridgeError("bridge_sender_mapping_ambiguous")
        return dict(rows[0]) if rows else None

    def ready_registrations(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM registrations WHERE status = 'ready'")]

    def mark_proof_accepted(self, handset_e164: str) -> None:
        self.connection.execute(
            "UPDATE registrations SET proof_accepted = 1, updated_at = ? WHERE handset_e164 = ? AND status = 'ready'",
            (_utc_now(), handset_e164),
        )

    def disable_registration(self, handset_e164: str) -> None:
        self.connection.execute(
            "UPDATE registrations SET status = 'disabled', updated_at = ? WHERE handset_e164 = ?",
            (_utc_now(), handset_e164),
        )

    def record_inbound(self, event: ProviderInbound) -> str:
        canonical = json.dumps(event.raw_event, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM inbound_events WHERE project_id = ? AND provider_message_id = ?",
                (event.raw_event["project_id"], event.provider_message_id),
            ).fetchone()
            if row and row["payload_sha256"] != digest:
                self.connection.execute(
                    """
                    UPDATE inbound_events SET status = 'quarantined', event_json = NULL, updated_at = ?
                    WHERE project_id = ? AND provider_message_id = ?
                    """,
                    (now, event.raw_event["project_id"], event.provider_message_id),
                )
                self.connection.execute("COMMIT")
                raise BridgeError("bridge_inbound_replay_conflict", status_code=409)
            if not row:
                self.connection.execute(
                    """
                    INSERT INTO inbound_events (
                        project_id, provider_message_id, payload_sha256, event_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (event.raw_event["project_id"], event.provider_message_id, digest, canonical, now, now),
                )
                state = "new"
            else:
                state = str(row["status"])
            self.connection.execute("COMMIT")
            return state
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def pending_inbound(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM inbound_events WHERE status IN ('pending','processing') ORDER BY created_at"
            )
        ]

    def mark_inbound_processing(self, provider_message_id: str) -> None:
        self.connection.execute(
            "UPDATE inbound_events SET status = 'processing', updated_at = ? WHERE provider_message_id = ?",
            (_utc_now(), provider_message_id),
        )

    def mark_inbound_terminal(self, provider_message_id: str) -> None:
        self.connection.execute(
            """
            UPDATE inbound_events SET status = 'terminal', event_json = NULL, updated_at = ?
            WHERE provider_message_id = ?
            """,
            (_utc_now(), provider_message_id),
        )

    def prepare_delivery(
        self,
        *,
        response: dict[str, Any],
        line_identity: str,
        contact_identity: str,
        connection_id: str,
        handset_e164: str,
    ) -> dict[str, Any]:
        receipt_id = _uuid_text(response.get("receipt_id"), "bridge_delivery_receipt_invalid")
        delivery_key = _uuid_text(response.get("delivery_idempotency_key"), "bridge_delivery_key_invalid")
        generation = response.get("binding_generation")
        text = response.get("text")
        if (
            not isinstance(generation, int)
            or generation < 1
            or not isinstance(text, str)
            or not text
            or len(text) > PROVIDER_OUTBOUND_MAX_CHARS
        ):
            raise BridgeError("bridge_delivery_response_invalid")
        now = _utc_now()
        self.connection.execute(
            """
            INSERT INTO deliveries (
                receipt_id, delivery_idempotency_key, binding_generation, line_identity,
                contact_identity, connection_id, handset_e164, reply_text, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
            ON CONFLICT (receipt_id) DO NOTHING
            """,
            (
                receipt_id,
                delivery_key,
                generation,
                line_identity,
                contact_identity,
                connection_id,
                handset_e164,
                text,
                now,
                now,
            ),
        )
        row = self.connection.execute("SELECT * FROM deliveries WHERE receipt_id = ?", (receipt_id,)).fetchone()
        if not row or row["delivery_idempotency_key"] != delivery_key:
            raise BridgeError("bridge_delivery_replay_conflict", status_code=409)
        return dict(row)

    def mark_provider_send_started(self, receipt_id: str) -> dict[str, Any]:
        self.connection.execute(
            """
            UPDATE deliveries SET provider_send_started = 1, status = 'sending', updated_at = ?
            WHERE receipt_id = ? AND status = 'prepared' AND provider_send_started = 0
            """,
            (_utc_now(), receipt_id),
        )
        row = self.connection.execute("SELECT * FROM deliveries WHERE receipt_id = ?", (receipt_id,)).fetchone()
        if not row or row["status"] != "sending":
            raise BridgeError("bridge_delivery_send_already_started", status_code=409)
        return dict(row)

    def record_provider_message(self, receipt_id: str, provider_message_id: str) -> None:
        self.connection.execute(
            """
            UPDATE deliveries SET provider_message_id = ?, status = 'ack_pending', reply_text = NULL, updated_at = ?
            WHERE receipt_id = ? AND provider_send_started = 1 AND status = 'sending'
            """,
            (provider_message_id, _utc_now(), receipt_id),
        )

    def mark_delivery_terminal(self, receipt_id: str, status_value: str, error_code: Optional[str] = None) -> None:
        if status_value not in {"delivered", "uncertain"}:
            raise BridgeError("bridge_delivery_terminal_state_invalid")
        self.connection.execute(
            """
            UPDATE deliveries SET status = ?, error_code = ?, reply_text = NULL, updated_at = ?
            WHERE receipt_id = ?
            """,
            (status_value, error_code, _utc_now(), receipt_id),
        )

    def open_deliveries(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM deliveries WHERE status IN ('prepared','sending','ack_pending') ORDER BY created_at"
            )
        ]


class Registrar:
    def __init__(self, config: BridgeConfig, journal: BridgeJournal, provider: PhotonProvider) -> None:
        self.config = config
        self.journal = journal
        self.provider = provider
        self.lock = asyncio.Lock()

    async def register(self, provider_request_id: str, handset_e164: str) -> tuple[int, dict[str, Any]]:
        _uuid_text(provider_request_id, "bridge_registration_idempotency_key_invalid")
        if not E164_RE.fullmatch(handset_e164):
            raise BridgeError("bridge_handset_invalid", status_code=422)
        async with self.lock:
            row = self.journal.begin_registration(provider_request_id, self.config.project_id, handset_e164)
            if row["status"] == "ready":
                return 200, _registration_response(row)
            if not await self.provider.healthy():
                raise BridgeError("bridge_provider_unhealthy")
            matches = _matching_provider_users(await self.provider.list_users(), handset_e164)
            if len(matches) > 1:
                self.journal.mark_registration_uncertain(str(row["provider_request_id"]))
                raise BridgeError("bridge_provider_user_ambiguous", status_code=409)
            user = matches[0] if matches else None
            if user is None and not bool(row["provider_attempt_started"]):
                self.journal.mark_registration_attempt_started(str(row["provider_request_id"]))
                try:
                    user = await self.provider.register_user(handset_e164)
                except Exception as exc:
                    self.journal.mark_registration_uncertain(str(row["provider_request_id"]))
                    raise BridgeError("bridge_provider_registration_uncertain") from exc
            elif user is None:
                self.journal.mark_registration_uncertain(str(row["provider_request_id"]))
                raise BridgeError("bridge_provider_registration_pending")
            registration = _provider_registration(self.config.project_id, handset_e164, user)
            complete = self.journal.complete_registration(str(row["provider_request_id"]), registration)
            return 201, _registration_response(complete)


class ImessagePhotonBridge:
    def __init__(
        self,
        *,
        config: BridgeConfig,
        journal: BridgeJournal,
        provider: PhotonProvider,
        backend: EllaBackend,
        connection_id: Optional[str] = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self.provider = provider
        self.backend = backend
        self.connection_id = connection_id or str(uuid.uuid4())
        self.registrar = Registrar(config, journal, provider)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task[None]] = None
        self.heartbeat_task: Optional[asyncio.Task[None]] = None
        self.provider_ready = False
        self.stopping = False

    async def start(self) -> None:
        if not await self.provider.connect(self.accept_provider_event):
            raise BridgeError("bridge_provider_connect_failed")
        self.provider_ready = await self.provider.healthy()
        if not self.provider_ready:
            await self.provider.disconnect()
            raise BridgeError("bridge_provider_stream_not_healthy")
        self.worker_task = asyncio.create_task(self._worker(), name="ella-imessage-worker")
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="ella-imessage-heartbeat")
        await self.reconcile()

    async def stop(self) -> None:
        self.stopping = True
        for task in (self.heartbeat_task, self.worker_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.heartbeat_task, self.worker_task) if task is not None),
            return_exceptions=True,
        )
        await self.provider.disconnect()
        self.provider_ready = False

    async def accept_provider_event(self, raw_event: dict[str, Any]) -> None:
        try:
            inbound = _normalize_provider_event(self.config.project_id, raw_event)
            registration = self.journal.registration_for_handset(inbound.sender_e164)
            if registration is None:
                return
            state = self.journal.record_inbound(inbound)
            if state in {"new", "pending", "processing"}:
                await self.queue.put(inbound.provider_message_id)
        except BridgeError:
            return

    async def reconcile(self) -> None:
        for delivery in self.journal.open_deliveries():
            if delivery["status"] == "ack_pending" and delivery["provider_message_id"]:
                await self._ack_delivery(delivery)
            elif bool(delivery["provider_send_started"]):
                await self._uncertain_delivery(delivery, "bridge_restart_after_send_start")
        for event in self.journal.pending_inbound():
            if event.get("event_json"):
                await self.queue.put(str(event["provider_message_id"]))

    async def deregister_all(self) -> None:
        for registration in self.journal.ready_registrations():
            payload = self._transport_identity(registration)
            result = await self.backend.deregister(payload)
            if result.get("status") not in {"not_connected", "quarantined"}:
                raise BridgeError("bridge_deregister_response_invalid")
            self.journal.disable_registration(str(registration["handset_e164"]))

    async def _worker(self) -> None:
        while True:
            provider_message_id = await self.queue.get()
            try:
                await self._process(provider_message_id)
            except BridgeError:
                pass
            finally:
                self.queue.task_done()

    async def _process(self, provider_message_id: str) -> None:
        row = next(
            (item for item in self.journal.pending_inbound() if item["provider_message_id"] == provider_message_id),
            None,
        )
        if row is None or not row.get("event_json"):
            return
        raw_event = json.loads(str(row["event_json"]))
        inbound = _normalize_provider_event(self.config.project_id, raw_event)
        registration = self.journal.registration_for_handset(inbound.sender_e164)
        if registration is None:
            self.journal.mark_inbound_terminal(provider_message_id)
            return
        self.journal.mark_inbound_processing(provider_message_id)
        identity = self._transport_identity(registration)
        if PROOF_RE.fullmatch(inbound.text) and not bool(registration["proof_accepted"]):
            proof_result = await self.backend.proof(
                {
                    "assigned_destination": registration["assigned_destination"],
                    "handset_e164": inbound.sender_e164,
                    "code": inbound.text,
                    "provider_message_id": inbound.provider_message_id,
                    "line_identity": identity["line_identity"],
                    "contact_identity": identity["contact_identity"],
                }
            )
            if proof_result.get("status") != "accepted":
                raise BridgeError("bridge_proof_response_invalid")
            self.journal.mark_proof_accepted(inbound.sender_e164)
            self.journal.mark_inbound_terminal(provider_message_id)
            return
        result = await self.backend.inbound(
            {
                **identity,
                "provider_message_id": inbound.provider_message_id,
                "text": inbound.text,
                "occurred_at": inbound.occurred_at,
                "attachment_count": 0,
                "group_message": False,
            }
        )
        status_value = str(result.get("status") or "")
        if status_value == "awaiting_delivery":
            await self._start_and_send(registration, identity, result)
            self.journal.mark_inbound_terminal(provider_message_id)
        elif status_value == "sending":
            delivery = _delivery_from_inbound_result(registration, identity, result)
            await self._uncertain_delivery(delivery, "bridge_backend_send_state_unowned")
            self.journal.mark_inbound_terminal(provider_message_id)
        elif status_value in TERMINAL_RECEIPT_STATES or status_value == "unknown_sender":
            self.journal.mark_inbound_terminal(provider_message_id)
        elif status_value in {"claimed", "running"}:
            # Keep the exact original event for bounded restart/replay. Never alter its timestamp.
            return
        else:
            raise BridgeError("bridge_inbound_response_invalid")

    async def _start_and_send(
        self,
        registration: dict[str, Any],
        identity: dict[str, str],
        inbound_result: dict[str, Any],
    ) -> None:
        delivery_identity = _delivery_from_inbound_result(registration, identity, inbound_result)
        start = await self.backend.delivery_start(_delivery_request(delivery_identity))
        delivery = self.journal.prepare_delivery(
            response=start,
            line_identity=identity["line_identity"],
            contact_identity=identity["contact_identity"],
            connection_id=identity["connection_id"],
            handset_e164=str(registration["handset_e164"]),
        )
        delivery = self.journal.mark_provider_send_started(str(delivery["receipt_id"]))
        try:
            provider_message_id = await self.provider.send_text(
                str(delivery["handset_e164"]), str(delivery["reply_text"])
            )
        except Exception as exc:
            await self._uncertain_delivery(delivery, "bridge_provider_send_uncertain")
            raise BridgeError("bridge_provider_send_uncertain") from exc
        if not provider_message_id or len(provider_message_id) > 512:
            await self._uncertain_delivery(delivery, "bridge_provider_message_id_missing")
            return
        self.journal.record_provider_message(str(delivery["receipt_id"]), provider_message_id)
        pending = next(item for item in self.journal.open_deliveries() if item["receipt_id"] == delivery["receipt_id"])
        await self._ack_delivery(pending)

    async def _ack_delivery(self, delivery: dict[str, Any]) -> None:
        await self.backend.delivery_ack(
            {
                **_delivery_request(delivery),
                "outbound_provider_message_id": delivery["provider_message_id"],
            }
        )
        self.journal.mark_delivery_terminal(str(delivery["receipt_id"]), "delivered")

    async def _uncertain_delivery(self, delivery: dict[str, Any], error_code: str) -> None:
        await self.backend.delivery_uncertain({**_delivery_request(delivery), "error_code": error_code})
        self.journal.mark_delivery_terminal(str(delivery["receipt_id"]), "uncertain", error_code)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_seconds)
            healthy = await self.provider.healthy()
            self.provider_ready = healthy
            if not healthy:
                continue
            for registration in self.journal.ready_registrations():
                try:
                    result = await self.backend.heartbeat(self._transport_identity(registration))
                except BridgeError:
                    continue
                if result.get("status") == "unknown_sender" and bool(registration["proof_accepted"]):
                    self.journal.disable_registration(str(registration["handset_e164"]))

    def _transport_identity(self, registration: dict[str, Any]) -> dict[str, str]:
        provider_user_id = str(registration.get("provider_user_id") or "")
        handset = str(registration.get("handset_e164") or "")
        if not provider_user_id or not E164_RE.fullmatch(handset):
            raise BridgeError("bridge_registration_mapping_invalid")
        return {
            "line_identity": f"photon:{self.config.project_id}:shared",
            "contact_identity": f"photon:{self.config.project_id}:{provider_user_id}:{handset}",
            "connection_id": self.connection_id,
        }


class BridgeHttpServer:
    def __init__(self, bridge: ImessagePhotonBridge) -> None:
        self.bridge = bridge
        self.runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        self.runner = web.AppRunner(self.application(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.bridge.config.registrar_bind, self.bridge.config.registrar_port)
        await site.start()

    def application(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_post("/v1/registrations", self._register)
        app.router.add_get("/healthz", self._health)
        return app

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def _register(self, request: web.Request) -> web.Response:
        if not _bearer_ok(request.headers.get("Authorization"), self.bridge.config.registrar_token):
            return web.json_response(
                {"detail": {"code": "invalid_registrar_token"}}, status=401, headers=NO_STORE_HEADERS
            )
        provider_request_id = request.headers.get("Idempotency-Key", "")
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"channel", "mode", "handset_e164"}:
                raise BridgeError("bridge_registration_request_invalid", status_code=422)
            if body["channel"] != "imessage" or body["mode"] != "text_dm":
                raise BridgeError("bridge_registration_request_invalid", status_code=422)
            status_code, response = await self.bridge.registrar.register(provider_request_id, body["handset_e164"])
            return web.json_response(response, status=status_code, headers=NO_STORE_HEADERS)
        except (json.JSONDecodeError, TypeError):
            error = BridgeError("bridge_registration_request_invalid", status_code=422)
        except BridgeError as exc:
            error = exc
        return web.json_response({"detail": {"code": error.code}}, status=error.status_code, headers=NO_STORE_HEADERS)

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ready" if self.bridge.provider_ready else "degraded"},
            status=200 if self.bridge.provider_ready else 503,
            headers=NO_STORE_HEADERS,
        )


def _normalize_provider_event(project_id: str, raw_event: dict[str, Any]) -> ProviderInbound:
    if not isinstance(raw_event, dict):
        raise BridgeError("bridge_provider_event_invalid", status_code=422)
    space = raw_event.get("space")
    sender = raw_event.get("sender")
    content = raw_event.get("content")
    platform = str(raw_event.get("platform") or "").lower()
    if platform != "imessage" or not isinstance(space, dict) or not isinstance(sender, dict):
        raise BridgeError("bridge_provider_event_invalid", status_code=422)
    if space.get("type") != "dm" or space.get("phone") != "shared":
        raise BridgeError("bridge_provider_event_not_text_dm", status_code=422)
    if not isinstance(content, dict) or content.get("type") != "text":
        raise BridgeError("bridge_provider_event_not_text_dm", status_code=422)
    provider_message_id = str(raw_event.get("messageId") or "")
    sender_e164 = str(sender.get("id") or "")
    text = content.get("text")
    occurred_at = str(raw_event.get("timestamp") or "")
    if (
        not provider_message_id
        or len(provider_message_id) > 512
        or not E164_RE.fullmatch(sender_e164)
        or not isinstance(text, str)
        or not text
        or len(text) > 32_768
        or len(occurred_at) > 64
    ):
        raise BridgeError("bridge_provider_event_invalid", status_code=422)
    try:
        timestamp = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BridgeError("bridge_provider_event_invalid", status_code=422) from exc
    if timestamp.tzinfo is None:
        raise BridgeError("bridge_provider_event_invalid", status_code=422)
    occurred_at = timestamp.astimezone(timezone.utc).isoformat()
    canonical_event = {
        "project_id": project_id,
        "messageId": provider_message_id,
        "platform": "iMessage",
        "space": {"type": "dm", "phone": "shared"},
        "sender": {"id": sender_e164},
        "content": {"type": "text", "text": text},
        "timestamp": occurred_at,
    }
    return ProviderInbound(
        provider_message_id=provider_message_id,
        sender_e164=sender_e164,
        text=text,
        occurred_at=occurred_at,
        raw_event=canonical_event,
    )


def _provider_registration(project_id: str, handset_e164: str, user: dict[str, Any]) -> ProviderRegistration:
    provider_user_id = str(user.get("id") or "")
    assigned_destination = str(user.get("assignedPhoneNumber") or "")
    provider_handset = _canonical_provider_phone(str(user.get("phoneNumber") or ""))
    if not provider_user_id or provider_handset != handset_e164 or not E164_RE.fullmatch(assigned_destination):
        raise BridgeError("bridge_provider_registration_incomplete")
    return ProviderRegistration(
        project_id=project_id,
        registration_id=f"{project_id}:{provider_user_id}",
        provider_user_id=provider_user_id,
        handset_e164=handset_e164,
        assigned_destination=assigned_destination,
    )


def _is_provider_stream_healthy(body: Any) -> bool:
    stream = body.get("stream") if isinstance(body, dict) else None
    staleness = stream.get("staleness") if isinstance(stream, dict) else None
    return bool(
        isinstance(stream, dict)
        and stream.get("ok") is True
        and stream.get("state") == "healthy"
        and not (isinstance(staleness, dict) and staleness.get("zombieSuspected") is True)
    )


def _matching_provider_users(users: list[dict[str, Any]], handset_e164: str) -> list[dict[str, Any]]:
    return [user for user in users if _canonical_provider_phone(str(user.get("phoneNumber") or "")) == handset_e164]


def _canonical_provider_phone(value: str) -> str:
    value = re.sub(r"[^+0-9]", "", value)
    return value if E164_RE.fullmatch(value) else ""


def _registration_response(row: dict[str, Any]) -> dict[str, str]:
    registration_id = str(row.get("registration_id") or "")
    assigned_destination = str(row.get("assigned_destination") or "")
    if not registration_id or not E164_RE.fullmatch(assigned_destination):
        raise BridgeError("bridge_registration_state_incomplete")
    return {"registration_id": registration_id, "assigned_destination": assigned_destination}


def _delivery_from_inbound_result(
    registration: dict[str, Any], identity: dict[str, str], result: dict[str, Any]
) -> dict[str, Any]:
    return {
        **identity,
        "handset_e164": registration["handset_e164"],
        "receipt_id": _uuid_text(result.get("receipt_id"), "bridge_delivery_receipt_invalid"),
        "delivery_idempotency_key": _uuid_text(result.get("delivery_idempotency_key"), "bridge_delivery_key_invalid"),
        "binding_generation": result.get("binding_generation"),
    }


def _delivery_request(delivery: dict[str, Any]) -> dict[str, Any]:
    generation = delivery.get("binding_generation")
    if not isinstance(generation, int) or generation < 1:
        raise BridgeError("bridge_delivery_generation_invalid")
    return {
        "line_identity": delivery["line_identity"],
        "contact_identity": delivery["contact_identity"],
        "connection_id": delivery["connection_id"],
        "receipt_id": _uuid_text(delivery.get("receipt_id"), "bridge_delivery_receipt_invalid"),
        "delivery_idempotency_key": _uuid_text(delivery.get("delivery_idempotency_key"), "bridge_delivery_key_invalid"),
        "binding_generation": generation,
    }


def _uuid_text(value: Any, code: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise BridgeError(code, status_code=422) from exc


def _bearer_ok(header: Optional[str], expected: str) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    provided = header.removeprefix("Bearer ")
    return bool(provided) and hmac.compare_digest(provided, expected)


def _require_secret(value: str, code: str) -> None:
    if len(value) < 32 or value != value.strip():
        raise BridgeError(code)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _integer_environment(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise BridgeError(f"{name.lower()}_invalid") from exc


def _float_environment(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise BridgeError(f"{name.lower()}_invalid") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
