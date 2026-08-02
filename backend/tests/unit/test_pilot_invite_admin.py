import argparse
import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from database import invitations
from ella.services import ai_consent, invitation_authority
from scripts import pilot_invite_admin


def _issue_args(root: Path, output: Path, *, email: str = "pilot@example.test") -> argparse.Namespace:
    email_input = root / "scope.input"
    if email:
        email_input.write_text(f"{email}\n", encoding="utf-8")
        email_input.chmod(0o600)
    return argparse.Namespace(
        kind="ordinary",
        email_input_file=str(email_input) if email else None,
        expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        expected_environment="unit-test",
        approved_code_output_root=str(root),
        code_output_file=str(output),
    )


def test_self_hosted_admission_uses_verified_email_and_current_policy_without_uid_allowlist(monkeypatch):
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED", "false")
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED_UIDS", "different-user")

    admission = invitation_authority.authorize_self_hosted_invitation(
        "firebase-user",
        "Pilot@Example.Test",
    )

    assert admission.account_uid == admission.profile_uid == "firebase-user"
    assert admission.verified_email == "pilot@example.test"
    assert admission.required_profile_class == "real"
    assert admission.consent_pending is True
    assert admission.consent_receipt_id == admission.profile_binding_id == ""
    assert admission.policy_version == ai_consent.CURRENT_POLICY_VERSION
    assert admission.processor_set_hash == ai_consent.CURRENT_PROCESSOR_SET_HASH
    assert admission.scope_version == ai_consent.CURRENT_SCOPE_VERSION
    assert admission.scope_hash == ai_consent.CURRENT_SCOPE_HASH


def test_protected_show_once_operator_never_prints_code_or_email(tmp_path, monkeypatch, capsys):
    root = tmp_path / "handoff"
    root.mkdir(mode=0o700)
    output = root / "pilot.code"
    captured = {}
    config = invitations.InvitationConfig(
        hmac_pepper=b"pilot-operator-unit-test-pepper",
        redemption_enabled=True,
        ordinary_enabled=True,
        app_review_enabled=True,
    )

    async def fake_issue(**kwargs):
        captured.update(kwargs)
        return {
            "receipt_id": "11111111-1111-1111-1111-111111111111",
            "kind": "ordinary",
            "state": "sent",
            "email_scoped": True,
            "idempotent": False,
        }

    monkeypatch.setattr(pilot_invite_admin, "ROOT_UID", os.getuid())
    monkeypatch.setattr(pilot_invite_admin, "_configuration", lambda: config)
    monkeypatch.setattr(pilot_invite_admin, "_issue_invitation", fake_issue)

    asyncio.run(pilot_invite_admin._issue(_issue_args(root, output)))

    code = output.read_text(encoding="ascii").strip()
    streams = capsys.readouterr()
    assert invitations.normalize_invite_code(code)
    assert code not in streams.out
    assert code not in streams.err
    assert "pilot@example.test" not in streams.out
    assert "pilot@example.test" not in streams.err
    assert oct(output.stat().st_mode & 0o777) == "0o400"
    assert captured["allowed_email_hash"] == pilot_invite_admin._email_hash(config, "pilot@example.test")
    assert "email" not in captured
    assert captured["code"] == code
    assert captured["code_file_existed"] is False


