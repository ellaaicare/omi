"""
Ella Emergency Contacts - Firestore CRUD

Stores emergency contacts under users/{uid}/ella_contacts/{contact_id}.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from ._client import db


def _contacts_ref(uid: str):
    return db.collection('users').document(uid).collection('ella_contacts')


def create_contact(uid: str, data: dict) -> dict:
    contact_id = data.get('id') or str(uuid.uuid4())
    data['id'] = contact_id
    data['uid'] = uid
    data['created_at'] = datetime.now(timezone.utc).isoformat()
    data['updated_at'] = data['created_at']
    _contacts_ref(uid).document(contact_id).set(data)
    return data


def get_contact(uid: str, contact_id: str) -> Optional[dict]:
    doc = _contacts_ref(uid).document(contact_id).get()
    if doc.exists:
        return doc.to_dict()
    return None


def get_contacts(uid: str) -> list[dict]:
    docs = _contacts_ref(uid).stream()
    return [doc.to_dict() for doc in docs]


def update_contact(uid: str, contact_id: str, data: dict) -> Optional[dict]:
    ref = _contacts_ref(uid).document(contact_id)
    doc = ref.get()
    if not doc.exists:
        return None
    data['updated_at'] = datetime.now(timezone.utc).isoformat()
    data.pop('id', None)
    data.pop('uid', None)
    data.pop('created_at', None)
    ref.update(data)
    return ref.get().to_dict()


def delete_contact(uid: str, contact_id: str) -> bool:
    ref = _contacts_ref(uid).document(contact_id)
    doc = ref.get()
    if not doc.exists:
        return False
    ref.delete()
    return True
