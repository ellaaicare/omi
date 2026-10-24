import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from pinecone import Pinecone

from database.honcho_attestation import authority_credential
from models.conversation import Conversation
from utils.llm.clients import embeddings

logger = logging.getLogger(__name__)

if 'PINECONE_API_KEY' in os.environ:
    pc = Pinecone(api_key=authority_credential('PINECONE_API_KEY', strip=False))
    index = pc.Index(os.getenv('PINECONE_INDEX_NAME', ''))
else:
    index = None

CONVERSATIONS_NAMESPACE = "ns1"


def build_conversation_vector_id(uid: str, conversation_id: str) -> str:
    return f'{uid}-{conversation_id}'


def _get_data(uid: str, conversation_id: str, vector: List[float]):
    return {
        "id": build_conversation_vector_id(uid, conversation_id),
        "values": vector,
        'metadata': {
            'uid': uid,
            'memory_id': conversation_id,
            'created_at': int(datetime.now(timezone.utc).timestamp()),
        },
    }


def upsert_vector(uid: str, conversation: Conversation, vector: List[float]):
    if index is None:
        logger.warning(
            "pinecone_index_unavailable action=upsert_vector uid=%s conversation_id=%s", uid, conversation.id
        )
        return None
    try:
        res = index.upsert(vectors=[_get_data(uid, conversation.id, vector)], namespace=CONVERSATIONS_NAMESPACE)
        logger.info("conversation_vector_upserted uid=%s conversation_id=%s result=%s", uid, conversation.id, res)
        return res
    except Exception:
        logger.exception("conversation_vector_upsert_failed uid=%s conversation_id=%s", uid, conversation.id)
        raise


def upsert_vector2(uid: str, conversation: Conversation, vector: List[float], metadata: dict):
    return upsert_conversation_vector(uid, conversation.id, vector, metadata)


def upsert_conversation_vector(uid: str, conversation_id: str, vector: List[float], metadata: Optional[dict] = None):
    if index is None:
        logger.warning(
            "pinecone_index_unavailable action=upsert_conversation_vector uid=%s conversation_id=%s",
            uid,
            conversation_id,
        )
        return None
    data = _get_data(uid, conversation_id, vector)
    if metadata:
        data['metadata'].update(metadata)
    try:
        res = index.upsert(vectors=[data], namespace=CONVERSATIONS_NAMESPACE)
        logger.info(
            "conversation_vector_upserted uid=%s conversation_id=%s metadata_keys=%s result=%s",
            uid,
            conversation_id,
            sorted((metadata or {}).keys()),
            res,
        )
        return res
    except Exception:
        logger.exception("conversation_vector_upsert_failed uid=%s conversation_id=%s", uid, conversation_id)
        raise


def update_vector_metadata(uid: str, conversation_id: str, metadata: dict):
    if index is None:
        logger.warning(
            "pinecone_index_unavailable action=update_vector_metadata uid=%s conversation_id=%s",
            uid,
            conversation_id,
        )
        return None
    metadata['uid'] = uid
    metadata['memory_id'] = conversation_id
    try:
        return index.update(
            build_conversation_vector_id(uid, conversation_id),
            set_metadata=metadata,
            namespace=CONVERSATIONS_NAMESPACE,
        )
    except Exception:
        logger.exception("conversation_vector_metadata_update_failed uid=%s conversation_id=%s", uid, conversation_id)
        raise


def upsert_vectors(uid: str, vectors: List[List[float]], conversations: List[Conversation]):
    if index is None:
        logger.warning("pinecone_index_unavailable action=upsert_vectors uid=%s count=%s", uid, len(conversations))
        return None
    data = [_get_data(uid, conversation.id, vector) for conversation, vector in zip(conversations, vectors)]
    try:
        res = index.upsert(vectors=data, namespace=CONVERSATIONS_NAMESPACE)
        logger.info("conversation_vectors_upserted uid=%s count=%s result=%s", uid, len(data), res)
        return res
    except Exception:
        logger.exception("conversation_vectors_upsert_failed uid=%s count=%s", uid, len(data))
        raise


