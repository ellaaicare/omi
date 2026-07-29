#!/usr/bin/env python3
"""Root-only protected-file ceremony for one synthetic Ella invitation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import firestore

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import invitation_operator, invitations
from ella.services import ai_consent
from ella.services.invitation_authority import (
    assert_invitation_pilot_rollout,
    authorize_invitation_pilot,
)

ROOT_UID = 0
CODE_FILE_CREATE_MODE = 0o600
CODE_FILE_FINAL_MODE = 0o400
CODE_FILE_ALLOWED_MODES = {CODE_FILE_CREATE_MODE, CODE_FILE_FINAL_MODE}
MAX_CODE_FILE_BYTES = 32
SAFE_CODE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ProtectedCodeFileError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _print_receipt(action: str, receipt: dict[str, Any], *, code_output_file: str = "") -> None:
    payload = {
        "action": action,
        **receipt,
    }
    if code_output_file:
        payload["code_output_file"] = code_output_file
    print(json.dumps(payload, sort_keys=True))


def _require_root() -> None:
    if os.geteuid() != ROOT_UID:
        raise SystemExit("operator_refused:root_required")


def _lexical_absolute_path(value: str, *, error_code: str) -> Path:
    if not value or not os.path.isabs(value) or os.path.normpath(value) != value:
        raise ProtectedCodeFileError(error_code)
    return Path(value)


def _open_secure_root(path: Path, *, expected_owner_uid: int) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProtectedCodeFileError("code_output_root_unavailable") from exc
    metadata = os.fstat(descriptor)
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != expected_owner_uid or mode != 0o700:
        os.close(descriptor)
        raise ProtectedCodeFileError("code_output_root_insecure")
    return descriptor


def _read_existing_code(
    root_descriptor: int,
    filename: str,
    *,
    expected_owner_uid: int,
) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(filename, flags, dir_fd=root_descriptor)
    except OSError as exc:
        raise ProtectedCodeFileError("code_output_file_unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_owner_uid
            or mode not in CODE_FILE_ALLOWED_MODES
            or metadata.st_nlink != 1
            or metadata.st_size < 9
            or metadata.st_size > MAX_CODE_FILE_BYTES
        ):
            raise ProtectedCodeFileError("code_output_file_insecure")
        content = os.read(descriptor, MAX_CODE_FILE_BYTES + 1)
        if len(content) > MAX_CODE_FILE_BYTES:
            raise ProtectedCodeFileError("code_output_file_insecure")
        try:
            code = content.decode("ascii").rstrip("\n")
        except UnicodeDecodeError as exc:
            raise ProtectedCodeFileError("code_output_file_invalid") from exc
        normalized = invitations.normalize_invite_code(code)
        if not normalized or code != f"{normalized[:4]}-{normalized[4:]}":
            raise ProtectedCodeFileError("code_output_file_invalid")
        return code
    finally:
        os.close(descriptor)


def prepare_protected_code_file(
    *,
    approved_root: str,
    code_output_file: str,
    expected_owner_uid: int = ROOT_UID,
) -> tuple[str, bool]:
    """Create the code once or securely recover it for an idempotent retry."""
    root = _lexical_absolute_path(
        approved_root,
        error_code="code_output_root_must_be_absolute",
    )
    output = _lexical_absolute_path(
        code_output_file,
        error_code="code_output_file_must_be_absolute",
    )
    if output.parent != root or not SAFE_CODE_FILENAME_RE.fullmatch(output.name):
        raise ProtectedCodeFileError("code_output_file_outside_approved_root")
    root_descriptor = _open_secure_root(
        root,
        expected_owner_uid=expected_owner_uid,
    )
    generated_code = invitations.generate_invite_code()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            descriptor = os.open(
                output.name,
                flags,
                CODE_FILE_CREATE_MODE,
                dir_fd=root_descriptor,
            )
        except FileExistsError:
            generated_code = ""
            return (
                _read_existing_code(
                    root_descriptor,
                    output.name,
                    expected_owner_uid=expected_owner_uid,
                ),
                True,
            )
        except OSError as exc:
            generated_code = ""
            raise ProtectedCodeFileError("code_output_file_create_failed") from exc

        try:
            payload = (generated_code + "\n").encode("ascii")
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise ProtectedCodeFileError("code_output_file_write_failed")
                offset += written
            os.fsync(descriptor)
            os.fchmod(descriptor, CODE_FILE_FINAL_MODE)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != expected_owner_uid
                or stat.S_IMODE(metadata.st_mode) != CODE_FILE_FINAL_MODE
                or metadata.st_nlink != 1
            ):
                raise ProtectedCodeFileError("code_output_file_insecure")
        finally:
            os.close(descriptor)
        return generated_code, False
    finally:
        os.close(root_descriptor)


def _parse_expiry(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise SystemExit("operator_refused:expiry_invalid") from exc
    if parsed.tzinfo is None:
        raise SystemExit("operator_refused:expiry_timezone_required")
    return parsed.astimezone(timezone.utc)


def _configuration() -> invitations.InvitationConfig:
    try:
        config = invitations.InvitationConfig.from_env()
    except invitations.InviteConfigurationError as exc:
        raise SystemExit("operator_refused:invite_configuration_invalid") from exc
    if not config.redemption_enabled or config.ordinary_enabled or config.app_review_enabled:
        raise SystemExit("operator_refused:invite_flags_invalid")
    return config


def _identity(args: argparse.Namespace) -> invitation_operator.SyntheticInvitationIdentity:
    identity = invitation_operator.SyntheticInvitationIdentity(
        uid=args.uid,
        account_uid=args.account_uid,
        profile_uid=args.profile_uid,
    )
    try:
        identity.validate()
    except invitation_operator.SyntheticInvitationOperatorError as exc:
        raise SystemExit(f"operator_refused:{exc.code}") from exc
    return identity


def _context(args: argparse.Namespace) -> invitation_operator.SyntheticInvitationContext:
    configured_environment = os.getenv("ELLA_INVITE_OPERATOR_ENVIRONMENT", "").strip()
    if (
        not configured_environment
        or not args.expected_environment
        or configured_environment != args.expected_environment
    ):
        raise SystemExit("operator_refused:environment_mismatch")
    context = invitation_operator.SyntheticInvitationContext(
        environment=args.expected_environment,
        expected_database=args.expected_database,
        operator=args.operator,
    )
    try:
        context.validate()
    except invitation_operator.SyntheticInvitationOperatorError as exc:
        raise SystemExit(f"operator_refused:{exc.code}") from exc
    return context


def _configure_consent_authority(expected_firestore_project: str) -> None:
    configured_project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if not expected_firestore_project or not configured_project or configured_project != expected_firestore_project:
        raise SystemExit("operator_refused:firestore_project_mismatch")
    try:
        firestore_db = firestore.Client(project=expected_firestore_project)
    except Exception as exc:
        raise SystemExit("operator_refused:consent_authority_unavailable") from exc
    ai_consent.configure_firestore_db(firestore_db)


def _assert_issue_rollout(
    uid: str,
    *,
    expected_firestore_project: str,
) -> invitations.InvitationPilotAdmission:
    try:
        assert_invitation_pilot_rollout(uid)
        _configure_consent_authority(expected_firestore_project)
        return authorize_invitation_pilot(uid)
    except invitations.InvitePilotGateDenied as exc:
        raise SystemExit("operator_refused:pilot_rollout_invalid") from exc


def _assert_lifecycle_rollout(uid: str) -> None:
    try:
        assert_invitation_pilot_rollout(uid)
    except invitations.InvitePilotGateDenied as exc:
        raise SystemExit("operator_refused:pilot_rollout_invalid") from exc


async def _issue(args: argparse.Namespace) -> None:
    identity = _identity(args)
    context = _context(args)
    config = _configuration()
    admission = _assert_issue_rollout(
        identity.uid,
        expected_firestore_project=args.expected_firestore_project,
    )
    expires_at = _parse_expiry(args.expires_at)
    try:
        code, existed = prepare_protected_code_file(
            approved_root=args.approved_code_output_root,
            code_output_file=args.code_output_file,
            expected_owner_uid=ROOT_UID,
        )
        code_file_ref_hmac = invitations.invitation_code_file_ref(
            config,
            args.code_output_file,
        )
        receipt = await invitation_operator.issue_synthetic_invitation(
            identity=identity,
            context=context,
            admission=admission,
            code=code,
            code_file_existed=existed,
            code_file_ref_hmac=code_file_ref_hmac,
            expires_at=expires_at,
            config=config,
        )
    except ProtectedCodeFileError as exc:
        raise SystemExit(f"operator_refused:{exc.code}") from exc
    except invitation_operator.SyntheticInvitationOperatorError as exc:
        raise SystemExit(f"operator_refused:{exc.code}") from exc
    finally:
        if "code" in locals():
            code = ""
    _print_receipt(
        "issue",
        {
            "receipt_id": receipt["receipt_id"],
            "content_free": True,
        },
        code_output_file=args.code_output_file,
    )


async def _show(args: argparse.Namespace) -> None:
    identity = _identity(args)
    context = _context(args)
    config = _configuration()
    _assert_lifecycle_rollout(identity.uid)
    try:
        receipt = await invitation_operator.show_synthetic_invitation(
            receipt_id=args.receipt_id,
            identity=identity,
            context=context,
            config=config,
        )
    except invitation_operator.SyntheticInvitationOperatorError as exc:
        raise SystemExit(f"operator_refused:{exc.code}") from exc
    _print_receipt("show", receipt)


async def _revoke(args: argparse.Namespace) -> None:
    identity = _identity(args)
    context = _context(args)
    config = _configuration()
    _assert_lifecycle_rollout(identity.uid)
    try:
        receipt = await invitation_operator.revoke_synthetic_invitation(
            receipt_id=args.receipt_id,
            expected_version=args.expected_version,
            identity=identity,
            context=context,
            config=config,
        )
    except invitation_operator.SyntheticInvitationOperatorError as exc:
        raise SystemExit(f"operator_refused:{exc.code}") from exc
    _print_receipt("revoke", receipt)


async def _cleanup(args: argparse.Namespace) -> None:
    identity = _identity(args)
    context = _context(args)
    config = _configuration()
    _assert_lifecycle_rollout(identity.uid)
    try:
        receipt = await invitation_operator.cleanup_synthetic_invitation(
            receipt_id=args.receipt_id,
            expected_version=args.expected_version,
            identity=identity,
            context=context,
            config=config,
        )
    except invitation_operator.SyntheticInvitationOperatorError as exc:
        raise SystemExit(f"operator_refused:{exc.code}") from exc
    _print_receipt("cleanup", receipt)


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--uid", required=True)
    parser.add_argument("--account-uid", required=True)
    parser.add_argument("--profile-uid", required=True)
    parser.add_argument("--expected-environment", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--operator", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    issue = subparsers.add_parser("issue")
    _add_identity_arguments(issue)
    issue.add_argument("--expected-firestore-project", required=True)
    issue.add_argument("--expires-at", required=True)
    issue.add_argument("--approved-code-output-root", required=True)
    issue.add_argument("--code-output-file", required=True)
    issue.set_defaults(handler=_issue)

    show = subparsers.add_parser("show")
    _add_identity_arguments(show)
    show.add_argument("--receipt-id", required=True)
    show.set_defaults(handler=_show)

    revoke = subparsers.add_parser("revoke")
    _add_identity_arguments(revoke)
    revoke.add_argument("--receipt-id", required=True)
    revoke.add_argument("--expected-version", required=True, type=int)
    revoke.set_defaults(handler=_revoke)

    cleanup = subparsers.add_parser("cleanup")
    _add_identity_arguments(cleanup)
    cleanup.add_argument("--receipt-id", required=True)
    cleanup.add_argument("--expected-version", required=True, type=int)
    cleanup.set_defaults(handler=_cleanup)
    return parser


async def _main() -> None:
    _require_root()
    args = _parser().parse_args()
    await args.handler(args)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(130)
