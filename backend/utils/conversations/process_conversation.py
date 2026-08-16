import os
import random
import re
import threading
import uuid
import logging
import asyncio
from datetime import timezone, timedelta, datetime
from types import SimpleNamespace
from typing import Any, Callable, Union, Tuple, List, Optional

from fastapi import HTTPException

from database import redis_db
import database.memories as memories_db
import database.conversations as conversations_db
import database.notifications as notification_db
import database.users as users_db
import database.tasks as tasks_db
import database.trends as trends_db
import database.action_items as action_items_db
import database.folders as folders_db
import database.calendar_meetings as calendar_db
from database.vector_db import find_similar_memories, upsert_memory_vector, delete_memory_vector
from utils.llm.memories import resolve_memory_conflict
from database.apps import record_app_usage, get_omi_personas_by_uid_db, get_app_by_id_db
from models.app import App, UsageHistoryType
from models.memories import MemoryDB, Memory
from models.conversation import *
from models.conversation import (
    ExternalIntegrationCreateConversation,
    Conversation,
    CreateConversation,
    ConversationSource,
)
from utils.notifications import send_important_conversation_message
from models.conversation import CalendarMeetingContext
from models.other import Person
from models.task import Task, TaskStatus, TaskAction, TaskActionProvider
from models.trend import Trend
from models.notification_message import NotificationMessage
from utils.apps import get_available_apps, update_personas_async, sync_update_persona_prompt
from utils.llm.conversation_processing import (
    get_transcript_structure,
    get_app_result,
    should_discard_conversation,
    select_best_app_for_conversation,
    get_suggested_apps_for_conversation,
    get_reprocess_transcript_structure,
    assign_conversation_to_folder,
)
from utils.analytics import record_usage
from utils.llm.memories import extract_memories_from_text, new_memories_extractor
from utils.llm.external_integrations import summarize_experience_text
from utils.llm.trends import trends_extractor
from utils.llm.goals import extract_and_update_goal_progress
from utils.llm.chat import (
    obtain_emotional_message,
)
from utils.llm.external_integrations import get_message_structure
from utils.notifications import send_notification
from utils.other.hume import get_hume, HumeJobCallbackModel, HumeJobModelPredictionResponseModel
from utils.retrieval.rag import retrieve_rag_conversation_context
from utils.webhooks import conversation_created_webhook

# ====== ELLA POST-PROCESS HOOK IMPORT ======
try:
    from utils.ella.postprocess import fire_postprocess_webhook
except ImportError:
    fire_postprocess_webhook = None
# ====== END ELLA IMPORT ======
from utils.notifications import send_action_item_data_message
from utils.task_sync import auto_sync_action_items_batch
from utils.other.storage import precache_conversation_audio
from utils.conversations.failure_state import (
    CONVERSATION_PROCESSING_FAILED,
    apply_conversation_processing_failed,
    clear_conversation_processing_error,
)
from utils.conversations.vector import save_structured_vector

SideEffectGuard = Optional[Callable[[str], None]]


class CaptureFinalizationLeaseLost(RuntimeError):
    pass


class CaptureFinalizationEffectRunner:
    """Durable per-effect fencing for capture finalization side effects."""

    def __init__(
        self,
        uid: str,
        conversation_id: str,
        capture_finalization: Tuple[str, str, str],
    ):
        self.uid = uid
        self.conversation_id = conversation_id
        self.generation, self.owner_token, self.claim_token = capture_finalization

    def operation_token(self, effect_id: str) -> str:
        return conversations_db.capture_finalization_effect_operation_token(
            self.conversation_id,
            self.generation,
            self.owner_token,
            effect_id,
        )

    def run(
        self,
        effect_id: str,
        operation: Callable[[str], Any],
        *,
        encode: Callable[[Any], Any] = lambda value: value,
        decode: Callable[[Any], Any] = lambda value: value,
    ) -> Any:
        claim = conversations_db.claim_capture_finalization_effect(
            self.uid,
            self.conversation_id,
            self.generation,
            self.owner_token,
            self.claim_token,
            effect_id,
        )
        if claim.get('outcome') == 'completed':
            return decode(claim.get('result'))
        if claim.get('outcome') != 'claimed':
            raise CaptureFinalizationLeaseLost(f'Capture finalization lease was lost before {effect_id}')

        operation_token = claim['operation_token']
        result = operation(operation_token)
        if not conversations_db.complete_capture_finalization_effect(
            self.uid,
            self.conversation_id,
            self.generation,
            self.owner_token,
            self.claim_token,
            effect_id,
            operation_token,
            encode(result),
        ):
            raise CaptureFinalizationLeaseLost(f'Capture finalization lease was lost while completing {effect_id}')
        return result


def _guard_side_effect(guard: SideEffectGuard, boundary: str) -> None:
    if guard:
        guard(boundary)


def _run_side_effect(
    runner: Optional[CaptureFinalizationEffectRunner],
    effect_id: str,
    operation: Callable[[str], Any],
    *,
    encode: Callable[[Any], Any] = lambda value: value,
    decode: Callable[[Any], Any] = lambda value: value,
) -> Any:
    if runner:
        return runner.run(effect_id, operation, encode=encode, decode=decode)
    return operation('')


