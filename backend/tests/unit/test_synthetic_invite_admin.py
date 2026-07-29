import argparse
import asyncio
import hashlib
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from database import invitation_operator, invitations
from scripts import synthetic_invite_admin


def _identity(uid: str = "synthetic-iris") -> invitation_operator.SyntheticInvitationIdentity:
    return invitation_operator.SyntheticInvitationIdentity(
        uid=uid,
        account_uid=uid,
        profile_uid=uid,
    )


def _admission(uid: str = "synthetic-iris") -> invitations.InvitationPilotAdmission:
    return invitations.InvitationPilotAdmission(
        account_uid=uid,
        profile_uid=uid,
        consent_receipt_id="synthetic-consent-receipt",
        profile_binding_id="synthetic-profile-binding",
        policy_version="ai-data-processors-v8",
        processor_set_hash="sha256:" + ("a" * 64),
        scope_version="managed-cloud-v3",
        scope_hash="sha256:" + ("b" * 64),
    )


def _issue_args(
    root,
    output,
    *,
    expires_at: datetime | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        uid="synthetic-iris",
        account_uid="synthetic-iris",
        profile_uid="synthetic-iris",
        expected_environment="prototype",
        expected_database="ella_ai",
        expected_firestore_project="ella-ai-care",
        operator="iris",
        expires_at=(expires_at or datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        approved_code_output_root=str(root),
        code_output_file=str(output),
    )


def _file_recovery_binding(code: str) -> str:
    return hashlib.sha256(f"unit-recovery:{code}".encode("ascii")).hexdigest()


def test_identity_requires_one_exact_non_plato_subject():
    _identity().validate()
    with pytest.raises(
        invitation_operator.SyntheticInvitationOperatorError,
        match="operator_identity_refused",
    ):
        invitation_operator.SyntheticInvitationIdentity(
            uid="synthetic-iris",
            account_uid="different-account",
            profile_uid="synthetic-iris",
        ).validate()
    for forbidden in ("realcryptoplato", "plato_eval", "plato-eval"):
        with pytest.raises(
            invitation_operator.SyntheticInvitationOperatorError,
            match="operator_identity_refused",
        ):
            _identity(forbidden).validate()


def test_protected_code_file_is_exclusive_root_scoped_and_owner_read_only(tmp_path, monkeypatch):
    approved = tmp_path / "approved"
    approved.mkdir(mode=0o700)
    output = approved / "invite-code"
    owner_uid = os.geteuid()
    directory_fsyncs = []
    original_fsync = os.fsync

    def tracking_fsync(descriptor):
        directory_fsyncs.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        original_fsync(descriptor)

    monkeypatch.setattr(synthetic_invite_admin.os, "fsync", tracking_fsync)

    prepared = synthetic_invite_admin.prepare_protected_code_file(
        approved_root=str(approved),
        code_output_file=str(output),
        recovery_binding_for_code=_file_recovery_binding,
        expected_owner_uid=owner_uid,
    )
    retried = synthetic_invite_admin.prepare_protected_code_file(
        approved_root=str(approved),
        code_output_file=str(output),
        recovery_binding_for_code=_file_recovery_binding,
        expected_owner_uid=owner_uid,
    )

    assert not prepared.existed
    assert retried.existed
    assert retried.code == prepared.code
    assert retried.recovery_binding_hmac == prepared.recovery_binding_hmac
    assert invitations.normalize_invite_code(prepared.code)
    metadata = output.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o400
    assert metadata.st_uid == owner_uid
    assert metadata.st_nlink == 1
    recovery_receipt_path = approved / synthetic_invite_admin._recovery_receipt_filename(output.name)
    recovery_metadata = recovery_receipt_path.stat()
    assert stat.S_IMODE(recovery_metadata.st_mode) == 0o400
    assert recovery_metadata.st_uid == owner_uid
    assert recovery_metadata.st_nlink == 1
    recovery_receipt = recovery_receipt_path.read_text(encoding="ascii")
    assert prepared.code not in recovery_receipt
    assert prepared.recovery_binding_hmac in recovery_receipt
    assert directory_fsyncs.count(True) == 2


@pytest.mark.parametrize(
    ("root_value", "output_value", "expected_code"),
    [
        ("relative", "/tmp/invite", "code_output_root_must_be_absolute"),
        ("/tmp", "relative", "code_output_file_must_be_absolute"),
        ("/tmp/approved", "/tmp/outside", "code_output_file_outside_approved_root"),
    ],
)
def test_protected_code_file_rejects_unapproved_paths(
    root_value,
    output_value,
    expected_code,
):
    with pytest.raises(synthetic_invite_admin.ProtectedCodeFileError) as error:
        synthetic_invite_admin.prepare_protected_code_file(
            approved_root=root_value,
            code_output_file=output_value,
            recovery_binding_for_code=_file_recovery_binding,
            expected_owner_uid=os.geteuid(),
        )
    assert error.value.code == expected_code


def test_protected_code_file_rejects_insecure_root_and_symlink(tmp_path):
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o777)
    insecure.chmod(0o777)
    with pytest.raises(synthetic_invite_admin.ProtectedCodeFileError) as error:
        synthetic_invite_admin.prepare_protected_code_file(
            approved_root=str(insecure),
            code_output_file=str(insecure / "invite"),
            recovery_binding_for_code=_file_recovery_binding,
            expected_owner_uid=os.geteuid(),
        )
    assert error.value.code == "code_output_root_insecure"

    approved = tmp_path / "approved"
    approved.mkdir(mode=0o700)
    target = approved / "target"
    target.write_text("ABCD-2345\n", encoding="ascii")
    target.chmod(0o400)
    output = approved / "invite"
    output.symlink_to(target)
    with pytest.raises(synthetic_invite_admin.ProtectedCodeFileError) as error:
        synthetic_invite_admin.prepare_protected_code_file(
            approved_root=str(approved),
            code_output_file=str(output),
            recovery_binding_for_code=_file_recovery_binding,
            expected_owner_uid=os.geteuid(),
        )
    assert error.value.code == "code_output_file_unavailable"


