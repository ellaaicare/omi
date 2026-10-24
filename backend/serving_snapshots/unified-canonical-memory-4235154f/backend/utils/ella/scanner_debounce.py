# Ella Scanner Debounce
#
# Accumulates transcript segments per conversation and flushes to scanner
# when silence is detected (no new segments for SILENCE_THRESHOLD seconds).
# This prevents the scanner from processing partial sentences.
#
# Only active for CYBORG and CHATBOT guardian modes.
# Other modes (ACTIVE_SUPPORT, MAXIMUM_AWARENESS, etc.) bypass debounce
# and send segments immediately for lowest-latency safety monitoring.

import time
import threading
from typing import List, Optional, Dict, Tuple


# Configuration
SILENCE_THRESHOLD_S = 2.0    # Seconds of silence before flushing (no new segments)
MAX_BUFFER_DURATION_S = 30.0 # Max time to buffer before forced flush
MAX_SEGMENTS = 20            # Max segments before forced flush
DEBOUNCE_MODES = {'CYBORG', 'CHATBOT'}  # Modes that use debounce

# In-memory buffer: key = (uid, conversation_id), value = BufferEntry
_buffers: Dict[Tuple[str, str], dict] = {}
_lock = threading.Lock()
_timers: Dict[Tuple[str, str], threading.Timer] = {}


def _flush_buffer(key: Tuple[str, str], send_fn) -> None:
    """Flush accumulated segments to scanner."""
    with _lock:
        entry = _buffers.pop(key, None)
        timer = _timers.pop(key, None)
        if timer:
            timer.cancel()

    if entry and entry['segments']:
        uid, conversation_id = key
        combined_text = ' '.join(s.get('text', '') for s in entry['segments'])
        print(f"📡 Debounce flush: {len(entry['segments'])} segments, "
              f"{len(combined_text)} chars, "
              f"{time.time() - entry['first_time']:.1f}s buffered → scanner",
              flush=True)
        send_fn(
            uid=uid,
            conversation_id=conversation_id,
            segments=entry['segments'],
        )


def add_segments(
    uid: str,
    conversation_id: str,
    segments: List[dict],
    guardian_mode: str,
    send_fn,
) -> bool:
    """
    Add segments to the debounce buffer.

    For CYBORG/CHATBOT modes: buffers segments and flushes on silence detection.
    For other modes: returns False (caller should send directly for lowest latency).

    Args:
        uid: User ID
        conversation_id: Current conversation ID
        segments: List of segment dicts (from TranscriptSegment.dict())
        guardian_mode: User's guardian mode (e.g., 'CYBORG', 'ACTIVE_SUPPORT')
        send_fn: Callable that sends segments to scanner (the original send_to_scanner)

    Returns:
        True if segments were buffered (debounce active), False if caller should send directly
    """
    if guardian_mode not in DEBOUNCE_MODES:
        return False  # Caller sends directly — no debounce for safety modes

    if not segments:
        return True

    key = (uid, conversation_id)
    now = time.time()

    with _lock:
        if key not in _buffers:
            _buffers[key] = {
                'segments': [],
                'first_time': now,
                'last_time': now,
            }

        entry = _buffers[key]
        entry['segments'].extend(segments)
        entry['last_time'] = now

        # Cancel existing silence timer
        old_timer = _timers.pop(key, None)
        if old_timer:
            old_timer.cancel()

        # Check forced flush conditions
        buffer_duration = now - entry['first_time']
        should_force = (
            buffer_duration >= MAX_BUFFER_DURATION_S or
            len(entry['segments']) >= MAX_SEGMENTS
        )

    if should_force:
        _flush_buffer(key, send_fn)
    else:
        # Set new silence timer — flushes after SILENCE_THRESHOLD_S of no new segments
        timer = threading.Timer(SILENCE_THRESHOLD_S, _flush_buffer, args=(key, send_fn))
        timer.daemon = True
        timer.start()
        with _lock:
            _timers[key] = timer

    return True


def flush_conversation(uid: str, conversation_id: str, send_fn) -> None:
    """Force flush any buffered segments for a conversation (e.g., on disconnect)."""
    key = (uid, conversation_id)
    _flush_buffer(key, send_fn)


def clear_conversation(uid: str, conversation_id: str) -> None:
    """Discard buffer for a conversation without sending."""
    key = (uid, conversation_id)
    with _lock:
        _buffers.pop(key, None)
        timer = _timers.pop(key, None)
        if timer:
            timer.cancel()