def query_vectors(query: str, uid: str, starts_at: int = None, ends_at: int = None, k: int = 5) -> List[str]:
    if index is None:
        logger.warning("pinecone_index_unavailable action=query_vectors uid=%s", uid)
        return []
    filter_data = {'uid': uid}
    if starts_at is not None:
        filter_data['created_at'] = {'$gte': starts_at, '$lte': ends_at}

    try:
        xq = embeddings.embed_query(query)
        xc = index.query(
            vector=xq,
            top_k=k,
            include_metadata=False,
            filter=filter_data,
            namespace=CONVERSATIONS_NAMESPACE,
        )
        return [item['id'].replace(f'{uid}-', '') for item in xc['matches']]
    except Exception:
        logger.exception(
            "conversation_vector_query_failed uid=%s starts_at=%s ends_at=%s k=%s", uid, starts_at, ends_at, k
        )
        raise


def fetch_existing_conversation_vector_ids(uid: str, conversation_ids: List[str]) -> set[str]:
    """Return conversation ids that currently have Pinecone vectors."""
    if index is None or not conversation_ids:
        return set()
    vector_ids = [build_conversation_vector_id(uid, conversation_id) for conversation_id in conversation_ids]
    try:
        fetched = index.fetch(ids=vector_ids, namespace=CONVERSATIONS_NAMESPACE)
        vectors = getattr(fetched, "vectors", None)
        if vectors is None and isinstance(fetched, dict):
            vectors = fetched.get("vectors") or {}
        return {
            str(vector_id).replace(f'{uid}-', '')
            for vector_id in (vectors or {}).keys()
            if str(vector_id).startswith(f'{uid}-')
        }
    except Exception:
        logger.exception("conversation_vector_fetch_failed uid=%s count=%s", uid, len(conversation_ids))
        raise


def fetch_conversation_vector_metadata(uid: str, conversation_id: str) -> Optional[dict]:
    """Return metadata for one conversation vector, or None when it is absent."""
    if index is None:
        return None
    vector_id = build_conversation_vector_id(uid, conversation_id)
    try:
        fetched = index.fetch(ids=[vector_id], namespace=CONVERSATIONS_NAMESPACE)
        vectors = getattr(fetched, 'vectors', None)
        if vectors is None and isinstance(fetched, dict):
            vectors = fetched.get('vectors') or {}
        vector = (vectors or {}).get(vector_id)
        if vector is None:
            return None
        metadata = getattr(vector, 'metadata', None)
        if metadata is None and isinstance(vector, dict):
            metadata = vector.get('metadata')
        return dict(metadata or {})
    except Exception:
        logger.exception(
            'conversation_vector_metadata_fetch_failed uid=%s conversation_id=%s',
            uid,
            conversation_id,
        )
        raise


def query_vectors_by_metadata(
    uid: str,
    vector: List[float],
    dates_filter: List[datetime],
    people: List[str],
    topics: List[str],
    entities: List[str],
    dates: List[str],
    limit: int = 5,
):
    if index is None:
        logger.warning("pinecone_index_unavailable action=query_vectors_by_metadata uid=%s", uid)
        return []
    filter_data = {
        '$and': [
            {'uid': {'$eq': uid}},
        ]
    }
    if people or topics or entities or dates:
        filter_data['$and'].append(
            {
                '$or': [
                    {'people': {'$in': people}},
                    {'topics': {'$in': topics}},
                    {'entities': {'$in': entities}},
                    # {'dates': {'$in': dates_mentioned}},
                ]
            }
        )
    if dates_filter and len(dates_filter) == 2 and dates_filter[0] and dates_filter[1]:
        print('dates_filter', dates_filter)
        filter_data['$and'].append(
            {'created_at': {'$gte': int(dates_filter[0].timestamp()), '$lte': int(dates_filter[1].timestamp())}}
        )

    try:
        xc = index.query(
            vector=vector,
            filter=filter_data,
            namespace=CONVERSATIONS_NAMESPACE,
            include_values=False,
            include_metadata=True,
            top_k=1000,
        )
    except Exception:
        logger.exception("conversation_vector_metadata_query_failed uid=%s", uid)
        raise
    if not xc['matches']:
        if len(filter_data['$and']) == 3:
            filter_data['$and'].pop(1)
            logger.info("conversation_vector_metadata_query_retry uid=%s filter=%s", uid, json.dumps(filter_data))
            try:
                xc = index.query(
                    vector=vector,
                    filter=filter_data,
                    namespace=CONVERSATIONS_NAMESPACE,
                    include_values=False,
                    include_metadata=True,
                    top_k=20,
                )
            except Exception:
                logger.exception("conversation_vector_metadata_query_retry_failed uid=%s", uid)
                raise
        else:
            return []

    conversation_id_to_matches = defaultdict(int)
    for item in xc['matches']:
        metadata = item['metadata']
        conversation_id = metadata['memory_id']
        for topic in topics:
            if topic in metadata.get('topics', []):
                conversation_id_to_matches[conversation_id] += 1
        for entity in entities:
            if entity in metadata.get('entities', []):
                conversation_id_to_matches[conversation_id] += 1
        for person in people:
            if person in metadata.get('people_mentioned', []):
                conversation_id_to_matches[conversation_id] += 1

    conversations_id = [item['id'].replace(f'{uid}-', '') for item in xc['matches']]
    conversations_id.sort(key=lambda x: conversation_id_to_matches[x], reverse=True)
    return conversations_id[:limit] if len(conversations_id) > limit else conversations_id


