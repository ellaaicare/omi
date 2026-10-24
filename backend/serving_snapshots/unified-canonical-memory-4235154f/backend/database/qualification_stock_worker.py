"""Internal child entry, not an HTTP/authentication surface or alternate producer."""
import contextlib
import json
import os
import sys
from datetime import datetime, timezone


def execute(packet):
    if type(packet) is not dict or set(packet) != {
            'uid', 'request_id', 'source_id', 'worker_nonce', 'deadline'}:
        raise ValueError('stock_worker_packet_invalid')
    from database._client import db
    from database.qualification_transcript_producer import start_stock_worker, record_completion
    from database.conversations import _prepare_conversation_for_read
    from ella.services.ai_consent import assert_current_ai_consent, configure_firestore_db
    from models.conversation import Conversation
    from utils.conversations.process_conversation import process_conversation
    uid = packet['uid']
    deadline = datetime.fromisoformat(packet['deadline'])
    configure_firestore_db(db)
    assert_current_ai_consent(uid)
    value = start_stock_worker(db.transaction(), db, uid, packet['request_id'],
                              source_id=packet['source_id'], worker_nonce=packet['worker_nonce'],
                              deadline=deadline, now=datetime.now(timezone.utc))
    source = Conversation(**_prepare_conversation_for_read(value['stored_document'], uid))
    # Check after model decoding / consent / transaction waits, immediately before
    # normal processing. The supervising parent enforces the same absolute deadline.
    if datetime.now(timezone.utc) >= deadline:
        raise ValueError('stock_worker_deadline_before_effect')
    process_conversation(uid, 'en', source, force_process=True)
    return record_completion(db.transaction(), db, uid, packet['request_id'],
                             source_id=packet['source_id'], now=datetime.now(timezone.utc))


def main():
    receipt_fd=None
    try:
        # Keep fd1 redirected for the entire child lifetime, including late thread/native logs.
        receipt_fd=os.dup(sys.stdout.fileno())
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            return 2
        # Suppress normal source/provider logs; only the fixed neutral receipt is
        # returned to the authenticated caller. No private text or errors exported.
        with open(os.devnull, 'w') as sink:
            os.dup2(sink.fileno(),sys.stdout.fileno())
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = execute(json.loads(raw))
        # Sole neutral receipt goes through the private saved pipe, not mutable global stdout.
        with os.fdopen(receipt_fd,'w') as receipt:
            receipt_fd=None
            print(json.dumps(result),file=receipt,flush=True)
        return 0
    except Exception as error:
        from ella.services.causal_diagnostics import emit
        emit(error,'stock_worker_execute')
        return 2
    finally:
        if receipt_fd is not None:os.close(receipt_fd)


if __name__ == '__main__':
    # Normal Python shutdown drains ordinary stock post-commit threads. If any
    # hangs, the parent deadline terminates this owned group and reports unknown.
    sys.exit(main())
