"""
Ella post-save notification hook.

Sends a lightweight notification (conversation_id + uid only) to a
configured webhook when a conversation has been processed and saved.
This allows external agents (e.g. OpenClaw) to be notified without
receiving the full transcript, avoiding context bloat.

Part of #35 - Enhanced Conversation Summaries via OpenClaw
"""

import logging
import os
import threading

import requests

logger = logging.getLogger(__name__)

CONVERSATION_READY_WEBHOOK = os.getenv(
    'ELLA_CONVERSATION_READY_WEBHOOK', ''
)


def notify_conversation_ready(uid: str, conversation_id: str):
    """
    Fire-and-forget notification that a conversation is ready for
    post-processing enhancement. Sends only IDs, not content.
    """
    if not CONVERSATION_READY_WEBHOOK:
        return

    def _send():
        try:
            resp = requests.post(
                CONVERSATION_READY_WEBHOOK,
                json={
                    'uid': uid,
                    'conversation_id': conversation_id,
                    'event': 'conversation_ready',
                },
                timeout=5,
            )
            logger.info(
                'conversation-ready webhook: %s status=%s',
                conversation_id,
                resp.status_code,
            )
        except Exception as e:
            logger.warning('conversation-ready webhook failed: %s', e)

    threading.Thread(target=_send, daemon=True).start()
