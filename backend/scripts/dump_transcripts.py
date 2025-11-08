#!/usr/bin/env python3
"""
Dump Recent Transcripts - Easy CLI Tool

Quick script to dump recent transcripts for analysis.
No need to check Firestore console manually!

Usage:
    python scripts/dump_transcripts.py --uid <user-id>
    python scripts/dump_transcripts.py --uid <user-id> --source edge_asr
    python scripts/dump_transcripts.py --uid <user-id> --provider parakeet
    python scripts/dump_transcripts.py --uid <user-id> --hours 48 --limit 20
"""

import argparse
import sys
import os
from datetime import datetime, timedelta, timezone

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import firestore
import database.conversations as conversations_db


def dump_transcripts(
    uid: str,
    limit: int = 10,
    hours: int = 24,
    source: str = None,
    asr_provider: str = None,
    output_format: str = "table",
):
    """Dump recent transcripts for a user"""

    print(f"{'=' * 80}")
    print(f"DUMPING TRANSCRIPTS")
    print(f"{'=' * 80}")
    print(f"User: {uid}")
    print(f"Time window: Last {hours} hours")
    print(f"Limit: {limit} conversations")
    if source:
        print(f"Filter by source: {source}")
    if asr_provider:
        print(f"Filter by ASR provider: {asr_provider}")
    print(f"{'=' * 80}\n")

    # Get conversations
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    conversations = conversations_db.get_conversations(uid, limit=limit, start_date=since)

    if not conversations:
        print("❌ No conversations found in time window")
        return

    # Process conversations
    matching_conversations = []

    for conv in conversations:
        conv_dict = conv.to_dict() if hasattr(conv, "to_dict") else conv
        segments = conv_dict.get("transcript_segments", [])

        # Apply filters
        if source:
            if not any(seg.get("source") == source for seg in segments):
                continue

        if asr_provider:
            if not any(seg.get("asr_provider") == asr_provider for seg in segments):
                continue

        matching_conversations.append((conv, conv_dict, segments))

    if not matching_conversations:
        print(f"❌ No conversations matching filters")
        return

    print(f"✅ Found {len(matching_conversations)} matching conversations\n")

    # Display based on format
    if output_format == "table":
        display_table(matching_conversations)
    elif output_format == "json":
        display_json(matching_conversations)
    elif output_format == "csv":
        display_csv(matching_conversations)
    elif output_format == "detailed":
        display_detailed(matching_conversations)


def display_table(conversations):
    """Display as table format"""

    print(f"{'=' * 120}")
    print(f"{'ID':<25} {'Created':<20} {'Source':<15} {'Provider':<15} {'Segments':<10} {'Text Preview':<30}")
    print(f"{'=' * 120}")

    for conv, conv_dict, segments in conversations:
        conv_id = conv.id[:23]
        created = conv_dict.get("created_at")
        created_str = created.strftime("%Y-%m-%d %H:%M") if created else "N/A"

        # Get dominant source and provider
        sources = [seg.get("source") for seg in segments if seg.get("source")]
        providers = [seg.get("asr_provider") for seg in segments if seg.get("asr_provider")]

        source_str = sources[0] if sources else "N/A"
        provider_str = providers[0] if providers else "N/A"

        transcript = conv_dict.get("transcript", "")
        preview = transcript[:30] + "..." if len(transcript) > 30 else transcript

        print(f"{conv_id:<25} {created_str:<20} {source_str:<15} {provider_str:<15} {len(segments):<10} {preview:<30}")

    print(f"{'=' * 120}\n")


def display_detailed(conversations):
    """Display detailed view"""

    for i, (conv, conv_dict, segments) in enumerate(conversations, 1):
        print(f"\n{'=' * 80}")
        print(f"CONVERSATION {i}/{len(conversations)}")
        print(f"{'=' * 80}")
        print(f"ID: {conv.id}")
        print(f"Created: {conv_dict.get('created_at')}")
        print(f"Language: {conv_dict.get('language', 'N/A')}")
        print(f"Segments: {len(segments)}")
        print(f"\nFull Transcript:")
        print(f"{'-' * 80}")
        print(conv_dict.get("transcript", "(empty)"))
        print(f"{'-' * 80}")

        # Show segment details
        print(f"\nSegment Breakdown:")
        for j, seg in enumerate(segments[:5], 1):  # Show first 5 segments
            source = seg.get("source", "N/A")
            provider = seg.get("asr_provider", "N/A")
            text = seg.get("text", "")[:60]
            print(f"  {j}. [{source}/{provider}] {text}...")

        if len(segments) > 5:
            print(f"  ... and {len(segments) - 5} more segments")


