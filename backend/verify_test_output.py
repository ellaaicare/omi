#!/usr/bin/env python3
"""
Verify Test Output - Check Firestore for Conversations/Memories

This script queries Firestore to show recent conversations and transcripts
created by the test user (UID 123).

Usage:
    python verify_test_output.py
    python verify_test_output.py --uid your-custom-uid
    python verify_test_output.py --limit 10
"""
import os
import sys
import gzip
import base64
import json
from datetime import datetime
from dotenv import load_dotenv
from google.cloud import firestore

# Load environment variables
load_dotenv()

# Ensure Google credentials are set
if not os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.join(
        os.path.dirname(__file__),
        'google-credentials.json'
    )


def format_timestamp(timestamp):
    """Format Firestore timestamp to human-readable string."""
    if timestamp:
        return timestamp.strftime('%Y-%m-%d %H:%M:%S')
    return 'N/A'


def decompress_segments(data):
    """
    Decompress transcript segments stored as compressed blob.

    Args:
        data: Firestore data dictionary

    Returns:
        List of transcript segments, or empty list if not available
    """
    # Check if segments are compressed
    if data.get('transcript_segments_compressed'):
        try:
            # Get the compressed byte string
            compressed_data = data.get('_byteString')
            if not compressed_data:
                return []

            # Base64 decode and gzip decompress
            decoded = base64.b64decode(compressed_data)
            decompressed = gzip.decompress(decoded)
            segments = json.loads(decompressed.decode('utf-8'))

            return segments
        except Exception as e:
            print(f"   ⚠️  Error decompressing segments: {e}")
            return []
    else:
        # Segments stored uncompressed
        return data.get('transcript_segments', [])


def verify_conversations(uid='123', limit=5):
    """
    Query Firestore for recent conversations.

    Args:
        uid: User ID to query (default: '123' for test user)
        limit: Maximum number of conversations to display
    """
    print("\n" + "="*70)
    print("🔍 FIRESTORE VERIFICATION - Conversations & Transcripts")
    print("="*70 + "\n")

    try:
        # Initialize Firestore client
        db = firestore.Client()
        print(f"✅ Connected to Firestore (Project: {os.getenv('FIREBASE_PROJECT_ID', 'unknown')})")
        print(f"📊 Querying conversations for UID: {uid}")
        print(f"📈 Limit: {limit} most recent\n")

        # Query conversations for the user
        # Try with ordering first (requires composite index)
        conversations_ref = db.collection('conversations')

        try:
            query = conversations_ref.where('uid', '==', uid) \
                .order_by('created_at', direction=firestore.Query.DESCENDING) \
                .limit(limit)
            conversations = list(query.stream())
        except Exception as e:
            if 'index' in str(e).lower():
                print(f"\n⚠️  Composite index not created yet - using simpler query")
                print(f"   (Results may not be in chronological order)\n")

                # Fallback: just filter by uid without ordering
                query = conversations_ref.where('uid', '==', uid).limit(limit)
                conversations = list(query.stream())

                # Sort manually by created_at if available
                conversations.sort(
                    key=lambda c: c.to_dict().get('created_at') or datetime.min,
                    reverse=True
                )
            else:
                raise

        if not conversations:
            print("❌ No conversations found for this user.")
            print("\n💡 Possible reasons:")
            print("   1. Test hasn't been run yet")
            print("   2. Conversations are stored under a different UID")
            print("   3. Firestore index not created (check backend logs)")
            print("\n🔧 Try running:")
            print("   python test_omi_device_simulation.py --audio-file test_audio/pyannote_sample.wav")
            return

        print(f"✅ Found {len(conversations)} conversation(s)\n")

        # Display each conversation
        for i, conv in enumerate(conversations, 1):
            conv_id = conv.id
            data = conv.to_dict()

            print(f"{'─'*70}")
            print(f"📝 CONVERSATION #{i}")
            print(f"{'─'*70}")
            print(f"🆔 ID:          {conv_id}")
            print(f"👤 UID:         {data.get('uid', 'N/A')}")
            print(f"📅 Created:     {format_timestamp(data.get('created_at'))}")
            print(f"🔄 Status:      {data.get('status', 'N/A')}")
            print(f"🌐 Language:    {data.get('language', 'N/A')}")

            # Transcript
            transcript = data.get('transcript', '')
            if transcript:
                print(f"\n📄 TRANSCRIPT:")
                print(f"   Length: {len(transcript)} characters")
                # Show first 300 characters
                if len(transcript) > 300:
                    print(f"   Preview: {transcript[:300]}...")
                else:
                    print(f"   Full: {transcript}")
            else:
                print(f"\n📄 TRANSCRIPT: (empty)")

            # Transcript segments (decompress if needed)
            segments = decompress_segments(data)
            if segments:
                print(f"\n🎯 SEGMENTS: {len(segments)} segments")
                if data.get('transcript_segments_compressed'):
                    print(f"   ℹ️  Decompressed from blob storage")

                for j, segment in enumerate(segments[:5], 1):  # Show first 5
                    speaker = segment.get('speaker', 'Unknown')
                    text = segment.get('text', '')
                    start = segment.get('start', 0)
                    end = segment.get('end', 0)
                    print(f"   {j}. [{start:.1f}s - {end:.1f}s] {speaker}: {text[:80]}...")

                if len(segments) > 5:
                    print(f"   ... and {len(segments) - 5} more segments")
            elif data.get('transcript_segments_compressed'):
                print(f"\n🎯 SEGMENTS: Compressed (unable to decompress)")

            # Photos (if available)
            photos = data.get('photos', [])
            if photos:
                print(f"\n📷 PHOTOS: {len(photos)} photo(s)")

            # Additional metadata
            structured = data.get('structured', {})
            if structured:
                print(f"\n📊 STRUCTURED DATA:")
                if 'title' in structured:
                    print(f"   Title: {structured['title']}")
                if 'overview' in structured:
                    print(f"   Overview: {structured['overview'][:100]}...")

            print()  # Blank line between conversations

        print(f"{'='*70}")
        print(f"✅ Verification Complete - Found {len(conversations)} conversation(s)")
        print(f"{'='*70}\n")

        # Summary statistics
        total_chars = sum(len(conv.to_dict().get('transcript', '')) for conv in conversations)
        print(f"📊 SUMMARY:")
        print(f"   Total conversations: {len(conversations)}")
        print(f"   Total transcript characters: {total_chars:,}")
        print(f"   Average per conversation: {total_chars // len(conversations):,} characters")
        print()

    except Exception as e:
        print(f"\n❌ Error querying Firestore: {e}")
        print("\n💡 Troubleshooting:")
        print("   1. Ensure google-credentials.json exists")
        print("   2. Check FIREBASE_PROJECT_ID in .env")
        print("   3. Verify Firestore API is enabled")
        print("   4. Check if composite index is created")
        print("\n🔧 Create test user:")
        print("   python create_test_user.py")
        import traceback
        traceback.print_exc()


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Verify Firestore conversations and transcripts"
    )
    parser.add_argument(
        '--uid',
        type=str,
        default='123',
        help='User ID to query (default: 123 for test user)'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=5,
        help='Maximum number of conversations to display (default: 5)'
    )

    args = parser.parse_args()

    verify_conversations(uid=args.uid, limit=args.limit)


if __name__ == "__main__":
    main()
