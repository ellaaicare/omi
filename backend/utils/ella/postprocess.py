# Ella AI Post-Process Hook
#
# Fires after a conversation is fully processed and saved to Firestore.
# Sends the completed conversation data to an n8n webhook for downstream
# processing (summarizer agent, caregiver notifications, etc.)

import os
import threading
import time

import requests

from database.hermes_cloud_enrichment_outbox import FirestoreHermesCloudEnrichmentOutbox
from models.hermes_cloud_enrichment_contract import (
    HERMES_CLOUD_ENRICHMENT_POLICY_VERSION,
    build_enrichment_identity,
)

from .config import ELLA_CONFIG

# Configurable via environment variable
POSTPROCESS_WEBHOOK_URL = os.getenv(
    'ELLA_POSTPROCESS_WEBHOOK_URL', f'{ELLA_CONFIG.n8n_base_url}/webhook/conversation-completed'
)
POSTPROCESS_ENABLED = os.getenv('ELLA_POSTPROCESS_ENABLED', 'true').lower() == 'true'
POSTPROCESS_TIMEOUT = float(os.getenv('ELLA_POSTPROCESS_TIMEOUT', '10'))

CONVERSATION_READY_WEBHOOK_URL = os.getenv(
    'ELLA_CONVERSATION_READY_WEBHOOK', f'{ELLA_CONFIG.n8n_base_url}/webhook/conversation-ready'
)

HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS = frozenset(
    item.strip() for item in os.getenv('ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS', '').split(',') if item.strip()
)


def _conversation_payload(conversation) -> dict:
    if hasattr(conversation, "model_dump"):
        return conversation.model_dump(mode="json")
    if hasattr(conversation, "dict"):
        return conversation.dict()
    segments = []
    for segment in getattr(conversation, "transcript_segments", []) or []:
        segments.append(
            {
                "is_user": bool(getattr(segment, "is_user", False)),
                "speaker": getattr(segment, "speaker", None),
                "text": getattr(segment, "text", ""),
            }
        )
    return {
        "id": getattr(conversation, "id", ""),
        "transcript_segments": segments,
    }


def enqueue_cloud_enrichment(uid: str, conversation) -> dict:
    conversation_id = str(getattr(conversation, "id", "") or "")
    identity = build_enrichment_identity(
        uid=uid,
        conversation_id=conversation_id,
        conversation=_conversation_payload(conversation),
    )
    return FirestoreHermesCloudEnrichmentOutbox().enqueue(
        job_id=identity.job_id,
        uid=uid,
        conversation_id=conversation_id,
        client_interaction_id=identity.client_interaction_id,
        transcript_sha256=identity.transcript_sha256,
        policy_version=HERMES_CLOUD_ENRICHMENT_POLICY_VERSION,
    )


