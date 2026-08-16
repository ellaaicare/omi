import copy
import json
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional, Dict, Any

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter, transactional

import utils.other.hume as hume
from database import users as users_db
from models.conversation import (
    ConversationPhoto,
    PostProcessingStatus,
    PostProcessingModel,
    ConversationStatus,
    AudioFile,
)
from models.transcript_segment import TranscriptSegment
from utils import encryption
from ._client import db
from .helpers import set_data_protection_level, prepare_for_write, prepare_for_read, with_photos
from utils.other.storage import list_audio_chunks

conversations_collection = 'conversations'
conversation_processing_retries_collection = 'processing_retries'
conversation_summary_retryable_errors = {
    'conversation_summary_failed',
    'conversation_summary_recovery_failed',
}
conversation_enriched_summary_kinds = {
    'observer_enriched',
    'corrected_enriched',
    'hermes_enriched',
    'recovered_enriched',
}
conversation_processing_retry_lease_seconds = 900


def _active_summary_version(conversation: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    versions = conversation.get('summary_versions') or []
    active_id = conversation.get('active_summary_version_id')
    if active_id:
        for version in versions:
            if str(version.get('id') or '') == str(active_id):
                return version
    return next((version for version in reversed(versions) if version.get('is_active')), None)


def has_usable_conversation_summary(conversation: Dict[str, Any]) -> bool:
    structured = conversation.get('structured') or {}
    return bool(str(structured.get('title') or '').strip() and str(structured.get('overview') or '').strip())


def has_enriched_conversation_summary(conversation: Dict[str, Any]) -> bool:
    active_version = _active_summary_version(conversation) or {}
    enrichment_state = conversation.get('enrichment_state') or {}
    return bool(
        active_version.get('kind') in conversation_enriched_summary_kinds
        and enrichment_state.get('status') == 'writeback_applied'
        and enrichment_state.get('kind') in conversation_enriched_summary_kinds
    )


def conversation_processing_recovery_mode(conversation: Dict[str, Any]) -> tuple[Optional[str], str]:
    status = getattr(conversation.get('status'), 'value', conversation.get('status'))
    if conversation.get('discarded'):
        return None, 'intentional_discard'
    if status == ConversationStatus.processing.value:
        return None, 'active_processing'
    if status == ConversationStatus.failed.value:
        if conversation.get('processing_error') in conversation_summary_retryable_errors:
            return 'full', 'retryable_summary_failure'
        return None, 'non_retryable_failure'
    if status == ConversationStatus.completed.value:
        if has_enriched_conversation_summary(conversation):
            if conversation.get('processing_retry_enrichment_vector_status') in {'pending', 'failed'}:
                return 'enrichment_only', 'enriched_summary_without_confirmed_vector'
            return None, 'already_enriched'
        if has_usable_conversation_summary(conversation):
            return 'enrichment_only', 'generic_summary_without_enrichment'
        return None, 'completed_without_usable_summary'
    return None, 'invalid_state'


def _processing_retry_lease_expired(conversation: Dict[str, Any], now: datetime) -> bool:
    lease_expires_at = conversation.get('processing_retry_lease_expires_at')
    if not isinstance(lease_expires_at, datetime):
        return True
    return _ensure_timezone_aware(lease_expires_at) <= _ensure_timezone_aware(now)


def _retry_claim_metadata(
    outcome: str,
    *,
    retry_data: Optional[Dict[str, Any]] = None,
    mode: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    retry_data = retry_data or {}
    return {
        'outcome': outcome,
        'mode': mode if mode is not None else retry_data.get('mode'),
        'reason': reason,
        'phase': retry_data.get('phase'),
        'generic_status': retry_data.get('generic_status'),
        'generic_vector_status': retry_data.get('generic_vector_status'),
        'enrichment_status': retry_data.get('enrichment_status'),
        'vector_status': retry_data.get('vector_status'),
        'lease_expires_at': retry_data.get('lease_expires_at'),
        'attempt_count': int(retry_data.get('attempt_count') or 0),
    }


def _ensure_timezone_aware(dt: datetime) -> datetime:
    """
    Ensure a datetime object is timezone-aware.
    If naive, assume UTC timezone.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _category_value(value: Any) -> str:
    if value is None:
        return 'other'
    raw = getattr(value, 'value', value)
    return str(raw or 'other')


def _has_summary_content(structured: Optional[Dict[str, Any]]) -> bool:
    structured = structured or {}
    return any(str(structured.get(field) or '').strip() for field in ('title', 'overview', 'emoji', 'category'))


def _build_summary_version_payload(
    *,
    structured: Dict[str, Any],
    created_at: datetime,
    source: str,
    kind: str,
    is_active: bool,
    correction_id: Optional[str] = None,
    based_on_version_id: Optional[str] = None,
    version_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        'id': version_id or str(uuid.uuid4()),
        'created_at': _ensure_timezone_aware(created_at),
        'source': source,
        'kind': kind,
        'title': structured.get('title') or '',
        'overview': structured.get('overview') or '',
        'emoji': structured.get('emoji') or 'brain',
        'category': _category_value(structured.get('category')),
        'correction_id': correction_id,
        'based_on_version_id': based_on_version_id,
        'is_active': is_active,
    }


def bootstrap_summary_versioning_update(conversation_data: Dict[str, Any]) -> Dict[str, Any]:
    if not conversation_data or conversation_data.get('summary_versions'):
        return {}

    structured = conversation_data.get('structured') or {}
    if not _has_summary_content(structured):
        return {}

    created_at = conversation_data.get('created_at') or datetime.now(timezone.utc)
    version = _build_summary_version_payload(
        structured=structured,
        created_at=_ensure_timezone_aware(created_at),
        source='legacy',
        kind='legacy_current',
        is_active=True,
    )
    if version['emoji'] == 'brain':
        version['emoji'] = '\U0001f9e0'
    return {
        'summary_versions': [version],
        'active_summary_version_id': version['id'],
    }


def build_summary_version_update(
    conversation_data: Dict[str, Any],
    *,
    next_structured: Dict[str, Any],
    source: str = 'observer',
    kind: str = 'observer_enriched',
    correction_id: Optional[str] = None,
    based_on_version_id: Optional[str] = None,
    activate: bool = True,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    bootstrap_update = bootstrap_summary_versioning_update(conversation_data)
    versions = copy.deepcopy(
        conversation_data.get('summary_versions') or bootstrap_update.get('summary_versions') or []
    )
    active_summary_version_id = conversation_data.get('active_summary_version_id') or bootstrap_update.get(
        'active_summary_version_id'
    )

    if activate:
        for version in versions:
            version['is_active'] = False

    base_version_id = based_on_version_id or active_summary_version_id
    new_version = _build_summary_version_payload(
        structured=next_structured,
        created_at=now,
        source=source,
        kind=kind,
        correction_id=correction_id,
        based_on_version_id=base_version_id,
        is_active=activate,
    )
    if new_version['emoji'] == 'brain':
        new_version['emoji'] = '\U0001f9e0'
    versions.append(new_version)

    return {
        'summary_versions': versions,
        'active_summary_version_id': new_version['id'] if activate else active_summary_version_id,
        'new_summary_version_id': new_version['id'],
    }


def _category_value(value: Any) -> str:
    if value is None:
        return 'other'
    raw = getattr(value, 'value', value)
    return str(raw or 'other')


def _has_summary_content(structured: Optional[Dict[str, Any]]) -> bool:
    structured = structured or {}
    return any(str(structured.get(field) or '').strip() for field in ('title', 'overview', 'emoji', 'category'))


def _build_summary_version_payload(
    *,
    structured: Dict[str, Any],
    created_at: datetime,
    source: str,
    kind: str,
    is_active: bool,
    correction_id: Optional[str] = None,
    based_on_version_id: Optional[str] = None,
    version_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        'id': version_id or str(uuid.uuid4()),
        'created_at': _ensure_timezone_aware(created_at),
        'source': source,
        'kind': kind,
        'title': structured.get('title') or '',
        'overview': structured.get('overview') or '',
        'emoji': structured.get('emoji') or '🧠',
        'category': _category_value(structured.get('category')),
        'correction_id': correction_id,
        'based_on_version_id': based_on_version_id,
        'is_active': is_active,
    }


def bootstrap_summary_versioning_update(conversation_data: Dict[str, Any]) -> Dict[str, Any]:
    if not conversation_data or conversation_data.get('summary_versions'):
        return {}

    structured = conversation_data.get('structured') or {}
    if not _has_summary_content(structured):
        return {}

    created_at = conversation_data.get('created_at') or datetime.now(timezone.utc)
    version = _build_summary_version_payload(
        structured=structured,
        created_at=_ensure_timezone_aware(created_at),
        source='legacy',
        kind='legacy_current',
        is_active=True,
    )
    return {
        'summary_versions': [version],
        'active_summary_version_id': version['id'],
    }


def build_summary_version_update(
    conversation_data: Dict[str, Any],
    *,
    next_structured: Dict[str, Any],
    source: str = 'observer',
    kind: str = 'observer_enriched',
    correction_id: Optional[str] = None,
    based_on_version_id: Optional[str] = None,
    activate: bool = True,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    bootstrap_update = bootstrap_summary_versioning_update(conversation_data)
    versions = copy.deepcopy(
        conversation_data.get('summary_versions') or bootstrap_update.get('summary_versions') or []
    )
    active_summary_version_id = conversation_data.get('active_summary_version_id') or bootstrap_update.get(
        'active_summary_version_id'
    )

    if activate:
        for version in versions:
            version['is_active'] = False

    base_version_id = based_on_version_id or active_summary_version_id
    new_version = _build_summary_version_payload(
        structured=next_structured,
        created_at=now,
        source=source,
        kind=kind,
        correction_id=correction_id,
        based_on_version_id=base_version_id,
        is_active=activate,
    )
    versions.append(new_version)

    return {
        'summary_versions': versions,
        'active_summary_version_id': new_version['id'] if activate else active_summary_version_id,
        'new_summary_version_id': new_version['id'],
    }


# *********************************
# ******* ENCRYPTION HELPERS ******
# *********************************


def _decrypt_conversation_data(conversation_data: Dict[str, Any], uid: str) -> Dict[str, Any]:
    data = copy.deepcopy(conversation_data)

    if 'transcript_segments' not in data:
        return data

    if isinstance(data['transcript_segments'], str):
        try:
            decrypted_payload = encryption.decrypt(data['transcript_segments'], uid)
            if data.get('transcript_segments_compressed'):
                compressed_bytes = bytes.fromhex(decrypted_payload)
                decompressed_json = zlib.decompress(compressed_bytes).decode('utf-8')
                data['transcript_segments'] = json.loads(decompressed_json)
            # backward compatibility, will be removed soon
            else:
                data['transcript_segments'] = json.loads(decrypted_payload)
        except (json.JSONDecodeError, TypeError, zlib.error, ValueError) as e:
            print(e, uid)
            data['transcript_segments'] = []
    # backward compatibility, will be removed soon
    elif isinstance(data['transcript_segments'], bytes):
        try:
            compressed_bytes = data['transcript_segments']
            if data.get('transcript_segments_compressed'):
                decompressed_json = zlib.decompress(compressed_bytes).decode('utf-8')
                data['transcript_segments'] = json.loads(decompressed_json)
        except (json.JSONDecodeError, TypeError, zlib.error, ValueError) as e:
            print(e, uid)
            data['transcript_segments'] = []

    return data


def _prepare_conversation_for_write(data: Dict[str, Any], uid: str, level: str) -> Dict[str, Any]:
    data = copy.deepcopy(data)
    if 'transcript_segments' in data and isinstance(data['transcript_segments'], list):
        segments_json = json.dumps(data['transcript_segments'])
        compressed_segments_bytes = zlib.compress(segments_json.encode('utf-8'))
        data['transcript_segments_compressed'] = True

        if level == 'enhanced':
            encrypted_segments = encryption.encrypt(compressed_segments_bytes.hex(), uid)
            data['transcript_segments'] = encrypted_segments
        else:
            data['transcript_segments'] = compressed_segments_bytes
    return data


def _prepare_conversation_for_read(conversation_data: Optional[Dict[str, Any]], uid: str) -> Optional[Dict[str, Any]]:
    if not conversation_data:
        return None

    data = copy.deepcopy(conversation_data)
    level = data.get('data_protection_level')

    if level == 'enhanced':
        return _decrypt_conversation_data(data, uid)

    # Handle standard level with potential compression
    if data.get('transcript_segments_compressed'):
        if 'transcript_segments' in data and isinstance(data['transcript_segments'], bytes):
            try:
                decompressed_json = zlib.decompress(data['transcript_segments']).decode('utf-8')
                data['transcript_segments'] = json.loads(decompressed_json)
            except (json.JSONDecodeError, TypeError, zlib.error) as e:
                print(e)
                pass

    return data


def _prepare_photo_for_write(data: Dict[str, Any], uid: str, level: str) -> Dict[str, Any]:
    data = copy.deepcopy(data)
    data['data_protection_level'] = level
    if level == 'enhanced' and 'base64' in data and isinstance(data['base64'], str):
        data['base64'] = encryption.encrypt(data['base64'], uid)
    return data


def _prepare_photo_for_read(photo_data: Optional[Dict[str, Any]], uid: str) -> Optional[Dict[str, Any]]:
    if not photo_data:
        return None
    data = copy.deepcopy(photo_data)
    level = data.get('data_protection_level')
    if level == 'enhanced' and 'base64' in data and isinstance(data['base64'], str):
        try:
            data['base64'] = encryption.decrypt(data['base64'], uid)
        except Exception:
            # If decryption fails, it might be already decrypted or not encrypted.
            # We can log this, but for now, we'll just pass.
            pass
    return data


@prepare_for_read(decrypt_func=_prepare_photo_for_read)
def get_conversation_photos(uid: str, conversation_id: str):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    photos_ref = conversation_ref.collection('photos')
    photos = [doc.to_dict() for doc in photos_ref.stream()]
    return photos


# *****************************
# ********** CRUD *************
# *****************************


@set_data_protection_level(data_arg_name='conversation_data')
@prepare_for_write(data_arg_name='conversation_data', prepare_func=_prepare_conversation_for_write)
def upsert_conversation(uid: str, conversation_data: dict):
    if 'audio_base64_url' in conversation_data:
        del conversation_data['audio_base64_url']
    if 'photos' in conversation_data:
        del conversation_data['photos']

    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_data['id'])
    conversation_ref.set(conversation_data)


@prepare_for_read(decrypt_func=_prepare_conversation_for_read)
@with_photos(get_conversation_photos)
def get_conversation(uid, conversation_id):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    conversation_data = conversation_ref.get().to_dict()
    return conversation_data


@prepare_for_read(decrypt_func=_prepare_conversation_for_read)
@with_photos(get_conversation_photos)
def get_conversations(
    uid: str,
    limit: int = 100,
    offset: int = 0,
    include_discarded: bool = False,
    statuses: List[str] = [],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    categories: Optional[List[str]] = None,
    folder_id: Optional[str] = None,
    starred: Optional[bool] = None,
):
    conversations_ref = db.collection('users').document(uid).collection(conversations_collection)
    if not include_discarded:
        conversations_ref = conversations_ref.where(filter=FieldFilter('discarded', '==', False))
    if len(statuses) > 0:
        conversations_ref = conversations_ref.where(filter=FieldFilter('status', 'in', statuses))

    if categories:
        conversations_ref = conversations_ref.where(filter=FieldFilter('structured.category', 'in', categories))

    if folder_id:
        conversations_ref = conversations_ref.where(filter=FieldFilter('folder_id', '==', folder_id))

    if starred is not None:
        conversations_ref = conversations_ref.where(filter=FieldFilter('starred', '==', starred))

    # Apply date range filters if provided
    if start_date:
        conversations_ref = conversations_ref.where(filter=FieldFilter('created_at', '>=', start_date))
    if end_date:
        conversations_ref = conversations_ref.where(filter=FieldFilter('created_at', '<=', end_date))

    # Sort
    conversations_ref = conversations_ref.order_by('created_at', direction=firestore.Query.DESCENDING)

    # Limits
    conversations_ref = conversations_ref.limit(limit).offset(offset)

    conversations = [doc.to_dict() for doc in conversations_ref.stream()]
    return conversations


@prepare_for_read(decrypt_func=_prepare_conversation_for_read)
def get_conversations_without_photos(
    uid: str,
    limit: int = 100,
    offset: int = 0,
    include_discarded: bool = False,
    statuses: List[str] = [],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    categories: Optional[List[str]] = None,
):
    """
    Same as get_conversations but without loading photos.
    Much faster for bulk operations like Wrapped where photos aren't needed.
    """
    conversations_ref = db.collection('users').document(uid).collection(conversations_collection)
    if not include_discarded:
        conversations_ref = conversations_ref.where(filter=FieldFilter('discarded', '==', False))
    if len(statuses) > 0:
        conversations_ref = conversations_ref.where(filter=FieldFilter('status', 'in', statuses))

    if categories:
        conversations_ref = conversations_ref.where(filter=FieldFilter('structured.category', 'in', categories))

    # Apply date range filters if provided
    if start_date:
        conversations_ref = conversations_ref.where(filter=FieldFilter('created_at', '>=', start_date))
    if end_date:
        conversations_ref = conversations_ref.where(filter=FieldFilter('created_at', '<=', end_date))

    # Sort
    conversations_ref = conversations_ref.order_by('created_at', direction=firestore.Query.DESCENDING)

    # Limits
    conversations_ref = conversations_ref.limit(limit).offset(offset)

    conversations = [doc.to_dict() for doc in conversations_ref.stream()]
    return conversations


def update_conversation(uid: str, conversation_id: str, update_data: dict):
    doc_ref = db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    doc_snapshot = doc_ref.get()
    if not doc_snapshot.exists:
        return

    doc_level = doc_snapshot.to_dict().get('data_protection_level', 'standard')
    prepared_data = _prepare_conversation_for_write(update_data, uid, doc_level)
    doc_ref.update(prepared_data)


def _update_conversation_if_active_summary_version_transaction(
    transaction,
    conversation_ref,
    uid: str,
    expected_active_summary_version_id: Optional[str],
    update_data: dict,
) -> bool:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    conversation = snapshot.to_dict() or {}
    if str(conversation.get('active_summary_version_id') or '') != str(expected_active_summary_version_id or ''):
        return False
    doc_level = conversation.get('data_protection_level', 'standard')
    prepared_data = _prepare_conversation_for_write(update_data, uid, doc_level)
    transaction.update(conversation_ref, prepared_data)
    return True


@transactional
def _update_conversation_if_active_summary_version(
    transaction,
    conversation_ref,
    uid: str,
    expected_active_summary_version_id: Optional[str],
    update_data: dict,
) -> bool:
    return _update_conversation_if_active_summary_version_transaction(
        transaction,
        conversation_ref,
        uid,
        expected_active_summary_version_id,
        update_data,
    )


def update_conversation_if_active_summary_version(
    uid: str,
    conversation_id: str,
    expected_active_summary_version_id: Optional[str],
    update_data: dict,
) -> bool:
    conversation_ref = (
        db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    )
    return _update_conversation_if_active_summary_version(
        db.transaction(),
        conversation_ref,
        uid,
        expected_active_summary_version_id,
        update_data,
    )


def _update_conversation_with_builder_transaction(transaction, conversation_ref, uid: str, update_builder):
    """Read, derive, and update one owner-bound conversation in one transaction."""
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return None
    stored_conversation = snapshot.to_dict() or {}
    conversation = _prepare_conversation_for_read(stored_conversation, uid) or {}
    update_data, result = update_builder(conversation)
    if update_data:
        doc_level = stored_conversation.get('data_protection_level', 'standard')
        prepared_data = _prepare_conversation_for_write(update_data, uid, doc_level)
        transaction.update(conversation_ref, prepared_data)
    return {
        'conversation': conversation,
        'update_data': update_data,
        'result': result,
    }


@transactional
def _update_conversation_with_builder(transaction, conversation_ref, uid: str, update_builder):
    return _update_conversation_with_builder_transaction(transaction, conversation_ref, uid, update_builder)


def update_conversation_with_builder(uid: str, conversation_id: str, update_builder):
    conversation_ref = (
        db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    )
    return _update_conversation_with_builder(db.transaction(), conversation_ref, uid, update_builder)


def _ensure_voice_memory_summary_version_transaction(
    transaction,
    conversation_ref,
    expected_active_summary_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Establish a durable active summary version before evaluating a voice CAS."""
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return {'status': 'not_found'}

    conversation = snapshot.to_dict() or {}
    versions = conversation.get('summary_versions')
    versions = copy.deepcopy(versions) if isinstance(versions, list) else []
    active_version = _active_summary_version(conversation)
    update_data: Dict[str, Any] = {}

    if not active_version:
        if not versions:
            bootstrap_update = bootstrap_summary_versioning_update(conversation)
            if bootstrap_update:
                versions = bootstrap_update['summary_versions']
                active_version = versions[0]
                update_data.update(bootstrap_update)
        else:
            # Recover a missing active pointer from the newest durable version.
            active_version = next(
                (
                    version
                    for version in reversed(versions)
                    if isinstance(version, dict) and str(version.get('id') or '').strip()
                ),
                None,
            )
            if active_version:
                active_id = str(active_version['id'])
                for version in versions:
                    if isinstance(version, dict):
                        version['is_active'] = str(version.get('id') or '') == active_id
                update_data.update(
                    {
                        'summary_versions': versions,
                        'active_summary_version_id': active_id,
                    }
                )

    active_version_id = str((active_version or {}).get('id') or '').strip()
    if not active_version_id:
        return {'status': 'version_unavailable'}

    if str(conversation.get('active_summary_version_id') or '').strip() != active_version_id:
        update_data['active_summary_version_id'] = active_version_id
    if update_data:
        transaction.update(conversation_ref, update_data)
        conversation.update(update_data)

    expected_version_id = str(expected_active_summary_version_id or '').strip()
    if expected_version_id and expected_version_id != active_version_id:
        return {
            'status': 'stale',
            'active_summary_version_id': active_version_id,
            'conversation': conversation,
        }
    return {
        'status': 'ready',
        'active_summary_version_id': active_version_id,
        'conversation': conversation,
    }


@transactional
def _ensure_voice_memory_summary_version(
    transaction,
    conversation_ref,
    expected_active_summary_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    return _ensure_voice_memory_summary_version_transaction(
        transaction,
        conversation_ref,
        expected_active_summary_version_id,
    )


def ensure_voice_memory_summary_version(
    uid: str,
    conversation_id: str,
    expected_active_summary_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Load one user-owned memory and atomically establish its voice CAS version."""
    conversation_ref = (
        db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    )
    result = _ensure_voice_memory_summary_version(
        db.transaction(),
        conversation_ref,
        expected_active_summary_version_id,
    )
    conversation = result.get('conversation')
    if isinstance(conversation, dict):
        result = {**result, 'conversation': _prepare_conversation_for_read(conversation, uid)}
    return result


def create_audio_files_from_chunks(
    uid: str,
    conversation_id: str,
) -> List[AudioFile]:
    """
    Create audio file records by merging chunks from a conversation.
    Chunks are merged unless there's a gap > 30 seconds between segments.

    Args:
        uid: User ID
        conversation_id: Conversation ID

    Returns:
        List of AudioFile objects
    """
    # Get all chunks for this conversation
    chunks = list_audio_chunks(uid, conversation_id)
    if not chunks:
        return []

    # Group chunks based on 30-second gap rule
    audio_files = []
    current_group = []

    for i, chunk in enumerate(chunks):
        if not current_group:
            current_group.append(chunk)
        else:
            # Check if there's a gap > 30 seconds between chunks
            prev_chunk = current_group[-1]
            time_gap = chunk['timestamp'] - prev_chunk['timestamp']
            if time_gap > 30:
                # Gap detected, finalize current group
                audio_file = _finalize_audio_file_group(uid, conversation_id, current_group, audio_files)
                if audio_file:
                    audio_files.append(audio_file)
                current_group = [chunk]
            else:
                current_group.append(chunk)

    # Finalize last group
    if current_group:
        audio_file = _finalize_audio_file_group(uid, conversation_id, current_group, audio_files)
        if audio_file:
            audio_files.append(audio_file)

    return audio_files


def _finalize_audio_file_group(
    uid: str, conversation_id: str, chunk_group: List[dict], existing_files: List[AudioFile]
) -> Optional[AudioFile]:
    """
    Create an AudioFile record that references chunks (no merging).

    Args:
        uid: User ID
        conversation_id: Conversation ID
        chunk_group: List of chunk dicts to reference
        existing_files: List of existing audio files

    Returns:
        AudioFile object or None if failed
    """
    if not chunk_group:
        return None

    # Generate file ID
    file_id = str(uuid.uuid4())

    # Extract timestamps
    timestamps = [chunk['timestamp'] for chunk in chunk_group]

    # Calculate started_at and duration from timestamps
    started_at = datetime.fromtimestamp(chunk_group[0]['timestamp'], tz=timezone.utc)
    last_chunk_start = datetime.fromtimestamp(chunk_group[-1]['timestamp'], tz=timezone.utc)
    # Add 5 seconds for the last chunk's duration
    duration = (last_chunk_start - started_at).total_seconds() + 5.0

    return AudioFile(
        id=file_id,
        uid=uid,
        conversation_id=conversation_id,
        chunk_timestamps=timestamps,
        provider='gcp',
        started_at=started_at,
        duration=duration,
    )


def update_conversation_title(uid: str, conversation_id: str, title: str):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)

    doc_snapshot = conversation_ref.get()
    if not doc_snapshot.exists:
        return

    conversation_ref.update({'structured.title': title})


def delete_conversation_photos(uid: str, conversation_id: str) -> int:
    """
    Delete all photos in a conversation's photos subcollection.

    IMPORTANT: Firestore does NOT cascade delete subcollections when you delete
    a parent document. This function must be called before deleting a conversation.

    Args:
        uid: User ID
        conversation_id: Conversation ID

    Returns:
        Number of photos deleted
    """
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    photos_ref = conversation_ref.collection('photos')

    # Get all photo documents
    photos = photos_ref.stream()
    deleted_count = 0

    # Delete in batches of 500 (Firestore batch limit)
    batch = db.batch()
    batch_count = 0

    for photo_doc in photos:
        batch.delete(photo_doc.reference)
        batch_count += 1
        deleted_count += 1

        if batch_count >= 500:
            batch.commit()
            batch = db.batch()
            batch_count = 0

    # Commit remaining
    if batch_count > 0:
        batch.commit()

    return deleted_count


def delete_conversation(uid, conversation_id):
    """
    Delete a conversation and its photos subcollection.

    Args:
        uid: User ID
        conversation_id: Conversation ID
    """
    # Delete photos subcollection first
    delete_conversation_photos(uid, conversation_id)

    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    conversation_ref.delete()


def update_conversation_merged_data(uid: str, conversation_id: str, merged_data: dict):
    """
    Update a conversation with merged data from multiple conversations.

    This function handles the bulk update of all merged fields and respects
    the conversation's data protection level.

    Args:
        uid: User ID
        conversation_id: Primary conversation ID to update
        merged_data: Dictionary containing all merged fields
    """
    doc_ref = db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    doc_snapshot = doc_ref.get()
    if not doc_snapshot.exists:
        return

    doc_level = doc_snapshot.to_dict().get('data_protection_level', 'standard')
    prepared_data = _prepare_conversation_for_write(merged_data, uid, doc_level)
    doc_ref.update(prepared_data)


def delete_conversations_by_source(uid: str, source: str, batch_size: int = 450) -> int:
    """
    Delete all conversations with a specific source.

    Args:
        uid: User ID
        source: Source type (e.g., 'limitless')
        batch_size: Number of documents to delete per batch

    Returns:
        Number of deleted conversations
    """
    user_ref = db.collection('users').document(uid)
    conversations_ref = user_ref.collection(conversations_collection)

    total_deleted = 0

    while True:
        # Query for conversations with matching source
        query = conversations_ref.where(filter=FieldFilter('source', '==', source)).limit(batch_size)
        docs = list(query.stream())

        if not docs:
            break

        batch = db.batch()
        for doc in docs:
            batch.delete(doc.reference)
            total_deleted += 1
        batch.commit()

        if len(docs) < batch_size:
            break

    return total_deleted


@prepare_for_read(decrypt_func=_prepare_conversation_for_read)
@with_photos(get_conversation_photos)
def filter_conversations_by_date(uid, start_date, end_date):
    user_ref = db.collection('users').document(uid)
    query = (
        user_ref.collection(conversations_collection)
        .where(filter=FieldFilter('created_at', '>=', start_date))
        .where(filter=FieldFilter('created_at', '<=', end_date))
        .where(filter=FieldFilter('discarded', '==', False))
        .order_by('created_at', direction=firestore.Query.DESCENDING)
    )
    conversations = [doc.to_dict() for doc in query.stream()]
    return conversations


@prepare_for_read(decrypt_func=_prepare_conversation_for_read)
@with_photos(get_conversation_photos)
def get_conversations_by_id(uid, conversation_ids):
    user_ref = db.collection('users').document(uid)
    conversations_ref = user_ref.collection(conversations_collection)

    doc_refs = [conversations_ref.document(str(conversation_id)) for conversation_id in conversation_ids]
    docs = db.get_all(doc_refs)

    conversations = []
    for doc in docs:
        if doc.exists:
            data = doc.to_dict()
            if data.get('discarded'):
                continue
            conversations.append(data)

    return conversations


# **************************************
# ********* MIGRATION HELPERS **********
# **************************************


def get_conversations_to_migrate(uid: str, target_level: str) -> List[dict]:
    """
    Finds all conversations that are not at the target protection level by fetching all documents
    and filtering them in memory. This simplifies the code but may be less performant for
    users with a very large number of documents.
    """
    conversations_ref = db.collection('users').document(uid).collection(conversations_collection)
    all_conversations = conversations_ref.select(['data_protection_level', 'visibility']).stream()

    to_migrate = []
    for doc in all_conversations:
        doc_data = doc.to_dict()
        if doc_data.get('visibility') in ['public', 'shared']:
            continue

        current_level = doc_data.get('data_protection_level', 'standard')
        if target_level != current_level:
            to_migrate.append({'id': doc.id, 'type': 'conversation'})

    return to_migrate


def migrate_conversations_level_batch(uid: str, conversation_ids: List[str], target_level: str):
    """
    Migrates a batch of conversations to the target protection level, committing in batches of 450.
    """
    batch = db.batch()
    batch_count = 0
    conversations_ref = db.collection('users').document(uid).collection(conversations_collection)
    doc_refs = [conversations_ref.document(conv_id) for conv_id in conversation_ids]
    doc_snapshots = db.get_all(
        doc_refs, field_paths=['data_protection_level', 'transcript_segments', 'transcript_segments_compressed']
    )

    for doc_snapshot in doc_snapshots:
        if not doc_snapshot.exists:
            print(f"Conversation {doc_snapshot.id} not found, skipping.")
            continue

        conversation_data = doc_snapshot.to_dict()
        current_level = conversation_data.get('data_protection_level', 'standard')

        if current_level == target_level:
            continue

        # Decrypt/decompress the data to get a clean slate.
        plain_data = _prepare_conversation_for_read(conversation_data, uid)

        # Re-prepare the segments for writing with the new level.
        update_payload = {'transcript_segments': plain_data.get('transcript_segments')}
        prepared_payload = _prepare_conversation_for_write(update_payload, uid, target_level)

        # Update the document with the migrated data and the new protection level.
        update_data = {
            'data_protection_level': target_level,
        }
        if 'transcript_segments' in prepared_payload:
            update_data['transcript_segments'] = prepared_payload['transcript_segments']
            update_data['transcript_segments_compressed'] = prepared_payload.get(
                'transcript_segments_compressed', False
            )

        if not update_data.get('transcript_segments_compressed'):
            update_data['transcript_segments_compressed'] = firestore.DELETE_FIELD

        batch.update(doc_snapshot.reference, update_data)
        batch_count += 1
        if batch_count >= 100:
            batch.commit()
            batch = db.batch()
            batch_count = 0

        # Now migrate photos for this conversation in the same batch
        photos_ref = doc_snapshot.reference.collection('photos')
        photos_stream = photos_ref.select(['data_protection_level', 'base64']).stream()
        for photo_doc in photos_stream:
            photo_data = photo_doc.to_dict()
            current_photo_level = photo_data.get('data_protection_level', 'standard')
            if current_photo_level == target_level:
                continue

            # Decrypt first to get a clean state
            plain_photo_data = _prepare_photo_for_read(photo_data, uid)

            # Prepare the specific fields for update
            photo_update_payload = {'data_protection_level': target_level}
            if target_level == 'enhanced':
                photo_update_payload['base64'] = encryption.encrypt(plain_photo_data['base64'], uid)
            else:  # Moving from enhanced to standard
                photo_update_payload['base64'] = plain_photo_data['base64']

            # Add photo update to the batch
            batch.update(photo_doc.reference, photo_update_payload)
            batch_count += 1
            if batch_count >= 100:
                batch.commit()
                batch = db.batch()
                batch_count = 0

    if batch_count > 0:
        batch.commit()


# **************************************
# ********** STATUS *************
# **************************************


@prepare_for_read(decrypt_func=_prepare_conversation_for_read)
@with_photos(get_conversation_photos)
def get_in_progress_conversation(uid: str):
    user_ref = db.collection('users').document(uid)
    conversations_ref = (
        user_ref.collection(conversations_collection)
        .where(filter=FieldFilter('status', '==', 'in_progress'))
        .order_by('created_at', direction=firestore.Query.DESCENDING)
        .limit(1)
    )
    docs = [doc.to_dict() for doc in conversations_ref.stream()]
    conversation = docs[0] if docs else None
    return conversation


@prepare_for_read(decrypt_func=_prepare_conversation_for_read)
@with_photos(get_conversation_photos)
def get_in_progress_conversations(uid: str):
    """Get all in-progress conversations for a user, ordered by created_at descending."""
    user_ref = db.collection('users').document(uid)
    conversations_ref = (
        user_ref.collection(conversations_collection)
        .where(filter=FieldFilter('status', '==', 'in_progress'))
        .order_by('created_at', direction=firestore.Query.DESCENDING)
    )
    conversations = [doc.to_dict() for doc in conversations_ref.stream()]
    return conversations


@prepare_for_read(decrypt_func=_prepare_conversation_for_read)
@with_photos(get_conversation_photos)
def get_processing_conversations(uid: str):
    user_ref = db.collection('users').document(uid)
    conversations_ref = user_ref.collection(conversations_collection).where(
        filter=FieldFilter('status', '==', 'processing')
    )
    conversations = [doc.to_dict() for doc in conversations_ref.stream()]
    return conversations


def update_conversation_status(uid: str, conversation_id: str, status: str):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    conversation_ref.update({'status': status})


def _claim_conversation_processing_retry_transaction(
    transaction,
    conversation_ref,
    retry_ref,
    request_id: str,
    requested_at: datetime,
    lease_seconds: int = conversation_processing_retry_lease_seconds,
):
    snapshot = conversation_ref.get(transaction=transaction)
    retry_snapshot = retry_ref.get(transaction=transaction)
    if not snapshot.exists:
        return _retry_claim_metadata('not_found')

    conversation = snapshot.to_dict()
    status = getattr(conversation.get('status'), 'value', conversation.get('status'))

    if conversation.get('is_locked', False):
        return _retry_claim_metadata('locked')

    lease_expires_at = requested_at + timedelta(seconds=max(1, lease_seconds))
    retry_data = retry_snapshot.to_dict() if retry_snapshot.exists else None
    if retry_data and retry_data.get('outcome') == ConversationStatus.completed.value:
        return _retry_claim_metadata(ConversationStatus.completed.value, retry_data=retry_data)
    current_retry_id = conversation.get('processing_retry_id')
    stale_active_retry = bool(
        current_retry_id
        and not conversation.get('processing_retry_completed_at')
        and _processing_retry_lease_expired(conversation, requested_at)
    )
    if (
        current_retry_id
        and current_retry_id != request_id
        and not conversation.get('processing_retry_completed_at')
        and not stale_active_retry
    ):
        return _retry_claim_metadata('busy')

    if retry_data and retry_data.get('outcome') == ConversationStatus.failed.value:
        return _retry_claim_metadata(ConversationStatus.failed.value, retry_data=retry_data)

    if retry_snapshot.exists:
        retry_data = retry_data or {}
        retry_outcome = retry_data.get('outcome')
        retry_lease_expires_at = retry_data.get('lease_expires_at')
        retry_lease_active = bool(
            retry_outcome == ConversationStatus.processing.value
            and isinstance(retry_lease_expires_at, datetime)
            and _ensure_timezone_aware(retry_lease_expires_at) > _ensure_timezone_aware(requested_at)
        )
        if retry_lease_active:
            return _retry_claim_metadata(retry_outcome, retry_data=retry_data)
        if retry_outcome != ConversationStatus.processing.value:
            return _retry_claim_metadata('invalid_state', retry_data=retry_data)

        current_mode, _ = conversation_processing_recovery_mode(conversation)
        mode = current_mode or retry_data.get('mode')
        if mode not in {'full', 'enrichment_only'}:
            return _retry_claim_metadata('invalid_state', retry_data=retry_data)

        attempt_count = int(retry_data.get('attempt_count') or 1) + 1
        conversation_update = {
            'processing_retry_id': request_id,
            'processing_retry_mode': mode,
            'processing_retry_started_at': requested_at,
            'processing_retry_lease_expires_at': lease_expires_at,
            'processing_retry_completed_at': None,
            'processing_retry_attempt_count': attempt_count,
        }
        if mode == 'full' and not has_usable_conversation_summary(conversation):
            conversation_update['status'] = ConversationStatus.processing.value
        retry_update = {
            'outcome': ConversationStatus.processing.value,
            'mode': mode,
            'lease_expires_at': lease_expires_at,
            'attempt_count': attempt_count,
            'updated_at': requested_at,
            'last_reclaimed_at': requested_at,
        }
        transaction.update(conversation_ref, conversation_update)
        transaction.update(retry_ref, retry_update)
        return _retry_claim_metadata(
            'claimed',
            retry_data={**retry_data, **retry_update},
            mode=mode,
            reason='lease_reclaimed',
        )

    if stale_active_retry and status == ConversationStatus.processing.value:
        if has_usable_conversation_summary(conversation):
            mode, reason = 'enrichment_only', 'stale_worker_after_generic'
        elif conversation.get('processing_error') in conversation_summary_retryable_errors:
            mode, reason = 'full', 'stale_worker_before_generic'
        else:
            mode, reason = None, 'active_processing'
    else:
        mode, reason = conversation_processing_recovery_mode(conversation)
    if reason == 'already_enriched':
        receipt = {
            'request_id': request_id,
            'outcome': ConversationStatus.completed.value,
            'mode': None,
            'phase': 'completed',
            'generic_status': 'completed',
            'generic_vector_status': 'completed',
            'enrichment_status': 'completed',
            'vector_status': 'completed',
            'attempt_count': 0,
            'requested_at': requested_at,
            'updated_at': requested_at,
        }
        transaction.set(
            retry_ref,
            receipt,
        )
        return _retry_claim_metadata('completed', retry_data=receipt)
    if reason == 'active_processing':
        return _retry_claim_metadata('busy', reason=reason)
    if reason in {'intentional_discard', 'non_retryable_failure'}:
        return _retry_claim_metadata('not_retryable', reason=reason)
    if mode is None:
        return _retry_claim_metadata('invalid_state', reason=reason)

    update_data = {
        'processing_retry_id': request_id,
        'processing_retry_mode': mode,
        'processing_retry_started_at': requested_at,
        'processing_retry_lease_expires_at': lease_expires_at,
        'processing_retry_completed_at': None,
        'processing_retry_attempt_count': 1,
    }
    if mode == 'full':
        update_data['status'] = ConversationStatus.processing.value
    transaction.update(conversation_ref, update_data)
    receipt = {
        'request_id': request_id,
        'outcome': ConversationStatus.processing.value,
        'mode': mode,
        'phase': 'claimed',
        'generic_status': 'completed' if mode == 'enrichment_only' else 'pending',
        'generic_vector_status': 'unknown' if mode == 'enrichment_only' else 'pending',
        'enrichment_status': 'pending',
        'vector_status': 'pending',
        'lease_expires_at': lease_expires_at,
        'attempt_count': 1,
        'requested_at': requested_at,
        'updated_at': requested_at,
    }
    transaction.set(retry_ref, receipt)
    return _retry_claim_metadata('claimed', retry_data=receipt, mode=mode, reason=reason)


@transactional
def _claim_conversation_processing_retry(
    transaction,
    conversation_ref,
    retry_ref,
    request_id: str,
    requested_at: datetime,
    lease_seconds: int,
):
    return _claim_conversation_processing_retry_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        request_id,
        requested_at,
        lease_seconds,
    )


def claim_conversation_processing_retry(
    uid: str,
    conversation_id: str,
    request_id: str,
    requested_at: Optional[datetime] = None,
    lease_seconds: int = conversation_processing_retry_lease_seconds,
):
    conversation_ref = (
        db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    )
    retry_ref = conversation_ref.collection(conversation_processing_retries_collection).document(request_id)
    transaction = db.transaction()
    return _claim_conversation_processing_retry(
        transaction,
        conversation_ref,
        retry_ref,
        request_id,
        requested_at or datetime.now(timezone.utc),
        lease_seconds,
    )


def _record_conversation_processing_retry_source_transaction(
    transaction,
    conversation_ref,
    retry_ref,
    request_id: str,
    transcript_sha256: str,
    segment_count: int,
    updated_at: datetime,
    lease_expires_at: datetime,
    attempt_count: Optional[int] = None,
    generic_summary_sha256: Optional[str] = None,
):
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    retry_snapshot = retry_ref.get(transaction=transaction)
    if not conversation_snapshot.exists or not retry_snapshot.exists:
        return False
    conversation = conversation_snapshot.to_dict()
    retry_data = retry_snapshot.to_dict()
    if conversation.get('processing_retry_id') != request_id or retry_data.get('request_id') != request_id:
        return False
    if attempt_count is not None and (
        conversation.get('processing_retry_attempt_count') != attempt_count
        or retry_data.get('attempt_count') != attempt_count
    ):
        return False
    if retry_data.get('outcome') != ConversationStatus.processing.value:
        return False
    conversation_update = {
        'processing_retry_lease_expires_at': lease_expires_at,
        'processing_retry_transcript_sha256': transcript_sha256,
        'processing_retry_source_request_id': request_id,
    }
    retry_update = {
        'transcript_sha256': transcript_sha256,
        'transcript_segment_count': segment_count,
        'lease_expires_at': lease_expires_at,
        'updated_at': updated_at,
    }
    if generic_summary_sha256:
        conversation_update['processing_retry_generic_summary_sha256'] = generic_summary_sha256
        retry_update['generic_summary_sha256'] = generic_summary_sha256
    transaction.update(conversation_ref, conversation_update)
    transaction.update(retry_ref, retry_update)
    return True


@transactional
def _record_conversation_processing_retry_source(
    transaction,
    conversation_ref,
    retry_ref,
    request_id: str,
    transcript_sha256: str,
    segment_count: int,
    updated_at: datetime,
    lease_expires_at: datetime,
    attempt_count: Optional[int],
    generic_summary_sha256: Optional[str],
):
    return _record_conversation_processing_retry_source_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        request_id,
        transcript_sha256,
        segment_count,
        updated_at,
        lease_expires_at,
        attempt_count,
        generic_summary_sha256,
    )


def record_conversation_processing_retry_source(
    uid: str,
    conversation_id: str,
    request_id: str,
    transcript_sha256: str,
    segment_count: int,
    updated_at: Optional[datetime] = None,
    lease_seconds: int = conversation_processing_retry_lease_seconds,
    attempt_count: Optional[int] = None,
    generic_summary_sha256: Optional[str] = None,
):
    now = updated_at or datetime.now(timezone.utc)
    conversation_ref = (
        db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    )
    retry_ref = conversation_ref.collection(conversation_processing_retries_collection).document(request_id)
    return _record_conversation_processing_retry_source(
        db.transaction(),
        conversation_ref,
        retry_ref,
        request_id,
        transcript_sha256,
        segment_count,
        now,
        now + timedelta(seconds=max(1, lease_seconds)),
        attempt_count,
        generic_summary_sha256,
    )


def _record_conversation_processing_retry_summary_applied_transaction(
    transaction,
    conversation_ref,
    retry_ref,
    request_id: str,
    summary_version_id: str,
    applied_at: datetime,
    attempt_count: Optional[int] = None,
):
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    retry_snapshot = retry_ref.get(transaction=transaction)
    if not conversation_snapshot.exists or not retry_snapshot.exists:
        return False

    conversation = conversation_snapshot.to_dict()
    retry_data = retry_snapshot.to_dict()
    if conversation.get('processing_retry_id') != request_id or retry_data.get('request_id') != request_id:
        return False
    if attempt_count is not None and retry_data.get('attempt_count') != attempt_count:
        return False

    transaction.update(
        conversation_ref,
        {
            'status': ConversationStatus.completed.value,
            'discarded': False,
            'processing_error': None,
            'processing_error_at': None,
            'processing_retry_summary_version_id': summary_version_id,
            'processing_retry_generic_vector_status': 'pending',
        },
    )
    transaction.update(
        retry_ref,
        {
            'outcome': ConversationStatus.processing.value,
            'phase': 'generic_writeback_applied',
            'generic_status': 'writeback_applied',
            'generic_vector_status': 'pending',
            'enrichment_status': 'pending',
            'summary_version_id': summary_version_id,
            'updated_at': applied_at,
        },
    )
    return True


def _record_conversation_processing_retry_generic_vector_transaction(
    transaction,
    conversation_ref,
    retry_ref,
    request_id: str,
    status: str,
    updated_at: datetime,
    attempt_count: Optional[int] = None,
):
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    retry_snapshot = retry_ref.get(transaction=transaction)
    if not conversation_snapshot.exists or not retry_snapshot.exists:
        return False

    conversation = conversation_snapshot.to_dict()
    retry_data = retry_snapshot.to_dict()
    if conversation.get('processing_retry_id') != request_id or retry_data.get('request_id') != request_id:
        return False
    if attempt_count is not None and retry_data.get('attempt_count') != attempt_count:
        return False

    completed = status == 'completed'
    transaction.update(
        conversation_ref,
        {
            'status': ConversationStatus.completed.value,
            'discarded': False,
            'processing_error': None,
            'processing_error_at': None,
            'processing_retry_generic_vector_status': status,
            **({'processing_retry_completed_at': updated_at} if not completed else {}),
        },
    )
    transaction.update(
        retry_ref,
        {
            'outcome': ConversationStatus.processing.value if completed else ConversationStatus.failed.value,
            'phase': 'generic_completed' if completed else 'generic_vector_failed',
            'generic_status': 'completed' if completed else 'writeback_applied',
            'generic_vector_status': status,
            'updated_at': updated_at,
        },
    )
    return True


@transactional
def _record_conversation_processing_retry_generic_vector(
    transaction,
    conversation_ref,
    retry_ref,
    request_id: str,
    status: str,
    updated_at: datetime,
    attempt_count: Optional[int],
):
    return _record_conversation_processing_retry_generic_vector_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        request_id,
        status,
        updated_at,
        attempt_count,
    )


def record_conversation_processing_retry_generic_vector(
    uid: str,
    conversation_id: str,
    request_id: str,
    status: str,
    updated_at: Optional[datetime] = None,
    attempt_count: Optional[int] = None,
):
    if status not in {'completed', 'failed'}:
        raise ValueError(f'Unsupported generic vector status: {status}')
    conversation_ref = (
        db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    )
    retry_ref = conversation_ref.collection(conversation_processing_retries_collection).document(request_id)
    return _record_conversation_processing_retry_generic_vector(
        db.transaction(),
        conversation_ref,
        retry_ref,
        request_id,
        status,
        updated_at or datetime.now(timezone.utc),
        attempt_count,
    )


@transactional
def _record_conversation_processing_retry_summary_applied(
    transaction,
    conversation_ref,
    retry_ref,
    request_id: str,
    summary_version_id: str,
    applied_at: datetime,
    attempt_count: Optional[int],
):
    return _record_conversation_processing_retry_summary_applied_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        request_id,
        summary_version_id,
        applied_at,
        attempt_count,
    )


def record_conversation_processing_retry_summary_applied(
    uid: str,
    conversation_id: str,
    request_id: str,
    summary_version_id: str,
    applied_at: Optional[datetime] = None,
    attempt_count: Optional[int] = None,
):
    conversation_ref = (
        db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    )
    retry_ref = conversation_ref.collection(conversation_processing_retries_collection).document(request_id)
    transaction = db.transaction()
    return _record_conversation_processing_retry_summary_applied(
        transaction,
        conversation_ref,
        retry_ref,
        request_id,
        summary_version_id,
        applied_at or datetime.now(timezone.utc),
        attempt_count,
    )


def _finish_conversation_processing_retry_transaction(
    transaction,
    conversation_ref,
    retry_ref,
    request_id: str,
    outcome: str,
    finished_at: datetime,
    error_code: Optional[str],
    preserve_completed_summary: bool = False,
    attempt_count: Optional[int] = None,
):
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    retry_snapshot = retry_ref.get(transaction=transaction)
    if not conversation_snapshot.exists or not retry_snapshot.exists:
        return False

    retry_data = retry_snapshot.to_dict()
    if retry_data.get('request_id') != request_id:
        return False
    if attempt_count is not None and retry_data.get('attempt_count') != attempt_count:
        return False

    retry_update = {
        'outcome': outcome,
        'phase': 'completed' if outcome == ConversationStatus.completed.value else 'failed',
        'updated_at': finished_at,
    }
    if error_code:
        retry_update['error_code'] = error_code
    transaction.update(retry_ref, retry_update)
    conversation = conversation_snapshot.to_dict()
    if conversation.get('processing_retry_id') != request_id:
        return True

    if outcome == ConversationStatus.completed.value:
        transaction.update(
            conversation_ref,
            {
                'status': ConversationStatus.completed.value,
                'discarded': False,
                'processing_error': None,
                'processing_error_at': None,
                'processing_retry_completed_at': finished_at,
            },
        )
    elif preserve_completed_summary:
        transaction.update(
            conversation_ref,
            {
                'status': ConversationStatus.completed.value,
                'discarded': False,
                'processing_retry_completed_at': finished_at,
            },
        )
    else:
        transaction.update(
            conversation_ref,
            {
                'status': ConversationStatus.failed.value,
                'discarded': False,
                'processing_error': error_code or 'conversation_summary_failed',
                'processing_error_at': finished_at,
            },
        )
    return True


@transactional
def _finish_conversation_processing_retry(
    transaction,
    conversation_ref,
    retry_ref,
    request_id: str,
    outcome: str,
    finished_at: datetime,
    error_code: Optional[str],
    preserve_completed_summary: bool,
    attempt_count: Optional[int],
):
    return _finish_conversation_processing_retry_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        request_id,
        outcome,
        finished_at,
        error_code,
        preserve_completed_summary,
        attempt_count,
    )


def finish_conversation_processing_retry(
    uid: str,
    conversation_id: str,
    request_id: str,
    outcome: str,
    finished_at: Optional[datetime] = None,
    error_code: Optional[str] = None,
    preserve_completed_summary: bool = False,
    attempt_count: Optional[int] = None,
):
    if outcome not in {ConversationStatus.completed.value, ConversationStatus.failed.value}:
        raise ValueError(f'Unsupported conversation processing retry outcome: {outcome}')

    conversation_ref = (
        db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    )
    retry_ref = conversation_ref.collection(conversation_processing_retries_collection).document(request_id)
    transaction = db.transaction()
    return _finish_conversation_processing_retry(
        transaction,
        conversation_ref,
        retry_ref,
        request_id,
        outcome,
        finished_at or datetime.now(timezone.utc),
        error_code,
        preserve_completed_summary,
        attempt_count,
    )


def _record_conversation_processing_retry_enrichment_transaction(
    transaction,
    conversation_ref,
    retry_ref,
    conversation_id: str,
    request_id: str,
    status: str,
    updated_at: datetime,
    summary_version_id: Optional[str],
    vector_content_sha256: Optional[str] = None,
    attempt_count: Optional[int] = None,
):
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    retry_snapshot = retry_ref.get(transaction=transaction)
    if not conversation_snapshot.exists or not retry_snapshot.exists:
        return False

    conversation = conversation_snapshot.to_dict()
    retry_data = retry_snapshot.to_dict()
    if retry_data.get('request_id') != request_id:
        return False
    if attempt_count is not None and retry_data.get('attempt_count') != attempt_count:
        return False
    if (
        summary_version_id
        and status in {'canonical_completed', 'completed', 'vector_failed'}
        and str(conversation.get('active_summary_version_id') or '') != str(summary_version_id)
    ):
        return False

    if status == 'canonical_completed':
        retry_update = {
            'enrichment_status': 'canonical_completed',
            'vector_status': 'pending',
            'outcome': ConversationStatus.processing.value,
            'phase': 'enrichment_canonical_completed',
            'updated_at': updated_at,
        }
    elif status == 'completed':
        retry_update = {
            'enrichment_status': 'completed',
            'vector_status': 'completed',
            'outcome': ConversationStatus.completed.value,
            'phase': 'completed',
            'updated_at': updated_at,
        }
    elif status == 'canonical_failed':
        retry_update = {
            'enrichment_status': 'writeback_pending_canonical',
            'vector_status': 'pending',
            'outcome': ConversationStatus.failed.value,
            'phase': 'enrichment_canonical_failed',
            'updated_at': updated_at,
        }
    elif status == 'vector_failed':
        retry_update = {
            'enrichment_status': 'canonical_completed',
            'vector_status': 'failed',
            'outcome': ConversationStatus.failed.value,
            'phase': 'enrichment_vector_failed',
            'updated_at': updated_at,
        }
    else:
        retry_update = {
            'enrichment_status': 'failed',
            'outcome': ConversationStatus.failed.value,
            'phase': 'enrichment_failed',
            'updated_at': updated_at,
        }
    if summary_version_id:
        retry_update['enriched_summary_version_id'] = summary_version_id
    if status in {'completed', 'vector_failed'} and summary_version_id:
        retry_update['vector_summary_version_id'] = summary_version_id
    if status in {'completed', 'vector_failed'} and vector_content_sha256:
        retry_update['vector_content_sha256'] = vector_content_sha256
    transaction.update(retry_ref, retry_update)

    if conversation.get('processing_retry_id') != request_id:
        return True
    if status == 'canonical_completed' and summary_version_id:
        transaction.update(
            conversation_ref,
            {
                'status': ConversationStatus.completed.value,
                'processing_retry_enriched_version_id': summary_version_id,
                'processing_retry_enrichment_vector_status': 'pending',
            },
        )
    elif status == 'completed' and summary_version_id:
        transaction.update(
            conversation_ref,
            {
                'status': ConversationStatus.completed.value,
                'processing_retry_enriched_version_id': summary_version_id,
                'processing_retry_enrichment_vector_status': 'completed',
                'processing_retry_enrichment_vector_version_id': summary_version_id,
                'processing_retry_enrichment_vector_sha256': vector_content_sha256,
                'processing_retry_completed_at': updated_at,
            },
        )
    elif status == 'canonical_failed':
        transaction.update(
            conversation_ref,
            {
                'status': ConversationStatus.completed.value,
                'processing_retry_enrichment_vector_status': 'pending',
                'processing_retry_completed_at': updated_at,
            },
        )
    elif status == 'vector_failed':
        transaction.update(
            conversation_ref,
            {
                'status': ConversationStatus.completed.value,
                'processing_retry_enrichment_vector_status': 'failed',
                'processing_retry_enrichment_vector_version_id': summary_version_id,
                'processing_retry_enrichment_vector_sha256': vector_content_sha256,
                'processing_retry_completed_at': updated_at,
            },
        )
    elif status == 'failed':
        transaction.update(
            conversation_ref,
            {
                'status': ConversationStatus.completed.value,
                'processing_retry_completed_at': updated_at,
                'enrichment_state': {
                    'status': 'failed',
                    'pending': True,
                    'source': 'observer',
                    'kind': 'recovered_enriched',
                    'trace_id': f'summary-retry:{conversation_id}:{request_id}:hermes',
                    'updated_at': updated_at,
                    'error': 'hermes_temporarily_unavailable',
                },
            },
        )
    return True


@transactional
def _record_conversation_processing_retry_enrichment(
    transaction,
    conversation_ref,
    retry_ref,
    conversation_id: str,
    request_id: str,
    status: str,
    updated_at: datetime,
    summary_version_id: Optional[str],
    vector_content_sha256: Optional[str],
    attempt_count: Optional[int],
):
    return _record_conversation_processing_retry_enrichment_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        conversation_id,
        request_id,
        status,
        updated_at,
        summary_version_id,
        vector_content_sha256,
        attempt_count,
    )


def record_conversation_processing_retry_enrichment(
    uid: str,
    conversation_id: str,
    request_id: str,
    status: str,
    summary_version_id: Optional[str] = None,
    updated_at: Optional[datetime] = None,
    vector_content_sha256: Optional[str] = None,
    attempt_count: Optional[int] = None,
):
    if status not in {'canonical_completed', 'canonical_failed', 'completed', 'vector_failed', 'failed'}:
        raise ValueError(f'Unsupported conversation processing retry enrichment status: {status}')
    conversation_ref = (
        db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    )
    retry_ref = conversation_ref.collection(conversation_processing_retries_collection).document(request_id)
    transaction = db.transaction()
    return _record_conversation_processing_retry_enrichment(
        transaction,
        conversation_ref,
        retry_ref,
        conversation_id,
        request_id,
        status,
        updated_at or datetime.now(timezone.utc),
        summary_version_id,
        vector_content_sha256,
        attempt_count,
    )


def set_conversation_as_discarded(uid: str, conversation_id: str):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    conversation_ref.update({'discarded': True})


# *********************************
# ********** CALENDAR *************
# *********************************


def update_conversation_events(uid: str, conversation_id: str, events: List[dict]):
    update_conversation(uid, conversation_id, {'structured.events': events})


# *********************************
# ******** ACTION ITEMS ***********
# *********************************


def update_conversation_action_items(uid: str, conversation_id: str, action_items: List[dict]):
    update_conversation(uid, conversation_id, {'structured.action_items': action_items})


def get_action_items(
    uid: str,
    limit: int = 100,
    offset: int = 0,
    include_completed: bool = True,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    """Fetch action items directly from conversations collection"""
    conversations_ref = db.collection('users').document(uid).collection(conversations_collection)

    # Only get completed conversations with action items
    conversations_ref = conversations_ref.where(filter=FieldFilter('status', '==', 'completed'))

    # Apply date range filters if provided
    if start_date:
        conversations_ref = conversations_ref.where(filter=FieldFilter('created_at', '>=', start_date))
    if end_date:
        conversations_ref = conversations_ref.where(filter=FieldFilter('created_at', '<=', end_date))

    # Sort by created_at descending
    conversations_ref = conversations_ref.order_by('created_at', direction=firestore.Query.DESCENDING)

    # Get all conversations with action items
    conversations = []
    for doc in conversations_ref.stream():
        conversation_data = doc.to_dict()

        # Check if conversation has action items
        structured = conversation_data.get('structured', {})
        raw_action_items = structured.get('action_items', [])

        if raw_action_items:
            # Decrypt conversation data for proper reading
            decrypted_data = _prepare_conversation_for_read(conversation_data, uid)
            conversations.append(decrypted_data)

    # Extract and flatten action items with metadata
    action_items = []
    for conversation in conversations:
        conversation_id = conversation['id']
        conversation_title = conversation.get('structured', {}).get('title', 'Untitled')
        conversation_created_at = _ensure_timezone_aware(conversation['created_at'])

        raw_items = conversation.get('structured', {}).get('action_items', [])

        for idx, item in enumerate(raw_items):
            # Skip deleted items
            if isinstance(item, dict) and item.get('deleted', False):
                continue

            # Skip completed items if not requested
            is_completed = False
            if isinstance(item, dict):
                is_completed = item.get('completed', False)

            if not include_completed and is_completed:
                continue

            # Handle backwards compatibility for dates
            created_at = None
            completed_at = None

            if isinstance(item, dict):
                created_at = item.get('created_at')
                completed_at = item.get('completed_at')

            # Ensure timezone awareness for action item dates
            if created_at is not None:
                created_at = _ensure_timezone_aware(created_at)
            if completed_at is not None:
                completed_at = _ensure_timezone_aware(completed_at)

            # Fallback to conversation created_at if dates are missing
            if created_at is None:
                created_at = conversation_created_at

            # If item is completed but no completed_at date, use conversation created_at
            if is_completed and completed_at is None:
                completed_at = conversation_created_at

            action_item_data = {
                'id': f"{conversation_id}_{idx}",
                'conversation_id': conversation_id,
                'conversation_title': conversation_title,
                'conversation_created_at': conversation_created_at,
                'index': idx,
                'description': item.get('description', item) if isinstance(item, dict) else item,
                'completed': is_completed,
                'deleted': item.get('deleted', False) if isinstance(item, dict) else False,
                'created_at': created_at,
                'completed_at': completed_at,
            }
            action_items.append(action_item_data)

    # Sort by newest first
    action_items.sort(key=lambda x: -x['conversation_created_at'].timestamp())

    # Apply pagination
    start_idx = offset
    end_idx = offset + limit

    return action_items[start_idx:end_idx]


# ******************************
# ********** OTHER *************
# ******************************


def update_conversation_finished_at(uid: str, conversation_id: str, finished_at: datetime):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    conversation_ref.update({'finished_at': finished_at})


def update_conversation_segments(uid: str, conversation_id: str, segments: List[dict], finished_at: datetime = None):
    doc_ref = db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    doc_snapshot = doc_ref.get(field_paths=['data_protection_level'])
    if not doc_snapshot.exists:
        return

    doc_level = doc_snapshot.to_dict().get('data_protection_level', 'standard')
    update_payload = {'transcript_segments': segments}
    if finished_at:
        update_payload['finished_at'] = finished_at
    prepared_payload = _prepare_conversation_for_write(update_payload, uid, doc_level)
    doc_ref.update(prepared_payload)


# ***********************************
# ********** VISIBILITY *************
# ***********************************


def set_conversation_visibility(uid: str, conversation_id: str, visibility: str):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    conversation_ref.update({'visibility': visibility})


def set_conversation_starred(uid: str, conversation_id: str, starred: bool):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    conversation_ref.update({'starred': starred})


def unlock_all_conversations(uid: str):
    """
    Finds all conversations for a user with is_locked: True and updates them to is_locked = False.
    """
    conversations_ref = db.collection('users').document(uid).collection(conversations_collection)
    locked_conversations_query = conversations_ref.where(filter=FieldFilter('is_locked', '==', True))

    batch = db.batch()
    docs = locked_conversations_query.stream()
    count = 0
    for doc in docs:
        batch.update(doc.reference, {'is_locked': False})
        count += 1
        if count >= 499:  # Firestore batch limit is 500
            batch.commit()
            batch = db.batch()
            count = 0
    if count > 0:
        batch.commit()
    print(f"Unlocked all conversations for user {uid}")


def get_public_conversations(data: List[Tuple[str, str]]):
    """
    Fetches multiple public conversations sequentially.
    """
    conversations = []
    for uid, conversation_id in data:
        # get_conversation is already decorated to return a fully populated and decrypted conversation
        conversation_data = get_conversation(uid=uid, conversation_id=conversation_id)
        if conversation_data and conversation_data.get('visibility') == 'public':
            conversations.append(conversation_data)
    return conversations


# ****************************************
# ********** POSTPROCESSING **************
# ****************************************


def set_postprocessing_status(
    uid: str,
    conversation_id: str,
    status: PostProcessingStatus,
    fail_reason: str = None,
    model: PostProcessingModel = PostProcessingModel.fal_whisperx,
):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    conversation_ref.update(
        {'postprocessing.status': status, 'postprocessing.model': model, 'postprocessing.fail_reason': fail_reason}
    )


def store_model_segments_result(uid: str, conversation_id: str, model_name: str, segments: List[TranscriptSegment]):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    segments_ref = conversation_ref.collection(model_name)
    batch = db.batch()
    for i, segment in enumerate(segments):
        segment_id = str(uuid.uuid4())
        segment_ref = segments_ref.document(segment_id)
        batch.set(segment_ref, segment.dict())
        if i >= 400:
            batch.commit()
            batch = db.batch()
    batch.commit()


def store_model_emotion_predictions_result(
    uid: str, conversation_id: str, model_name: str, predictions: List[hume.HumeJobModelPredictionResponseModel]
):
    now = datetime.now()
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    predictions_ref = conversation_ref.collection(model_name)
    batch = db.batch()
    count = 0
    for prediction in predictions:
        prediction_id = str(uuid.uuid4())
        prediction_ref = predictions_ref.document(prediction_id)
        batch.set(
            prediction_ref,
            {
                "created_at": now,
                "start": prediction.time[0],
                "end": prediction.time[1],
                "emotions": json.dumps(hume.HumePredictionEmotionResponseModel.to_multi_dict(prediction.emotions)),
            },
        )
        count = count + 1
        if count >= 100:
            batch.commit()
            batch = db.batch()
            count = 0
    batch.commit()


def get_conversation_transcripts_by_model(uid: str, conversation_id: str):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)
    deepgram_ref = conversation_ref.collection('deepgram_streaming')
    soniox_ref = conversation_ref.collection('soniox_streaming')
    speechmatics_ref = conversation_ref.collection('speechmatics_streaming')
    whisperx_ref = conversation_ref.collection('fal_whisperx')

    return {
        'deepgram': list(sorted([doc.to_dict() for doc in deepgram_ref.stream()], key=lambda x: x['start'])),
        'soniox': list(sorted([doc.to_dict() for doc in soniox_ref.stream()], key=lambda x: x['start'])),
        'speechmatics': list(sorted([doc.to_dict() for doc in speechmatics_ref.stream()], key=lambda x: x['start'])),
        'whisperx': list(sorted([doc.to_dict() for doc in whisperx_ref.stream()], key=lambda x: x['start'])),
    }


# ***********************************
# ********** OPENGLASS **************
# ***********************************


def store_conversation_photos(uid: str, conversation_id: str, photos: List[ConversationPhoto]):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection(conversations_collection).document(conversation_id)

    conversation_snapshot = conversation_ref.get(field_paths=['data_protection_level'])
    level = 'standard'
    if conversation_snapshot.exists:
        level = conversation_snapshot.to_dict().get('data_protection_level', 'standard')

    photos_ref = conversation_ref.collection('photos')
    batch = db.batch()
    for photo in photos:
        photo_id = photo.id or str(uuid.uuid4())
        photo_ref = photos_ref.document(photo_id)
        data = photo.dict()
        data['id'] = photo_id
        prepared_data = _prepare_photo_for_write(data, uid, level)
        batch.set(photo_ref, prepared_data)
    batch.commit()


# ********************************
# ********** SYNCING *************
# ********************************


@prepare_for_read(decrypt_func=_prepare_conversation_for_read)
@with_photos(get_conversation_photos)
def get_closest_conversation_to_timestamps(uid: str, start_timestamp: int, end_timestamp: int) -> Optional[dict]:
    start_threshold = datetime.fromtimestamp(start_timestamp, tz=timezone.utc) - timedelta(minutes=2)
    end_threshold = datetime.fromtimestamp(end_timestamp, tz=timezone.utc) + timedelta(minutes=2)

    query = (
        db.collection('users')
        .document(uid)
        .collection(conversations_collection)
        .where(filter=FieldFilter('finished_at', '>=', start_threshold))
        .where(filter=FieldFilter('started_at', '<=', end_threshold))
        .order_by('created_at', direction=firestore.Query.DESCENDING)
    )

    conversations = [doc.to_dict() for doc in query.stream()]
    print('get_closest_conversation_to_timestamps len(conversations)', len(conversations))
    if not conversations:
        return None

    print('get_closest_conversation_to_timestamps found:')
    for conversation in conversations:
        print('-', conversation['id'], conversation['started_at'], conversation['finished_at'])

    # get the conversation that has the closest start timestamp or end timestamp
    closest_conversation = None
    min_diff = float('inf')
    for conversation in conversations:
        conversation_start_timestamp = conversation['started_at'].timestamp()
        conversation_end_timestamp = conversation['finished_at'].timestamp()
        diff1 = abs(conversation_start_timestamp - start_timestamp)
        diff2 = abs(conversation_end_timestamp - end_timestamp)
        if diff1 < min_diff or diff2 < min_diff:
            min_diff = min(diff1, diff2)
            closest_conversation = conversation

    print('get_closest_conversation_to_timestamps closest_conversation:', closest_conversation['id'])
    return closest_conversation


@prepare_for_read(decrypt_func=_prepare_conversation_for_read)
@with_photos(get_conversation_photos)
def get_last_completed_conversation(uid: str) -> Optional[dict]:
    query = (
        db.collection('users')
        .document(uid)
        .collection(conversations_collection)
        .where(filter=FieldFilter('status', '==', ConversationStatus.completed))
        .order_by('created_at', direction=firestore.Query.DESCENDING)
        .limit(1)
    )
    conversations = [doc.to_dict() for doc in query.stream()]
    conversation = conversations[0] if conversations else None
    return conversation