def _get_structured(
    uid: str,
    language_code: str,
    conversation: Union[Conversation, CreateConversation, ExternalIntegrationCreateConversation],
    force_process: bool = False,
    people: List[Person] = None,
) -> Tuple[Structured, bool]:
    try:
        tz = notification_db.get_user_time_zone(uid)

        # Fetch existing action items from past 2 days for deduplication
        existing_action_items = None
        try:
            two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
            existing_action_items = action_items_db.get_action_items(uid=uid, start_date=two_days_ago, limit=50)
        except Exception as e:
            print(f"Error fetching existing action items for deduplication: {e}")

        # Extract calendar context from external_data
        calendar_context = None
        if hasattr(conversation, 'external_data') and conversation.external_data:
            calendar_data = conversation.external_data.get('calendar_meeting_context')
            if calendar_data:
                calendar_context = CalendarMeetingContext(**calendar_data)

        if (
            conversation.source == ConversationSource.workflow
            or conversation.source == ConversationSource.external_integration
        ):
            if conversation.text_source == ExternalIntegrationConversationSource.audio:
                structured = get_transcript_structure(
                    conversation.text,
                    conversation.started_at,
                    language_code,
                    tz,
                    existing_action_items=existing_action_items,
                    calendar_meeting_context=calendar_context,
                    uid=uid,
                )
                return structured, False

            if conversation.text_source == ExternalIntegrationConversationSource.message:
                structured = get_message_structure(
                    conversation.text, conversation.started_at, language_code, tz, conversation.text_source_spec
                )
                return structured, False

            if conversation.text_source == ExternalIntegrationConversationSource.other:
                structured = summarize_experience_text(conversation.text, conversation.text_source_spec)
                return structured, False

            # not supported conversation source
            raise HTTPException(status_code=400, detail=f'Invalid conversation source: {conversation.text_source}')

        transcript_text = conversation.get_transcript(False, people=people)

        # For re-processing, we don't discard, just re-structure.
        if force_process:
            # reprocess endpoint
            return (
                get_reprocess_transcript_structure(
                    transcript_text,
                    conversation.started_at,
                    language_code,
                    tz,
                    conversation.structured.title,
                    photos=conversation.photos,
                    existing_action_items=existing_action_items,
                ),
                False,
            )

        # Determine whether to discard the conversation based on its content (transcript and/or photos).
        print(
            f"[FLOW:PROCESS] checking discard uid={uid} transcript_len={len(transcript_text) if transcript_text else 0}",
            flush=True,
        )
        discarded = should_discard_conversation(transcript_text, conversation.photos)
        if discarded:
            print(f"[FLOW:PROCESS] DISCARDED uid={uid}", flush=True)
            return Structured(emoji=random.choice(['🧠', '🎉'])), True

        # If not discarded, proceed to generate the structured summary from transcript and/or photos.
        return (
            get_transcript_structure(
                transcript_text,
                conversation.started_at,
                language_code,
                tz,
                photos=conversation.photos,
                existing_action_items=existing_action_items,
                calendar_meeting_context=calendar_context,
                uid=uid,
                existing_conversation_id=conversation.id if hasattr(conversation, 'id') else None,
            ),
            False,
        )
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail="Error processing conversation, please try again later")


def _get_conversation_obj(
    uid: str,
    structured: Structured,
    conversation: Union[Conversation, CreateConversation, ExternalIntegrationCreateConversation],
    discarded: bool,
):
    if isinstance(conversation, CreateConversation):
        conversation_dict = conversation.dict()
        # Store calendar context in external_data if available
        calendar_context = conversation_dict.pop('calendar_meeting_context', None)

        # Use started_at as created_at for imported conversations to preserve original timestamp
        created_at = conversation.started_at if conversation.started_at else datetime.now(timezone.utc)
        conversation = Conversation(
            id=str(uuid.uuid4()),
            uid=uid,
            structured=structured,
            created_at=created_at,
            discarded=discarded,
            **conversation_dict,
        )

        # Add calendar metadata to external_data
        if calendar_context:
            if not conversation.external_data:
                conversation.external_data = {}
            conversation.external_data['calendar_meeting_context'] = calendar_context

        if conversation.photos:
            conversations_db.store_conversation_photos(uid, conversation.id, conversation.photos)
    elif isinstance(conversation, ExternalIntegrationCreateConversation):
        create_conversation = conversation
        # Use started_at as created_at for external integrations to preserve original timestamp
        created_at = conversation.started_at if conversation.started_at else datetime.now(timezone.utc)
        conversation = Conversation(
            id=str(uuid.uuid4()),
            **conversation.dict(),
            created_at=created_at,
            structured=structured,
            discarded=discarded,
        )
        conversation.external_data = create_conversation.dict()
        conversation.app_id = create_conversation.app_id
    else:
        conversation.structured = structured
        conversation.discarded = discarded

    return conversation


def mark_conversation_processing_failed(
    uid: str,
    conversation: Conversation,
    error_code: str = "conversation_summary_failed",
    capture_finalization: Optional[Tuple[str, str, str]] = None,
):
    apply_conversation_processing_failed(conversation, error_code=error_code)
    if capture_finalization:
        generation, owner_token, claim_token = capture_finalization
        if not conversations_db.upsert_conversation_if_capture_finalizer(
            uid,
            conversation.dict(),
            generation,
            owner_token,
            claim_token,
        ):
            raise RuntimeError('Capture finalization lease was lost before failure persistence')
    else:
        conversations_db.upsert_conversation(uid, conversation.dict())


