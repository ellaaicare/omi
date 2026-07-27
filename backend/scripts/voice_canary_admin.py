#!/usr/bin/env python3
"""One-operator CLI for Ella's Phase-1 voice canary controls."""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import voice_canary

DEFAULT_PROVIDERS = ["grok-voice"]
DEFAULT_MODES = ["v4"]
DEFAULT_FALLBACK_POLICY = {"enabled": False, "order": []}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))


async def _grant(args: argparse.Namespace) -> None:
    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO voice_entitlements (
                uid, status, plan, daily_limit_s, monthly_limit_s,
                max_session_s, max_concurrent, soft_limit_ratio,
                provider_allowlist, mode_allowlist, fallback_policy, operator_note
            ) VALUES (
                $1, 'active', $2, $3, $4, $5, $6, $7, $8::text[], $9::text[],
                $10::jsonb, $11
            )
            ON CONFLICT (uid) DO UPDATE SET
                status = 'active',
                plan = EXCLUDED.plan,
                revision = voice_entitlements.revision + 1,
                daily_limit_s = EXCLUDED.daily_limit_s,
                monthly_limit_s = EXCLUDED.monthly_limit_s,
                max_session_s = EXCLUDED.max_session_s,
                max_concurrent = EXCLUDED.max_concurrent,
                soft_limit_ratio = EXCLUDED.soft_limit_ratio,
                provider_allowlist = EXCLUDED.provider_allowlist,
                mode_allowlist = EXCLUDED.mode_allowlist,
                fallback_policy = EXCLUDED.fallback_policy,
                operator_note = EXCLUDED.operator_note,
                updated_at = NOW()
            RETURNING *
            """,
            args.uid,
            args.plan,
            args.daily_minutes * 60,
            args.monthly_hours * 60 * 60,
            args.max_session_minutes * 60,
            args.max_concurrent,
            args.soft_warning_percent / 100,
            args.provider,
            args.mode,
            json.dumps(DEFAULT_FALLBACK_POLICY),
            args.note,
        )
    _print({"action": "grant", "entitlement": dict(row)})


async def _status(args: argparse.Namespace) -> None:
    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE voice_entitlements
            SET status = $2,
                revision = revision + 1,
                operator_note = COALESCE($3, operator_note),
                updated_at = NOW()
            WHERE uid = $1
            RETURNING *
            """,
            args.uid,
            args.status,
            args.note,
        )
    if not row:
        raise SystemExit(f"No entitlement exists for {args.uid}")
    _print({"action": args.status, "entitlement": dict(row)})


async def _kill(args: argparse.Namespace) -> None:
    scope_value = "*" if args.scope == "global" else args.value
    if not scope_value:
        raise SystemExit("--value is required for user/provider kill switches")
    row = await voice_canary.set_kill_switch(
        scope_type=args.scope,
        scope_value=scope_value,
        enabled=args.enabled == "on",
        reason=args.reason,
        updated_by=args.operator,
    )
    _print({"action": "kill_switch", "switch": dict(row)})


async def _show(args: argparse.Namespace) -> None:
    contract = await voice_canary.get_entitlement_contract(args.uid)
    entitlement = await voice_canary.get_entitlement(args.uid)
    _print({"uid": args.uid, "entitlement": entitlement, "contract": contract})


async def _delete_voice(args: argparse.Namespace) -> None:
    if args.confirm_uid != args.uid:
        raise SystemExit("--confirm-uid must exactly match --uid")
    deleted = await voice_canary.delete_user_voice_data(args.uid)
    receipt = {
        "receipt_version": 1,
        "kind": "ella_voice_canary_manual_deletion",
        "uid_sha256_required_for_external_copy": True,
        "uid": args.uid,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "operator": args.operator,
        "deleted_rows": deleted,
        "content_retained": False,
    }
    if args.receipt:
        receipt_path = Path(args.receipt).expanduser().resolve()
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        receipt["receipt_path"] = str(receipt_path)
    _print(receipt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    grant = subparsers.add_parser("grant", help="Create or reactivate a canary entitlement")
    grant.add_argument("uid")
    grant.add_argument("--plan", default="canary")
    grant.add_argument("--daily-minutes", type=int, default=45)
    grant.add_argument("--monthly-hours", type=int, default=12)
    grant.add_argument("--max-session-minutes", type=int, default=20)
    grant.add_argument("--max-concurrent", type=int, default=1)
    grant.add_argument("--soft-warning-percent", type=int, default=80)
    grant.add_argument("--provider", action="append", default=None)
    grant.add_argument("--mode", action="append", default=None)
    grant.add_argument("--note", default="Phase-1 manual canary grant")
    grant.set_defaults(handler=_grant)

    for name in ("suspend", "revoke", "expire"):
        command = subparsers.add_parser(name)
        command.add_argument("uid")
        command.add_argument("--note")
        command.set_defaults(
            handler=_status,
            status={"suspend": "suspended", "revoke": "revoked", "expire": "expired"}[name],
        )

    kill = subparsers.add_parser("kill", help="Flip a global, user, or provider kill switch")
    kill.add_argument("scope", choices=["global", "user", "provider"])
    kill.add_argument("enabled", choices=["on", "off"])
    kill.add_argument("--value")
    kill.add_argument("--reason", default="Operator canary control")
    kill.add_argument("--operator", default="greg")
    kill.set_defaults(handler=_kill)

    show = subparsers.add_parser("show")
    show.add_argument("uid")
    show.set_defaults(handler=_show)

    delete_voice = subparsers.add_parser(
        "delete-voice-data",
        help="Delete entitlement, active leases, rate events, and usage ledger for one UID",
    )
    delete_voice.add_argument("--uid", required=True)
    delete_voice.add_argument("--confirm-uid", required=True)
    delete_voice.add_argument("--operator", default="greg")
    delete_voice.add_argument("--receipt")
    delete_voice.set_defaults(handler=_delete_voice)
    return parser


async def _main() -> None:
    args = _parser().parse_args()
    if getattr(args, "provider", None) is None:
        args.provider = DEFAULT_PROVIDERS
    if getattr(args, "mode", None) is None:
        args.mode = DEFAULT_MODES
    await args.handler(args)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(130)
