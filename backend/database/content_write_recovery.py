"""Fail-closed, OS-verified recovery for orphaned durable content writers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import platform
import re
from typing import Any, Callable

from database import content_write_fence, content_writer_owner

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContentWriterRecoveryError(RuntimeError):
    """Recovery could not prove that the exact recorded owner is terminal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TerminalProcessProof:
    owner: content_writer_owner.ProcessOwner
    proof_kind: str


@dataclass(frozen=True)
class ContentWriterRecoveryReceipt:
    subject_hash: str
    token_hash: str
    owner_fingerprint: str
    result: str
    proof_kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": "content_writer_recovery",
            "content_free": True,
            "owner_fingerprint": self.owner_fingerprint,
            "proof_kind": self.proof_kind,
            "result": self.result,
            "subject_hash": self.subject_hash,
            "token_hash": self.token_hash,
        }


def hash_selector(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_selector(value: str, *, code: str) -> str:
    normalized = value.strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ContentWriterRecoveryError(code)
    return normalized


def _reference(db: Any, subject_hash: str) -> Any:
    return db.collection(content_write_fence.FENCE_COLLECTION).document(subject_hash)


def _matching_token(values: dict[str, Any], token_hash: str) -> str | None:
    matches = [token for token in values if hmac.compare_digest(hash_selector(token), token_hash)]
    if len(matches) > 1:
        raise ContentWriterRecoveryError("account_writer_recovery_selector_ambiguous")
    return matches[0] if matches else None


def prove_recorded_owner_terminal(owner: content_writer_owner.ProcessOwner) -> TerminalProcessProof:
    """Use only the local kernel boundary; no caller-supplied claim is accepted."""
    try:
        boundary = content_writer_owner.current_process_boundary()
    except content_writer_owner.ProcessOwnerError as exc:
        raise ContentWriterRecoveryError(exc.code) from exc
    if owner.system != boundary.system:
        raise ContentWriterRecoveryError("account_writer_recovery_system_mismatch")
    if owner.host_id != boundary.host_id:
        raise ContentWriterRecoveryError("account_writer_recovery_host_mismatch")
    if owner.boot_id != boundary.boot_id:
        raise ContentWriterRecoveryError("account_writer_recovery_boot_mismatch")
    try:
        namespace_seen = True
        if owner.pid_namespace == boundary.pid_namespace:
            snapshot = content_writer_owner.process_snapshot(owner.system, owner.pid)
        elif owner.system == "Linux":
            snapshot, namespace_seen = content_writer_owner.linux_supervisor_process_snapshot(
                owner.pid_namespace,
                owner.pid,
            )
        else:
            raise ContentWriterRecoveryError("account_writer_recovery_pid_namespace_mismatch")
    except content_writer_owner.ProcessOwnerError as exc:
        if exc.code == "account_writer_supervisor_authority_required":
            raise ContentWriterRecoveryError("account_writer_recovery_pid_namespace_mismatch") from exc
        raise ContentWriterRecoveryError(exc.code) from exc
    if snapshot is None:
        proof_kind = "kernel_process_absent" if namespace_seen else "kernel_pid_namespace_absent"
        return TerminalProcessProof(owner=owner, proof_kind=proof_kind)
    if snapshot.start_id != owner.start_id:
        # A reused PID proves the recorded owner is not the visible process,
        # but recovery still refuses so an operator cannot normalize a
        # namespace/start mismatch into release authority.
        raise ContentWriterRecoveryError("account_writer_recovery_pid_reused")
    if snapshot.state == "Z":
        return TerminalProcessProof(owner=owner, proof_kind="kernel_process_zombie")
    raise ContentWriterRecoveryError("account_writer_recovery_owner_live")


def _receipt(
    *,
    subject_hash: str,
    token_hash: str,
    owner: content_writer_owner.ProcessOwner,
    result: str,
    proof_kind: str,
) -> ContentWriterRecoveryReceipt:
    return ContentWriterRecoveryReceipt(
        subject_hash=subject_hash,
        token_hash=token_hash,
        owner_fingerprint=owner.fingerprint(),
        result=result,
        proof_kind=proof_kind,
    )


def recover_orphaned_writer(
    db: Any,
    *,
    subject_hash: str,
    token_hash: str,
    transactional: Callable[[Callable[..., Any]], Callable[..., Any]],
) -> ContentWriterRecoveryReceipt:
    """Recover one exact record after OS proof and a transactional owner CAS."""
    if platform.system() != "Linux":
        raise ContentWriterRecoveryError("account_writer_recovery_system_unsupported")
    subject_hash = _validate_selector(subject_hash, code="account_writer_recovery_subject_selector_invalid")
    token_hash = _validate_selector(token_hash, code="account_writer_recovery_token_selector_invalid")
    reference = _reference(db, subject_hash)
    try:
        initial = content_write_fence._snapshot_data(reference.get())
        writers = content_write_fence._registered_writers(initial)
        recovery = content_write_fence._registered_recovery(initial)
    except content_write_fence.ContentWriteFenceError as exc:
        raise ContentWriterRecoveryError(exc.code) from exc

    token = _matching_token(writers, token_hash)
    if token is not None and recovery is not None and hmac.compare_digest(recovery.token_hash, token_hash):
        raise ContentWriterRecoveryError("account_writer_recovery_selector_ambiguous")
    if token is None:
        if recovery is None or not hmac.compare_digest(recovery.token_hash, token_hash):
            raise ContentWriterRecoveryError("account_writer_recovery_stale_token")
        return _receipt(
            subject_hash=subject_hash,
            token_hash=token_hash,
            owner=recovery.owner,
            result="already_recovered",
            proof_kind="durable_recovery_receipt",
        )
    registration = writers[token]
    if registration.owner is None:
        raise ContentWriterRecoveryError("account_writer_recovery_owner_unknown")
    proof = prove_recorded_owner_terminal(registration.owner)
    now = datetime.now(timezone.utc)

    @transactional
    def compare_and_set(transaction: Any) -> str:
        try:
            snapshot = reference.get(transaction=transaction)
            data = content_write_fence._snapshot_data(snapshot)
            current_writers = content_write_fence._registered_writers(data)
            current_recovery = content_write_fence._registered_recovery(data)
        except content_write_fence.ContentWriteFenceError as exc:
            raise ContentWriterRecoveryError(exc.code) from exc
        current = current_writers.get(token)
        if current is None:
            if (
                current_recovery is not None
                and hmac.compare_digest(current_recovery.token_hash, token_hash)
                and current_recovery.owner == proof.owner
            ):
                return "already_recovered"
            raise ContentWriterRecoveryError("account_writer_recovery_record_replaced")
        if current.owner != proof.owner:
            raise ContentWriterRecoveryError("account_writer_recovery_record_replaced")
        current_writers.pop(token)
        current_recovery = content_write_fence.WriterRecovery(
            token_hash=token_hash,
            owner=proof.owner,
            recovered_at=now,
        )
        updated = dict(data)
        updated.pop("writer_recoveries", None)
        updated.update(
            {
                "writers": content_write_fence._writers_to_storage(current_writers),
                "writer_recovery": current_recovery.to_storage(),
                "updated_at": now,
            }
        )
        transaction.set(reference, updated)
        return "recovered"

    try:
        result = compare_and_set(db.transaction())
    except ContentWriterRecoveryError:
        raise
    except Exception as exc:
        raise ContentWriterRecoveryError("account_writer_recovery_transaction_unavailable") from exc
    return _receipt(
        subject_hash=subject_hash,
        token_hash=token_hash,
        owner=proof.owner,
        result=result,
        proof_kind=proof.proof_kind,
    )