def mark_unexpected_conversation_processing_failed(
    uid: str,
    conversation: Conversation,
    capture_finalization: Optional[Tuple[str, str, str]] = None,
) -> bool:
    if conversation.status == ConversationStatus.completed:
        return False
    if conversation.status == ConversationStatus.failed and conversation.processing_error:
        return False
    mark_conversation_processing_failed(
        uid,
        conversation,
        error_code=CONVERSATION_PROCESSING_FAILED,
        capture_finalization=capture_finalization,
    )
    return True


# Function to get conversation summary apps from Redis
def get_default_conversation_summarized_apps():
    """
    Get conversation summary apps from Redis.
    Falls back to environment variable if Redis is empty.
    """
    default_apps = []

    # Try to get from Redis first
    redis_app_ids = redis_db.get_conversation_summary_app_ids()

    if redis_app_ids:
        # Use apps from Redis
        for app_id in redis_app_ids:
            app_data = get_app_by_id_db(app_id.strip())
            if app_data:
                default_apps.append(App(**app_data))
    else:
        # Fallback to environment variable for backward compatibility
        env_app_ids = os.getenv(
            'CONVERSATION_SUMMARIZED_APP_IDS', 'summary_assistant,action_item_extractor,insight_analyzer'
        ).split(',')

        for app_id in env_app_ids:
            app_data = get_app_by_id_db(app_id.strip())
            if app_data:
                default_apps.append(App(**app_data))

    return default_apps


def _trigger_apps(
    uid: str,
    conversation: Conversation,
    is_reprocess: bool = False,
    app_id: Optional[str] = None,
    language_code: str = 'en',
    people: List[Person] = None,
    side_effect_guard: SideEffectGuard = None,
    effect_runner: Optional[CaptureFinalizationEffectRunner] = None,
):
    # Get default apps for auto-selection
    default_apps = get_default_conversation_summarized_apps()
    default_apps_dict = {app.id: app for app in default_apps}

    # Also get user's installed apps (only used for preferred app lookup and reprocessing)
    apps: List[App] = get_available_apps(uid)
    conversation_apps = [app for app in apps if app.works_with_memories() and app.enabled]

    # Combined dict for looking up preferred apps or specific app_id requests
    all_apps_dict = {app.id: app for app in conversation_apps}
    all_apps_dict.update(default_apps_dict)

    # Combined list for suggestions: default apps + user's installed apps (no duplicates)
    all_suggestion_apps = list(all_apps_dict.values())

    app_to_run = None

    # Always generate/update suggestions if not already set (even during reprocessing)
    if not conversation.suggested_summarization_apps:
        suggested_apps, reasoning = get_suggested_apps_for_conversation(conversation, all_suggestion_apps)
        conversation.suggested_summarization_apps = suggested_apps
        print(f"Generated suggested apps for conversation {conversation.id}: {suggested_apps}")

    # If a specific app_id is provided (for reprocessing), find and use it.
    if app_id:
        app_to_run = all_apps_dict.get(app_id)
    else:
        # Check if user has a preferred app set
        preferred_app_id = redis_db.get_user_preferred_app(uid)
        if preferred_app_id and preferred_app_id in all_apps_dict:
            app_to_run = all_apps_dict.get(preferred_app_id)
            print(f"Using user's preferred app: {app_to_run.name} (id: {preferred_app_id})")
        elif conversation.suggested_summarization_apps:
            # Use the first suggested app if available
            first_suggested_app_id = conversation.suggested_summarization_apps[0]
            app_to_run = all_apps_dict.get(first_suggested_app_id)
            if app_to_run:
                print(f"Using first suggested app: {app_to_run.name}")
            else:
                print(f"First suggested app '{first_suggested_app_id}' not found in apps.")

    filtered_apps = [app_to_run] if app_to_run else []

    if not filtered_apps:
        print(f"No summarization app selected for conversation {conversation.id}", uid)

    # Clear existing app results
    conversation.apps_results = []

    def execute_app(app):
        result = _run_side_effect(
            effect_runner,
            f'app:{app.id}:invoke',
            lambda _: get_app_result(
                conversation.get_transcript(False, people=people),
                conversation.photos,
                app,
                language_code=language_code,
            ).strip(),
        )
        _guard_side_effect(side_effect_guard, f'app:{app.id}:result')
        conversation.apps_results.append(AppResult(app_id=app.id, content=result))
        if not is_reprocess:
            _run_side_effect(
                effect_runner,
                f'app:{app.id}:usage',
                lambda _: record_app_usage(
                    uid,
                    app.id,
                    UsageHistoryType.memory_created_prompt,
                    conversation_id=conversation.id,
                ),
            )

    if side_effect_guard:
        for app in filtered_apps:
            execute_app(app)
    else:
        threads = [threading.Thread(target=execute_app, args=(app,)) for app in filtered_apps]
        [t.start() for t in threads]
        [t.join() for t in threads]


def _update_goal_progress(
    uid: str,
    conversation: Conversation,
    side_effect_guard: SideEffectGuard = None,
    effect_runner: Optional[CaptureFinalizationEffectRunner] = None,
):
    """Extract and update goal progress from conversation text."""
    try:
        # Get conversation text
        text = ""
        if conversation.structured and conversation.structured.overview:
            text = conversation.structured.overview
        elif conversation.transcript_segments:
            text = " ".join([s.text for s in conversation.transcript_segments[:20]])

        if not text or len(text) < 10:
            return

        # Use utility function to extract and update goal progress
        _run_side_effect(effect_runner, 'goals:update', lambda _: extract_and_update_goal_progress(uid, text))
    except Exception as e:
        if isinstance(e, CaptureFinalizationLeaseLost):
            raise
        print(f"[GOAL] Error updating progress: {e}")


