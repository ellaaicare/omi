#!/usr/bin/env python3
"""Root-only production invitation admin for self-hosted Ella pilot and App Review codes.

Issues invitation codes for the self-hosted Hermes runtime.
Codes can be open (any authenticated user) or email-scoped.

Usage:
  # Open code — any signed-in user can redeem
  sudo python pilot_invite_admin.py issue --kind ordinary --slots 5 --expiry-days 90

  # Email-scoped code — only the matching email can redeem
  sudo python pilot_invite_admin.py issue --kind ordinary --email alice@example.com

  # App Review code — open, with reviewer-friendly limits
  sudo python pilot_invite_admin.py issue --kind app_review --slots 22 --expiry-days 365

  # Inspect / revoke / rotate
  sudo python pilot_invite_admin.py show --receipt-id <uuid>
  sudo python pilot_invite_admin.py revoke --receipt-id <uuid>
  sudo python pilot_invite_admin.py rotate --receipt-id <uuid> --kind ordinary
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import authority_advisory_lock, invitations, voice_canary
from database.invitations import (
    SELF_HOSTED_OPERATOR_COHORT,
    SELF_HOSTED_OPERATOR_ENTITLEMENT_POLICY,
    SELF_HOSTED_OPERATOR_POLICY_REVISION,
    email_hash_for,
)

ROOT_UID = 0
OPERATOR_PURPOSE = "self_hosted_pilot_issuance"
MIN_EXPIRY = timedelta(minutes=5)
MAX_EXPIRY = timedelta(days=400)


class PilotInvitationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require_root() -> None:
    if os.geteuid() != ROOT_UID:
        raise SystemExit("operator_refused:root_required")


def _print_receipt(action: str, receipt: dict[str, Any]) -> None:
    print(json.dumps({"action": action, **receipt}, sort_keys=True, default=str))


async def _issue_invitation(
    *,
    email: str,
    kind: str,
    reserved_slots: int,
    max_redemptions: int,
    expires_at: datetime,
    config: invitations.InvitationConfig,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expiry = expires_at.astimezone(timezone.utc)

    if kind not in {"ordinary", "app_review"}:
        raise PilotInvitationError("invalid_kind")
    if reserved_slots < 1 or reserved_slots > 100:
        raise PilotInvitationError("invalid_slots")
    if max_redemptions < 1 or (kind == "ordinary" and max_redemptions > 100):
        raise PilotInvitationError("invalid_max_redemptions")
    if expiry <= now or expiry > now + MAX_EXPIRY:
        raise PilotInvitationError("invalid_expiry")
    if not config.redemption_enabled:
        raise PilotInvitationError("redemption_disabled")
    if kind == "ordinary" and not config.ordinary_enabled:
        raise PilotInvitationError("ordinary_disabled")
    if kind == "app_review" and not config.app_review_enabled:
        raise PilotInvitationError("app_review_disabled")

    code = invitations.generate_invite_code()
    normalized_code = invitations.normalize_invite_code(code)
    invitation_code_hmac = invitations.code_hmac(config, normalized_code)

    # Compute email hash if scoped
    allowed_email_hash = None
    email_display = "open"
    if email:
        allowed_email_hash = email_hash_for(config, email)
        email_display = email

    # Entitlement policy
    if kind == "app_review":
        entitlement_policy = {
            **SELF_HOSTED_OPERATOR_ENTITLEMENT_POLICY,
            "daily_limit_s": 14400,
            "monthly_limit_s": 432000,
            "max_session_s": 3600,
            "max_audio_bytes_per_session": 500_000_000,
        }
    else:
        entitlement_policy = SELF_HOSTED_OPERATOR_ENTITLEMENT_POLICY

    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Check for code collision (astronomically unlikely but guard anyway)
            if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM ella_invitations WHERE code_hmac = $1)",
                invitation_code_hmac,
            ):
                raise PilotInvitationError("code_collision_retry")

            reservation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitation_capacity_reservations (
                    pool_key, state, reserved_slots, expires_at
                ) VALUES ($1, 'reserved', $2, $3)
                RETURNING id
                """,
                f"pilot_{kind}",
                reserved_slots,
                expiry,
            )
            invitation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitations (
                    capacity_reservation_id, kind, code_hmac, display_hint,
                    state, delivery_state, usage_mode, max_redemptions,
                    reserved_setup_slots, entitlement_policy_revision,
                    entitlement_policy, required_consent_policy_version,
                    required_consent_processor_set_hash,
                    required_consent_scope_version, required_consent_scope_hash,
                    cohort, exclude_from_product_analytics,
                    first_sent_at, expires_at, allowed_email_hash
                ) VALUES (
                    $1, $2, $3, $4,
                    'sent', 'sent', 'multi_use', $5,
                    $6, $7, $8::jsonb, $9, $10, $11, $12,
                    $13, $14, $15, $16, $17
                )
                RETURNING id
                """,
                reservation_id,
                kind,
                invitation_code_hmac,
                f"{normalized_code[:2]}**",
                max_redemptions,
                reserved_slots,
                SELF_HOSTED_OPERATOR_POLICY_REVISION,
                json.dumps(entitlement_policy, sort_keys=True),
                "self-hosted-v1",
                "self-hosted-hash",
                "self-hosted-scope-v1",
                "self-hosted-scope-hash",
                SELF_HOSTED_OPERATOR_COHORT,
                kind == "app_review",
                now,
                expiry,
                allowed_email_hash,
            )
            # Create a placeholder target (real users bind at redemption time)
            await conn.execute(
                """
                INSERT INTO ella_invitation_targets (
                    invitation_id, account_ref_hmac, profile_ref_hmac,
                    required_profile_class
                ) VALUES ($1, $2, $3, 'real')
                """,
                invitation_id,
                "self-hosted-open" if not email else f"self-hosted-email-{normalized_code[:4]}",
                "self-hosted-open" if not email else f"self-hosted-email-{normalized_code[:4]}",
            )

    return {
        "receipt_id": str(invitation_id),
        "kind": kind,
        "cohort": SELF_HOSTED_OPERATOR_COHORT,
        "state": "sent",
        "code": code,
        "display_hint": f"{normalized_code[:2]}**",
        "email": email_display if email else None,
        "email_scoped": bool(email),
        "max_redemptions": max_redemptions,
        "reserved_slots": reserved_slots,
        "expires_at": expiry.isoformat(),
        "content_free": True,
    }


async def _show_invitation(*, receipt_id: str) -> dict[str, Any]:
    import uuid as _uuid

    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM ella_invitations WHERE id = $1", _uuid.UUID(receipt_id))
        if not row:
            raise PilotInvitationError("receipt_not_found")
        r = dict(row)
        return {
            "receipt_id": str(r["id"]),
            "kind": str(r["kind"]),
            "cohort": str(r["cohort"]),
            "state": str(r["state"]),
            "delivery_state": str(r["delivery_state"]),
            "redemption_count": int(r["redemption_count"]),
            "max_redemptions": int(r["max_redemptions"]),
            "email_scoped": r["allowed_email_hash"] is not None,
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
            "content_free": True,
        }


async def _revoke_invitation(*, receipt_id: str) -> dict[str, Any]:
    import uuid as _uuid

    now = datetime.now(timezone.utc)
    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM ella_invitations WHERE id = $1 FOR UPDATE",
                _uuid.UUID(receipt_id),
            )
            if not row:
                raise PilotInvitationError("receipt_not_found")
            r = dict(row)
            if str(r["state"]) == "revoked":
                return {"receipt_id": str(r["id"]), "state": "revoked", "idempotent": True}
            if str(r["state"]) not in {"issued", "sent"}:
                raise PilotInvitationError("not_revokable")
            if int(r["redemption_count"]) > 0:
                raise PilotInvitationError("has_redemptions")
            await conn.execute(
                """
                UPDATE ella_invitations
                SET state = 'revoked', delivery_state = 'suppressed',
                    revoked_at = $2, version = version + 1, updated_at = $2
                WHERE id = $1
                """,
                r["id"],
                now,
            )
            await conn.execute(
                """
                UPDATE ella_invitation_capacity_reservations
                SET state = 'released', released_at = $2,
                    version = version + 1, updated_at = $2
                WHERE id = $1 AND state = 'reserved'
                """,
                r["capacity_reservation_id"],
                now,
            )
            return {"receipt_id": str(r["id"]), "state": "revoked"}


async def main() -> None:
    _require_root()
    config = invitations.InvitationConfig.from_env()

    parser = argparse.ArgumentParser(description="Pilot invitation admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue")
    issue.add_argument("--kind", required=True, choices=["ordinary", "app_review"])
    issue.add_argument("--email", default=None, help="Email to scope code to (omit for open code)")
    issue.add_argument("--slots", type=int, default=5)
    issue.add_argument("--expiry-days", type=int, default=90)

    sub.add_parser("show").add_argument("--receipt-id", required=True)
    sub.add_parser("revoke").add_argument("--receipt-id", required=True)
    rotate_parser = sub.add_parser("rotate")
    rotate_parser.add_argument("--receipt-id", required=True)
    rotate_parser.add_argument("--kind", required=True)
    rotate_parser.add_argument("--email", default=None)

    args = parser.parse_args()

    try:
        if args.command == "issue":
            max_redemptions = 1 if args.kind == "ordinary" else 22
            expires_at = datetime.now(timezone.utc) + timedelta(days=args.expiry_days)
            receipt = await _issue_invitation(
                email=args.email or "",
                kind=args.kind,
                reserved_slots=args.slots,
                max_redemptions=max_redemptions,
                expires_at=expires_at,
                config=config,
            )
            _print_receipt("issued", receipt)
            scope_msg = f"scoped to {args.email}" if args.email else "OPEN (any authenticated user)"
            print(f"\nPLAINTEXT CODE (show once only): {receipt['code']}", file=sys.stderr)
            print(f"Scope: {scope_msg}", file=sys.stderr)
            print(f"Receipt ID: {receipt['receipt_id']}", file=sys.stderr)

        elif args.command == "show":
            receipt = await _show_invitation(receipt_id=args.receipt_id)
            _print_receipt("show", receipt)

        elif args.command == "revoke":
            receipt = await _revoke_invitation(receipt_id=args.receipt_id)
            _print_receipt("revoke", receipt)

        elif args.command == "rotate":
            await _revoke_invitation(receipt_id=args.receipt_id)
            max_redemptions = 1 if args.kind == "ordinary" else 22
            expires_at = datetime.now(timezone.utc) + timedelta(days=90)
            receipt = await _issue_invitation(
                email=args.email or "",
                kind=args.kind,
                reserved_slots=5,
                max_redemptions=max_redemptions,
                expires_at=expires_at,
                config=config,
            )
            _print_receipt("rotated", {"revoked_receipt_id": args.receipt_id, **receipt})

    except PilotInvitationError as exc:
        print(json.dumps({"status": "error", "code": exc.code}), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"status": "error", "code": "internal_error", "detail": str(exc)}), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
