"""Capture-owned ambient deadlines; no transcript synthesis or provider calls."""

import asyncio
from typing import Callable


class AmbientDeadlines:
    def __init__(
        self, enqueue: Callable, discard: Callable, active: Callable, current: Callable, *, max_pending: int = 32
    ):
        self._loop = asyncio.get_running_loop()
        self._enqueue = enqueue
        self._discard = discard
        self._active = active
        self._current = current
        self._max_pending = max_pending
        self._pending = {}
        self._closed = False

    @staticmethod
    def _key(item):
        return tuple(
            item.get(k) for k in ('uid', 'conversation_id', 'origin_generation', 'origin_owner_token', 'device_type')
        )

    def schedule_from_thread(self, item: dict, receipt: dict) -> None:
        # Keep only the dispatch context; the batch owns its actual segments.
        item = {**item, 'segments': [], 'ambient_batch_id': receipt['batch_id']}
        try:
            self._loop.call_soon_threadsafe(self._schedule, item, dict(receipt))
        except RuntimeError:
            self._discard(item, receipt['batch_id'])

    def _schedule(self, item: dict, receipt: dict) -> None:
        key = self._key(item)
        if (
            self._closed
            or not self._active(item)
            or not self._current(item, receipt['batch_id'])
            or (key not in self._pending and len(self._pending) >= self._max_pending)
        ):
            self._discard(item, receipt['batch_id'])
            return
        previous = self._pending.pop(key, None)
        if previous:
            if previous[2] is not None:
                previous[2].cancel()
            if previous[1]['batch_id'] != receipt['batch_id']:
                self._discard(previous[0], previous[1]['batch_id'])
        deadline = receipt.get('deadline_monotonic', self._loop.time() + receipt['delay_seconds'])
        if previous and previous[1]['batch_id'] == receipt['batch_id'] and previous[2] is not None:
            deadline = min(deadline, previous[1]['deadline_monotonic'])
        receipt['deadline_monotonic'] = deadline
        handle = self._loop.call_later(max(0, deadline - self._loop.time()), self._fire, key, receipt['batch_id'])
        self._pending[key] = (item, receipt, handle)

    def _fire(self, key, batch_id) -> None:
        pending = self._pending.get(key)
        if not pending or pending[1]['batch_id'] != batch_id or pending[2] is None:
            return
        item, receipt, _handle = pending
        self._pending[key] = (item, receipt, None)
        if self._closed or not self._active(item) or not self._current(item, batch_id) or not self._enqueue(item):
            self.complete(item, batch_id)

    def complete(self, item: dict, batch_id: str) -> None:
        key = self._key(item)
        pending = self._pending.get(key)
        if pending and pending[1]['batch_id'] == batch_id:
            self._pending.pop(key)
            if pending[2] is not None:
                pending[2].cancel()
        self._discard(item, batch_id)

    def discard_except(self, conversation_id: str) -> None:
        for key, pending in tuple(self._pending.items()):
            if pending[0]['conversation_id'] != conversation_id:
                self._pending.pop(key)
                if pending[2] is not None:
                    pending[2].cancel()
                self._discard(pending[0], pending[1]['batch_id'])

    def close(self) -> None:
        self._closed = True
        for item, receipt, handle in self._pending.values():
            if handle is not None:
                handle.cancel()
            self._discard(item, receipt['batch_id'])
        self._pending.clear()