def _extract_memories(
    uid: str,
    conversation: Conversation,
    side_effect_guard: SideEffectGuard = None,
    effect_runner: Optional[CaptureFinalizationEffectRunner] = None,
):
    # Delete old memories for this conversation (if reprocessing)
    # Also get the IDs to delete from Pinecone
    existing_memory_ids = memories_db.get_memory_ids_for_conversation(uid, conversation.id)
    if not effect_runner:
        for memory_id in existing_memory_ids:
            delete_memory_vector(uid, memory_id)
        memories_db.delete_memories_for_conversation(uid, conversation.id)

    new_memories: List[Memory] = []

    # Extract memories based on conversation source
    if conversation.source == ConversationSource.external_integration:
        text_content = conversation.external_data.get('text')
        if text_content and len(text_content) > 0:
            text_source = conversation.external_data.get('text_source', 'other')
            new_memories = _run_side_effect(
                effect_runner,
                'memories:extract_external',
                lambda _: extract_memories_from_text(uid, text_content, text_source),
                encode=lambda memories: [memory.dict() for memory in memories],
                decode=lambda memories: [Memory(**memory) for memory in (memories or [])],
            )
    else:
        # For regular conversations with transcript segments
        new_memories = _run_side_effect(
            effect_runner,
            'memories:extract',
            lambda _: new_memories_extractor(uid, conversation.transcript_segments),
            encode=lambda memories: [memory.dict() for memory in memories],
            decode=lambda memories: [Memory(**memory) for memory in (memories or [])],
        )

    is_locked = conversation.is_locked
    parsed_memories = []
    memories_to_delete = []

    for memory_index, memory in enumerate(new_memories):
        # Find similar existing memories
        _guard_side_effect(side_effect_guard, 'memories:similarity_search')
        similar_matches = find_similar_memories(uid, memory.content, threshold=0.7, limit=3)

        # Fetch content for each similar memory
        similar_memories = []
        for match in similar_matches:
            memory_data = memories_db.get_memory(uid, match['memory_id'])
            if memory_data:
                similar_memories.append(
                    {
                        'memory_id': match['memory_id'],
                        'category': match['category'],
                        'score': match['score'],
                        'content': memory_data.get('content', ''),
                    }
                )

        if similar_memories:
            resolution = _run_side_effect(
                effect_runner,
                f'memories:resolve_conflict:{memory_index}',
                lambda _: resolve_memory_conflict(memory.content, similar_memories),
                encode=lambda value: value.dict(),
                decode=lambda value: SimpleNamespace(**value),
            )

            if resolution.action == 'keep_existing':
                continue

            elif resolution.action == 'merge':
                # Replace existing memory with merged version
                if resolution.merged_content:
                    memories_to_delete.append(similar_memories[0]['memory_id'])
                    memory.content = resolution.merged_content

            elif resolution.action == 'keep_both':
                pass

        memory_db_obj = MemoryDB.from_memory(memory, uid, conversation.id, False)
        if effect_runner:
            memory_db_obj.id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f'omi:capture-memory:{uid}:{conversation.id}:{memory_index}:{memory.content}',
                )
            )
        memory_db_obj.is_locked = is_locked
        parsed_memories.append(memory_db_obj)

    for memory_id in memories_to_delete:
        _run_side_effect(
            effect_runner,
            f'memories:merge_delete_vector:{memory_id}',
            lambda _, current_memory_id=memory_id: delete_memory_vector(uid, current_memory_id),
        )
        _run_side_effect(
            effect_runner,
            f'memories:merge_delete:{memory_id}',
            lambda _, current_memory_id=memory_id: memories_db.delete_memory(uid, current_memory_id),
        )

    if effect_runner:
        replacement_ids = {memory.id for memory in parsed_memories}
        for memory_id in existing_memory_ids:
            if memory_id in replacement_ids:
                continue
            _run_side_effect(
                effect_runner,
                f'memories:delete_vector:{memory_id}',
                lambda _, current_memory_id=memory_id: delete_memory_vector(uid, current_memory_id),
            )
            _run_side_effect(
                effect_runner,
                f'memories:delete:{memory_id}',
                lambda _, current_memory_id=memory_id: memories_db.delete_memory(uid, current_memory_id),
            )

    if len(parsed_memories) == 0:
        print(f"No memories extracted for conversation {conversation.id}")
        return

    print(f"Saving {len(parsed_memories)} memories for conversation {conversation.id}")
    _run_side_effect(
        effect_runner,
        'memories:save',
        lambda _: memories_db.save_memories(uid, [fact.dict() for fact in parsed_memories]),
    )

    for memory_db_obj in parsed_memories:
        _run_side_effect(
            effect_runner,
            f'memories:upsert_vector:{memory_db_obj.id}',
            lambda _, memory=memory_db_obj: upsert_memory_vector(
                uid,
                memory.id,
                memory.content,
                memory.category.value,
            ),
        )

    if len(parsed_memories) > 0:
        _run_side_effect(
            effect_runner,
            'memories:usage',
            lambda operation_token: record_usage(
                uid,
                memories_created=len(parsed_memories),
                idempotency_key=operation_token,
            ),
        )

        try:
            from utils.llm.knowledge_graph import extract_knowledge_from_memory
            from database import users as users_db

            user = users_db.get_user_store_recording_permission(uid)
            user_name = user.get('name', 'User') if user else 'User'

            from database.memories import set_memory_kg_extracted

            for memory_db_obj in parsed_memories:
                if memory_db_obj.kg_extracted:
                    continue
                _run_side_effect(
                    effect_runner,
                    f'memories:knowledge_graph:{memory_db_obj.id}',
                    lambda operation_token, memory=memory_db_obj: extract_knowledge_from_memory(
                        uid,
                        memory.content,
                        memory.id,
                        user_name,
                        idempotency_key=operation_token,
                    ),
                )
                _run_side_effect(
                    effect_runner,
                    f'memories:knowledge_graph_receipt:{memory_db_obj.id}',
                    lambda _, memory=memory_db_obj: set_memory_kg_extracted(uid, memory.id),
                )
        except Exception:
            if side_effect_guard:
                _guard_side_effect(side_effect_guard, 'memories:knowledge_graph_error_fence')
            logging.exception("Error extracting knowledge graph from memory.")


