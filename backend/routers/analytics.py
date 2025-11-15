"""
Analytics API Endpoints

Provides easy access to transcript data for analysis and comparison.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

import database.conversations as conversations_db
from utils.other import endpoints as auth

router = APIRouter()


class TranscriptSegmentResponse(BaseModel):
    text: str
    speaker: str
    start: float
    end: float
    source: Optional[str] = None
    asr_provider: Optional[str] = None


class ConversationResponse(BaseModel):
    id: str
    uid: str
    created_at: datetime
    transcript: str
    language: str
    source: Optional[str] = None
    asr_provider: Optional[str] = None
    segments: List[TranscriptSegmentResponse]


@router.get("/v1/analytics/transcripts", response_model=List[ConversationResponse])
def get_recent_transcripts(
    uid: str = Depends(auth.get_current_user_uid),
    limit: int = Query(default=10, le=100, description="Number of conversations to return (max 100)"),
    hours: int = Query(default=24, le=168, description="Look back this many hours (max 168 = 1 week)"),
    source: Optional[str] = Query(default=None, description="Filter by source: edge_asr, deepgram, etc."),
    asr_provider: Optional[str] = Query(
        default=None, description="Filter by ASR provider: apple_speech, parakeet, etc."
    ),
):
    """
    Get recent transcripts for analysis.

    Query parameters:
    - limit: Number of conversations (max 100, default 10)
    - hours: Look back window (max 168 = 1 week, default 24)
    - source: Filter by source (edge_asr, deepgram, etc.)
    - asr_provider: Filter by ASR provider (apple_speech, parakeet, etc.)

    Examples:
    - Get last 10 edge ASR conversations:
      GET /v1/analytics/transcripts?source=edge_asr&limit=10

    - Get last 20 Parakeet transcripts from last 48 hours:
      GET /v1/analytics/transcripts?asr_provider=parakeet&hours=48&limit=20

    - Get all edge ASR from last week:
      GET /v1/analytics/transcripts?source=edge_asr&hours=168&limit=100
    """

    # Calculate time window
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Get conversations from database
    conversations = conversations_db.get_conversations(uid, limit=limit, start_date=since)

    results = []
    for conv in conversations:
        conv_dict = conv.to_dict() if hasattr(conv, "to_dict") else conv

        # Extract segment data
        segments = conv_dict.get("transcript_segments", [])

        # Apply source filter
        if source:
            # Check if any segment matches source
            if not any(seg.get("source") == source for seg in segments):
                continue

        # Apply asr_provider filter
        if asr_provider:
            # Check if any segment matches asr_provider
            if not any(seg.get("asr_provider") == asr_provider for seg in segments):
                continue

        # Build response
        segment_responses = [
            TranscriptSegmentResponse(
                text=seg.get("text", ""),
                speaker=seg.get("speaker", "SPEAKER_00"),
                start=seg.get("start", 0),
                end=seg.get("end", 0),
                source=seg.get("source"),
                asr_provider=seg.get("asr_provider"),
            )
            for seg in segments
        ]

        # Determine dominant source and provider for the conversation
        sources = [seg.get("source") for seg in segments if seg.get("source")]
        providers = [seg.get("asr_provider") for seg in segments if seg.get("asr_provider")]

        conversation_source = sources[0] if sources else None
        conversation_provider = providers[0] if providers else None

        results.append(
            ConversationResponse(
                id=conv.id,
                uid=conv_dict.get("uid", uid),
                created_at=conv_dict.get("created_at"),
                transcript=conv_dict.get("transcript", ""),
                language=conv_dict.get("language", "en"),
                source=conversation_source,
                asr_provider=conversation_provider,
                segments=segment_responses,
            )
        )

    return results


@router.get("/v1/analytics/transcripts/compare")
def compare_asr_providers(
    uid: str = Depends(auth.get_current_user_uid),
    hours: int = Query(default=24, le=168),
):
    """
    Compare ASR provider statistics.

    Returns counts and metrics for each ASR provider.

    Example:
    GET /v1/analytics/transcripts/compare?hours=48
    """

    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Get all conversations
    conversations = conversations_db.get_conversations(uid, limit=1000, start_date=since)

    # Count by provider
    provider_counts = {}
    total_segments = 0

    for conv in conversations:
        conv_dict = conv.to_dict() if hasattr(conv, "to_dict") else conv
        segments = conv_dict.get("transcript_segments", [])

        for seg in segments:
            total_segments += 1
            provider = seg.get("asr_provider", "unknown")
            source = seg.get("source", "unknown")

            key = f"{source}"
            if provider:
                key = f"{source} ({provider})"

            provider_counts[key] = provider_counts.get(key, 0) + 1

    return {
        "time_window_hours": hours,
        "total_segments": total_segments,
        "provider_breakdown": provider_counts,
        "total_conversations": len(conversations),
    }


@router.get("/v1/analytics/transcripts/export")
def export_transcripts_csv(
    uid: str = Depends(auth.get_current_user_uid),
    hours: int = Query(default=24, le=168),
    source: Optional[str] = Query(default=None),
    asr_provider: Optional[str] = Query(default=None),
):
    """
    Export transcripts as CSV for analysis.

    Returns CSV with columns:
    conversation_id, created_at, text, speaker, source, asr_provider, start, end

    Example:
    GET /v1/analytics/transcripts/export?source=edge_asr&asr_provider=parakeet
    """

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    conversations = conversations_db.get_conversations(uid, limit=1000, start_date=since)

    # Build CSV
    csv_lines = ["conversation_id,created_at,text,speaker,source,asr_provider,start,end"]

    for conv in conversations:
        conv_dict = conv.to_dict() if hasattr(conv, "to_dict") else conv
        conv_id = conv.id
        created_at = conv_dict.get("created_at")
        segments = conv_dict.get("transcript_segments", [])

        for seg in segments:
            seg_source = seg.get("source")
            seg_provider = seg.get("asr_provider")

            # Apply filters
            if source and seg_source != source:
                continue
            if asr_provider and seg_provider != asr_provider:
                continue

            text = seg.get("text", "").replace('"', '""')  # Escape quotes
            speaker = seg.get("speaker", "SPEAKER_00")
            start = seg.get("start", 0)
            end = seg.get("end", 0)

            csv_lines.append(
                f'{conv_id},{created_at},"{text}",{speaker},{seg_source or ""},'
                f'{seg_provider or ""},{start},{end}'
            )

    csv_content = "\n".join(csv_lines)

    return {"csv": csv_content, "total_segments": len(csv_lines) - 1}
