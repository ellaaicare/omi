"""
Ella Compatibility Layer
========================

Handles data model differences between old Ella data and vanilla OMI models.

This module provides functions to patch/normalize conversation data before
it hits Pydantic validation, ensuring old Ella conversations work with
the new vanilla OMI models.

Usage:
    from ella.compat import patch_conversation_data

    # In database layer or before Pydantic validation:
    raw_data = get_from_firestore(...)
    patched_data = patch_conversation_data(raw_data)
    conversation = Conversation(**patched_data)

This is designed to be:
1. Modular - lives in ella/ so upstream pulls don't conflict
2. Non-invasive - patches data, doesn't modify upstream models
3. Optional - can be disabled via ELLA_COMPAT_ENABLED=false
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Configuration - disabled by default (only enable if you have old Ella data to migrate)
ELLA_COMPAT_ENABLED = os.getenv("ELLA_COMPAT_ENABLED", "false").lower() == "true"


def patch_conversation_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Patch conversation data to be compatible with vanilla OMI Conversation model.

    Fixes:
    - Missing 'started_at' field (uses created_at as fallback)
    - Missing 'structured' field (creates default Structured)
    - Handles old Ella voice conversation format

    Args:
        data: Raw conversation dict from Firestore

    Returns:
        Patched dict compatible with Conversation model
    """
    if not ELLA_COMPAT_ENABLED:
        return data

    if not isinstance(data, dict):
        return data

    # Make a copy to avoid mutating original
    patched = data.copy()

    # Fix missing 'started_at' - use created_at as fallback
    if 'started_at' not in patched or patched.get('started_at') is None:
        patched['started_at'] = patched.get('created_at') or datetime.now(timezone.utc)

    # Fix missing 'structured' - create default
    if 'structured' not in patched or patched.get('structured') is None:
        patched['structured'] = _create_default_structured(patched)

    # Ensure structured has all required fields
    elif isinstance(patched.get('structured'), dict):
        patched['structured'] = _patch_structured(patched['structured'])

    # Fix transcript_segments - either missing or in old format
    if 'transcript_segments' not in patched:
        patched['transcript_segments'] = []
    else:
        # Patch existing transcript_segments to new format
        patched['transcript_segments'] = _patch_transcript_segments(patched.get('transcript_segments', []))

    return patched


def patch_conversation_list(conversations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Patch a list of conversations for compatibility.

    Args:
        conversations: List of raw conversation dicts

    Returns:
        List of patched conversation dicts
    """
    if not ELLA_COMPAT_ENABLED:
        return conversations

    return [patch_conversation_data(c) for c in conversations]


def _create_default_structured(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a default Structured object from conversation data."""
    # Try to extract title from various sources
    title = (
        data.get('title') or
        data.get('summary', {}).get('title') if isinstance(data.get('summary'), dict) else None or
        _generate_title_from_transcript(data) or
        'Untitled Conversation'
    )

    # Try to extract overview
    overview = (
        data.get('overview') or
        data.get('summary', {}).get('overview') if isinstance(data.get('summary'), dict) else None or
        data.get('transcript', '')[:200] if data.get('transcript') else '' or
        ''
    )

    # Try to extract category
    category = (
        data.get('category') or
        data.get('summary', {}).get('category') if isinstance(data.get('summary'), dict) else None or
        'other'
    )

    # Try to extract emoji
    emoji = (
        data.get('emoji') or
        data.get('summary', {}).get('emoji') if isinstance(data.get('summary'), dict) else None or
        '🧠'
    )

    return {
        'title': title,
        'overview': overview,
        'emoji': emoji,
        'category': category,
        'action_items': data.get('action_items', []),
        'events': data.get('events', []),
    }


def _patch_structured(structured: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure structured dict has all required fields with defaults."""
    defaults = {
        'title': 'Untitled Conversation',
        'overview': '',
        'emoji': '🧠',
        'category': 'other',
        'action_items': [],
        'events': [],
    }

    patched = defaults.copy()
    patched.update({k: v for k, v in structured.items() if v is not None})
    return patched


def _generate_title_from_transcript(data: Dict[str, Any]) -> Optional[str]:
    """Generate a title from transcript if available."""
    transcript = data.get('transcript', '')
    if not transcript:
        return None

    # Take first 50 chars of transcript as title
    title = transcript[:50].strip()
    if len(transcript) > 50:
        title += '...'

    return title or None


def _patch_transcript_segments(segments: List[Any]) -> List[Dict[str, Any]]:
    """
    Patch transcript segments to new format.

    Old Ella format:
        {'text': '...', 'role': 'user', 'timestamp': 123.45}

    New vanilla OMI format:
        {'text': '...', 'is_user': True, 'start': 123.45, 'end': 124.45, 'speaker': 'SPEAKER_00'}
    """
    if not segments:
        return []

    patched = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue

        # Check if already in new format
        if 'is_user' in seg and 'start' in seg and 'end' in seg:
            patched.append(seg)
            continue

        # Convert old format to new format
        new_seg = {
            'text': seg.get('text', ''),
            'speaker': seg.get('speaker', 'SPEAKER_00'),
            'speaker_id': seg.get('speaker_id', 0),
            # Convert 'role' to 'is_user'
            'is_user': seg.get('role') == 'user' if 'role' in seg else seg.get('is_user', False),
            # Convert 'timestamp' to 'start'/'end'
            'start': seg.get('start', seg.get('timestamp', 0.0)),
            'end': seg.get('end', seg.get('timestamp', 0.0) + 1.0),
        }

        # Copy any other fields that might be useful
        for key in ['person_id', 'cache_key']:
            if key in seg:
                new_seg[key] = seg[key]

        patched.append(new_seg)

    return patched


# Export for easy access
__all__ = [
    'ELLA_COMPAT_ENABLED',
    'patch_conversation_data',
    'patch_conversation_list',
]