def send_new_memories_notification(user_id: str, memories: [MemoryDB]):
    memories_str = ", ".join([memory.content for memory in memories])
    message = f"New memories {memories_str}"
    ai_message = NotificationMessage(
        text=message,
        from_integration='false',
        type='text',
        notification_type='new_fact',
        navigate_to="/facts",
    )

    send_notification(user_id, "omi" + ' says', message, NotificationMessage.get_message_as_dict(ai_message))


def _extract_trends(
    uid: str,
    conversation: Conversation,
    side_effect_guard: SideEffectGuard = None,
    effect_runner: Optional[CaptureFinalizationEffectRunner] = None,
):
    extracted_items = _run_side_effect(
        effect_runner,
        'trends:extract',
        lambda _: trends_extractor(uid, conversation),
        encode=lambda items: [item.dict() for item in items],
        decode=lambda items: [SimpleNamespace(**item) for item in (items or [])],
    )
    parsed = [Trend(category=item.category, topics=[item.topic], type=item.type) for item in extracted_items]
    _run_side_effect(effect_runner, 'trends:save', lambda _: trends_db.save_trends(conversation, parsed))


def _save_action_items(
    uid: str,
    conversation: Conversation,
    side_effect_guard: SideEffectGuard = None,
    effect_runner: Optional[CaptureFinalizationEffectRunner] = None,
):
    """
    Save action items from a conversation to the dedicated action_items collection.
    This runs in addition to storing them in the conversation for backward compatibility.
    """
    if not conversation.structured or not conversation.structured.action_items:
        return

    is_locked = conversation.is_locked
    action_items_data = []
    now = datetime.now(timezone.utc)

    for action_item in conversation.structured.action_items:
        action_item_data = {
            'description': action_item.description,
            'completed': action_item.completed,
            'created_at': action_item.created_at or now,
            'updated_at': action_item.updated_at or now,
            'due_at': action_item.due_at,
            'completed_at': action_item.completed_at,
            'conversation_id': conversation.id,
            'is_locked': is_locked,
        }
        action_items_data.append(action_item_data)

    if action_items_data:
        if effect_runner:
            action_item_ids = _run_side_effect(
                effect_runner,
                'action_items:create',
                lambda operation_token: action_items_db.create_action_items_batch(
                    uid,
                    action_items_data,
                    idempotency_key=operation_token,
                ),
            )
            _run_side_effect(
                effect_runner,
                'action_items:delete_existing',
                lambda _: action_items_db.delete_action_items_for_conversation(
                    uid,
                    conversation.id,
                    preserve_ids=action_item_ids,
                ),
            )
        else:
            action_items_db.delete_action_items_for_conversation(uid, conversation.id)
            action_item_ids = action_items_db.create_action_items_batch(uid, action_items_data)
        print(f"Saved {len(action_item_ids)} action items for conversation {conversation.id}")

        # Send FCM data messages for action items with due dates
        for idx, action_item in enumerate(conversation.structured.action_items):
            if action_item.due_at and idx < len(action_item_ids):
                action_item_id = action_item_ids[idx]
                _run_side_effect(
                    effect_runner,
                    f'action_items:notify:{action_item_id}',
                    lambda operation_token, current_id=action_item_id, current_item=action_item: send_action_item_data_message(
                        user_id=uid,
                        action_item_id=current_id,
                        description=current_item.description,
                        due_at=current_item.due_at.isoformat(),
                        idempotency_key=operation_token,
                    ),
                )

        # Auto-sync to task integration
        created_items = [{"id": aid, **data} for aid, data in zip(action_item_ids, action_items_data)]

        def _run_auto_sync():
            asyncio.run(auto_sync_action_items_batch(uid, created_items))

        if side_effect_guard:
            _run_side_effect(effect_runner, 'action_items:auto_sync', lambda _: _run_auto_sync())
        else:
            threading.Thread(target=_run_auto_sync, daemon=True).start()


