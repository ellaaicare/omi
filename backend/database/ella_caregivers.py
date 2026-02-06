"""
Ella Caregivers - Firestore CRUD

Stores caregivers under users/{uid}/ella_caregivers/{caregiver_id}.
"""

import random
import string
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from ._client import db


def _caregivers_ref(uid: str):
    return db.collection('users').document(uid).collection('ella_caregivers')


def create_caregiver(uid: str, data: dict) -> dict:
    caregiver_id = data.get('id') or str(uuid.uuid4())
    data['id'] = caregiver_id
    data['uid'] = uid
    now = datetime.now(timezone.utc).isoformat()
    data['invited_at'] = now
    data['joined_at'] = None
    data['status'] = 'invited'
    data['invite_code'] = ''.join(random.choices(string.digits, k=6))
    data['invite_expires_at'] = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    _caregivers_ref(uid).document(caregiver_id).set(data)
    return data


def get_caregiver(uid: str, caregiver_id: str) -> Optional[dict]:
    doc = _caregivers_ref(uid).document(caregiver_id).get()
    if doc.exists:
        return doc.to_dict()
    return None


def get_caregivers(uid: str) -> list[dict]:
    docs = _caregivers_ref(uid).stream()
    return [doc.to_dict() for doc in docs]


def update_caregiver(uid: str, caregiver_id: str, data: dict) -> Optional[dict]:
    ref = _caregivers_ref(uid).document(caregiver_id)
    doc = ref.get()
    if not doc.exists:
        return None
    data['updated_at'] = datetime.now(timezone.utc).isoformat()
    data.pop('id', None)
    data.pop('uid', None)
    data.pop('invited_at', None)
    ref.update(data)
    return ref.get().to_dict()


def delete_caregiver(uid: str, caregiver_id: str) -> bool:
    ref = _caregivers_ref(uid).document(caregiver_id)
    doc = ref.get()
    if not doc.exists:
        return False
    ref.delete()
    return True
