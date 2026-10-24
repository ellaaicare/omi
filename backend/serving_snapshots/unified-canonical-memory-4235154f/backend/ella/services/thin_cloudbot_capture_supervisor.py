"""One fixed Thin stock/STT child, finite deadline, owned cleanup, never retry."""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from ella.services.thin_cloudbot_consent import need, ThinConsentHold


def run_capture(authority, path, started_at, reservation):
    packet = {'uid': authority.uid, 'auth_time': authority.auth_time, 'scope': authority.scope,
              'path': path, 'started_at': started_at.isoformat() if started_at is not None else None,
              'reservation': {**reservation, 'expires_at': reservation['expires_at'].isoformat()}}
    if path is None:
        packet={'uid':authority.uid,'auth_time':authority.auth_time,'scope':authority.scope,
                'live':reservation['live'],'reservation':{**reservation,'expires_at':reservation['expires_at'].isoformat()}}
    seconds = min(120, (reservation['expires_at'] - datetime.now(timezone.utc)).total_seconds())
    need(seconds > 0, 'thin_capture_deadline')
    expires = time.monotonic() + seconds
    process = None
    result = None
    cleanup = False
    try:
        process = subprocess.Popen([sys.executable, '-m', 'ella.services.thin_cloudbot_capture'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, cwd=str(Path(__file__).resolve().parents[2]))
        remaining = expires - time.monotonic()
        need(remaining > 0, 'thin_capture_spawn_deadline_unknown')
        raw, _ = process.communicate(json.dumps(packet).encode(), timeout=remaining)
        need(time.monotonic() < expires and process.returncode == 0 and len(raw) <= 8192,
             'thin_capture_child_unknown_no_retry')
        result = json.loads(raw)
        need(type(result) is dict and set(result) == {'kind','source_id'}
             and result['kind'] == 'new_memories' and result['source_id'] == reservation['source_id'],
             'thin_capture_child_receipt_unknown')
    except BaseException as error:
        # Do not publish exception/provider/body text. The permanent reservation remains.
        raise ThinConsentHold('thin_capture_execution_unknown_no_retry') from None
    finally:
        if process is not None:
            for sig, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 3)):
                try: os.killpg(process.pid, sig)
                except ProcessLookupError: pass
                try: process.communicate(timeout=grace)
                except subprocess.TimeoutExpired: continue
                try: os.killpg(process.pid, 0)
                except ProcessLookupError:
                    cleanup = True
                    break
            for stream in (process.stdin, process.stdout):
                if stream is not None: stream.close()
            need(cleanup and process.poll() is not None, 'thin_capture_owned_cleanup_unknown')
    # Local closure says nothing about remote STT/model drain on timeout.
    return result['kind'], result['source_id']