def test_issue_receipt_and_failures_never_emit_the_code(
    monkeypatch,
    tmp_path,
    capsys,
):
    approved = tmp_path / "approved"
    approved.mkdir(mode=0o700)
    output = approved / "invite"
    captured_code = {}
    issue_args = _issue_args(
        approved,
        output,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    async def fake_issue(**kwargs):
        captured_code["value"] = kwargs["code"]
        return {
            "receipt_id": "11111111-1111-1111-1111-111111111111",
            "state": "sent",
            "version": 1,
            "content_free": True,
        }

    monkeypatch.setattr(synthetic_invite_admin, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(
        synthetic_invite_admin,
        "_assert_issue_rollout",
        lambda _uid, **_kwargs: _admission(),
    )
    monkeypatch.setattr(
        synthetic_invite_admin.invitation_operator,
        "issue_synthetic_invitation",
        fake_issue,
    )
    monkeypatch.setenv("ELLA_INVITE_OPERATOR_ENVIRONMENT", "prototype")
    monkeypatch.setenv("ELLA_INVITE_REDEMPTION_ENABLED", "true")
    monkeypatch.setenv("ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED", "false")
    monkeypatch.setenv("ELLA_INVITE_APP_REVIEW_ENABLED", "false")
    monkeypatch.setenv("ELLA_INVITE_HMAC_PEPPER", "p" * 32)

    asyncio.run(synthetic_invite_admin._issue(issue_args))

    emitted = capsys.readouterr()
    secret = captured_code["value"]
    assert secret
    assert secret not in emitted.out
    assert secret not in emitted.err
    assert secret not in str(
        {
            "receipt_id": "11111111-1111-1111-1111-111111111111",
            "path": output,
        }
    )
    assert str(output) in emitted.out
    assert "content_free" in emitted.out
    assert '"state"' not in emitted.out
    assert '"version"' not in emitted.out

    async def fake_failure(**_kwargs):
        raise invitation_operator.SyntheticInvitationOperatorError(
            "operator_stale_code_receipt",
        )

    monkeypatch.setattr(
        synthetic_invite_admin.invitation_operator,
        "issue_synthetic_invitation",
        fake_failure,
    )
    with pytest.raises(SystemExit) as error:
        asyncio.run(synthetic_invite_admin._issue(issue_args))
    failed_emitted = capsys.readouterr()
    assert secret not in str(error.value)
    assert secret not in failed_emitted.out
    assert secret not in failed_emitted.err

    ambiguous_output = approved / "ambiguous-invite"
    ambiguous_attempts = []
    ambiguous_args = _issue_args(
        approved,
        ambiguous_output,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    async def fake_ambiguous_issue(**kwargs):
        ambiguous_attempts.append(dict(kwargs))
        if len(ambiguous_attempts) == 1:
            raise ConnectionError("injected transaction outcome ambiguity")
        return {
            "receipt_id": "22222222-2222-2222-2222-222222222222",
            "state": "sent",
            "version": 1,
            "content_free": True,
        }

    monkeypatch.setattr(
        synthetic_invite_admin.invitation_operator,
        "issue_synthetic_invitation",
        fake_ambiguous_issue,
    )
    with pytest.raises(
        SystemExit,
        match="operator_refused:invitation_outcome_ambiguous",
    ):
        asyncio.run(
            synthetic_invite_admin._issue(
                ambiguous_args,
            )
        )
    ambiguous_failure = capsys.readouterr()
    ambiguous_code = ambiguous_attempts[0]["code"]
    assert ambiguous_output.exists()
    assert ambiguous_code not in ambiguous_failure.out
    assert ambiguous_code not in ambiguous_failure.err

    asyncio.run(
        synthetic_invite_admin._issue(
            ambiguous_args,
        )
    )
    ambiguous_retry = capsys.readouterr()
    assert ambiguous_attempts[0]["code_file_existed"] is False
    assert ambiguous_attempts[1]["code_file_existed"] is True
    assert ambiguous_attempts[0]["code"] == ambiguous_attempts[1]["code"]
    assert ambiguous_attempts[0]["recovery_binding_hmac"] == ambiguous_attempts[1]["recovery_binding_hmac"]
    assert ambiguous_code not in ambiguous_retry.out
    assert ambiguous_code not in ambiguous_retry.err
    recovery_receipt_path = approved / synthetic_invite_admin._recovery_receipt_filename(
        ambiguous_output.name,
    )
    recovery_receipt = recovery_receipt_path.read_text(encoding="ascii")
    assert ambiguous_code not in recovery_receipt
    assert '"content_free":true' in recovery_receipt


def test_environment_and_flag_drift_fail_before_file_creation(monkeypatch, tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir(mode=0o700)
    output = approved / "invite"
    args = _issue_args(approved, output)
    monkeypatch.setenv("ELLA_INVITE_OPERATOR_ENVIRONMENT", "wrong")
    with pytest.raises(SystemExit, match="operator_refused:environment_mismatch"):
        asyncio.run(synthetic_invite_admin._issue(args))
    assert not output.exists()

    monkeypatch.setenv("ELLA_INVITE_OPERATOR_ENVIRONMENT", "prototype")
    monkeypatch.setenv("ELLA_INVITE_REDEMPTION_ENABLED", "true")
    monkeypatch.setenv("ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED", "true")
    monkeypatch.setenv("ELLA_INVITE_APP_REVIEW_ENABLED", "false")
    monkeypatch.setenv("ELLA_INVITE_HMAC_PEPPER", "p" * 32)
    with pytest.raises(SystemExit, match="operator_refused:invite_flags_invalid"):
        asyncio.run(synthetic_invite_admin._issue(args))
    assert not output.exists()


def test_issue_initializes_explicit_adc_firestore_authority(monkeypatch):
    project = "ella-ai-care"
    client = object()
    configured = {}

    def fake_firestore_client(*, project):
        configured["client_project"] = project
        return client

    def fake_configure(value):
        configured["client"] = value

    def fake_authorize(uid):
        assert configured == {
            "client_project": project,
            "client": client,
        }
        return _admission(uid)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project)
    monkeypatch.setattr(
        synthetic_invite_admin,
        "assert_invitation_pilot_rollout",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        synthetic_invite_admin.firestore,
        "Client",
        fake_firestore_client,
    )
    monkeypatch.setattr(
        synthetic_invite_admin.ai_consent,
        "configure_firestore_db",
        fake_configure,
    )
    monkeypatch.setattr(
        synthetic_invite_admin,
        "authorize_invitation_pilot",
        fake_authorize,
    )

    admission = synthetic_invite_admin._assert_issue_rollout(
        "synthetic-iris",
        expected_firestore_project=project,
    )
    assert admission == _admission()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "wrong-project")
    with pytest.raises(
        SystemExit,
        match="operator_refused:firestore_project_mismatch",
    ):
        synthetic_invite_admin._assert_issue_rollout(
            "synthetic-iris",
            expected_firestore_project=project,
        )


def test_lifecycle_commands_recheck_rollout_before_database(monkeypatch):
    args = argparse.Namespace(
        uid="synthetic-iris",
        account_uid="synthetic-iris",
        profile_uid="synthetic-iris",
        expected_environment="prototype",
        expected_database="ella_ai",
        operator="iris",
        receipt_id="11111111-1111-1111-1111-111111111111",
        expected_version=1,
    )
    monkeypatch.setenv("ELLA_INVITE_OPERATOR_ENVIRONMENT", "prototype")
    monkeypatch.setenv("ELLA_INVITE_REDEMPTION_ENABLED", "true")
    monkeypatch.setenv("ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED", "false")
    monkeypatch.setenv("ELLA_INVITE_APP_REVIEW_ENABLED", "false")
    monkeypatch.setenv("ELLA_INVITE_HMAC_PEPPER", "p" * 32)
    monkeypatch.setattr(
        synthetic_invite_admin,
        "assert_invitation_pilot_rollout",
        lambda _uid: (_ for _ in ()).throw(
            invitations.InvitePilotGateDenied("disabled"),
        ),
    )

    async def database_must_not_run(**_kwargs):
        raise AssertionError("database action ran after rollout refusal")

    monkeypatch.setattr(
        synthetic_invite_admin.invitation_operator,
        "show_synthetic_invitation",
        database_must_not_run,
    )
    monkeypatch.setattr(
        synthetic_invite_admin.invitation_operator,
        "revoke_synthetic_invitation",
        database_must_not_run,
    )
    monkeypatch.setattr(
        synthetic_invite_admin.invitation_operator,
        "cleanup_synthetic_invitation",
        database_must_not_run,
    )

    for handler in (
        synthetic_invite_admin._show,
        synthetic_invite_admin._revoke,
        synthetic_invite_admin._cleanup,
    ):
        with pytest.raises(
            SystemExit,
            match="operator_refused:pilot_rollout_invalid",
        ):
            asyncio.run(handler(args))


def test_non_root_cli_refuses_before_argument_or_secret_processing(monkeypatch):
    monkeypatch.setattr(synthetic_invite_admin, "ROOT_UID", os.geteuid() + 1)
    with pytest.raises(SystemExit, match="operator_refused:root_required"):
        asyncio.run(synthetic_invite_admin._main())
