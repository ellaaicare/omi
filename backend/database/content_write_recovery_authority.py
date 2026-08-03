"""Pinned, protected Firestore authority for host-supervisor recovery."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
from typing import Any, Callable

RECOVERY_CONFIG_PATH = Path("/etc/omi/content-writer-recovery.json")
CONFIG_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 16 * 1024
MAX_RECEIPT_BYTES = 16 * 1024
MAX_CREDENTIAL_BYTES = 128 * 1024
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
DATABASE_ID_RE = re.compile(r"^(?:\(default\)|[a-z][a-z0-9_-]{0,61}[a-z0-9])$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RecoveryAuthorityError(RuntimeError):
    """The protected recovery authority could not be proven exactly."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RecoveryAuthority:
    project_id: str
    database_id: str
    credential_sha256: str


def _protected_parent_chain(path: Path) -> None:
    if not path.is_absolute():
        raise RecoveryAuthorityError("account_writer_recovery_authority_path_invalid")
    current = path.parent
    while True:
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise RecoveryAuthorityError("account_writer_recovery_authority_path_unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RecoveryAuthorityError("account_writer_recovery_authority_path_unprotected")
        if current == current.parent:
            return
        current = current.parent


def _read_protected_file(path: Path, *, maximum: int, code: str) -> bytes:
    _protected_parent_chain(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RecoveryAuthorityError(code) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & (stat.S_IXUSR | stat.S_IRWXG | stat.S_IRWXO)
            or metadata.st_size > maximum
        ):
            raise RecoveryAuthorityError("account_writer_recovery_authority_file_unprotected")
        chunks: list[bytes] = []
        length = 0
        while True:
            chunk = os.read(descriptor, min(16 * 1024, maximum + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > maximum:
                raise RecoveryAuthorityError("account_writer_recovery_authority_file_oversized")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_object(payload: bytes, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryAuthorityError(code) from exc
    if not isinstance(value, dict):
        raise RecoveryAuthorityError(code)
    return value


def _validated_config(config_path: Path) -> tuple[str, str, Path, Path]:
    config = _decode_object(
        _read_protected_file(
            config_path,
            maximum=MAX_CONFIG_BYTES,
            code="account_writer_recovery_config_unavailable",
        ),
        code="account_writer_recovery_config_invalid",
    )
    if set(config) != {
        "schema_version",
        "project_id",
        "database_id",
        "credential_file",
        "deployment_receipt_file",
    }:
        raise RecoveryAuthorityError("account_writer_recovery_config_invalid")
    project_id = config["project_id"]
    database_id = config["database_id"]
    credential_file = config["credential_file"]
    receipt_file = config["deployment_receipt_file"]
    if (
        not isinstance(config["schema_version"], int)
        or isinstance(config["schema_version"], bool)
        or config["schema_version"] != CONFIG_SCHEMA_VERSION
        or not isinstance(project_id, str)
        or PROJECT_ID_RE.fullmatch(project_id) is None
        or not isinstance(database_id, str)
        or DATABASE_ID_RE.fullmatch(database_id) is None
        or not isinstance(credential_file, str)
        or not isinstance(receipt_file, str)
    ):
        raise RecoveryAuthorityError("account_writer_recovery_config_invalid")
    credential_path = Path(credential_file)
    receipt_path = Path(receipt_file)
    if not credential_path.is_absolute() or not receipt_path.is_absolute():
        raise RecoveryAuthorityError("account_writer_recovery_config_invalid")
    return project_id, database_id, credential_path, receipt_path


def _validated_receipt(
    path: Path,
    *,
    project_id: str,
    database_id: str,
    credential_sha256: str,
) -> None:
    receipt = _decode_object(
        _read_protected_file(
            path,
            maximum=MAX_RECEIPT_BYTES,
            code="account_writer_recovery_deployment_receipt_unavailable",
        ),
        code="account_writer_recovery_deployment_receipt_invalid",
    )
    if set(receipt) != {"schema_version", "project_id", "database_id", "credential_sha256"}:
        raise RecoveryAuthorityError("account_writer_recovery_deployment_receipt_invalid")
    if (
        not isinstance(receipt["schema_version"], int)
        or isinstance(receipt["schema_version"], bool)
        or receipt["schema_version"] != RECEIPT_SCHEMA_VERSION
        or receipt["project_id"] != project_id
        or receipt["database_id"] != database_id
        or not isinstance(receipt["credential_sha256"], str)
        or SHA256_RE.fullmatch(receipt["credential_sha256"]) is None
        or receipt["credential_sha256"] != credential_sha256
    ):
        raise RecoveryAuthorityError("account_writer_recovery_deployment_receipt_mismatch")


def _load_recovery_firestore_client(
    config_path: Path,
    *,
    credentials_factory: Callable[[dict[str, Any]], Any],
    client_factory: Callable[..., Any],
) -> tuple[Any, RecoveryAuthority]:
    if os.environ.get("FIRESTORE_EMULATOR_HOST") is not None:
        raise RecoveryAuthorityError("account_writer_recovery_emulator_forbidden")
    project_id, database_id, credential_path, receipt_path = _validated_config(config_path)
    credential_payload = _read_protected_file(
        credential_path,
        maximum=MAX_CREDENTIAL_BYTES,
        code="account_writer_recovery_credentials_unavailable",
    )
    credential_sha256 = hashlib.sha256(credential_payload).hexdigest()
    _validated_receipt(
        receipt_path,
        project_id=project_id,
        database_id=database_id,
        credential_sha256=credential_sha256,
    )
    credential_info = _decode_object(
        credential_payload,
        code="account_writer_recovery_credentials_invalid",
    )
    if credential_info.get("type") != "service_account" or credential_info.get("project_id") != project_id:
        raise RecoveryAuthorityError("account_writer_recovery_credentials_mismatch")
    try:
        credentials = credentials_factory(credential_info)
        del credential_info, credential_payload
        client = client_factory(project=project_id, database=database_id, credentials=credentials)
    except Exception as exc:
        raise RecoveryAuthorityError("account_writer_recovery_client_unavailable") from exc
    selected_project = getattr(client, "project", getattr(client, "_project", None))
    selected_database = getattr(client, "database", getattr(client, "_database", None))
    if selected_project != project_id or selected_database != database_id:
        raise RecoveryAuthorityError("account_writer_recovery_client_mismatch")
    return client, RecoveryAuthority(
        project_id=project_id,
        database_id=database_id,
        credential_sha256=credential_sha256,
    )


def load_production_recovery_firestore_client() -> tuple[Any, RecoveryAuthority, Callable[..., Any]]:
    """Load the only production recovery authority; callers cannot select it."""
    if platform.system() != "Linux":
        raise RecoveryAuthorityError("account_writer_recovery_system_unsupported")
    # Delayed imports are mandatory here: the production bootstrap proves the
    # initial-host supervisor before this function can load Google code.
    from google.cloud import firestore
    from google.oauth2 import service_account

    client, authority = _load_recovery_firestore_client(
        RECOVERY_CONFIG_PATH,
        credentials_factory=service_account.Credentials.from_service_account_info,
        client_factory=firestore.Client,
    )
    return client, authority, firestore.transactional
