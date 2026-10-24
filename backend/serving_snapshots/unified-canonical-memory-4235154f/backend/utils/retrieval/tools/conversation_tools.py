"""
Tools for accessing user conversations.
"""

from datetime import datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo
import contextvars
import re

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

import database.conversations as conversations_db
import database.users as users_db
import database.vector_db as vector_db
from models.conversation import Conversation
from models.other import Person
from utils.llm.clients import embeddings

# Import agent_config_context for fallback config access
try:
    from utils.retrieval.agentic import agent_config_context
except ImportError:
    # Fallback if import fails
    agent_config_context = contextvars.ContextVar('agent_config', default=None)


_SEARCH_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "in",
    "on",
    "of",
    "for",
    "with",
    "what",
    "where",
    "when",
    "who",
    "why",
    "how",
    "did",
    "do",
    "does",
    "i",
    "me",
    "my",
    "you",
    "tell",
    "check",
    "latest",
    "recent",
    "memory",
    "memories",
    "omi",
    "conversation",
    "conversations",
    "transcript",
    "transcripts",
    "summary",
    "summaries",
    "happened",
    "happen",
    "find",
    "pull",
    "about",
}


def _keyword_score(text: str, terms: list[str]) -> int:
    text_lower = text.lower()
    score = 0
    for term in terms:
        term = (term or "").strip().lower()
        if not term:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text_lower):
            score += 1
    return score


def _normalized_query_terms(query: str) -> list[str]:
    terms = [term.strip(".,?!:;()[]{}\"'’‘“”").lower() for term in str(query or "").split()]
    filtered = [term for term in terms if len(term) > 2 and term not in _SEARCH_STOPWORDS]
    return filtered or [term for term in terms if len(term) > 2]


def _significant_query_terms(query: str) -> list[str]:
    terms = [term.strip(".,?!:;()[]{}\"'’‘“”").lower() for term in str(query or "").split()]
    return [term for term in terms if len(term) > 2 and term not in _SEARCH_STOPWORDS]


def _expand_query_terms(terms: list[str]) -> list[str]:
    expanded = list(terms)
    aliases = {
        "meisheng": ["mei", "sheng", "may", "shing"],
        "meishengs": ["mei", "sheng", "may", "shing"],
    }
    for term in terms:
        expanded.extend(aliases.get(term, []))
    return list(dict.fromkeys(expanded))


def _conversation_search_blob(conv_data: dict, *, include_transcript: bool = True) -> str:
    structured = conv_data.get("structured") or {}
    fields = [
        structured.get("title", ""),
        structured.get("overview", ""),
        structured.get("category", ""),
        structured.get("emoji", ""),
    ]
    created = conv_data.get("created_at")
    if created and hasattr(created, "strftime"):
        fields.append(created.strftime("%B %d %Y %b %A %I:%M %p"))
    elif created:
        fields.append(str(created))

    if include_transcript:
        transcript_parts = []
        for segment in conv_data.get("transcript_segments") or []:
            if isinstance(segment, dict):
                transcript_parts.append(str(segment.get("text") or segment.get("transcript") or ""))
            else:
                transcript_parts.append(str(getattr(segment, "text", "") or getattr(segment, "transcript", "")))
        fields.append(" ".join(part for part in transcript_parts if part))

    return " ".join(str(field or "") for field in fields).lower()


def _rank_exact_conversation_ids(
    conversations_data: list[dict],
    query: str,
    limit: int,
    *,
    allow_date_only: bool = False,
) -> list[str]:
    significant_terms = _significant_query_terms(query)
    query_terms = _expand_query_terms(significant_terms or ([] if allow_date_only else _normalized_query_terms(query)))
    query_phrase = " ".join(str(query or "").lower().split())
    ranked = []

    for conv_data in conversations_data:
        conversation_id = conv_data.get("id")
        if not conversation_id:
            continue
        searchable = _conversation_search_blob(conv_data, include_transcript=True)
        score = _keyword_score(searchable, query_terms) if query_terms else (1 if allow_date_only else 0)
        if query_phrase and query_phrase in searchable:
            score += max(2, len(query_terms))
        if score <= 0:
            continue
        created = conv_data.get("created_at")
        if isinstance(created, datetime):
            created_sort = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        else:
            created_sort = datetime.min.replace(tzinfo=timezone.utc)
        ranked.append((score, created_sort, str(conversation_id)))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [conversation_id for _score, _created, conversation_id in ranked[:limit]]


