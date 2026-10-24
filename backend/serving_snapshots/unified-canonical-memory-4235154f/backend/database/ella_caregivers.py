"""
Ella Caregivers - Firestore CRUD

Stores caregivers under users/{uid}/ella_caregivers/{caregiver_id}.
"""

import secrets
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
    data['invite_code'] = ''.join(secrets.choice(string.digits) for _ in range(6))
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


def set_emergency_caregiver(uid: str, caregiver_id: str) -> Optional[str]:
    """Select at most one caregiver as the user's emergency contact."""
    caregivers = list(_caregivers_ref(uid).stream())
    if caregiver_id and not any(document.id == caregiver_id for document in caregivers):
        return None
    batch = db.batch()
    for document in caregivers:
        batch.update(document.reference, {'is_emergency_contact': document.id == caregiver_id})
    batch.commit()
    return caregiver_id


def get_emergency_caregiver_id(uid: str) -> Optional[str]:
    for document in _caregivers_ref(uid).stream():
        if document.to_dict().get('is_emergency_contact') is True:
            return document.id
    return None


def refresh_caregiver_invite(uid: str, caregiver_id: str) -> Optional[dict]:
    ref = _caregivers_ref(uid).document(caregiver_id)
    document = ref.get()
    if not document.exists:
        return None
    now = datetime.now(timezone.utc)
    ref.update(
        {
            'invite_code': ''.join(secrets.choice(string.digits) for _ in range(6)),
            'invite_expires_at': (now + timedelta(days=7)).isoformat(),
            'invited_at': now.isoformat(),
            'status': 'invited',
            'updated_at': now.isoformat(),
        }
    )
    return ref.get().to_dict()
