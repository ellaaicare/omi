"""Bound only the explicit fictional producer's ordinary stock-processing child.

Local process death is never a claim of remote provider cancellation. The parent
has already consumed the durable attempt; every failure is non-retryable.
"""
import json
import hashlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STOCK_SECONDS = 120
TERM_GRACE_SECONDS = 2
KILL_REAP_SECONDS = 3


def _group_exists(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _close_group(process):
    """Bounded local cleanup, including process-exit / kill races."""
    for sig, grace in ((signal.SIGTERM, TERM_GRACE_SECONDS),
                       (signal.SIGKILL, KILL_REAP_SECONDS)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            # Still reap the owned leader; inability to fence is not success.
            pass
        try:
            process.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        if not _group_exists(process.pid):
            return True
    return process.poll() is not None and not _group_exists(process.pid)


def _supervise(command, packet, seconds, *, cwd=None, expected_consumer="hermes"):
    """Private runner; command is fixed by run_stock, never HTTP/model input."""
    started = time.monotonic()
    out = {'local_status': 'spawn_unknown', 'local_group_absent': False,
           'remote_provider_drain': 'unknown', 'retry_allowed': False}
    process = None
    try:
        payload = json.dumps(packet, separators=(',', ':')).encode('utf-8')
        dispositions={'hermes':'separate_normal_worker_readback_required',
                      'muse':'not_admitted_muse_pending_readback_required'}
        if expected_consumer not in dispositions or len(payload) > 8192 or not 0 < seconds <= STOCK_SECONDS:
            out['local_status'] = 'invalid_server_packet_or_deadline'
            return out
        process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True, cwd=cwd)
        # Include Popen overhead. No fresh budget after a slow spawn.
        remaining = seconds - (time.monotonic() - started)
        if remaining <= 0:
            out['local_status'] = 'deadline_before_communicate'
            return out
        try:
            stdout, _ = process.communicate(input=payload, timeout=remaining)
        except subprocess.TimeoutExpired:
            out['local_status'] = 'deadline_exceeded'
            return out
        out['exit_code'] = process.returncode
        if process.returncode != 0:
            out['local_status'] = 'child_failed'
            return out
        if len(stdout) > 8192:
            out['local_status'] = 'child_receipt_invalid'
            return out
        out['receipt_diagnostic']={'stdout_bytes':len(stdout),'stdout_sha256':hashlib.sha256(stdout).hexdigest()}
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeError) as error:
            out['receipt_diagnostic']['reason']='invalid_json'
            out['receipt_diagnostic']['exception_type']=type(error).__name__
            if isinstance(error,json.JSONDecodeError):out['receipt_diagnostic'].update(json_error_position=error.pos,json_error_line=error.lineno,json_error_column=error.colno)
            out['local_status'] = 'child_receipt_invalid'
            return out
        expected = {'source_id', 'state', 'observed_active_summary_version_id',
                    'hermes_enrichment'}
        if (type(result) is not dict or set(result) != expected
                or result['source_id'] != packet['source_id']
                or result['state'] != 'source_completed'
                or type(result['observed_active_summary_version_id']) is not str
                or not result['observed_active_summary_version_id']
                or result['hermes_enrichment'] != dispositions[expected_consumer]):
            out['receipt_diagnostic']['reason']='closed_receipt_contract_mismatch'
            out['receipt_diagnostic']['object_type_valid']=type(result) is dict
            out['receipt_diagnostic']['exact_keys_valid']=type(result) is dict and set(result)==expected
            out['local_status'] = 'child_receipt_invalid'
            return out
        out['receipt_diagnostic']['reason']='valid'
        out['result'] = result
        out['local_status'] = 'completed'
        return out
    except Exception as error:
        from ella.services.causal_diagnostics import emit
        out['failure_diagnostic']=emit(error,'stock_supervisor')
        # Raw provider messages, source text and credentials are not receipts.
        out['local_status'] = 'supervisor_exception_unknown'
        return out
    finally:
        if process is not None:
            try:
                out['local_group_absent'] = _close_group(process)
            except Exception:
                out['local_group_absent'] = False
            out['leader_reaped'] = process.poll() is not None
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        out['elapsed_monotonic_seconds'] = time.monotonic() - started


def run_stock(uid, request_id, source_id, worker_nonce, deadline, *, expected_consumer="hermes"):
    now = datetime.now(timezone.utc)
    seconds = (deadline - now).total_seconds()
    packet = {'uid': uid, 'request_id': request_id, 'source_id': source_id,
              'worker_nonce': worker_nonce, 'deadline': deadline.isoformat()}
    return _supervise([sys.executable, '-m', 'database.qualification_stock_worker'],
                      packet, min(STOCK_SECONDS, seconds),
                      cwd=str(Path(__file__).resolve().parents[1]), expected_consumer=expected_consumer)