def _merge_ranked_ids(primary: list[str], secondary: list[str], limit: int) -> list[str]:
    merged = []
    seen = set()
    for conversation_id in list(primary or []) + list(secondary or []):
        if not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        merged.append(conversation_id)
        if len(merged) >= limit:
            break
    return merged


@tool
def get_conversations_tool(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    include_discarded: bool = False,
    statuses: Optional[str] = "processing,completed",
    max_transcript_segments: int = 0,
    include_transcript: bool = True,
    include_timestamps: bool = False,
    config: RunnableConfig = None,
) -> str:
    """
    Retrieve user conversations with complete details including transcripts, summaries, and metadata.

    Use this tool when:
    - User asks about recent conversations or specific time periods
    - You need conversation transcripts to answer questions
    - User wants to review what they discussed

    **IMPORTANT for summarization queries:**
    When user asks for weekly, monthly, or yearly summaries/overviews:
    - Set limit=5000 to retrieve ALL conversations in that period
    - Set max_transcript_segments=0 to exclude transcripts (reduce context size)
    - This prevents missing conversations and avoids context overflow from transcripts
    Examples: "summarize my week", "what did I do this month", "recap my year"

    Transcript retrieval guidance:
    - By default (max_transcript_segments=0), no transcript segments are included
    - Only increase max_transcript_segments when user explicitly needs transcript content
    - Use reasonable limits (10-50 segments) for most queries - this usually covers key parts
    - Set max_transcript_segments=100 only when user needs extensive transcript details
    - AVOID max_transcript_segments=-1 (full transcript) unless absolutely critical:
      * User explicitly asks for "full transcript" or "complete unabridged transcript"
      * User needs to analyze the entire conversation word-by-word
      * WARNING: -1 can return thousands of segments and flood context
    - Prefer using conversation summaries/overviews when possible instead of full transcripts
    - Maximum allowed is 1000 segments to prevent context overflow

    To include transcripts efficiently:
    - Start with max_transcript_segments=20 for basic transcript needs
    - Use max_transcript_segments=50 for detailed questions
    - Only use max_transcript_segments=-1 as last resort when complete transcript is explicitly required

    Args:
        start_date: Filter conversations after this date (ISO format in user's timezone: YYYY-MM-DDTHH:MM:SS+HH:MM, e.g. "2024-01-19T15:00:00-08:00")
        end_date: Filter conversations before this date (ISO format in user's timezone: YYYY-MM-DDTHH:MM:SS+HH:MM, e.g. "2024-01-19T23:59:59-08:00")
        limit: Number of conversations to retrieve (default: 20, max: 100)
        offset: Pagination offset (default: 0)
        include_discarded: Include deleted conversations (default: False)
        statuses: Filter by status, comma-separated (default: all)
        max_transcript_segments: Limit transcript segments (default: 0=none, suggest 20-50 for normal use, avoid -1 except when critical, max: 1000)
        include_transcript: Include full transcript (default: True)
        include_timestamps: Add timestamps to transcript segments (default: False)

    Returns:
        Formatted string with conversation details including title, overview, transcript, photos,
        action items, events, and attendees.
    """
    print(f"🔧 get_conversations_tool called with params:")
    print(f"   start_date: {start_date}")
    print(f"   end_date: {end_date}")
    print(f"   limit: {limit}")
    print(f"   offset: {offset}")
    print(f"   include_discarded: {include_discarded}")
    print(f"   statuses: {statuses}")
    print(f"   max_transcript_segments: {max_transcript_segments}")
    print(f"   include_transcript: {include_transcript}")
    print(f"   include_timestamps: {include_timestamps}")
    # print(f"   config: {config}")

    # Get config from parameter or context variable (like other tools do)
    if config is None:
        try:
            config = agent_config_context.get()
            if config:
                print(f"🔧 get_conversations_tool - got config from context variable")
        except LookupError:
            print(f"❌ get_conversations_tool - config not found in context variable")
            config = None

    if config is None:
        print(f"❌ get_conversations_tool - config is None")
        return "Error: Configuration not available"

    try:
        uid = config['configurable'].get('user_id')
    except (KeyError, TypeError) as e:
        print(f"❌ get_conversations_tool - error accessing config: {e}")
        return "Error: Configuration not available"

    if not uid:
        print(f"❌ get_conversations_tool - no user_id in config")
        return "Error: User ID not found in configuration"
    print(f"✅ get_conversations_tool - uid: {uid}")

    # Get safety guard from config if available
    safety_guard = config['configurable'].get('safety_guard')

    # Cap max_transcript_segments at 1000 to prevent flooding LLM context
    if max_transcript_segments != -1:
        max_transcript_segments = min(max_transcript_segments, 1000)
        print(f"📊 max_transcript_segments capped at: {max_transcript_segments}")

    # Parse dates if provided (always in UTC)
    start_dt = None
    end_dt = None

    if start_date:
        try:
            # Parse ISO format with timezone - should be in user's timezone (YYYY-MM-DDTHH:MM:SS+HH:MM)
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            if start_dt.tzinfo is None:
                return f"Error: start_date must include timezone in user's timezone format YYYY-MM-DDTHH:MM:SS+HH:MM (e.g., '2024-01-19T15:00:00-08:00'): {start_date}"
            print(f"📅 Parsed start_date '{start_date}' as {start_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        except ValueError as e:
            return f"Error: Invalid start_date format. Expected YYYY-MM-DDTHH:MM:SS+HH:MM in user's timezone: {start_date} - {str(e)}"

    if end_date:
        try:
            # Parse ISO format with timezone - should be in user's timezone (YYYY-MM-DDTHH:MM:SS+HH:MM)
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            if end_dt.tzinfo is None:
                return f"Error: end_date must include timezone in user's timezone format YYYY-MM-DDTHH:MM:SS+HH:MM (e.g., '2024-01-19T23:59:59-08:00'): {end_date}"
            print(f"📅 Parsed end_date '{end_date}' as {end_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        except ValueError as e:
            return f"Error: Invalid end_date format. Expected YYYY-MM-DDTHH:MM:SS+HH:MM in user's timezone: {end_date} - {str(e)}"

    # Limit to reasonable max
    limit = min(limit, 5000)

    # Parse statuses if provided
    status_list = []
    if statuses:
        status_list = [s.strip() for s in statuses.split(',') if s.strip()]

    # Get conversations
    conversations_data = conversations_db.get_conversations(
        uid,
        limit=limit,
        offset=offset,
        start_date=start_dt,
        end_date=end_dt,
        include_discarded=include_discarded,
        statuses=status_list,
    )

    print(f"📊 get_conversations_tool - found {len(conversations_data) if conversations_data else 0} conversations")

    if not conversations_data:
        date_info = ""
        if start_dt and end_dt:
            date_info = f" between {start_dt.strftime('%Y-%m-%d')} and {end_dt.strftime('%Y-%m-%d')}"
        elif start_dt:
            date_info = f" after {start_dt.strftime('%Y-%m-%d')}"
        elif end_dt:
            date_info = f" before {end_dt.strftime('%Y-%m-%d')}"

        msg = f"No conversations found{date_info}. The user may not have recorded any conversations yet, or the date range may be outside their conversation history."
        print(f"⚠️ get_conversations_tool - {msg}")
        return msg

    try:
        # Only load people if transcripts will be included (people are used for speaker names in transcripts)
        people = []
        if include_transcript:
            # Get all person IDs from all conversations
            all_person_ids = set()
            for conv_data in conversations_data:
                segments = conv_data.get('transcript_segments', [])
                all_person_ids.update([s.get('person_id') for s in segments if s.get('person_id')])

            print(f"🔍 get_conversations_tool - Found {len(all_person_ids)} unique person IDs")

            # Fetch people data
            if all_person_ids:
                people_data = users_db.get_people_by_ids(uid, list(all_person_ids))
                people = [Person(**p) for p in people_data]
                print(f"🔍 get_conversations_tool - Loaded {len(people)} people")
        else:
            print(f"🔍 get_conversations_tool - Skipping people loading (transcript not included)")

        # Convert to Conversation objects
        conversations = []
        for conv_data in conversations_data:
            try:
                conversation = Conversation(**conv_data)

                # Limit transcript segments if needed (mimicking integration.py pattern)
                if (
                    max_transcript_segments != -1
                    and conversation.transcript_segments
                    and len(conversation.transcript_segments) > max_transcript_segments
                ):
                    conversation.transcript_segments = conversation.transcript_segments[:max_transcript_segments]

                conversations.append(conversation)
            except Exception as e:
                print(f"Error parsing conversation {conv_data.get('id')}: {str(e)}")
                continue

        print(f"🔍 get_conversations_tool - Converted {len(conversations)} conversation objects")

        # Store conversations in config for citation tracking (as lightweight dicts)
        conversations_collected = config['configurable'].get('conversations_collected', [])
        for conv in conversations:
            conv_dict = conv.dict()
            # Remove heavy fields to reduce memory usage
            conv_dict.pop('transcript_segments', None)
            conv_dict.pop('photos', None)
            conv_dict.pop('audio_files', None)
            conversations_collected.append(conv_dict)
        print(
            f"📚 get_conversations_tool - Added {len(conversations)} conversations to collection (total: {len(conversations_collected)})"
        )

        # Return formatted string
        result = Conversation.conversations_to_string(
            conversations, use_transcript=include_transcript, include_timestamps=include_timestamps, people=people
        )
        print(f"🔍 get_conversations_tool - Generated result string, length: {len(result)}")
        return result

    except Exception as e:
        error_msg = f"Error formatting conversations: {str(e)}"
        print(f"❌ get_conversations_tool - {error_msg}")
        import traceback

        traceback.print_exc()
        return f"Found {len(conversations_data)} conversations but encountered an error formatting them: {str(e)}"


@tool
def search_conversations_tool(
    query: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 5,
    max_transcript_segments: int = 0,
    include_transcript: bool = True,
    include_timestamps: bool = False,
    config: RunnableConfig = None,
) -> str:
    """
    Search conversations using semantic vector search - USE THIS FOR EVENTS/INCIDENTS.

    This tool uses AI embeddings to find conversations that are semantically similar to your query,
    even if they don't contain the exact keywords. Perfect for finding when specific events happened.

    **CRITICAL: Use this tool for EVENT/INCIDENT questions:**
    - "When did a dog bite me?" → USE THIS TOOL
    - "What happened at the party?" → USE THIS TOOL
    - "When did I get injured?" → USE THIS TOOL
    - "When did I meet John?" → USE THIS TOOL
    - "What did I say about the accident?" → USE THIS TOOL
    - Any "when did X happen?" or "what happened when Y?" questions → USE THIS TOOL

    **When to use this tool:**
    - Questions about SPECIFIC EVENTS or INCIDENTS that happened to the user
    - Searching for concepts, themes, or topics (e.g., "discussions about personal growth", "health-related talks")
    - Finding similar conversations even without exact keyword matches
    - Broad subject searches (e.g., "what have I talked about regarding relationships?")
    - Understanding overall themes or patterns in conversations

    **When NOT to use this tool:**
    - For user preferences/facts (use get_memories_tool for "what's my favorite X?", "do I like Y?")

    **Tip:** For best results, use descriptive phrases about the event or concept you're looking for.

    Transcript retrieval guidance (same as other conversation tools):
    - By default (max_transcript_segments=0), no transcript segments are included
    - Only increase when user explicitly needs transcript content
    - Use 20-50 segments for most queries
    - Avoid -1 (full transcript) unless absolutely critical
    - Maximum allowed is 1000 segments to prevent context overflow

    Args:
        query: Natural language description of the concept/topic to search for (required)
        start_date: Filter conversations after this date (ISO format in user's timezone: YYYY-MM-DDTHH:MM:SS+HH:MM)
        end_date: Filter conversations before this date (ISO format in user's timezone: YYYY-MM-DDTHH:MM:SS+HH:MM)
        limit: Number of conversations to retrieve (default: 5, max: 20)
        max_transcript_segments: Limit transcript segments (default: 0=none, suggest 20-50 for normal use, max: 1000)
        include_transcript: Include full transcript (default: True)
        include_timestamps: Add timestamps to transcript segments (default: False)

    Returns:
        Formatted string with semantically matching conversations ranked by relevance, including transcripts,
        summaries, action items, events, and metadata.
    """
    print(f"🔧 search_conversations_tool called with query: {query}")

    # Get config from parameter or context variable (like other tools do)
    if config is None:
        try:
            config = agent_config_context.get()
            if config:
                print(f"🔧 search_conversations_tool - got config from context variable")
        except LookupError:
            print(f"❌ search_conversations_tool - config not found in context variable")
            config = None

    if config is None:
        print(f"❌ search_conversations_tool - config is None")
        return "Error: Configuration not available"

    try:
        uid = config['configurable'].get('user_id')
    except (KeyError, TypeError) as e:
        print(f"❌ search_conversations_tool - error accessing config: {e}")
        return "Error: Configuration not available"

    if not uid:
        print(f"❌ search_conversations_tool - no user_id in config")
        return "Error: User ID not found in configuration"
    print(f"✅ search_conversations_tool - uid: {uid}, query: {query}, limit: {limit}")

    # Get safety guard from config if available
    safety_guard = config['configurable'].get('safety_guard')

    # Cap max_transcript_segments at 1000 to prevent flooding LLM context
    if max_transcript_segments != -1:
        max_transcript_segments = min(max_transcript_segments, 1000)
        print(f"📊 max_transcript_segments capped at: {max_transcript_segments}")

    # Parse dates for Firestore exact/date retrieval and Pinecone metadata filters.
    # Firestore is the source of truth for exact/date lookups; Pinecone is a
    # semantic helper and may lag if vector coverage is incomplete.
    start_dt = None
    end_dt = None
    starts_at = None
    ends_at = None

    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            if start_dt.tzinfo is None:
                return f"Error: start_date must include timezone in user's timezone format YYYY-MM-DDTHH:MM:SS+HH:MM (e.g., '2024-01-19T15:00:00-08:00'): {start_date}"
            print(f"📅 Parsed start_date '{start_date}' as {start_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            starts_at = int(start_dt.timestamp())
        except ValueError as e:
            return f"Error: Invalid start_date format. Expected YYYY-MM-DDTHH:MM:SS+HH:MM in user's timezone: {start_date} - {str(e)}"

    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            if end_dt.tzinfo is None:
                return f"Error: end_date must include timezone in user's timezone format YYYY-MM-DDTHH:MM:SS+HH:MM (e.g., '2024-01-19T23:59:59-08:00'): {end_date}"
            print(f"📅 Parsed end_date '{end_date}' as {end_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            ends_at = int(end_dt.timestamp())
        except ValueError as e:
            return f"Error: Invalid end_date format. Expected YYYY-MM-DDTHH:MM:SS+HH:MM in user's timezone: {end_date} - {str(e)}"

    # Limit to reasonable max
    limit = min(limit, 20)

    try:
        # First-class exact/date retrieval from Firestore. This catches newly
        # indexed conversations even when Pinecone vectors have not been
        # backfilled yet.
        exact_candidates = conversations_db.get_conversations_without_photos(
            uid,
            limit=max(50, limit * 10),
            offset=0,
            start_date=start_dt,
            end_date=end_dt,
            include_discarded=False,
        )
        exact_ids = _rank_exact_conversation_ids(
            exact_candidates, query, limit, allow_date_only=bool(start_dt or end_dt)
        )
        print(f"📊 search_conversations_tool - exact/date Firestore results: {len(exact_ids)}")

        # Semantic vector search is still useful for fuzzy concepts, but it is
        # not the only retrieval path.
        vector_ids = []
        try:
            vector_ids = vector_db.query_vectors(query=query, uid=uid, starts_at=starts_at, ends_at=ends_at, k=limit)
        except Exception as vector_error:
            print(
                f"⚠️ search_conversations_tool - vector helper failed, using Firestore exact/date results: {vector_error}"
            )

        conversation_ids = _merge_ranked_ids(exact_ids, vector_ids, limit)

        print(
            f"📊 search_conversations_tool - merged results exact={len(exact_ids)} "
            f"vector={len(vector_ids)} total={len(conversation_ids)} for query: '{query}'"
        )

        if not conversation_ids:
            date_info = ""
            if start_dt and end_dt:
                date_info = f" in the specified date range"
            elif start_dt:
                date_info = f" after the specified start date"
            elif end_dt:
                date_info = f" before the specified end date"

            msg = f"No conversations found matching the concept '{query}'{date_info}. The user may not have discussed this topic yet, or it may not be in their recorded conversation history."
            print(f"⚠️ search_conversations_tool - {msg}")
            return msg

        # Get full conversation data
        conversations_data = conversations_db.get_conversations_by_id(uid, conversation_ids)

        if not conversations_data:
            return f"No conversations found matching query: '{query}'"

        print(f"🔍 search_conversations_tool - Loaded {len(conversations_data)} full conversations")

        # Only load people if transcripts will be included
        people = []
        if include_transcript:
            # Get all person IDs
            all_person_ids = set()
            for conv_data in conversations_data:
                segments = conv_data.get('transcript_segments', [])
                all_person_ids.update([s.get('person_id') for s in segments if s.get('person_id')])

            print(f"🔍 search_conversations_tool - Found {len(all_person_ids)} unique person IDs")

            # Fetch people data
            if all_person_ids:
                people_data = users_db.get_people_by_ids(uid, list(all_person_ids))
                people = [Person(**p) for p in people_data]
                print(f"🔍 search_conversations_tool - Loaded {len(people)} people")
        else:
            print(f"🔍 search_conversations_tool - Skipping people loading (transcript not included)")

        # Convert to Conversation objects
        conversations = []
        for conv_data in conversations_data:
            try:
                conversation = Conversation(**conv_data)

                # Limit transcript segments if needed
                if (
                    max_transcript_segments != -1
                    and conversation.transcript_segments
                    and len(conversation.transcript_segments) > max_transcript_segments
                ):
                    conversation.transcript_segments = conversation.transcript_segments[:max_transcript_segments]

                conversations.append(conversation)
            except Exception as e:
                print(f"Error parsing conversation {conv_data.get('id')}: {str(e)}")
                continue

        print(f"🔍 search_conversations_tool - Converted {len(conversations)} conversation objects")

        # Store conversations in config for citation tracking (as lightweight dicts)
        conversations_collected = config['configurable'].get('conversations_collected', [])
        for conv in conversations:
            conv_dict = conv.dict()
            # Remove heavy fields to reduce memory usage
            conv_dict.pop('transcript_segments', None)
            conv_dict.pop('photos', None)
            conv_dict.pop('audio_files', None)
            conversations_collected.append(conv_dict)
        print(
            f"📚 search_conversations_tool - Added {len(conversations)} conversations to collection (total: {len(conversations_collected)})"
        )

        # Return formatted string
        result = f"Found {len(conversations)} conversations matching '{query}' via exact/date and semantic search:\n\n"
        result += Conversation.conversations_to_string(
            conversations, use_transcript=include_transcript, include_timestamps=include_timestamps, people=people
        )

        print(f"🔍 search_conversations_tool - Generated result string, length: {len(result)}")

        return result

    except Exception as e:
        error_msg = f"Error performing vector search: {str(e)}"
        print(f"❌ search_conversations_tool - {error_msg}")
        import traceback

        traceback.print_exc()
        return f"Found vector search results but encountered an error processing them: {str(e)}"
