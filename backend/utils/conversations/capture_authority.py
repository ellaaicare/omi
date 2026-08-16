from typing import Callable, Optional, TypeVar

from database import redis_db

T = TypeVar('T')


class CaptureStreamAuthority:
    """Fail-closed ownership guard for one live transcript stream."""

    def __init__(
        self,
        uid: str,
        generation_id: str,
        on_loss: Optional[Callable[[str, str], None]] = None,
    ):
        self.uid = uid
        self.generation_id = generation_id
        self.lost = False
        self._on_loss = on_loss

    def _lose(self, checkpoint: str, conversation_id: str) -> bool:
        if not self.lost:
            self.lost = True
            if self._on_loss:
                self._on_loss(checkpoint, conversation_id)
        return False

    def refresh(self, conversation_id: str, checkpoint: str) -> bool:
        if self.lost:
            return False
        result = redis_db.refresh_in_progress_conversation_id(
            self.uid,
            self.generation_id,
            conversation_id,
        )
        if result not in {'refreshed', 'restored'}:
            return self._lose(checkpoint, conversation_id)
        return True

    def adopt(self, conversation_id: str, checkpoint: str) -> bool:
        """Take over one exact resumable conversation for this newly connected stream."""
        if self.lost:
            return False
        result = redis_db.adopt_capture_stream_authority(
            self.uid,
            self.generation_id,
            conversation_id,
        )
        if result != 'adopted':
            return self._lose(checkpoint, conversation_id)
        return True

    def run_if_owned(self, conversation_id: str, checkpoint: str, operation: Callable[[], T]) -> Optional[T]:
        """Run one synchronous durable update only while the exact pointer is owned."""
        if not self.refresh(conversation_id, checkpoint):
            return None
        return operation()

    def acquire(self, conversation_id: str, install: Callable[[], None], checkpoint: str) -> bool:
        """Acquire a missing pointer before installing the initial durable stub."""
        if self.lost:
            return False
        result = redis_db.acquire_capture_stream_authority(
            self.uid,
            self.generation_id,
            conversation_id,
        )
        if result != 'acquired':
            return self._lose(checkpoint, conversation_id)
        try:
            install()
        except Exception:
            redis_db.release_capture_stream_authority_if_matches(
                self.uid,
                self.generation_id,
                conversation_id,
            )
            raise
        return True

    def rotate(
        self,
        current_conversation_id: str,
        new_conversation_id: str,
        install: Callable[[], None],
        checkpoint: str,
    ) -> bool:
        """CAS the owned pointer before installing a same-stream successor stub."""
        if self.lost:
            return False
        result = redis_db.rotate_in_progress_conversation_id(
            self.uid,
            self.generation_id,
            current_conversation_id,
            new_conversation_id,
        )
        if result != 'rotated':
            return self._lose(checkpoint, current_conversation_id)
        try:
            install()
        except Exception:
            redis_db.rotate_in_progress_conversation_id(
                self.uid,
                self.generation_id,
                new_conversation_id,
                current_conversation_id,
            )
            raise
        return True