def _update_personas_async(uid: str):
    print(f"[PERSONAS] Starting persona updates in background thread for uid={uid}")
    personas = get_omi_personas_by_uid_db(uid)
    if personas:
        threads = []
        for persona in personas:
            threads.append(threading.Thread(target=sync_update_persona_prompt, args=(persona,)))

        [t.start() for t in threads]
        [t.join() for t in threads]
        print(f"[PERSONAS] Finished persona updates in background thread for uid={uid}")


def process_conversation(
    uid: str,
    language_code: str,
    conversation: Union[Conversation, CreateConversation, ExternalIntegrationCreateConversation],
    force_process: bool = False,
    is_reprocess: bool = False,
    app_id: Optional[str] = None,
    capture_finalization: Optional[Tuple[str, str, str]] = None,
) -> Conversation:
    side_effect_guard: SideEffectGuard = None
    effect_runner: Optional[CaptureFinalizationEffectRunner] = None
    if capture_finalization:
        generation, owner_token, claim_token = capture_finalization
        effect_runner = CaptureFinalizationEffectRunner(uid, conversation.id, capture_finalization)

        def require_live_capture_finalizer(boundary: str) -> None:
            if not conversations_db.renew_capture_finalization(
                uid,
                conversation.id,
                claim_token,
                generation=generation,
                owner_token=owner_token,
            ):
                raise CaptureFinalizationLeaseLost(f'Capture finalization lease was lost before {boundary}')

        side_effect_guard = require_live_capture_finalizer

    # Fetch meeting context from Firestore if meeting_id is associated with this conversation
    if hasattr(conversation, 'id') and conversation.id:
        meeting_id = redis_db.get_conversation_meeting_id(conversation.id)
        if meeting_id:
            try:
                meeting_data = calendar_db.get_meeting(uid, meeting_id)
                if meeting_data:
                    # Add meeting context to conversation's external_data
                    if not hasattr(conversation, 'external_data') or not conversation.external_data:
                        conversation.external_data = {}
                    conversation.external_data['calendar_meeting_context'] = meeting_data
                    print(f"Retrieved meeting context for conversation {conversation.id}: {meeting_data.get('title')}")
            except Exception as e:
                print(f"Error retrieving meeting context for conversation {conversation.id}: {e}")

    person_ids = conversation.get_person_ids()
    people = []
    if person_ids:
        people_data = users_db.get_people_by_ids(uid, list(set(person_ids)))
        people = [Person(**p) for p in people_data]

    try:
        structured, discarded = _run_side_effect(
            effect_runner,
            'result:structured',
            lambda _: _get_structured(uid, language_code, conversation, force_process, people=people),
            encode=lambda result: {'structured': result[0].dict(), 'discarded': result[1]},
            decode=lambda result: (Structured(**result['structured']), bool(result['discarded'])),
        )
    except CaptureFinalizationLeaseLost:
        raise
    except Exception:
        if isinstance(conversation, Conversation):
            mark_conversation_processing_failed(
                uid,
                conversation,
                capture_finalization=capture_finalization,
            )
        raise

    conversation = _get_conversation_obj(uid, structured, conversation, discarded)
    clear_conversation_processing_error(conversation)

    # AI-based folder assignment
    assigned_folder_id = None
    if not discarded and not is_reprocess and not conversation.folder_id:
        try:
            # Get user's folders
            user_folders = folders_db.get_folders(uid)
            if not user_folders:
                user_folders = _run_side_effect(
                    effect_runner,
                    'folders:initialize',
                    lambda _: folders_db.initialize_system_folders(uid),
                )

            if user_folders and conversation.structured:
                folder_id, confidence, reasoning = _run_side_effect(
                    effect_runner,
                    'folders:assign',
                    lambda _: assign_conversation_to_folder(
                        title=conversation.structured.title or '',
                        overview=conversation.structured.overview or '',
                        category=(
                            conversation.structured.category.value if conversation.structured.category else 'other'
                        ),
                        user_folders=user_folders,
                    ),
                    encode=lambda result: list(result),
                    decode=lambda result: tuple(result),
                )
                if folder_id:
                    conversation.folder_id = folder_id
                    assigned_folder_id = folder_id
                    print(
                        f"AI assigned conversation {conversation.id} to folder {folder_id} (confidence: {confidence:.2f}): {reasoning}"
                    )
        except Exception as e:
            if isinstance(e, CaptureFinalizationLeaseLost):
                raise
            print(f"Error during folder assignment for conversation {conversation.id}: {e}")

    if not discarded:
        # Analytics tracking
        insights_gained = 0
        if conversation.structured:
            # Count sentences with more than 5 words from title and overview
            for text in [conversation.structured.title, conversation.structured.overview]:
                if text:
                    sentences = re.split(r'[.!?]+', text)
                    for sentence in sentences:
                        if len(sentence.split()) > 5:
                            insights_gained += 1

            # Count number of action items and events
            insights_gained += len(conversation.structured.action_items)
            insights_gained += len(conversation.structured.events)

        # Count sentences with more than 5 words from app results
        for app_result in conversation.apps_results:
            if app_result.content:
                sentences = re.split(r'[.!?]+', app_result.content)
                for sentence in sentences:
                    if len(sentence.split()) > 5:
                        insights_gained += 1

        if insights_gained > 0:
            _run_side_effect(
                effect_runner,
                'usage:insights',
                lambda operation_token: record_usage(
                    uid,
                    insights_gained=insights_gained,
                    idempotency_key=operation_token,
                ),
            )

        _trigger_apps(
            uid,
            conversation,
            is_reprocess=is_reprocess,
            app_id=app_id,
            language_code=language_code,
            people=people,
            side_effect_guard=side_effect_guard,
            effect_runner=effect_runner,
        )
        if side_effect_guard:
            if not is_reprocess:
                _run_side_effect(
                    effect_runner,
                    'vectors:structured',
                    lambda _: save_structured_vector(uid, conversation),
                )
            _extract_memories(uid, conversation, side_effect_guard, effect_runner)
            _extract_trends(uid, conversation, side_effect_guard, effect_runner)
            _save_action_items(uid, conversation, side_effect_guard, effect_runner)
            _update_goal_progress(uid, conversation, side_effect_guard, effect_runner)
        else:
            (
                threading.Thread(
                    target=save_structured_vector,
                    args=(
                        uid,
                        conversation,
                    ),
                ).start()
                if not is_reprocess
                else None
            )
            threading.Thread(target=_extract_memories, args=(uid, conversation)).start()
            threading.Thread(target=_extract_trends, args=(uid, conversation)).start()
            threading.Thread(target=_save_action_items, args=(uid, conversation)).start()
            threading.Thread(target=_update_goal_progress, args=(uid, conversation)).start()

    # Create audio files from chunks if private cloud sync was enabled
    if not is_reprocess and conversation.private_cloud_sync_enabled:
        try:
            audio_files = _run_side_effect(
                effect_runner,
                'audio:create_files',
                lambda operation_token: conversations_db.create_audio_files_from_chunks(
                    uid,
                    conversation.id,
                    idempotency_key=operation_token or None,
                ),
                encode=lambda files: [audio_file.dict() for audio_file in files],
                decode=lambda files: [AudioFile(**audio_file) for audio_file in (files or [])],
            )
            if audio_files:
                conversation.audio_files = audio_files
                if not capture_finalization:
                    conversations_db.update_conversation(
                        uid, conversation.id, {'audio_files': [af.dict() for af in audio_files]}
                    )
                # Pre-cache audio files in background
                _run_side_effect(
                    effect_runner,
                    'audio:precache',
                    lambda _: precache_conversation_audio(
                        uid,
                        conversation.id,
                        [audio_file.dict() for audio_file in audio_files],
                    ),
                )
        except Exception as e:
            if isinstance(e, CaptureFinalizationLeaseLost):
                raise
            print(f"Error creating audio files: {e}")

    if not capture_finalization:
        conversation.status = ConversationStatus.completed
        conversations_db.upsert_conversation(uid, conversation.dict())
    else:
        conversation.status = ConversationStatus.completed
        generation, owner_token, claim_token = capture_finalization
        if not conversations_db.upsert_conversation_if_capture_finalizer(
            uid,
            conversation.dict(),
            generation,
            owner_token,
            claim_token,
        ):
            raise RuntimeError('Capture finalization lease was lost before result persistence')

    # Update folder conversation count before the final capture result checkpoint. The
    # durable per-effect receipt makes this resumable if the worker dies.
    if assigned_folder_id:
        _run_side_effect(
            effect_runner,
            'folders:update_count',
            lambda _: folders_db.update_folder_conversation_count(uid, assigned_folder_id),
        )

    if not is_reprocess:
        if side_effect_guard:
            _run_side_effect(
                effect_runner,
                'webhook:conversation_created',
                lambda operation_token: conversation_created_webhook(
                    uid,
                    conversation,
                    idempotency_key=operation_token,
                ),
            )
            _run_side_effect(
                effect_runner,
                'personas:update',
                lambda _: update_personas_async(uid),
            )
        else:
            threading.Thread(
                target=conversation_created_webhook,
                args=(
                    uid,
                    conversation,
                ),
            ).start()
            # Update persona prompts with new conversation
            threading.Thread(target=update_personas_async, args=(uid,)).start()

        # Disable important conversation for now
        # Send important conversation notification for long conversations (>30 minutes)
        # threading.Thread(
        #     target=_send_important_conversation_notification_if_needed,
        #     args=(uid, conversation),
        # ).start()

    # Ella post-process hook: notify n8n after conversation is fully saved
    if fire_postprocess_webhook:  # Fires for both initial processing and reprocessing
        if side_effect_guard:
            _run_side_effect(
                effect_runner,
                'webhook:postprocess',
                lambda operation_token: fire_postprocess_webhook(
                    uid,
                    conversation,
                    idempotency_key=operation_token,
                    synchronous=True,
                ),
            )
        else:
            threading.Thread(
                target=fire_postprocess_webhook,
                args=(uid, conversation),
            ).start()

    print('process_conversation completed conversation.id=', conversation.id)
    return conversation


