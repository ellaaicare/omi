"""Read-only SQL/state adapter; projection writes belong to the additive trigger."""
import asyncio
import json
from datetime import datetime, timezone

from ella.services.muse_private_reader import ReaderError


def row_dict(row):
    value = dict(row)
    if isinstance(value['payload'], str):
        value['payload'] = json.loads(value['payload'])
    if isinstance(value.get('available_at'), datetime):
        value['available_at'] = value['available_at'].isoformat()
    return value


class SourceStore:
    def __init__(self, pool, capture_observer, source_channel=None):
        self.pool, self.capture_observer = pool, capture_observer
        self.source_channel = source_channel

    async def window(self, uid, position, limit):
        async with self.pool.acquire(timeout=3) as connection:
            async with connection.transaction(readonly=True, isolation='repeatable_read'):
                await connection.execute("SET LOCAL statement_timeout='3000ms'")
                current = await connection.fetchval(
                    'SELECT last_sequence FROM ella_muse_source_positions WHERE uid=$1', uid, timeout=3)
                current = current or 0
                if position is None:
                    return current, []
                if current < position['watermark']:
                    raise ReaderError('feed_reset', 409)
                watermark = (current if position['after'] == position['watermark']
                             else position['watermark'])
                rows = await connection.fetch(
                    '''SELECT uid,canonical_identity,sequence,source_id,operation,
                              previous_sequence,payload,available_at
                       FROM ella_muse_source_changes
                       WHERE uid=$1 AND canonical_identity=$1 AND sequence>$2 AND sequence<=$3
                       AND ($5::text IS NULL OR payload->>'channel'=$5)
                       ORDER BY sequence ASC LIMIT $4''', uid, position['after'], watermark, limit, self.source_channel, timeout=3)
                return watermark, [row_dict(row) for row in rows]

    async def latest(self, uid, source_id):
        row = await self.pool.fetchrow(
            '''SELECT c.uid,c.canonical_identity,c.sequence,c.source_id,c.operation,
                      c.previous_sequence,c.payload,c.available_at,
                      ella_muse_transcript_text(to_jsonb(e)) AS current_text
               FROM ella_muse_source_changes c
               LEFT JOIN canonical_events e ON e.uid=c.uid AND e.canonical_identity=c.uid
                 AND e.event_id=c.event_id AND e.source_identity=c.source_identity
                 AND e.channel IN ('omi_transcript','omi','muse_fictional_test') AND NOT EXISTS(SELECT 1 FROM ella_muse_deleted_conversations d WHERE d.uid=c.uid AND d.conversation_id=e.source_ref->>'conversation_id') AND e.privacy_scope='user_private'
               WHERE c.uid=$1 AND c.canonical_identity=$1 AND c.source_id=$2 AND ($3::text IS NULL OR c.payload->>'channel'=$3)
               ORDER BY c.sequence DESC LIMIT 1''', uid, source_id, self.source_channel, timeout=3)
        return row_dict(row) if row else None

    async def capture_status(self, uid):
        return await asyncio.wait_for(asyncio.to_thread(self.capture_observer, uid), 4)


def capture_observer(uid):
    from utils.conversations.capture_protocol import _authority_ref, _conversation_ref
    from database import redis_db
    authority = _authority_ref(uid).get(timeout=3, retry=None).to_dict() or {}
    conversation_id = authority.get('conversation_id')
    conversation = (_conversation_ref(uid, conversation_id).get(timeout=3, retry=None).to_dict() or {}
                    if isinstance(conversation_id, str) and conversation_id else {})
    now = datetime.now(timezone.utc)
    state = authority.get('state')
    state = state if state in ('active', 'drained', 'finalizing', 'terminal') else 'unknown'
    def live(value):
        return isinstance(value, datetime) and value.tzinfo is not None and value > now
    exact = bool(conversation_id and authority.get('generation')
                 and authority.get('owner_token')
                 and conversation.get('capture_protocol_version') == 2
                 and conversation.get('capture_generation') == authority.get('generation')
                 and conversation.get('capture_owner_token') == authority.get('owner_token'))
    return {'server_capture_state': state,
            'server_capture_lease_current': bool(exact and state == 'active'
              and conversation.get('capture_state') == 'active'
              and conversation.get('capture_owner_id') == authority.get('owner_token')
              and live(authority.get('lease_expires_at')) and live(conversation.get('capture_lease_expires_at'))),
            'capture_pointer_present': bool(redis_db.get_in_progress_conversation_id(uid)),
            'socket_owner_present': bool(redis_db.get_in_progress_conversation_owner(uid)),
            'server_observed_at': now.isoformat()}