def test_scoped_email_interface_keeps_plaintext_out_of_argv_receipts_logs_and_errors(
    tmp_path,
    monkeypatch,
    capsys,
    caplog,
):
    root = tmp_path / "handoff"
    root.mkdir(mode=0o700)
    output = root / "pilot.code"
    secret_email = "private.person@example.test"
    args = _issue_args(root, output, email=secret_email)
    argv = [
        "issue",
        "--kind",
        "ordinary",
        "--email-input-file",
        args.email_input_file,
        "--expires-at",
        args.expires_at,
        "--expected-environment",
        args.expected_environment,
        "--approved-code-output-root",
        args.approved_code_output_root,
        "--code-output-file",
        args.code_output_file,
    ]
    assert secret_email not in argv
    parsed = pilot_invite_admin._parser().parse_args(argv)
    assert parsed.email_input_file == args.email_input_file
    assert not hasattr(parsed, "email")

    secret_code = "WXYZ-2345"
    assert secret_email not in argv
    assert secret_code not in argv
    for sensitive_argv in (["issue", "--email", secret_email], ["show", secret_code]):
        with pytest.raises(pilot_invite_admin.PilotInvitationError) as forbidden:
            pilot_invite_admin._reject_sensitive_argv(sensitive_argv)
        assert forbidden.value.code == "secret_argv_forbidden"
        assert secret_email not in str(forbidden.value)
        assert secret_code not in str(forbidden.value)

    async def ambiguous():
        raise RuntimeError(f"provider detail must stay hidden: {secret_email}")

    monkeypatch.setattr(pilot_invite_admin, "_main", ambiguous)
    assert pilot_invite_admin._run_cli() == 2
    streams = capsys.readouterr()
    assert secret_email not in streams.out
    assert secret_email not in streams.err
    assert "outcome_ambiguous" in streams.err
    assert secret_email not in caplog.text

    receipt = {
        "receipt_id": "11111111-1111-1111-1111-111111111111",
        "kind": "ordinary",
        "state": "sent",
        "email_scoped": True,
        "idempotent": False,
    }
    pilot_invite_admin._print_receipt("issue", receipt, code_output_file=str(output))
    rendered = capsys.readouterr().out
    assert secret_email not in rendered
    assert '"content_free": true' in rendered


def test_operator_examples_never_embed_plaintext_email_or_legacy_email_argument():
    root = Path(__file__).resolve().parents[2]
    rendered = (root / "ella" / "docs" / "INVITE_REDEMPTION_CONTROL_PLANE.md").read_text(encoding="utf-8")
    assert not re.search(r"--email(?:=|\s)", rendered)
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", rendered)
    assert not re.search(r"[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4}-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4}", rendered)
    assert "--email-input-file" in rendered
    assert "approved root-owned" in rendered


def test_protected_rotate_is_show_once_and_content_free(tmp_path, monkeypatch, capsys):
    root = tmp_path / "handoff"
    root.mkdir(mode=0o700)
    output = root / "rotated.code"
    captured = {}
    config = invitations.InvitationConfig(
        hmac_pepper=b"pilot-operator-unit-test-pepper",
        redemption_enabled=True,
        ordinary_enabled=True,
        app_review_enabled=True,
    )

    async def fake_rotate(**kwargs):
        captured.update(kwargs)
        return {
            "receipt_id": "22222222-2222-2222-2222-222222222222",
            "rotated_from": "11111111-1111-1111-1111-111111111111",
            "state": "sent",
            "idempotent": False,
        }

    monkeypatch.setattr(pilot_invite_admin, "ROOT_UID", os.getuid())
    monkeypatch.setattr(pilot_invite_admin, "_configuration", lambda: config)
    monkeypatch.setattr(pilot_invite_admin, "_rotate_invitation", fake_rotate)
    args = argparse.Namespace(
        receipt_id="11111111-1111-1111-1111-111111111111",
        expected_version=1,
        expected_environment="unit-test",
        approved_code_output_root=str(root),
        code_output_file=str(output),
    )

    asyncio.run(pilot_invite_admin._rotate(args))

    code = output.read_text(encoding="ascii").strip()
    streams = capsys.readouterr()
    assert invitations.normalize_invite_code(code)
    assert code not in streams.out
    assert code not in streams.err
    assert oct(output.stat().st_mode & 0o777) == "0o400"
    assert captured["code"] == code
    assert captured["code_file_existed"] is False


def test_migration_015_is_additive_and_reserves_invitation_lane():
    migration = (
        Path(__file__).resolve().parents[2] / "migrations" / "015_add_invitation_allowed_email_hash.sql"
    ).read_text(encoding="utf-8")
    for fragment in (
        "allowed_email_hash",
        "invitation_consent_pending",
        "consent_pending",
        "invitation_target_id",
        "ella_runtime_targets_invitation_target_key",
        "provider IN ('retained', 'hermes_cloud', 'hermes')",
        "required_profile_class IN ('real', 'synthetic')",
        "pilot_operator_rotated",
        "legacy_unmapped",
        "revoked_at",
    ):
        assert fragment in migration
    assert "DROP TABLE" not in migration
    assert "TRUNCATE" not in migration


@pytest.mark.parametrize("email", ["", "not-an-email"])
def test_self_hosted_admission_rejects_missing_verified_email(monkeypatch, email):
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED", "false")
    with pytest.raises(invitations.InvitePilotGateDenied):
        invitation_authority.authorize_self_hosted_invitation("firebase-user", email)