def _send_important_conversation_notification_if_needed(uid: str, conversation: Conversation):
    """
    Send notification for long conversations (>30 minutes) that just completed.
    Only sends once per conversation using Redis deduplication.
    """

    # Skip if conversation is discarded
    if conversation.discarded:
        return

    # Check if we have valid timestamps to compute duration
    if not conversation.started_at or not conversation.finished_at:
        print(f"Cannot compute duration for conversation {conversation.id}: missing timestamps")
        return

    # Calculate duration in seconds
    duration_seconds = (conversation.finished_at - conversation.started_at).total_seconds()

    # Only notify for conversations longer than 30 minutes (1800 seconds)
    if duration_seconds < 1800:
        return

    # Check if notification was already sent for this conversation
    if redis_db.has_important_conversation_notification_been_sent(uid, conversation.id):
        print(f"Important conversation notification already sent for {conversation.id}")
        return

    # Mark as sent before sending to prevent duplicates
    redis_db.set_important_conversation_notification_sent(uid, conversation.id)

    # Send the notification
    print(
        f"Sending important conversation notification for {conversation.id} (duration: {duration_seconds/60:.1f} mins)"
    )
    send_important_conversation_message(uid, conversation.id)


def process_user_emotion(uid: str, language_code: str, conversation: Conversation, urls: [str]):
    print('process_user_emotion conversation.id=', conversation.id)

    # save task
    now = datetime.now()
    task = Task(
        id=str(uuid.uuid4()),
        action=TaskAction.HUME_MERSURE_USER_EXPRESSION,
        user_uid=uid,
        memory_id=conversation.id,
        created_at=now,
        status=TaskStatus.PROCESSING,
    )
    tasks_db.create(task.dict())

    # emotion
    ok = get_hume().request_user_expression_mersurement(urls)
    if "error" in ok:
        err = ok["error"]
        print(err)
        return
    job = ok["result"]
    request_id = job.id
    if not request_id or len(request_id) == 0:
        print(f"Can not request users feeling. uid: {uid}")
        return

    # update task
    task.request_id = request_id
    task.updated_at = datetime.now()
    tasks_db.update(task.id, task.dict())

    return


