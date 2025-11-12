import hashlib
import json
import os
import uuid

from google.cloud import firestore
import firebase_admin
from firebase_admin import credentials

# Initialize Firestore client with in-memory credentials (no file write)
if os.environ.get('SERVICE_ACCOUNT_JSON'):
    service_account_info = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
    # Use credentials directly in memory - DO NOT write to disk
    cred = credentials.Certificate(service_account_info)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.Client()
else:
    # Fallback to Application Default Credentials
    db = firestore.Client()


def get_users_uid():
    users_ref = db.collection('users')
    return [str(doc.id) for doc in users_ref.stream()]


def document_id_from_seed(seed: str) -> uuid.UUID:
    """Avoid repeating the same data"""
    seed_hash = hashlib.sha256(seed.encode('utf-8')).digest()
    generated_uuid = uuid.UUID(bytes=seed_hash[:16], version=4)
    return str(generated_uuid)
