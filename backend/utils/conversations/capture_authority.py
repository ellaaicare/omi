import asyncio
import os
from typing import Awaitable, Callable, Optional, Set

from database import conversations as conversations_db
from database import redis_db


def capture_protocol_v2_rollout_enabled() -> bool:
    """V2 stays fail-closed until the documented legacy drain is attested."""
    return os.getenv('CAPTURE_PROTOCOL_V2_ROLLOUT_STATE', '').strip() == 'legacy_workers_drained'


def valid_capture_drain_body(
    body: dict,
    protocol_version: int,
    conversation_id: str,
    generation: str,
    owner_token: str,
) -> bool:
    return bool(
        body.get('type') == 'capture_drain'
        and body.get('protocol_version') == protocol_version
        and body.get('conversation_id') == conversation_id
        and body.get('generation') == generation
        and body.get('owner_token') == owner_token
    )


async def flush_capture_before_drained(
    finish_stt_inputs: Callable[[], Optional[Awaitable[None]]],
    persistence_tasks: Set[asyncio.Task],
    buffers_drained: asyncio.Event,
    *,
    timeout: float = 10.0,
) -> bool:
    """Stop STT input, await accepted background work, then await durable buffers."""
    # Discard an earlier idle indication before provider shutdown can schedule
    # its final transcript callback onto the persistence loop.
    buffers_drained.clear()
    result = finish_stt_inputs()
    if result is not None:
        await result
    if persistence_tasks:
        await asyncio.gather(*tuple(persistence_tasks), return_exceptions=True)
    try:
        await asyncio.wait_for(buffers_drained.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    return True


class CaptureStreamAuthority:
    """Durable Firestore authority for one protocol-v2 transcript stream.

    Redis is only a compatibility projection. Every durable mutation checks the
    generation and owner token in the same Firestore transaction as its data.
    """

    def __init__(
        self,
        uid: str,
        generation_id: str,
        owner_token: str,
        on_loss: Optional[Callable[[str, str], None]] = None,
    ):
        self.uid = uid
        self.generation_id = generation_id
        self.owner_token = owner_token
        self.lost = False
        self._on_loss = on_loss

    def _lose(self, checkpoint: str, conversation_id: str) -> bool:
        if not self.lost:
            self.lost = True
            if self._on_loss:
                self._on_loss(checkpoint, conversation_id)
        return False

    def lose(self, checkpoint: str, conversation_id: str) -> bool:
        return self._lose(checkpoint, conversation_id)

    def _project(self, conversation_id: str, state: str = 'active') -> None:
        try:
            redis_db.project_capture_stream_authority(
                self.uid,
                self.generation_id,
                self.owner_token,
                conversation_id,
                state,
            )
        except Exception as error:
            # Firestore is authoritative. A Redis outage cannot revoke a
            # transactionally installed owner or reopen a stale write window.
            print('Capture authority Redis projection failed', self.uid, conversation_id, error)

    def refresh(self, conversation_id: str, checkpoint: str) -> bool:
        if self.lost:
            return False
        if not conversations_db.renew_capture_authority(
            self.uid,
            conversation_id,
            self.generation_id,
            self.owner_token,
        ):
            return self._lose(checkpoint, conversation_id)
        self._project(conversation_id)
        return True

    def adopt(self, conversation_id: str, checkpoint: str) -> bool:
        if self.lost:
            return False
        result = conversations_db.adopt_capture_conversation(
            self.uid,
            conversation_id,
            self.generation_id,
            self.owner_token,
        )
        if result != 'adopted':
            return self._lose(f'{checkpoint}_{result}', conversation_id)
        self._project(conversation_id)
        return True

    def acquire(self, conversation_data: dict, checkpoint: str) -> bool:
        if self.lost:
            return False
        conversation_id = conversation_data['id']
        result = conversations_db.install_capture_conversation(
            self.uid,
            conversation_data,
            self.generation_id,
            self.owner_token,
        )
        if result != 'installed':
            return self._lose(f'{checkpoint}_{result}', conversation_id)
        self._project(conversation_id)
        return True

    def rotate(self, current_conversation_id: str, conversation_data: dict, checkpoint: str) -> bool:
        if self.lost:
            return False
        new_conversation_id = conversation_data['id']
        result = conversations_db.install_capture_conversation(
            self.uid,
            conversation_data,
            self.generation_id,
            self.owner_token,
            expected_conversation_id=current_conversation_id,
        )
        if result != 'installed':
            return self._lose(f'{checkpoint}_{result}', current_conversation_id)
        self._project(new_conversation_id)
        return True

    def drain(self, conversation_id: str, checkpoint: str) -> bool:
        if self.lost:
            return False
        if not conversations_db.mark_capture_drained(
            self.uid,
            conversation_id,
            self.generation_id,
            self.owner_token,
        ):
            return self._lose(checkpoint, conversation_id)
        self._project(conversation_id, state='drained')
        return True