def delete_vector(uid: str, conversation_id: str):
    """
    Delete a conversation vector from Pinecone.

    Note: Vectors are stored with ID format '{uid}-{conversation_id}'
    """
    if index is None:
        logger.warning(
            "pinecone_index_unavailable action=delete_vector uid=%s conversation_id=%s", uid, conversation_id
        )
        return None
    vector_id = build_conversation_vector_id(uid, conversation_id)
    try:
        result = index.delete(ids=[vector_id], namespace=CONVERSATIONS_NAMESPACE)
        logger.info("conversation_vector_deleted uid=%s conversation_id=%s result=%s", uid, conversation_id, result)
        return result
    except Exception:
        logger.exception("conversation_vector_delete_failed uid=%s conversation_id=%s", uid, conversation_id)
        raise


# ==========================================
# Memory Vector Functions
# For memory embeddings and semantic search
# ==========================================

MEMORIES_NAMESPACE = "ns2"


def upsert_memory_vector(uid: str, memory_id: str, content: str, category: str):
    """
    Upsert a memory embedding to Pinecone.
    """
    if index is None:
        print('Pinecone index not initialized, skipping memory vector upsert')
        return None

    vector = embeddings.embed_query(content)
    data = {
        "id": f'{uid}-{memory_id}',
        "values": vector,
        "metadata": {
            "uid": uid,
            "memory_id": memory_id,
            "category": category,
            "created_at": int(datetime.now(timezone.utc).timestamp()),
        },
    }
    res = index.upsert(vectors=[data], namespace=MEMORIES_NAMESPACE)
    print('upsert_memory_vector', memory_id, res)
    return vector


def find_similar_memories(uid: str, content: str, threshold: float = 0.85, limit: int = 5) -> List[dict]:
    """
    Find memories similar to the given content.
    Returns list of matches with similarity scores.
    Used for duplicate detection and semantic search.
    """
    if index is None:
        print('Pinecone index not initialized, skipping similarity search')
        return []

    vector = embeddings.embed_query(content)
    filter_data = {'uid': uid}

    xc = index.query(
        vector=vector, top_k=limit, include_metadata=True, filter=filter_data, namespace=MEMORIES_NAMESPACE
    )

    results = []
    for match in xc.get('matches', []):
        if match['score'] >= threshold:
            results.append(
                {
                    'memory_id': match['metadata'].get('memory_id'),
                    'category': match['metadata'].get('category'),
                    'score': match['score'],
                }
            )

    return results


def check_memory_duplicate(uid: str, content: str, threshold: float = 0.85) -> dict | None:
    """
    Check if a similar memory already exists.
    Returns the duplicate info if found, None otherwise.
    """
    similar = find_similar_memories(uid, content, threshold=threshold, limit=1)
    if similar:
        print(f'Found duplicate memory: {similar[0]}')
        return similar[0]
    return None


def search_memories_by_vector(uid: str, query: str, limit: int = 10) -> List[str]:
    """
    Semantic search for memories.
    Returns list of memory_ids ordered by relevance.
    """
    if index is None:
        print('Pinecone index not initialized, skipping memory search')
        return []

    vector = embeddings.embed_query(query)
    filter_data = {'uid': uid}

    xc = index.query(
        vector=vector, top_k=limit, include_metadata=True, filter=filter_data, namespace=MEMORIES_NAMESPACE
    )

    return [match['metadata'].get('memory_id') for match in xc.get('matches', [])]


def delete_memory_vector(uid: str, memory_id: str):
    """
    Delete a memory vector from Pinecone.
    """
    if index is None:
        print('Pinecone index not initialized, skipping memory vector delete')
        return

    vector_id = f'{uid}-{memory_id}'
    result = index.delete(ids=[vector_id], namespace=MEMORIES_NAMESPACE)
    print('delete_memory_vector', vector_id, result)
