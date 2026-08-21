import os
import random
import re
import threading
import uuid
import logging
import asyncio
from dataclasses import dataclass
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
from models.conversation_integrity import transcript_grounding_hash
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
from ella.services.ai_consent import assert_current_ai_consent

# ====== ELLA POST-PROCESS HOOK IMPORT ======
try:
    from utils.ella.postprocess import fire_postprocess_webhook, HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS
except ImportError:
    fire_postprocess_webhook = None
    HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS = frozenset()
# ====== END ELLA IMPORT ======
from utils.notifications import send_action_item_data_message
from utils.task_sync import auto_sync_action_items_batch
from utils.other.storage import precache_conversation_audio
from utils.conversations.failure_state import (
    CONVERSATION_PROCESSING_FAILED,
    CONVERSATION_SUMMARY_FAILED,
    clear_conversation_processing_error,
)
from utils.conversations.vector import save_structured_vector
from utils.conversations.capture_protocol import (
    capture_finalization_effect_operation_token,
    claim_capture_finalization_effect,
    complete_capture_finalization_effect,
)


@dataclass(frozen=True)
class ConversationProcessingOutcome:
    conversation: Conversation
    dispatched: bool
    status: str
    released_claim_token: Optional[str] = None


CONVERSATION_TRANSCRIPT_REDELIVERY_EXHAUSTED = 'transcript_redelivery_exhausted'


class CaptureFinalizationLeaseLost(RuntimeError):
    pass