def process_user_expression_measurement_callback(provider: str, request_id: str, callback: HumeJobCallbackModel):
    support_providers = [TaskActionProvider.HUME]
    if provider not in support_providers:
        print(f"Provider is not supported. {provider}")
        return

    # Get task
    task_action = ""
    if provider == TaskActionProvider.HUME:
        task_action = TaskAction.HUME_MERSURE_USER_EXPRESSION
    if len(task_action) == 0:
        print("Task action is empty")
        return

    task_data = tasks_db.get_task_by_action_request(task_action, request_id)
    if task_data is None:
        print(f"Task not found. Action: {task_action}, Request ID: {request_id}")
        return

    task = Task(**task_data)

    # Update
    task_status = task.status
    if callback.status == "COMPLETED":
        task_status = TaskStatus.DONE
    elif callback.status == "FAILED":
        task_status = TaskStatus.ERROR
    else:
        print(f"Not support status {callback.status}")
        return

    # Not changed
    if task_status == task.status:
        print("Task status are synced")
        return

    task.status = task_status
    task.updated_at = datetime.now()
    tasks_db.update(task.id, task.dict())

    # done or not
    if task.status != TaskStatus.DONE:
        print(f"Task is not done yet. Uid: {task.user_uid}, task_id: {task.id}, status: {task.status}")
        return

    uid = task.user_uid

    # Save predictions
    if len(callback.predictions) > 0:
        conversations_db.store_model_emotion_predictions_result(
            task.user_uid, task.memory_id, provider, callback.predictions
        )

    # Conversation
    conversation_data = conversations_db.get_conversation(uid, task.memory_id)
    if conversation_data is None:
        print(f"Conversation is not found. Uid: {uid}. Conversation: {task.memory_id}")
        return

    conversation = Conversation(**conversation_data)

    # Get prediction
    predictions = callback.predictions
    print(predictions)
    if len(predictions) == 0 or len(predictions[0].emotions) == 0:
        print(f"Can not predict user's expression. Uid: {uid}")
        return

    # Filter users emotions only
    users_frames = []
    for seg in filter(lambda seg: seg.is_user and 0 <= seg.start < seg.end, conversation.transcript_segments):
        users_frames.append((seg.start, seg.end))
    # print(users_frames)

    if len(users_frames) == 0:
        print(f"User time frames are empty. Uid: {uid}")
        return

    users_predictions = []
    for prediction in predictions:
        for uf in users_frames:
            print(uf, prediction.time)
            if uf[0] <= prediction.time[0] and prediction.time[1] <= uf[1]:
                users_predictions.append(prediction)
                break
    if len(users_predictions) == 0:
        print(f"Predictions are filtered by user transcript segments. Uid: {uid}")
        return

    # Top emotions
    emotion_filters = []
    user_emotions = []
    for up in users_predictions:
        user_emotions += up.emotions
    emotions = HumeJobModelPredictionResponseModel.get_top_emotion_names(user_emotions, 1, 0.5)
    # print(emotions)
    if len(emotion_filters) > 0:
        emotions = filter(lambda emotion: emotion in emotion_filters, emotions)
    if len(emotions) == 0:
        print(f"Can not extract users emmotion. uid: {uid}")
        return

    emotion = ','.join(emotions)
    print(f"Emotion Uid: {uid} {emotion}")

    # Ask llms about notification content
    title = "omi"
    context_str, _ = retrieve_rag_conversation_context(uid, conversation)

    response: str = obtain_emotional_message(uid, conversation, context_str, emotion)
    message = response

    # Send the notification
    send_notification(uid, title, message, None)

    return


def retrieve_in_progress_conversation(uid):
    conversation_id = redis_db.get_in_progress_conversation_id(uid)
    existing = None

    if conversation_id:
        existing = conversations_db.get_conversation(uid, conversation_id)
        if existing and existing['status'] != 'in_progress':
            existing = None

    if not existing:
        existing = conversations_db.get_in_progress_conversation(uid)
    return existing
