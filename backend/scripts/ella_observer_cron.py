#!/usr/bin/env python3
"""Hermes cron entrypoint for Ella Observer.

This script calls the backend Observer endpoint. Install it as a Hermes
no-agent cron job so the existing Hermes scheduler/ticker owns execution.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _default_since(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Ella Observer through the backend API")
    parser.add_argument("--backend-url", default=_env("ELLA_BACKEND_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--uid", default=_env("ELLA_OBSERVER_UID", _env("ELLA_PLATO_UID", "")))
    parser.add_argument("--canonical-identity", default=_env("ELLA_OBSERVER_CANONICAL_IDENTITY", ""))
    parser.add_argument("--since", default="")
    parser.add_argument("--lookback-minutes", type=int, default=int(_env("ELLA_OBSERVER_LOOKBACK_MINUTES", "30")))
    parser.add_argument("--limit", type=int, default=int(_env("ELLA_OBSERVER_LIMIT", "100")))
    parser.add_argument("--channels", default=_env("ELLA_OBSERVER_CHANNELS", ""))
    parser.add_argument("--extractor-mode", default=_env("ELLA_OBSERVER_EXTRACTOR_MODE", "hermes"))
    parser.add_argument(
        "--extractor-timeout-seconds",
        type=float,
        default=float(_env("ELLA_OBSERVER_EXTRACTOR_TIMEOUT_SECONDS", "45")),
    )
    parser.add_argument("--live", action="store_true", help="Create proposals instead of dry-running")
    args = parser.parse_args()

    token = _env("ELLA_OBSERVER_ADMIN_TOKEN")
    if not token:
        print("ELLA_OBSERVER_ADMIN_TOKEN is required", file=sys.stderr)
        return 2
    if not args.uid:
        print("--uid or ELLA_OBSERVER_UID is required", file=sys.stderr)
        return 2

    payload = {
        "uid": args.uid,
        "canonical_identity": args.canonical_identity,
        "since": args.since or _default_since(args.lookback_minutes),
        "dry_run": not args.live,
        "limit": args.limit,
        "channels": [part.strip() for part in args.channels.split(",") if part.strip()],
        "extractor_mode": args.extractor_mode,
        "extractor_timeout_seconds": args.extractor_timeout_seconds,
        "model_metadata": {
            "invoked_by": "hermes_cron_script",
            "script": "backend/scripts/ella_observer_cron.py",
        },
    }
    url = args.backend_url.rstrip("/") + "/v1/ella/observer/run"
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            url,
            headers={"X-Ella-Observer-Token": token, "X-Ella-Subject-Uid": args.uid},
            json=payload,
        )
    if response.status_code >= 400:
        print(f"Observer request failed: HTTP {response.status_code} {response.text[:500]}", file=sys.stderr)
        return 1
    data = response.json()
    print(json.dumps(data.get("observer_run") or data, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
