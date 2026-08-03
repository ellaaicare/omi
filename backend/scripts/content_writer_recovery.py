#!/usr/bin/env python3
"""Recover one orphaned content writer using local kernel terminal proof."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database._client import db as production_firestore_db
from database import content_write_recovery

ROOT_UID = 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Run only in the recorded worker's host, boot, and PID namespace. "
            "A refusal is an abort: retain DRAINING and retry only after the boundary is authoritative."
        ),
    )
    parser.add_argument(
        "--subject-hash",
        required=True,
        help="SHA-256 of the exact Firebase UID; plaintext UIDs are not accepted",
    )
    parser.add_argument(
        "--token-hash",
        required=True,
        help="SHA-256 of the exact durable writer token; plaintext tokens are not accepted",
    )
    return parser


def _print_receipt(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(
    argv: Sequence[str] | None = None,
    *,
    firestore_db: Any = production_firestore_db,
) -> int:
    args = _parser().parse_args(argv)
    refusal = {
        "action": "content_writer_recovery",
        "content_free": True,
        "result": "refused",
    }
    if os.geteuid() != ROOT_UID:
        _print_receipt({**refusal, "reason": "account_writer_recovery_root_required"})
        return 77
    try:
        receipt = content_write_recovery.recover_orphaned_writer(
            firestore_db,
            subject_hash=args.subject_hash,
            token_hash=args.token_hash,
        )
    except content_write_recovery.ContentWriterRecoveryError as exc:
        _print_receipt({**refusal, "reason": exc.code})
        return 2
    _print_receipt(receipt.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
