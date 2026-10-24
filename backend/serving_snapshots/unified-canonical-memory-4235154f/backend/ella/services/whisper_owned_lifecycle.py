"""Exact-work deadline/drain metadata; no runtime DDL, no provider cancellation claim."""

import asyncio
import math
import json
import time

from ella.services.whisper_delivery import DeliveryConflict, DeliveryNotFound, identifier


class ResponseBudget:
    def __init__(self, unix_seconds):
        if not isinstance(unix_seconds, (float, int)) or not math.isfinite(unix_seconds):
            raise DeliveryConflict('tool_deadline_invalid')
        self.unix = float(unix_seconds)
        self.end = time.monotonic() + max(0, self.unix - time.time())

    def remaining(self):
        return max(0, self.end - time.monotonic())

    def check(self):
        if self.remaining() <= 0:
            raise DeliveryConflict('tool_response_deadline_expired')


class LifecycleStore:
    def __init__(self, pool):
        self.pool = pool

    async def read(self, uid, work_id):
        identifier(work_id)
        async with self.pool.acquire(timeout=5) as conn:
            row = await conn.fetchrow(
                '''SELECT l.*,w.status,w.close_result,w.receipt
                FROM guardian_delivery_work_lifecycle l JOIN guardian_delivery_tool_work w
                USING(uid,tool_work_id) WHERE l.uid=$1 AND l.tool_work_id=$2''',
                uid,
                work_id,
            )
        if not row:
            raise DeliveryNotFound('tool_lifecycle_unknown')
        record = dict(row)
        for field in ('provider_receipt', 'receipt'):
            if isinstance(record[field], str):
                record[field] = json.loads(record[field])
        completed = record['provider_state'] == 'completed' and not record['active_synthesis']
        return {
            'tool_work_id': work_id,
            'response_deadline_unix': record['acceptance_deadline'].timestamp(),
            'provider_state': record['provider_state'],
            'active_synthesis': record['active_synthesis'],
            'synthesis_owner_state': (
                'active'
                if (uid, work_id) in _owned_syntheses
                else 'recovery_unknown' if record['active_synthesis'] else 'finished'
            ),
            'driver_state': record['driver_state'],
            'work_status': record['status'],
            'remote_cleanup': (
                'completed_response'
                if completed
                else 'not_started' if record['provider_state'] == 'not_started' else 'unknown'
            ),
            'provider_receipt': record['provider_receipt'],
            'delivery_receipt': record['receipt'],
        }

    async def driver(self, uid, work_id, state):
        if state not in {'running', 'completed', 'response_expired', 'cancel_requested', 'error'}:
            raise ValueError('invalid driver state')
        async with self.pool.acquire(timeout=5) as conn:
            result = await conn.execute(
                '''UPDATE guardian_delivery_work_lifecycle SET driver_state=$3,
                updated_at=clock_timestamp() WHERE uid=$1 AND tool_work_id=$2''',
                uid,
                work_id,
                state,
            )
            if result != 'UPDATE 1':
                raise DeliveryNotFound('tool_lifecycle_unknown')

    async def shorten(self, uid, work_id, unix_seconds):
        ResponseBudget(unix_seconds).check()
        async with self.pool.acquire(timeout=5) as conn:
            row = await conn.fetchrow(
                '''UPDATE guardian_delivery_work_lifecycle
                SET acceptance_deadline=LEAST(acceptance_deadline,to_timestamp($3))
                WHERE uid=$1 AND tool_work_id=$2 RETURNING acceptance_deadline''',
                uid,
                work_id,
                unix_seconds,
            )
        if not row:
            raise DeliveryNotFound('tool_lifecycle_unknown')
        return row['acceptance_deadline'].timestamp()

    async def begin_synthesis(self, uid, work_id):
        async with self.pool.acquire(timeout=5) as conn:
            result = await conn.fetchrow(
                '''UPDATE guardian_delivery_work_lifecycle
                SET active_synthesis=TRUE,provider_state='pending',updated_at=clock_timestamp()
                WHERE uid=$1 AND tool_work_id=$2 AND provider_state='not_started'
                AND acceptance_deadline>clock_timestamp() RETURNING tool_work_id''',
                uid,
                work_id,
            )
            if not result:
                raise DeliveryConflict('tool_synthesis_not_admitted')

    async def provider_success(self, uid, work_id, receipt):
        async with self.pool.acquire(timeout=5) as conn:
            await conn.execute(
                '''UPDATE guardian_delivery_work_lifecycle SET provider_state='completed',
                provider_receipt=$3::jsonb,updated_at=clock_timestamp() WHERE uid=$1 AND tool_work_id=$2''',
                uid,
                work_id,
                receipt,
            )

    async def synthesis_done(self, uid, work_id, successful):
        async with self.pool.acquire(timeout=5) as conn:
            await conn.execute(
                '''UPDATE guardian_delivery_work_lifecycle SET active_synthesis=FALSE,
                provider_state=CASE WHEN provider_state='completed' THEN 'completed' ELSE 'unknown' END,
                updated_at=clock_timestamp() WHERE uid=$1 AND tool_work_id=$2''',
                uid,
                work_id,
            )


_owned_syntheses = {}


async def synthesize_owned(lifecycle, uid, work, synthesize, message, budget):
    key = (uid, work['tool_work_id'])
    if key in _owned_syntheses:
        raise DeliveryConflict('tool_synthesis_already_owned')
    budget.check()
    await lifecycle.begin_synthesis(*key)

    async def worker():
        successful = False
        try:
            value = await synthesize(uid, message, work)
            successful = True
            return value
        finally:
            # Finishes only when the real synth coroutine ends. A driver timeout does not clear it.
            await lifecycle.synthesis_done(*key, successful)

    task = asyncio.create_task(worker())
    _owned_syntheses[key] = task

    def done(completed):
        _owned_syntheses.pop(key, None)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(done)
    # No cancellation of shared Kokoro: retain natural completion and supervise separately.
    return await asyncio.wait_for(asyncio.shield(task), budget.remaining())