def fire_postprocess_webhook(uid: str, conversation) -> None:
    """
    Fire post-process webhook after conversation is fully saved.

    This is called in a background thread from process_conversation.py.
    It sends the conversation metadata and transcript text to n8n
    for downstream processing.

    Args:
        uid: User ID
        conversation: Conversation object (already saved to Firestore)
    """
    cloud_selected = uid in HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS
    if cloud_selected:
        try:
            queued = enqueue_cloud_enrichment(uid, conversation)
        except Exception as exc:
            print(
                f"[FLOW:POSTPROCESS] BLOCKED hermes-cloud enrichment outbox "
                f"uid={uid} conv={conversation.id[:8]} "
                f"error={type(exc).__name__}",
                flush=True,
            )
            return
        print(
            f"[FLOW:POSTPROCESS] QUEUED hermes-cloud enrichment "
            f"uid={uid} conv={conversation.id[:8]} "
            f"status={queued.get('status')}",
            flush=True,
        )
        # The durable cloud outbox exclusively owns selected conversations.
        # Return before deriving or sending any legacy n8n/Mini payload.
        return

    if not POSTPROCESS_ENABLED:
        print(f"[FLOW:POSTPROCESS] DISABLED uid={uid} conv={conversation.id[:8]}...", flush=True)
        return

    _start = time.time()
    conv_short = conversation.id[:8] if conversation.id else "unknown"

    try:
        structured = {}
        if hasattr(conversation, 'structured') and conversation.structured:
            s = conversation.structured
            structured = {
                'title': getattr(s, 'title', None),
                'overview': getattr(s, 'overview', None),
                'emoji': getattr(s, 'emoji', None),
                'category': getattr(s, 'category', None) if hasattr(s, 'category') else None,
            }
            # Handle category enum
            if structured.get('category') and hasattr(structured['category'], 'value'):
                structured['category'] = structured['category'].value

        # Serialize transcript for downstream workspace file writes
        transcript_text = ""
        segment_count = 0
        if hasattr(conversation, 'transcript_segments') and conversation.transcript_segments:
            segment_count = len(conversation.transcript_segments)
            parts = []
            for seg in conversation.transcript_segments:
                speaker = "User" if seg.is_user else (seg.speaker or "Other")
                parts.append(f"{speaker}: {seg.text}")
            transcript_text = "\n\n".join(parts)

        payload = {
            'event': 'conversation.completed',
            'uid': uid,
            'conversation_id': conversation.id,
            'structured': structured,
            'started_at': conversation.started_at.isoformat() if conversation.started_at else None,
            'finished_at': conversation.finished_at.isoformat() if conversation.finished_at else None,
            'language': getattr(conversation, 'language', None),
            'status': getattr(conversation, 'status', None),
        }
        # Handle status enum
        if payload.get('status') and hasattr(payload['status'], 'value'):
            payload['status'] = payload['status'].value

        title = structured.get('title', 'untitled')
        emoji = structured.get('emoji', '')
        print(
            f"[FLOW:POSTPROCESS] firing webhook uid={uid} conv={conv_short} title={title} emoji={emoji} url={POSTPROCESS_WEBHOOK_URL}",
            flush=True,
        )

        try:
            response = requests.post(
                POSTPROCESS_WEBHOOK_URL,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=POSTPROCESS_TIMEOUT,
            )
            _elapsed_completed = int((time.time() - _start) * 1000)
            if response.status_code == 200:
                print(
                    f"[FLOW:POSTPROCESS] OK conversation-completed uid={uid} conv={conv_short} status=200 latency={_elapsed_completed}ms",
                    flush=True,
                )
            else:
                print(
                    f"[FLOW:POSTPROCESS] ERROR conversation-completed uid={uid} conv={conv_short} status={response.status_code} latency={_elapsed_completed}ms",
                    flush=True,
                )
        except requests.RequestException as exc:
            _elapsed_completed = int((time.time() - _start) * 1000)
            print(
                f"[FLOW:POSTPROCESS] ERROR conversation-completed uid={uid} "
                f"conv={conv_short} error={type(exc).__name__} "
                f"latency={_elapsed_completed}ms",
                flush=True,
            )

        # Notify the user's OpenClaw agent via conversation-ready webhook (fire-and-forget)
        ready_payload = {
            'event': 'conversation_ready',
            'uid': uid,
            'conversation_id': conversation.id,
            'transcript': transcript_text,
            'segment_count': segment_count,
            'structured': structured,
            'started_at': conversation.started_at.isoformat() if conversation.started_at else None,
            'finished_at': conversation.finished_at.isoformat() if conversation.finished_at else None,
        }

        def _fire_ready(_url, _payload, _uid, _conv_short, _segments):
            try:
                _t = time.time()
                r = requests.post(_url, json=_payload, headers={'Content-Type': 'application/json'}, timeout=60)
                _ms = int((time.time() - _t) * 1000)
                print(
                    f"[FLOW:POSTPROCESS] conversation-ready uid={_uid} conv={_conv_short} segments={_segments} status={r.status_code} latency={_ms}ms",
                    flush=True,
                )
            except Exception as _e:
                print(
                    f"[FLOW:POSTPROCESS] ERROR conversation-ready uid={_uid} conv={_conv_short} error={_e}", flush=True
                )

        threading.Thread(
            target=_fire_ready,
            args=(CONVERSATION_READY_WEBHOOK_URL, ready_payload, uid, conv_short, segment_count),
            daemon=True,
        ).start()
        print(
            f"[FLOW:POSTPROCESS] conversation-ready fired async uid={uid} conv={conv_short} segments={segment_count}",
            flush=True,
        )

    except requests.Timeout:
        _elapsed = int((time.time() - _start) * 1000)
        print(
            f"[FLOW:POSTPROCESS] TIMEOUT uid={uid} conv={conv_short} timeout={POSTPROCESS_TIMEOUT}s latency={_elapsed}ms",
            flush=True,
        )
    except requests.RequestException as e:
        _elapsed = int((time.time() - _start) * 1000)
        print(f"[FLOW:POSTPROCESS] ERROR uid={uid} conv={conv_short} error={e} latency={_elapsed}ms", flush=True)
    except Exception as e:
        print(f"[FLOW:POSTPROCESS] UNEXPECTED uid={uid} conv={conv_short} error={e}", flush=True)