class CaptureFinalizationEffectRunner:
    """Run capture finalization effects with durable retry receipts."""

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
        return capture_finalization_effect_operation_token(
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
        claim = claim_capture_finalization_effect(
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
            raise CaptureFinalizationLeaseLost(f'capture finalization lease lost before {effect_id}')

        operation_token = str(claim.get('operation_token') or '')
        if not operation_token:
            raise CaptureFinalizationLeaseLost(f'capture finalization operation token missing for {effect_id}')
        result = operation(operation_token)
        if not complete_capture_finalization_effect(
            self.uid,
            self.conversation_id,
            self.generation,
            self.owner_token,
            self.claim_token,
            effect_id,
            operation_token,
            encode(result),
        ):
            raise CaptureFinalizationLeaseLost(f'capture finalization lease lost while completing {effect_id}')
        return result


def _run_capture_effect(
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


def _run_post_commit_effect(name: str, effect, guard: Optional[Callable[[], None]] = None):
    try:
        if guard:
            guard()
        return effect()
    except CaptureFinalizationLeaseLost:
        raise
    except Exception as exc:
        print(f"Post-commit effect failed ({name}): {exc}", flush=True)
        return None


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
):
    failed_at = datetime.now(timezone.utc)
    result = conversations_db.mark_conversation_processing_failed_if_unfinished(
        uid,
        conversation.id,
        error_code,
        failed_at=failed_at,
    )
    if result.get('updated'):
        conversation.status = ConversationStatus.failed
        conversation.discarded = False
        conversation.processing_error = error_code
        conversation.processing_error_at = failed_at
        return True
    return False


def mark_conversation_processing_failed_update(
    uid: str,
    conversation: Conversation,
    error_code: str = "conversation_summary_failed",
):
    return mark_conversation_processing_failed(uid, conversation, error_code=error_code)


def mark_unexpected_conversation_processing_failed(uid: str, conversation: Conversation) -> bool:
    if conversation.status == ConversationStatus.failed and conversation.processing_error:
        return False
    return mark_conversation_processing_failed(uid, conversation, error_code=CONVERSATION_PROCESSING_FAILED)


def mark_released_conversation_processing_failed(
    uid: str,
    conversation: Conversation,
    release_token: Optional[str],
) -> bool:
    if not release_token:
        return False
    failed_at = datetime.now(timezone.utc)
    result = conversations_db.mark_conversation_processing_failed_if_released(
        uid,
        conversation.id,
        CONVERSATION_PROCESSING_FAILED,
        release_token,
        failed_at=failed_at,
    )
    if not result.get('updated'):
        return False
    conversation.status = ConversationStatus.failed
    conversation.discarded = False
    conversation.processing_error = CONVERSATION_PROCESSING_FAILED
    conversation.processing_error_at = failed_at
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
        result = _run_capture_effect(
            effect_runner,
            f'app:{app.id}:invoke',
            lambda _: get_app_result(
                conversation.get_transcript(False, people=people),
                conversation.photos,
                app,
                language_code=language_code,
            ).strip(),
        )
        conversation.apps_results.append(AppResult(app_id=app.id, content=result))
        if not is_reprocess:
            _run_capture_effect(
                effect_runner,
                f'app:{app.id}:usage',
                lambda _: record_app_usage(
                    uid,
                    app.id,
                    UsageHistoryType.memory_created_prompt,
                    conversation_id=conversation.id,
                ),
            )

    if effect_runner:
        for app in filtered_apps:
            execute_app(app)
    else:
        threads = [threading.Thread(target=execute_app, args=(app,)) for app in filtered_apps]
        [t.start() for t in threads]
        [t.join() for t in threads]


def _update_goal_progress(
    uid: str,
    conversation: Conversation,
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
        _run_capture_effect(
            effect_runner,
            'goals:update',
            lambda _: extract_and_update_goal_progress(uid, text),
        )
    except Exception as e:
        if isinstance(e, CaptureFinalizationLeaseLost):
            raise
        print(f"[GOAL] Error updating progress: {e}")


def _extract_memories(
    uid: str,
    conversation: Conversation,
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
            new_memories = _run_capture_effect(
                effect_runner,
                'memories:extract_external',
                lambda _: extract_memories_from_text(uid, text_content, text_source),
                encode=lambda memories: [memory.dict() for memory in memories],
                decode=lambda memories: [Memory(**memory) for memory in (memories or [])],
            )
    else:
        # For regular conversations with transcript segments
        new_memories = _run_capture_effect(
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

            resolution = _run_capture_effect(
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
        _run_capture_effect(
            effect_runner,
            f'memories:merge_delete_vector:{memory_id}',
            lambda _, current_memory_id=memory_id: delete_memory_vector(uid, current_memory_id),
        )
        _run_capture_effect(
            effect_runner,
            f'memories:merge_delete:{memory_id}',
            lambda _, current_memory_id=memory_id: memories_db.delete_memory(uid, current_memory_id),
        )

    if effect_runner:
        replacement_ids = {memory.id for memory in parsed_memories}
        for memory_id in existing_memory_ids:
            if memory_id in replacement_ids:
                continue
            _run_capture_effect(
                effect_runner,
                f'memories:delete_vector:{memory_id}',
                lambda _, current_memory_id=memory_id: delete_memory_vector(uid, current_memory_id),
            )
            _run_capture_effect(
                effect_runner,
                f'memories:delete:{memory_id}',
                lambda _, current_memory_id=memory_id: memories_db.delete_memory(uid, current_memory_id),
            )

    if len(parsed_memories) == 0:
        print(f"No memories extracted for conversation {conversation.id}")
        return

    print(f"Saving {len(parsed_memories)} memories for conversation {conversation.id}")
    _run_capture_effect(
        effect_runner,
        'memories:save',
        lambda _: memories_db.save_memories(uid, [fact.dict() for fact in parsed_memories]),
    )

    for memory_db_obj in parsed_memories:
        _run_capture_effect(
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
        _run_capture_effect(
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
                _run_capture_effect(
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
                _run_capture_effect(
                    effect_runner,
                    f'memories:knowledge_graph_receipt:{memory_db_obj.id}',
                    lambda _, memory=memory_db_obj: set_memory_kg_extracted(uid, memory.id),
                )
        except Exception as error:
            if isinstance(error, CaptureFinalizationLeaseLost):
                raise
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
    effect_runner: Optional[CaptureFinalizationEffectRunner] = None,
):
    extracted_items = _run_capture_effect(
        effect_runner,
        'trends:extract',
        lambda _: trends_extractor(uid, conversation),
        encode=lambda items: [item.dict() for item in items],
        decode=lambda items: [SimpleNamespace(**item) for item in (items or [])],
    )
    parsed = [Trend(category=item.category, topics=[item.topic], type=item.type) for item in extracted_items]
    _run_capture_effect(
        effect_runner,
        'trends:save',
        lambda _: trends_db.save_trends(conversation, parsed),
    )


def _save_action_items(
    uid: str,
    conversation: Conversation,
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
            action_item_ids = _run_capture_effect(
                effect_runner,
                'action_items:create',
                lambda operation_token: action_items_db.create_action_items_batch(
                    uid,
                    action_items_data,
                    idempotency_key=operation_token,
                ),
            )
            _run_capture_effect(
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
                _run_capture_effect(
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

        def _run_auto_sync(operation_token=None):
            return asyncio.run(
                auto_sync_action_items_batch(
                    uid,
                    created_items,
                    idempotency_key=operation_token,
                )
            )

        if effect_runner:
            _run_capture_effect(effect_runner, 'action_items:auto_sync', _run_auto_sync)
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


def process_conversation_with_outcome(
    uid: str,
    language_code: str,
    conversation: Union[Conversation, CreateConversation, ExternalIntegrationCreateConversation],
    force_process: bool = False,
    is_reprocess: bool = False,
    app_id: Optional[str] = None,
    _claim_already_held: bool = False,
    _initial_processing_claim_token: Optional[str] = None,
    _transcript_retry_count: int = 0,
    capture_finalization: Optional[Tuple[str, str, str]] = None,
) -> ConversationProcessingOutcome:
    assert_current_ai_consent(uid)
    effect_runner = (
        CaptureFinalizationEffectRunner(uid, conversation.id, capture_finalization)
        if capture_finalization and isinstance(conversation, Conversation)
        else None
    )
    allow_create = not isinstance(conversation, Conversation)
    resuming_completed_capture = False
    initial_processing_claim_held = bool(_claim_already_held)
    initial_processing_claim_token = _initial_processing_claim_token
    if isinstance(conversation, Conversation) and not is_reprocess and not _claim_already_held:
        claim_result = conversations_db.claim_initial_conversation_processing(uid, conversation.id)
        claim_status = claim_result.get('status')
        if claim_status == 'already_completed':
            durable_conversation = conversations_db.get_conversation(uid, conversation.id) or conversation.dict()
            if not effect_runner:
                return ConversationProcessingOutcome(
                    conversation=Conversation(**durable_conversation),
                    dispatched=False,
                    status=claim_status,
                )
            conversation = Conversation(**durable_conversation)
            resuming_completed_capture = True
        elif claim_status in {'processing_in_progress', 'capture_in_progress'}:
            durable_conversation = conversations_db.get_conversation(uid, conversation.id) or conversation.dict()
            return ConversationProcessingOutcome(
                conversation=Conversation(**durable_conversation),
                dispatched=False,
                status=claim_status,
            )
        elif claim_status == 'conversation_missing':
            return ConversationProcessingOutcome(
                conversation=conversation,
                dispatched=False,
                status=conversations_db.conversation_stock_summary_deleted,
            )
        elif claim_status != 'processing_claimed':
            raise RuntimeError(f"conversation processing claim unavailable: {claim_status}")
        if not resuming_completed_capture:
            initial_processing_claim_held = True
            initial_processing_claim_token = str(claim_result.get('claim_token') or '') or None
            conversation.status = ConversationStatus.processing
    elif isinstance(conversation, Conversation) and _claim_already_held:
        conversation.status = ConversationStatus.processing
    expected_active_summary_version_id = (
        conversation.active_summary_version_id if isinstance(conversation, Conversation) else None
    )
    expected_transcript_hash = (
        transcript_grounding_hash(conversation.transcript_segments) if isinstance(conversation, Conversation) else None
    )

    def run_post_commit(name: str, operation: Callable[[str], object]):
        if effect_runner:
            return _run_capture_effect(effect_runner, f'post:{name}', operation)
        return _run_post_commit_effect(name, lambda: operation(''))

    def run_post_commit_background(name: str, operation: Callable[..., object], args: tuple = ()):
        if effect_runner:
            return _run_capture_effect(
                effect_runner,
                f'post:{name}',
                lambda _: operation(*args),
            )
        return _run_post_commit_effect(name, lambda: threading.Thread(target=operation, args=args).start())

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
        structured, discarded = _run_capture_effect(
            effect_runner,
            'result:structured',
            lambda _: _get_structured(uid, language_code, conversation, force_process, people=people),
            encode=lambda result: {'structured': result[0].dict(), 'discarded': result[1]},
            decode=lambda result: (Structured(**result['structured']), bool(result['discarded'])),
        )
    except Exception:
        if isinstance(conversation, Conversation) and not capture_finalization:
            mark_conversation_processing_failed_update(uid, conversation)
        raise

    conversation = _get_conversation_obj(uid, structured, conversation, discarded)
    clear_conversation_processing_error(conversation)

    # AI-based folder assignment
    assigned_folder_id = conversation.folder_id if effect_runner else None
    initialize_system_folders_after_commit = False

    def select_assigned_folder_id(user_folders):
        folder_id, confidence, reasoning = assign_conversation_to_folder(
            title=conversation.structured.title or '',
            overview=conversation.structured.overview or '',
            category=conversation.structured.category.value if conversation.structured.category else 'other',
            user_folders=user_folders,
        )
        if folder_id:
            print(
                f"AI assigned conversation {conversation.id} to folder {folder_id} "
                f"(confidence: {confidence:.2f}): {reasoning}"
            )
        return folder_id

    if not discarded and not is_reprocess and not conversation.folder_id:
        try:
            user_folders = folders_db.get_folders(uid)
            if not user_folders:
                initialize_system_folders_after_commit = True
            elif conversation.structured:
                assigned_folder_id = _run_capture_effect(
                    effect_runner,
                    'folders:assign',
                    lambda _: select_assigned_folder_id(user_folders),
                )
                conversation.folder_id = assigned_folder_id
        except Exception as e:
            print(f"Error during folder assignment for conversation {conversation.id}: {e}")

    folder_persisted_by_commit = bool((allow_create or effect_runner) and assigned_folder_id)
    conversation.status = ConversationStatus.completed
    commit_kwargs = {
        'expected_active_summary_version_id': expected_active_summary_version_id,
        'allow_create': allow_create,
        'enqueue_hermes_cloud_enrichment': uid in HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS,
        'expected_transcript_hash': expected_transcript_hash,
    }
    if capture_finalization is not None:
        commit_kwargs['capture_finalization'] = capture_finalization
    commit_result = _run_capture_effect(
        effect_runner,
        'result:summary_commit',
        lambda _: conversations_db.commit_stock_summary_processing_result(
            uid,
            conversation.id,
            conversation.dict(),
            **commit_kwargs,
        ),
        encode=lambda result: {
            key: result.get(key)
            for key in (
                'status',
                'active_summary_version_id',
                'dispatched',
                'hermes_enrichment_job_id',
            )
            if key in result
        },
        decode=lambda result: result or {},
    )
    commit_status = commit_result.get('status')
    if commit_status == conversations_db.conversation_stock_summary_transcript_changed:
        durable_conversation = commit_result.get('conversation') or conversations_db.get_conversation(
            uid, conversation.id
        )
        if durable_conversation and _transcript_retry_count < 1:
            print(
                f"transcript changed while processing conversation.id={conversation.id}; retrying latest snapshot",
                flush=True,
            )
            return process_conversation_with_outcome(
                uid,
                language_code,
                Conversation(**durable_conversation),
                force_process=force_process,
                is_reprocess=is_reprocess,
                app_id=app_id,
                _claim_already_held=initial_processing_claim_held,
                _initial_processing_claim_token=initial_processing_claim_token,
                _transcript_retry_count=_transcript_retry_count + 1,
                capture_finalization=capture_finalization,
            )
        released_claim_token = None
        if initial_processing_claim_held:
            release_result = conversations_db.release_initial_conversation_processing_claim(
                uid,
                conversation.id,
                initial_processing_claim_token or '',
            )
            if release_result.get('released'):
                released_claim_token = str(release_result.get('release_token') or '') or None
        return ConversationProcessingOutcome(
            conversation=Conversation(**(durable_conversation or conversation.dict())),
            dispatched=False,
            status=commit_status,
            released_claim_token=released_claim_token,
        )
    if commit_status == conversations_db.capture_finalization_lost:
        raise CaptureFinalizationLeaseLost('capture_finalization_lease_lost')
    if commit_status in {
        conversations_db.conversation_stock_summary_cas_lost,
        conversations_db.conversation_stock_summary_deleted,
    }:
        durable_conversation = commit_result.get('conversation') or conversation.dict()
        print(
            f"stock summary CAS lost for conversation.id={conversation.id}; skipping processing side effects",
            flush=True,
        )
        return ConversationProcessingOutcome(
            conversation=Conversation(**durable_conversation),
            dispatched=False,
            status=commit_status,
        )
    if commit_status != 'committed':
        if not capture_finalization:
            mark_conversation_processing_failed_update(uid, conversation, error_code=CONVERSATION_SUMMARY_FAILED)
        raise RuntimeError(f"conversation summary authority unavailable: {commit_status}")

    durable_committed_conversation = commit_result.get('conversation') or conversations_db.get_conversation(
        uid, conversation.id
    )
    conversation = Conversation(**(durable_committed_conversation or conversation.dict()))
    should_dispatch_processing_side_effects = bool(commit_result.get('dispatched', True))

    if initialize_system_folders_after_commit and should_dispatch_processing_side_effects:
        try:
            user_folders = _run_capture_effect(
                effect_runner,
                'folders:initialize',
                lambda _: folders_db.initialize_system_folders(uid),
            )
            if user_folders and conversation.structured:
                assigned_folder_id = _run_capture_effect(
                    effect_runner,
                    'folders:assign',
                    lambda _: select_assigned_folder_id(user_folders),
                )
        except Exception as e:
            print(f"Error during post-commit folder initialization for conversation {conversation.id}: {e}")

    if not discarded and should_dispatch_processing_side_effects:

        def record_processing_usage(operation_token: str):
            insights_gained = 0
            if conversation.structured:
                for text in [conversation.structured.title, conversation.structured.overview]:
                    if text:
                        for sentence in re.split(r'[.!?]+', text):
                            if len(sentence.split()) > 5:
                                insights_gained += 1
                insights_gained += len(conversation.structured.action_items)
                insights_gained += len(conversation.structured.events)
            for app_result in conversation.apps_results:
                if app_result.content:
                    for sentence in re.split(r'[.!?]+', app_result.content):
                        if len(sentence.split()) > 5:
                            insights_gained += 1
            if insights_gained > 0:
                record_usage(
                    uid,
                    insights_gained=insights_gained,
                    idempotency_key=operation_token or None,
                )

        def trigger_and_persist_apps(_operation_token: str):
            _trigger_apps(
                uid,
                conversation,
                is_reprocess=is_reprocess,
                app_id=app_id,
                language_code=language_code,
                people=people,
                effect_runner=effect_runner,
            )
            conversations_db.update_conversation(
                uid,
                conversation.id,
                {
                    'apps_results': [result.dict() for result in conversation.apps_results],
                    'suggested_summarization_apps': conversation.suggested_summarization_apps,
                },
            )

        run_post_commit('usage', record_processing_usage)
        run_post_commit('apps', trigger_and_persist_apps)
        if not is_reprocess:
            if effect_runner:
                run_post_commit('structured_vector', lambda _: save_structured_vector(uid, conversation))
            else:
                run_post_commit_background('structured_vector', save_structured_vector, (uid, conversation))
        if effect_runner:
            _extract_memories(uid, conversation, effect_runner)
            _extract_trends(uid, conversation, effect_runner)
            _save_action_items(uid, conversation, effect_runner)
            _update_goal_progress(uid, conversation, effect_runner)
        else:
            run_post_commit_background('memories', _extract_memories, (uid, conversation))
            run_post_commit_background('trends', _extract_trends, (uid, conversation))
            run_post_commit_background('action_items', _save_action_items, (uid, conversation))
            run_post_commit_background('goals', _update_goal_progress, (uid, conversation))

    # Create audio files from chunks if private cloud sync was enabled
    if not is_reprocess and conversation.private_cloud_sync_enabled:
        try:
            audio_files = _run_capture_effect(
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
                _run_capture_effect(
                    effect_runner,
                    'audio:persist_files',
                    lambda _: conversations_db.update_conversation(
                        uid,
                        conversation.id,
                        {'audio_files': [af.dict() for af in audio_files]},
                    ),
                )
                _run_capture_effect(
                    effect_runner,
                    'audio:precache',
                    lambda _: precache_conversation_audio(
                        uid,
                        conversation.id,
                        [af.dict() for af in audio_files],
                    ),
                )
        except Exception as e:
            if isinstance(e, CaptureFinalizationLeaseLost):
                raise
            print(f"Error creating audio files: {e}")

    # Update folder conversation count after conversation is saved
    if assigned_folder_id and should_dispatch_processing_side_effects:
        folder_assigned = folder_persisted_by_commit or run_post_commit(
            'folder_assignment',
            lambda _: conversations_db.assign_conversation_folder_if_unset(uid, conversation.id, assigned_folder_id),
        )
        if folder_assigned:
            conversation.folder_id = assigned_folder_id
            run_post_commit(
                'folder_count', lambda _: folders_db.update_folder_conversation_count(uid, assigned_folder_id)
            )

    if not is_reprocess and should_dispatch_processing_side_effects:
        if effect_runner:
            run_post_commit(
                'conversation_created_webhook',
                lambda operation_token: conversation_created_webhook(
                    uid,
                    conversation,
                    idempotency_key=operation_token or None,
                ),
            )
            run_post_commit('persona_update', lambda _: update_personas_async(uid))
        else:
            run_post_commit_background(
                'conversation_created_webhook',
                conversation_created_webhook,
                (uid, conversation),
            )
            run_post_commit_background('persona_update', update_personas_async, (uid,))

        # Disable important conversation for now
        # Send important conversation notification for long conversations (>30 minutes)
        # threading.Thread(
        #     target=_send_important_conversation_notification_if_needed,
        #     args=(uid, conversation),
        # ).start()

    # Ella post-process hook: notify n8n after conversation is fully saved
    if (
        fire_postprocess_webhook
        and should_dispatch_processing_side_effects
        and uid not in HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS
    ):  # Cloud-selected conversations were queued atomically with the summary commit.
        if effect_runner:
            run_post_commit(
                'ella_postprocess_webhook',
                lambda operation_token: fire_postprocess_webhook(
                    uid,
                    conversation,
                    idempotency_key=operation_token or None,
                    synchronous=True,
                ),
            )
        else:
            run_post_commit_background(
                'ella_postprocess_webhook',
                fire_postprocess_webhook,
                (uid, conversation),
            )

    print('process_conversation completed conversation.id=', conversation.id)
    return ConversationProcessingOutcome(
        conversation=conversation,
        dispatched=should_dispatch_processing_side_effects,
        status='committed',
    )


def process_conversation_with_transcript_redelivery(
    uid: str,
    language_code: str,
    conversation: Conversation,
    *,
    max_redeliveries: int = 1,
) -> ConversationProcessingOutcome:
    """Run a fresh claimed processing invocation after transcript CAS exhaustion."""
    redeliveries = 0
    while True:
        outcome = process_conversation_with_outcome(uid, language_code, conversation)
        if outcome.status != conversations_db.conversation_stock_summary_transcript_changed:
            return outcome
        if redeliveries >= max_redeliveries:
            marked_failed = mark_released_conversation_processing_failed(
                uid,
                outcome.conversation,
                outcome.released_claim_token,
            )
            durable_conversation = conversations_db.get_conversation(uid, outcome.conversation.id)
            durable = Conversation(**(durable_conversation or outcome.conversation.dict()))
            if not marked_failed and durable.status == ConversationStatus.completed:
                return ConversationProcessingOutcome(
                    conversation=durable,
                    dispatched=False,
                    status='already_completed',
                )
            if not marked_failed and durable.status in {
                ConversationStatus.in_progress,
                ConversationStatus.processing,
            }:
                return ConversationProcessingOutcome(
                    conversation=durable,
                    dispatched=False,
                    status='processing_in_progress',
                )
            return ConversationProcessingOutcome(
                conversation=durable,
                dispatched=False,
                status=CONVERSATION_TRANSCRIPT_REDELIVERY_EXHAUSTED,
            )
        redeliveries += 1
        conversation = outcome.conversation
        print(
            f"re-dispatching conversation.id={conversation.id} after transcript CAS exhaustion "
            f"({redeliveries}/{max_redeliveries})",
            flush=True,
        )


def process_conversation(
    uid: str,
    language_code: str,
    conversation: Union[Conversation, CreateConversation, ExternalIntegrationCreateConversation],
    force_process: bool = False,
    is_reprocess: bool = False,
    app_id: Optional[str] = None,
) -> Conversation:
    outcome = process_conversation_with_outcome(
        uid,
        language_code,
        conversation,
        force_process=force_process,
        is_reprocess=is_reprocess,
        app_id=app_id,
    )
    if not outcome.dispatched and outcome.status != 'already_completed':
        raise RuntimeError(f"conversation processing not committed: {outcome.status}")
    return outcome.conversation


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