def display_json(conversations):
    """Display as JSON"""
    import json

    output = []
    for conv, conv_dict, segments in conversations:
        output.append(
            {
                "id": conv.id,
                "created_at": str(conv_dict.get("created_at")),
                "transcript": conv_dict.get("transcript"),
                "language": conv_dict.get("language"),
                "segments": [
                    {
                        "text": seg.get("text"),
                        "speaker": seg.get("speaker"),
                        "source": seg.get("source"),
                        "asr_provider": seg.get("asr_provider"),
                        "start": seg.get("start"),
                        "end": seg.get("end"),
                    }
                    for seg in segments
                ],
            }
        )

    print(json.dumps(output, indent=2))


def display_csv(conversations):
    """Display as CSV"""

    print("conversation_id,created_at,source,asr_provider,segment_index,text,speaker,start,end")

    for conv, conv_dict, segments in conversations:
        conv_id = conv.id
        created = conv_dict.get("created_at")

        for i, seg in enumerate(segments):
            text = seg.get("text", "").replace('"', '""')
            source = seg.get("source", "")
            provider = seg.get("asr_provider", "")
            speaker = seg.get("speaker", "")
            start = seg.get("start", 0)
            end = seg.get("end", 0)

            print(f'{conv_id},{created},{source},{provider},{i},"{text}",{speaker},{start},{end}')


def compare_providers(uid: str, hours: int = 24):
    """Compare ASR provider statistics"""

    print(f"{'=' * 80}")
    print(f"ASR PROVIDER COMPARISON")
    print(f"{'=' * 80}")
    print(f"User: {uid}")
    print(f"Time window: Last {hours} hours")
    print(f"{'=' * 80}\n")

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    conversations = conversations_db.get_conversations(uid, limit=1000, start_date=since)

    # Count by provider
    provider_counts = {}
    total_segments = 0

    for conv in conversations:
        conv_dict = conv.to_dict() if hasattr(conv, "to_dict") else conv
        segments = conv_dict.get("transcript_segments", [])

        for seg in segments:
            total_segments += 1
            source = seg.get("source", "unknown")
            provider = seg.get("asr_provider")

            if provider:
                key = f"{source} ({provider})"
            else:
                key = source

            provider_counts[key] = provider_counts.get(key, 0) + 1

    # Display results
    print(f"Total Conversations: {len(conversations)}")
    print(f"Total Segments: {total_segments}")
    print(f"\nProvider Breakdown:")
    print(f"{'-' * 60}")

    for provider, count in sorted(provider_counts.items(), key=lambda x: x[1], reverse=True):
        percentage = (count / total_segments * 100) if total_segments > 0 else 0
        print(f"  {provider:<40} {count:>6} ({percentage:>5.1f}%)")

    print(f"{'-' * 60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Dump recent transcripts for analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic dump (last 10 conversations, 24 hours)
  python scripts/dump_transcripts.py --uid HbBdbnRkPJhpYFIIsd34krM8FKD3

  # Filter by edge ASR only
  python scripts/dump_transcripts.py --uid <uid> --source edge_asr

  # Filter by Parakeet provider
  python scripts/dump_transcripts.py --uid <uid> --provider parakeet

  # Last 48 hours, 20 conversations
  python scripts/dump_transcripts.py --uid <uid> --hours 48 --limit 20

  # Export as CSV
  python scripts/dump_transcripts.py --uid <uid> --format csv > transcripts.csv

  # Detailed view
  python scripts/dump_transcripts.py --uid <uid> --format detailed

  # Compare providers
  python scripts/dump_transcripts.py --uid <uid> --compare
""",
    )

    parser.add_argument("--uid", required=True, help="User ID")
    parser.add_argument("--limit", type=int, default=10, help="Number of conversations (default: 10, max: 100)")
    parser.add_argument("--hours", type=int, default=24, help="Look back hours (default: 24)")
    parser.add_argument("--source", help="Filter by source (edge_asr, deepgram, etc.)")
    parser.add_argument("--provider", help="Filter by ASR provider (apple_speech, parakeet, etc.)")
    parser.add_argument(
        "--format",
        choices=["table", "json", "csv", "detailed"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument("--compare", action="store_true", help="Compare ASR provider statistics")

    args = parser.parse_args()

    # Ensure Google credentials are set
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "google-credentials.json"
        )

    if args.compare:
        compare_providers(args.uid, args.hours)
    else:
        dump_transcripts(
            uid=args.uid,
            limit=min(args.limit, 100),  # Cap at 100
            hours=args.hours,
            source=args.source,
            asr_provider=args.provider,
            output_format=args.format,
        )


if __name__ == "__main__":
    main()
