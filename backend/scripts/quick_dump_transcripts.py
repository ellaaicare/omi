#!/usr/bin/env python3
"""
Quick Transcript Dump - No Firestore Index Required!

Usage:
    python scripts/quick_dump_transcripts.py <uid> [limit]
    python scripts/quick_dump_transcripts.py 5aGC5YE9BnhcSoTxxtT4ar6ILQy2
    python scripts/quick_dump_transcripts.py 5aGC5YE9BnhcSoTxxtT4ar6ILQy2 10
"""
import sys
import os
import json
import zlib

sys.path.insert(0, '/Users/greg/repos/omi/backend')
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '/Users/greg/repos/omi/backend/google-credentials.json'

from google.cloud import firestore

db = firestore.Client()

# Get UID from command line
if len(sys.argv) < 2:
    print("Usage: python scripts/quick_dump_transcripts.py <uid> [limit]")
    print("Example: python scripts/quick_dump_transcripts.py 5aGC5YE9BnhcSoTxxtT4ar6ILQy2 5")
    sys.exit(1)

uid = sys.argv[1]
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5

print(f"Checking conversations for active user: {uid}")
print("=" * 80)

conversations_ref = db.collection('users').document(uid).collection('conversations')
conversations_ref = conversations_ref.order_by('created_at', direction=firestore.Query.DESCENDING).limit(limit)

conversations = list(conversations_ref.stream())

print(f"Found {len(conversations)} conversations")
print()

for i, conv in enumerate(conversations, 1):
    data = conv.to_dict()

    print(f"{'=' * 80}")
    print(f"CONVERSATION {i}")
    print(f"{'=' * 80}")
    print(f"ID: {conv.id}")
    print(f"Created: {data.get('created_at')}")
    print(f"Status: {data.get('status')}")
    print(f"Source: {data.get('source', 'N/A')}")

    # Get segments
    segments_raw = data.get('transcript_segments')
    compressed = data.get('transcript_segments_compressed', False)

    if segments_raw and compressed:
        try:
            decompressed = zlib.decompress(segments_raw)
            segments = json.loads(decompressed.decode('utf-8'))

            print(f"Segments: {len(segments)}")

            if segments:
                # Check sources
                sources = [s.get('source') for s in segments if isinstance(s, dict) and s.get('source')]
                providers = [s.get('asr_provider') for s in segments if isinstance(s, dict) and s.get('asr_provider')]

                if sources:
                    print(f"Segment sources: {set(sources)}")
                if providers:
                    print(f"ASR providers: {set(providers)}")

                # Show first segment
                if isinstance(segments[0], dict):
                    first = segments[0]
                    print(f"\nFirst segment:")
                    print(f"  Text: {first.get('text', '')[:80]}")
                    print(f"  Speaker: {first.get('speaker', 'N/A')}")
                    print(f"  Source: {first.get('source', 'N/A')}")
                    print(f"  ASR Provider: {first.get('asr_provider', 'N/A')}")
        except Exception as e:
            print(f"Error: {e}")
    else:
        print(f"No segments or not compressed")

    print()
