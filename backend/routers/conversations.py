from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from starlette.concurrency import run_in_threadpool
from typing import Optional, List
from datetime import datetime, timezone
import threading
import uuid

import database.conversations as conversations_db
import database.action_items as action_items_db
import database.redis_db as redis_db
import database.users as users_db
from database.vector_db import delete_vector
from models.conversation import (
    BaseModel,
    CalendarMeetingContext,
    Conversation,
    ConversationPhoto,
    ConversationStatus,
    ConversationVisibility,
    CreateConversationResponse,
    Geolocation,
    MergeConversationsRequest,
    MergeConversationsResponse,
    SetConversationEventsStateRequest,
    SetConversationActionItemsStateRequest,
    UpdateActionItemDescriptionRequest,
    DeleteActionItemRequest,
    BulkAssignSegmentsRequest,
    SearchRequest,
    TestPromptRequest,
)
from models.transcript_segment import TranscriptSegment
from models.other import Person

from utils.conversations.process_conversation import (
    process_conversation,
    process_conversation_with_outcome,
    retrieve_in_progress_conversation,
)
from utils.conversations.capture_protocol import (
    CAPTURE_PROTOCOL_VERSION,
    claim_capture_finalization,
    claim_capture_finalization_effect,
    complete_capture_finalization,
    complete_capture_finalization_effect,
    renew_capture_finalization,
    release_capture_finalization,
)
from utils.conversations.search import search_conversations
from utils.llm.conversation_processing import generate_summary_with_prompt
from utils.speaker_identification import extract_speaker_samples
from utils.other import endpoints as auth
from ella.services.ai_consent import require_current_ai_consent
from ella.services.today_card_postgres import invalidate_deleted_conversation_source
from utils.other.storage import get_conversation_recording_if_exists
from utils.app_integrations import trigger_external_integrations
from models.chat import Message
from utils.conversations.location import get_google_maps_location
from utils.ella.memory_artwork_storage import MemoryArtworkStorageError

router = APIRouter()


def _get_valid_conversation_by_id(uid: str, conversation_id: str) -> dict:
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conversation.get('is_locked', False):
        raise HTTPException(status_code=402, detail="Unlimited Plan Required to access this conversation.")

    return conversation


class ProcessConversationRequest(BaseModel):
    calendar_meeting_context: Optional[CalendarMeetingContext] = None
    conversation_id: Optional[str] = None
    protocol_version: Optional[int] = None
    generation: Optional[str] = None
    owner_token: Optional[str] = None


