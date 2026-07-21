"""Side-effect-free stock OMI summary generation for an existing conversation."""

from datetime import datetime, timedelta, timezone

import database.action_items as action_items_db
import database.notifications as notification_db
import database.users as users_db
from models.conversation import CalendarMeetingContext, Conversation, Structured
from models.other import Person
from utils.llm.conversation_processing import get_transcript_structure


def generate_stock_conversation_summary(uid: str, conversation: Conversation) -> Structured:
    timezone_name = notification_db.get_user_time_zone(uid)
    existing_action_items = None
    try:
        existing_action_items = action_items_db.get_action_items(
            uid=uid,
            start_date=datetime.now(timezone.utc) - timedelta(days=2),
            limit=50,
        )
    except Exception:
        existing_action_items = None

    people = []
    person_ids = conversation.get_person_ids()
    if person_ids:
        people = [Person(**person) for person in users_db.get_people_by_ids(uid, list(set(person_ids)))]

    calendar_context = None
    if conversation.external_data:
        calendar_data = conversation.external_data.get('calendar_meeting_context')
        if calendar_data:
            calendar_context = CalendarMeetingContext(**calendar_data)

    transcript = conversation.get_transcript(False, people=people)
    return get_transcript_structure(
        transcript,
        conversation.started_at,
        conversation.language or 'en',
        timezone_name,
        photos=conversation.photos,
        existing_action_items=existing_action_items,
        calendar_meeting_context=calendar_context,
        uid=uid,
        existing_conversation_id=conversation.id,
    )
