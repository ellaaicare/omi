#!/usr/bin/env python3
"""
List all Firestore collections and their document counts.

This helps debug what's actually stored in Firestore.
"""
import os
from dotenv import load_dotenv
from google.cloud import firestore

load_dotenv()

if not os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.join(
        os.path.dirname(__file__),
        'google-credentials.json'
    )

print("\n" + "="*70)
print("📊 FIRESTORE DATABASE CONTENTS")
print("="*70 + "\n")

try:
    db = firestore.Client()
    print(f"✅ Connected to project: {os.getenv('FIREBASE_PROJECT_ID', 'unknown')}\n")

    # Get all collections
    collections = ['conversations', 'users', 'memories', 'transcripts', 'sessions']

    for collection_name in collections:
        print(f"📁 Collection: {collection_name}")

        try:
            docs = list(db.collection(collection_name).limit(10).stream())

            if docs:
                print(f"   ✅ {len(docs)} document(s) found (showing up to 10)")

                for doc in docs:
                    data = doc.to_dict()
                    print(f"\n   🆔 Document ID: {doc.id}")
                    print(f"      Fields: {', '.join(data.keys())}")

                    # Show UID if available
                    if 'uid' in data:
                        print(f"      UID: {data['uid']}")

                    # Show created_at if available
                    if 'created_at' in data:
                        print(f"      Created: {data['created_at']}")

                    # Show transcript preview if available
                    if 'transcript' in data:
                        transcript = str(data['transcript'])
                        preview = transcript[:100] + "..." if len(transcript) > 100 else transcript
                        print(f"      Transcript: {preview}")

            else:
                print(f"   ℹ️  No documents found")

        except Exception as e:
            print(f"   ❌ Error accessing collection: {e}")

        print()

    print("="*70)
    print("✅ Database scan complete")
    print("="*70 + "\n")

except Exception as e:
    print(f"\n❌ Error connecting to Firestore: {e}\n")
    import traceback
    traceback.print_exc()