@router.post(
    "/v1/conversations",
    response_model=CreateConversationResponse,
    tags=['conversations'],
    dependencies=[Depends(require_current_ai_consent)],
)
def process_in_progress_conversation(
    request: ProcessConversationRequest = None, uid: str = Depends(auth.get_current_user_uid)
):
    requested_conversation_id = str(request.conversation_id or '').strip() if request else ''
    discovered_conversation = None if requested_conversation_id else retrieve_in_progress_conversation(uid)
    conversation_id = requested_conversation_id or str((discovered_conversation or {}).get('id') or '').strip()
    if not conversation_id:
        raise HTTPException(status_code=404, detail="Conversation in progress not found")

    initial_conversation = conversations_db.get_conversation(uid, conversation_id)
    if not initial_conversation:
        raise HTTPException(status_code=404, detail="Conversation in progress not found")
    capture_claim_token = None
    capture_finalization_claimed = False
    is_capture_v2 = initial_conversation.get('capture_protocol_version') == CAPTURE_PROTOCOL_VERSION
    if is_capture_v2:
        if (
            request is None
            or request.protocol_version != CAPTURE_PROTOCOL_VERSION
            or not str(request.generation or '').strip()
            or not str(request.owner_token or '').strip()
        ):
            raise HTTPException(status_code=409, detail="Capture finalization protocol tuple is invalid")
        capture_outcome, capture_claim_token = claim_capture_finalization(
            uid,
            conversation_id,
            request.generation or '',
            request.owner_token or '',
        )
        if capture_outcome == 'terminal':
            return CreateConversationResponse(conversation=Conversation(**initial_conversation), messages=[])
        if capture_outcome == 'not_found':
            raise HTTPException(status_code=404, detail="Conversation in progress not found")
        if capture_outcome in {'mismatch', 'not_drained'}:
            raise HTTPException(status_code=409, detail="Conversation capture authority is not drained for this owner")
        if capture_outcome == 'busy':
            raise HTTPException(status_code=409, detail="Conversation finalization lease is active")
        if capture_outcome != 'claimed':
            raise HTTPException(status_code=409, detail="Conversation finalization claim was not acquired")
        capture_finalization_claimed = True

    processing_fence_token = f'conversation-processing:{uuid.uuid4()}'
    if not redis_db.acquire_in_progress_processing_fence(uid, conversation_id, processing_fence_token):
        if capture_finalization_claimed:
            release_capture_finalization(
                uid,
                conversation_id,
                request.generation or '',
                request.owner_token or '',
                capture_claim_token or '',
            )
        raise HTTPException(
            status_code=409,
            detail="Capture transport is still active or finalization state changed; retry processing",
        )

    processing_fence_held = True
    finalization_heartbeat_stop = threading.Event()
    finalization_heartbeat_lost = threading.Event()
    finalization_heartbeat = None

    if is_capture_v2:

        def _renew_finalization_lease() -> None:
            while not finalization_heartbeat_stop.wait(10):
                if not renew_capture_finalization(
                    uid,
                    conversation_id,
                    request.generation or '',
                    request.owner_token or '',
                    capture_claim_token or '',
                ):
                    finalization_heartbeat_lost.set()
                    return

        finalization_heartbeat = threading.Thread(
            target=_renew_finalization_lease,
            name=f'capture-finalization-{conversation_id}',
            daemon=True,
        )
        finalization_heartbeat.start()

    def _complete_external_integration_effect(conversation: Conversation, dispatched: bool) -> List[Message]:
        if not is_capture_v2:
            return trigger_external_integrations(uid, conversation) if dispatched else []
        if finalization_heartbeat_lost.is_set():
            raise HTTPException(status_code=409, detail="Conversation finalization completion lost its lease")
        effect = claim_capture_finalization_effect(
            uid,
            conversation_id,
            request.generation or '',
            request.owner_token or '',
            capture_claim_token or '',
            'integrations:external',
        )
        effect_outcome = str(effect.get('outcome') or '')
        if effect_outcome == 'completed':
            return [Message(**item) for item in (effect.get('result') or [])]
        if effect_outcome != 'claimed':
            raise HTTPException(status_code=409, detail="Conversation finalization effect lost its lease")
        operation_token = str(effect.get('operation_token') or '')
        if not operation_token:
            raise HTTPException(status_code=409, detail="Conversation finalization effect token is unavailable")
        messages = (
            trigger_external_integrations(uid, conversation, idempotency_key=operation_token) if dispatched else []
        )
        if not complete_capture_finalization_effect(
            uid,
            conversation_id,
            request.generation or '',
            request.owner_token or '',
            capture_claim_token or '',
            'integrations:external',
            operation_token,
            [message.dict() for message in messages],
        ):
            raise HTTPException(status_code=409, detail="Conversation finalization effect completion lost its lease")
        return messages

    try:
        conversation = conversations_db.get_conversation(uid, conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation in progress not found")

        conversation = Conversation(**conversation)

        claim_result = conversations_db.claim_initial_conversation_processing(uid, conversation_id)
        claim_status = str(claim_result.get('status') or '')
        if claim_status == 'capture_in_progress':
            raise HTTPException(
                status_code=409,
                detail="Capture transport acquired the conversation before processing; retry after it closes",
            )
        if claim_status == 'conversation_missing':
            raise HTTPException(status_code=404, detail="Conversation in progress not found")
        if claim_status not in {'processing_claimed', 'already_completed', 'processing_in_progress'}:
            raise RuntimeError(f"conversation processing claim unavailable: {claim_status}")

        processing_fence_held = not redis_db.release_capture_commit_lease(uid, processing_fence_token)

        # Inject calendar context if provided
        if request and request.calendar_meeting_context:
            if not conversation.external_data:
                conversation.external_data = {}
            conversation.external_data['calendar_meeting_context'] = request.calendar_meeting_context.dict()

        # Geolocation
        geolocation = redis_db.get_cached_user_geolocation(uid)
        if geolocation:
            geolocation = Geolocation(**geolocation)
            conversation.geolocation = get_google_maps_location(geolocation.latitude, geolocation.longitude)

        outcome = process_conversation_with_outcome(
            uid,
            conversation.language,
            conversation,
            force_process=True,
            _claim_already_held=claim_status == 'processing_claimed',
            _initial_processing_claim_token=str(claim_result.get('claim_token') or '') or None,
            capture_finalization=(
                (
                    request.generation or '',
                    request.owner_token or '',
                    capture_claim_token or '',
                )
                if is_capture_v2
                else None
            ),
        )
        if outcome.status == 'capture_in_progress':
            raise HTTPException(
                status_code=409,
                detail="Capture transport acquired the conversation before processing; retry after it closes",
            )
        conversation = outcome.conversation
        messages = _complete_external_integration_effect(conversation, outcome.dispatched)

        if is_capture_v2:
            if not complete_capture_finalization(
                uid,
                conversation_id,
                request.generation or '',
                request.owner_token or '',
                capture_claim_token or '',
            ):
                raise HTTPException(status_code=409, detail="Conversation finalization completion lost its lease")
            terminal_conversation = conversations_db.get_conversation(uid, conversation_id)
            if not terminal_conversation or terminal_conversation.get('capture_state') != 'terminal':
                raise HTTPException(status_code=409, detail="Conversation finalization terminal result is unavailable")
            conversation = Conversation(**terminal_conversation)

        return CreateConversationResponse(conversation=conversation, messages=messages)
    except Exception:
        if is_capture_v2 and capture_finalization_claimed:
            release_capture_finalization(
                uid,
                conversation_id,
                request.generation or '',
                request.owner_token or '',
                capture_claim_token or '',
            )
        raise
    finally:
        finalization_heartbeat_stop.set()
        if finalization_heartbeat is not None:
            finalization_heartbeat.join(timeout=2)
        if processing_fence_held:
            redis_db.release_capture_commit_lease(uid, processing_fence_token)


@router.post(
    '/v1/conversations/{conversation_id}/reprocess',
    response_model=Conversation,
    tags=['conversations'],
    dependencies=[Depends(require_current_ai_consent)],
)
def reprocess_conversation(
    conversation_id: str,
    language_code: Optional[str] = None,
    app_id: Optional[str] = None,
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Whenever a user wants to reprocess a conversation, or wants to force process a discarded one
    :param conversation_id: The ID of the conversation to reprocess
    :param language_code: Optional language code to use for processing
    :param app_id: Optional app ID to use for processing (if provided, only this app will be triggered)
    :return: The updated conversation after reprocessing.
    """
    conversation = _get_valid_conversation_by_id(uid, conversation_id)
    conversation = Conversation(**conversation)
    if not language_code:
        language_code = conversation.language or 'en'

    processed_conversation = process_conversation(
        uid, language_code, conversation, force_process=True, is_reprocess=True, app_id=app_id
    )

    return processed_conversation


@router.get('/v1/conversations', response_model=List[Conversation], tags=['conversations'])
def get_conversations(
    limit: int = 100,
    offset: int = 0,
    statuses: Optional[str] = Query(
        "processing,completed",
        description="Comma-separated conversation statuses, including failed for retryable summary failures",
    ),
    include_discarded: bool = True,
    start_date: Optional[datetime] = Query(None, description="Filter by start date (inclusive)"),
    end_date: Optional[datetime] = Query(None, description="Filter by end date (inclusive)"),
    folder_id: Optional[str] = Query(None, description="Filter by folder ID"),
    starred: Optional[bool] = Query(None, description="Filter by starred status"),
    uid: str = Depends(auth.get_current_user_uid),
):
    print('get_conversations', uid, limit, offset, statuses, folder_id, starred)
    # force convos statuses to processing, completed on the empty filter
    if len(statuses) == 0:
        statuses = "processing,completed"

    conversations = conversations_db.get_conversations(
        uid,
        limit,
        offset,
        include_discarded=include_discarded,
        statuses=statuses.split(",") if len(statuses) > 0 else [],
        start_date=start_date,
        end_date=end_date,
        folder_id=folder_id,
        starred=starred,
    )

    for conv in conversations:
        if conv.get('is_locked', False):
            conv['structured']['action_items'] = []
            conv['structured']['events'] = []
            conv['apps_results'] = []
            conv['plugins_results'] = []
            conv['suggested_summarization_apps'] = []
    return conversations


@router.get("/v1/conversations/{conversation_id}", response_model=Conversation, tags=['conversations'])
def get_conversation_by_id(conversation_id: str, uid: str = Depends(auth.get_current_user_uid)):
    print('get_conversation_by_id', uid, conversation_id)
    return _get_valid_conversation_by_id(uid, conversation_id)


@router.patch("/v1/conversations/{conversation_id}/title", tags=['conversations'])
def patch_conversation_title(conversation_id: str, title: str, uid: str = Depends(auth.get_current_user_uid)):
    _get_valid_conversation_by_id(uid, conversation_id)
    conversations_db.update_conversation_title(uid, conversation_id, title)
    return {'status': 'Ok'}


@router.get(
    "/v1/conversations/{conversation_id}/photos", response_model=List[ConversationPhoto], tags=['conversations']
)
def get_conversation_photos(conversation_id: str, uid: str = Depends(auth.get_current_user_uid)):
    _get_valid_conversation_by_id(uid, conversation_id)
    return conversations_db.get_conversation_photos(uid, conversation_id)


@router.get(
    "/v1/conversations/{conversation_id}/transcripts",
    response_model=dict[str, List[TranscriptSegment]],
    tags=['conversations'],
)
def get_conversation_transcripts_by_models(conversation_id: str, uid: str = Depends(auth.get_current_user_uid)):
    _get_valid_conversation_by_id(uid, conversation_id)
    return conversations_db.get_conversation_transcripts_by_model(uid, conversation_id)


@router.delete("/v1/conversations/{conversation_id}", status_code=204, tags=['conversations'])
async def delete_conversation(conversation_id: str, uid: str = Depends(auth.get_current_user_uid)):
    print('delete_conversation', conversation_id, uid)
    await run_in_threadpool(_get_valid_conversation_by_id, uid, conversation_id)
    try:
        await invalidate_deleted_conversation_source(uid, conversation_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "today_card_source_invalidation_failed"}) from exc
    try:
        await run_in_threadpool(conversations_db.delete_conversation, uid, conversation_id)
    except MemoryArtworkStorageError as exc:
        raise HTTPException(status_code=503, detail={"code": str(exc), "retryable": True}) from exc
    await run_in_threadpool(delete_vector, uid, conversation_id)
    return {"status": "Ok"}


@router.get("/v1/conversations/{conversation_id}/recording", response_model=dict, tags=['conversations'])
def conversation_has_audio_recording(conversation_id: str, uid: str = Depends(auth.get_current_user_uid)):
    _get_valid_conversation_by_id(uid, conversation_id)
    return {'has_recording': get_conversation_recording_if_exists(uid, conversation_id) is not None}


@router.patch("/v1/conversations/{conversation_id}/events", response_model=dict, tags=['conversations'])
def set_conversation_events_state(
    conversation_id: str, data: SetConversationEventsStateRequest, uid: str = Depends(auth.get_current_user_uid)
):
    conversation = _get_valid_conversation_by_id(uid, conversation_id)
    conversation = Conversation(**conversation)
    events = conversation.structured.events
    for i, event_idx in enumerate(data.events_idx):
        if event_idx >= len(events):
            continue
        events[event_idx].created = data.values[i]

    conversations_db.update_conversation_events(uid, conversation_id, [event.dict() for event in events])
    return {"status": "Ok"}


@router.patch("/v1/conversations/{conversation_id}/action-items", response_model=dict, tags=['conversations'])
def set_action_item_status(
    data: SetConversationActionItemsStateRequest, conversation_id: str, uid=Depends(auth.get_current_user_uid)
):
    conversation = _get_valid_conversation_by_id(uid, conversation_id)
    conversation = Conversation(**conversation)
    action_items = conversation.structured.action_items
    for i, action_item_idx in enumerate(data.items_idx):
        if action_item_idx >= len(action_items):
            continue

        action_item = action_items[action_item_idx]
        new_completed_status = data.values[i]

        # Set completed status
        action_item.completed = new_completed_status

        # Handle created_at backwards compatibility
        if action_item.created_at is None:
            action_item.created_at = conversation.created_at

        # Set completed_at timestamp
        if new_completed_status:
            # Mark as completed - set completed_at to current time
            action_item.completed_at = datetime.now(timezone.utc)
        else:
            # Mark as incomplete - clear completed_at
            action_item.completed_at = None

    conversations_db.update_conversation_action_items(
        uid, conversation_id, [action_item.dict() for action_item in action_items]
    )

    # Mirror status updates to the standalone action_items collection
    try:
        existing_items = action_items_db.get_action_items_by_conversation(uid, conversation_id)
        # Map descriptions to item IDs for quick lookup
        description_to_ids = {}
        for ai in existing_items:
            desc = ai.get('description')
            if not desc:
                continue
            description_to_ids.setdefault(desc, []).append(ai['id'])

        for i, action_item_idx in enumerate(data.items_idx):
            if action_item_idx >= len(action_items):
                continue
            action_item = action_items[action_item_idx]
            new_completed_status = data.values[i]

            ids = description_to_ids.get(action_item.description, [])
            for action_item_id in ids:
                action_items_db.mark_action_item_completed(uid, action_item_id, bool(new_completed_status))
    except Exception as e:
        # Don't break conversation route if mirrored update fails
        print('Failed to mirror action item status update:', e)
    return {"status": "Ok"}


@router.patch(
    "/v1/conversations/{conversation_id}/action-items/{action_item_idx}", response_model=dict, tags=['conversations']
)
def update_action_item_description(
    conversation_id: str, data: UpdateActionItemDescriptionRequest, uid=Depends(auth.get_current_user_uid)
):
    conversation = _get_valid_conversation_by_id(uid, conversation_id)
    conversation = Conversation(**conversation)
    action_items = conversation.structured.action_items

    found_item = False
    for item in action_items:
        if item.description == data.old_description:
            item.description = data.description
            found_item = True
            break

    if not found_item:
        raise HTTPException(status_code=404, detail=f"Action item with description '{data.old_description}' not found")

    conversations_db.update_conversation_action_items(
        uid, conversation_id, [action_item.dict() for action_item in action_items]
    )

    # Mirror description update in the standalone action_items collection
    try:
        existing_items = action_items_db.get_action_items_by_conversation(uid, conversation_id)
        for ai in existing_items:
            if ai.get('description') == data.old_description:
                action_items_db.update_action_item(uid, ai['id'], {'description': data.description})
    except Exception as e:
        print('Failed to mirror action item description update:', e)
    return {"status": "Ok"}


@router.delete("/v1/conversations/{conversation_id}/action-items", response_model=dict, tags=['conversations'])
def delete_action_item(data: DeleteActionItemRequest, conversation_id: str, uid=Depends(auth.get_current_user_uid)):
    conversation = _get_valid_conversation_by_id(uid, conversation_id)
    conversation = Conversation(**conversation)
    action_items = conversation.structured.action_items
    updated_action_items = [item for item in action_items if not (item.description == data.description)]
    conversations_db.update_conversation_action_items(
        uid, conversation_id, [action_item.dict() for action_item in updated_action_items]
    )

    # Mirror deletion in the standalone action_items collection
    try:
        existing_items = action_items_db.get_action_items_by_conversation(uid, conversation_id)
        for ai in existing_items:
            if ai.get('description') == data.description:
                action_items_db.delete_action_item(uid, ai['id'])
    except Exception as e:
        print('Failed to mirror action item deletion:', e)
    return {"status": "Ok"}


@router.patch(
    '/v1/conversations/{conversation_id}/segments/{segment_idx}/assign',
    response_model=Conversation,
    tags=['conversations'],
)
def set_assignee_conversation_segment(
    conversation_id: str,
    segment_idx: int,
    assign_type: str,
    value: Optional[str] = None,
    use_for_speech_training: bool = True,
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Another complex endpoint.

    Modify the assignee of a segment in the transcript of a conversation.
    But,
    if `use_for_speech_training` is True, the corresponding audio segment will be used for speech training.

    Speech training of whom?

    If `assign_type` is 'is_user', the segment will be used for the user speech training.
    If `assign_type` is 'person_id', the segment will be used for the person with the given id speech training.

    What is required for a segment to be used for speech training?
    1. The segment must have more than 5 words.
    2. The conversation audio file shuold be already stored in the user's bucket.

    :return: The updated conversation.
    """
    print(
        'set_assignee_conversation_segment',
        conversation_id,
        segment_idx,
        assign_type,
        value,
        use_for_speech_training,
        uid,
    )
    conversation = _get_valid_conversation_by_id(uid, conversation_id)
    conversation = Conversation(**conversation)

    if value == 'null':
        value = None

    is_unassigning = value is None or value is False

    if assign_type == 'is_user':
        conversation.transcript_segments[segment_idx].is_user = bool(value) if value is not None else False
        conversation.transcript_segments[segment_idx].person_id = None
    elif assign_type == 'person_id':
        conversation.transcript_segments[segment_idx].is_user = False
        conversation.transcript_segments[segment_idx].person_id = value
    else:
        print(assign_type)
        raise HTTPException(status_code=400, detail="Invalid assign type")

    conversations_db.update_conversation_segments(
        uid, conversation_id, [segment.dict() for segment in conversation.transcript_segments]
    )
    # thinh's note: disabled for now
    # segment_words = len(conversation.transcript_segments[segment_idx].text.split(' '))
    # # TODO: can do this async
    # if use_for_speech_training and not is_unassigning and segment_words > 5:  # some decent sample at least
    #     person_id = value if assign_type == 'person_id' else None
    #     expand_speech_profile(conversation_id, uid, segment_idx, assign_type, person_id)
    # else:
    #     path = f'{conversation_id}_segment_{segment_idx}.wav'
    #     delete_additional_profile_audio(uid, path)
    #     delete_speech_sample_for_people(uid, path)

    return conversation


@router.patch(
    '/v1/conversations/{conversation_id}/assign-speaker/{speaker_id}',
    response_model=Conversation,
    tags=['conversations'],
)
def set_assignee_conversation_segment(
    conversation_id: str,
    speaker_id: int,
    assign_type: str,
    value: Optional[str] = None,
    use_for_speech_training: bool = True,
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Another complex endpoint.

    Modify the assignee of all segments in the transcript of a conversation with the given speaker_id.
    But,
    if `use_for_speech_training` is True, the corresponding audio segment will be used for speech training.

    Speech training of whom?

    If `assign_type` is 'is_user', the segment will be used for the user speech training.
    If `assign_type` is 'person_id', the segment will be used for the person with the given id speech training.

    What is required for a segment to be used for speech training?
    1. The segment must have more than 5 words.
    2. The conversation audio file should be already stored in the user's bucket.

    :return: The updated conversation.
    """
    print(
        'set_assignee_conversation_segment',
        conversation_id,
        speaker_id,
        assign_type,
        value,
        use_for_speech_training,
        uid,
    )
    conversation = _get_valid_conversation_by_id(uid, conversation_id)
    conversation = Conversation(**conversation)

    if value == 'null':
        value = None

    is_unassigning = value is None or value is False

    if assign_type == 'is_user':
        for segment in conversation.transcript_segments:
            if segment.speaker_id == speaker_id:
                segment.is_user = bool(value) if value is not None else False
                segment.person_id = None
    elif assign_type == 'person_id':
        for segment in conversation.transcript_segments:
            if segment.speaker_id == speaker_id:
                print(segment.speaker_id, speaker_id, value)
                segment.is_user = False
                segment.person_id = value
    else:
        print(assign_type)
        raise HTTPException(status_code=400, detail="Invalid assign type")

    conversations_db.update_conversation_segments(
        uid, conversation_id, [segment.dict() for segment in conversation.transcript_segments]
    )
    # This will be used when we setup recording for conversations, not used for now
    # get the segment with the most words with the speaker_id
    # segment_idx = 0
    # segment_words = 0
    # for segment in conversation.transcript_segments:
    #     if segment.speaker == speaker_id:
    #         if len(segment.text.split(' ')) > segment_words:
    #             segment_words = len(segment.text.split(' '))
    #             if segment_words > 5:
    #                 segment_idx = segment.idx
    #
    # if use_for_speech_training and not is_unassigning and segment_words > 5:  # some decent sample at least
    #     person_id = value if assign_type == 'person_id' else None
    #     expand_speech_profile(conversation_id, uid, segment_idx, assign_type, person_id)
    # else:
    #     path = f'{conversation_id}_segment_{segment_idx}.wav'
    #     delete_additional_profile_audio(uid, path)
    #     delete_speech_sample_for_people(uid, path)

    return conversation


@router.patch(
    '/v1/conversations/{conversation_id}/segments/assign-bulk',
    response_model=Conversation,
    tags=['conversations'],
)
def assign_segments_bulk(
    conversation_id: str,
    data: BulkAssignSegmentsRequest,
    background_tasks: BackgroundTasks,
    uid: str = Depends(auth.get_current_user_uid),
):
    conversation = _get_valid_conversation_by_id(uid, conversation_id)
    conversation = Conversation(**conversation)

    value = data.value
    if value == 'null':
        value = None

    segment_map = {segment.id: segment for segment in conversation.transcript_segments}

    for segment_id in data.segment_ids:
        if segment_id in segment_map:
            segment = segment_map[segment_id]
            if data.assign_type == 'is_user':
                segment.is_user = bool(value) if value is not None else False
                segment.person_id = None
            elif data.assign_type == 'person_id':
                segment.is_user = False
                segment.person_id = value
            else:
                raise HTTPException(status_code=400, detail="Invalid assign type")

    conversations_db.update_conversation_segments(
        uid, conversation_id, [segment.dict() for segment in conversation.transcript_segments]
    )

    # Trigger speaker sample extraction when assigning to a person
    if data.assign_type == 'person_id' and value:
        background_tasks.add_task(
            extract_speaker_samples,
            uid=uid,
            person_id=value,
            conversation_id=conversation_id,
            segment_ids=data.segment_ids,
        )

    return conversation


# *********************************************
# *********** SHARING conversations ***********
# *********************************************


@router.patch('/v1/conversations/{conversation_id}/visibility', tags=['conversations'])
def set_conversation_visibility(
    conversation_id: str, value: ConversationVisibility, uid: str = Depends(auth.get_current_user_uid)
):
    print('update_conversation_visibility', conversation_id, value, uid)
    _get_valid_conversation_by_id(uid, conversation_id)
    conversations_db.set_conversation_visibility(uid, conversation_id, value)
    if value == ConversationVisibility.private:
        redis_db.remove_conversation_to_uid(conversation_id)
        redis_db.remove_public_conversation(conversation_id)
    else:
        redis_db.store_conversation_to_uid(conversation_id, uid)
        redis_db.add_public_conversation(conversation_id)

    return {"status": "Ok"}


@router.patch('/v1/conversations/{conversation_id}/starred', tags=['conversations'])
def set_conversation_starred(conversation_id: str, starred: bool, uid: str = Depends(auth.get_current_user_uid)):
    print('update_conversation_starred', conversation_id, starred, uid)
    _get_valid_conversation_by_id(uid, conversation_id)
    conversations_db.set_conversation_starred(uid, conversation_id, starred)
    return {"status": "Ok"}


@router.get("/v1/conversations/{conversation_id}/shared", tags=['conversations'])
def get_shared_conversation_by_id(conversation_id: str):
    uid = redis_db.get_conversation_uid(conversation_id)
    if not uid:
        raise HTTPException(status_code=404, detail="Conversation is private")

    conversation = _get_valid_conversation_by_id(uid, conversation_id)
    visibility = conversation.get('visibility', ConversationVisibility.private)
    if not visibility or visibility == ConversationVisibility.private:
        raise HTTPException(status_code=404, detail="Conversation is private")
    conversation = Conversation(**conversation)
    conversation.geolocation = None

    # Fetch people data for speaker names
    person_ids = conversation.get_person_ids()
    people = []
    if person_ids:
        people_data = users_db.get_people_by_ids(uid, person_ids)
        people = [Person(**p) for p in people_data]

    # Return conversation with people data
    response_dict = conversation.as_dict_cleaned_dates()
    response_dict['people'] = [p.dict() for p in people]
    return response_dict


@router.get("/v1/public-conversations", response_model=List[Conversation], tags=['conversations'])
def get_public_conversations(offset: int = 0, limit: int = 1000):
    conversations = redis_db.get_public_conversations()
    data = []

    conversation_uids = redis_db.get_conversation_uids(conversations)

    data = [[uid, conversation_id] for conversation_id, uid in conversation_uids.items() if uid]
    # TODO: sort in some way to have proper pagination

    conversations = conversations_db.get_public_conversations(data[offset : offset + limit])
    for conversation in conversations:
        conversation['geolocation'] = None
    return conversations


@router.post("/v1/conversations/search", response_model=dict, tags=['conversations'])
def search_conversations_endpoint(search_request: SearchRequest, uid: str = Depends(auth.get_current_user_uid)):
    # Convert ISO datetime strings to Unix timestamps if provided
    start_timestamp = None
    end_timestamp = None

    if search_request.start_date:
        start_timestamp = int(datetime.fromisoformat(search_request.start_date).timestamp())

    if search_request.end_date:
        end_timestamp = int(datetime.fromisoformat(search_request.end_date).timestamp())

    return search_conversations(
        query=search_request.query,
        page=search_request.page,
        per_page=search_request.per_page,
        uid=uid,
        include_discarded=search_request.include_discarded,
        start_date=start_timestamp,
        end_date=end_timestamp,
    )


@router.get("/v1/conversations/{conversation_id}/suggested-apps", response_model=dict, tags=['conversations'])
def get_conversation_suggested_apps(conversation_id: str, uid: str = Depends(auth.get_current_user_uid)):
    from utils.apps import get_available_apps, get_available_app_by_id_with_reviews
    from models.app import App

    conversation_data = _get_valid_conversation_by_id(uid, conversation_id)
    conversation = Conversation(**conversation_data)

    # Get suggested app models with full data (similar to /v1/apps endpoint)
    suggested_apps = []
    for app_id in conversation.suggested_summarization_apps:
        app_data = get_available_app_by_id_with_reviews(app_id, uid)
        if app_data:
            app = App(**app_data)
            # Add user-specific data
            from utils.apps import get_is_user_paid_app

            app.is_user_paid = get_is_user_paid_app(app.id, uid)

            # Add payment link with user reference
            if app.payment_link:
                app.payment_link = f'{app.payment_link}?client_reference_id=uid_{uid}'

            # Generate thumbnail URLs if thumbnails exist
            if app.thumbnails:
                from utils.other.storage import get_app_thumbnail_url

                app.thumbnail_urls = [get_app_thumbnail_url(thumbnail_id) for thumbnail_id in app.thumbnails]

            suggested_apps.append(app)

    return {"suggested_apps": [app.dict() for app in suggested_apps], "conversation_id": conversation_id}


@router.post(
    "/v1/conversations/{conversation_id}/test-prompt",
    response_model=dict,
    tags=['conversations'],
    dependencies=[Depends(require_current_ai_consent)],
)
def test_prompt(conversation_id: str, request: TestPromptRequest, uid: str = Depends(auth.get_current_user_uid)):
    conversation_data = _get_valid_conversation_by_id(uid, conversation_id)
    conversation = Conversation(**conversation_data)

    full_transcript = "\n".join([seg.text for seg in conversation.transcript_segments if seg.text])

    if not full_transcript:
        raise HTTPException(status_code=400, detail="Conversation has no text content to summarize.")

    # Pass language code from conversation to match app behavior
    summary = generate_summary_with_prompt(full_transcript, request.prompt, language_code=conversation.language or 'en')

    return {"summary": summary}


# *********************************************
# *********** MERGING conversations ***********
# *********************************************


@router.post(
    '/v1/conversations/merge',
    response_model=MergeConversationsResponse,
    tags=['conversations'],
    dependencies=[Depends(require_current_ai_consent)],
)
async def merge_conversations(
    request: MergeConversationsRequest,
    background_tasks: BackgroundTasks,
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Merge multiple conversations into a new conversation (async).

    Flow:
    1. Validates conversations (locked? completed?)
    2. Returns immediately with 200 OK
    3. Background task creates new merged conversation
    4. Background task deletes source conversations
    5. FCM notification sent on completion

    The merged conversation will have:
    - A new ID (source conversations are deleted)
    - Merged transcript segments with adjusted timestamps
    - Copied audio chunks
    - Regenerated title, summary, action items, memories via process_conversation()
    """
    from utils.conversations.merge_conversations import validate_merge_compatibility, perform_merge_async

    # Validate minimum number of conversations
    if len(request.conversation_ids) < 2:
        raise HTTPException(status_code=400, detail="At least 2 conversations required to merge")

    # Fetch all conversations
    conversations = []
    for conv_id in request.conversation_ids:
        conv = conversations_db.get_conversation(uid, conv_id)
        if conv is None:
            raise HTTPException(status_code=404, detail=f"Conversation {conv_id} not found")
        conversations.append(conv)

    # Validate merge compatibility (returns warning for large gaps but doesn't reject)
    is_valid, error_message, warning_message = validate_merge_compatibility(conversations)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_message)

    # Set all source conversations to 'merging' status so user knows they're being processed
    for conv_id in request.conversation_ids:
        conversations_db.update_conversation_status(uid, conv_id, ConversationStatus.merging)

    # Start background merge task
    background_tasks.add_task(
        perform_merge_async,
        uid=uid,
        conversation_ids=request.conversation_ids,
        reprocess=request.reprocess,
    )

    return MergeConversationsResponse(
        status="merging",
        message="Merge started",
        warning=warning_message,
        conversation_ids=request.conversation_ids,
    )
