#!/usr/bin/env python3
"""
Firestore UID Migration Script — Issue #633
Migrates all user data from old Firebase UID to new Firebase UID.

Context: Build 723 caused Firebase Auth session loss. User re-signed-in with same
Google account but Firebase created a new UID, orphaning all Firestore data under
the old UID.

Usage (on VPS where SERVICE_ACCOUNT_JSON is set):
  python3 migrate_firestore_uid.py

Or with explicit credentials:
  GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json python3 migrate_firestore_uid.py

Safety:
  - Dry-run by default (set DRY_RUN=false to actually write)
  - Verbose logging of every operation
  - Skips if destination already has data
"""

import os
import sys
import time
from datetime import datetime, timezone

# ── Configuration ──────────────────────────────────────────────────────────────

OLD_UID = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"
NEW_UID = "9JG9j251ugNYEWiOm7Nmqjfs5Av2"

# Firestore subcollections under users/{uid}/ to migrate
SUBCOLLECTIONS = [
    "conversations",
    "memories",
    "people",            # knowledge graph nodes
    "messages",
    "action_items",
    "meetings",
    "ella_caregivers",
    "ella_contacts",
    "fcm_tokens",
    "dismissed_announcements",
    "hourly_usage",
    "files",
]

# Top-level user document fields to copy (if new UID doc is empty)
USER_DOC_FIELDS = [
    "email", "name", "given_name", "family_name", "timezone",
    "onboarding_completed", "acquisition_source", "created_at",
    "primary_language", "features", "settings",
]

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
BATCH_SIZE = 500  # Firestore write batch limit

# ── Migration Logic ────────────────────────────────────────────────────────────

def migrate_user_doc(db, dry_run):
    """Copy top-level user document fields from old UID to new UID."""
    old_ref = db.collection("users").document(OLD_UID)
    new_ref = db.collection("users").document(NEW_UID)

    old_doc = old_ref.get()
    if not old_doc.exists:
        print(f"  [SKIP] Old UID user doc does not exist")
        return 0

    new_doc = new_ref.get()
    old_data = old_doc.to_dict()

    # Build merge data — only copy fields that exist in old but are missing/empty in new
    new_data = new_doc.to_dict() if new_doc.exists else {}
    merge_data = {}
    for field in USER_DOC_FIELDS:
        if field in old_data and old_data[field] is not None:
            if field not in new_data or new_data.get(field) is None or new_data.get(field) == "":
                merge_data[field] = old_data[field]

    if not merge_data:
        print(f"  [SKIP] No fields to merge in user doc")
        return 0

    print(f"  [MERGE] User doc fields: {list(merge_data.keys())}")
    if not dry_run:
        new_ref.set(merge_data, merge=True)
    return 1


def migrate_subcollection(db, collection_name, dry_run):
    """Copy all documents from old UID subcollection to new UID subcollection."""
    old_col = db.collection("users").document(OLD_UID).collection(collection_name)
    new_col = db.collection("users").document(NEW_UID).collection(collection_name)

    # Check if destination already has data
    new_count = sum(1 for _ in new_col.limit(1).stream())
    if new_count > 0:
        print(f"  [SKIP] {collection_name}: destination already has data")
        return 0

    # Stream all docs from source
    docs = list(old_col.stream())
    if not docs:
        print(f"  [SKIP] {collection_name}: source is empty")
        return 0

    print(f"  [COPY] {collection_name}: {len(docs)} documents")
    if dry_run:
        print(f"         (dry run — not writing)")
        return len(docs)

    # Write in batches of BATCH_SIZE
    total_written = 0
    batch = db.batch()
    batch_count = 0

    for doc in docs:
        data = doc.to_dict()
        new_ref = new_col.document(doc.id)
        batch.set(new_ref, data)
        batch_count += 1
        total_written += 1

        if batch_count >= BATCH_SIZE:
            batch.commit()
            print(f"         committed batch of {batch_count} (total: {total_written})")
            batch = db.batch()
            batch_count = 0

    if batch_count > 0:
        batch.commit()
        print(f"         committed final batch of {batch_count} (total: {total_written})")

    return total_written


def main():
    print("=" * 70)
    print(f"Firestore UID Migration — Issue #633")
    print(f"  Old UID: {OLD_UID}")
    print(f"  New UID: {NEW_UID}")
    print(f"  Dry run: {DRY_RUN}")
    print(f"  Time:    {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    try:
        from google.cloud import firestore
    except ImportError:
        print("ERROR: google-cloud-firestore not installed. Run: pip install google-cloud-firestore")
        sys.exit(1)

    db = firestore.Client()

    # Verify both UIDs exist
    old_doc = db.collection("users").document(OLD_UID).get()
    new_doc = db.collection("users").document(NEW_UID).get()
    print(f"\nOld UID exists: {old_doc.exists}")
    print(f"New UID exists: {new_doc.exists}")

    if not old_doc.exists:
        print("ERROR: Old UID document does not exist in Firestore. Nothing to migrate.")
        sys.exit(1)

    # Count docs in old UID subcollections
    print(f"\nSource data inventory:")
    for col_name in SUBCOLLECTIONS:
        count = sum(1 for _ in db.collection("users").document(OLD_UID).collection(col_name).stream())
        if count > 0:
            print(f"  {col_name}: {count} docs")

    # Migrate
    print(f"\n{'DRY RUN — ' if DRY_RUN else ''}Migration starting:")
    total_ops = 0

    total_ops += migrate_user_doc(db, DRY_RUN)

    for col_name in SUBCOLLECTIONS:
        total_ops += migrate_subcollection(db, col_name, DRY_RUN)

    print(f"\n{'DRY RUN — ' if DRY_RUN else ''}Total operations: {total_ops}")

    if DRY_RUN:
        print("\nTo actually migrate, run:")
        print(f"  DRY_RUN=false python3 {sys.argv[0]}")

    print("\nDone.")


if __name__ == "__main__":
    main()
