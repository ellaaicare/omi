from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, MutableSequence, Sequence


@dataclass(frozen=True)
class CapturePersistenceBatch:
    conversation_id: str | None
    segments: tuple[Any, ...]
    photos: tuple[Any, ...]
    segment_object_ids: tuple[int, ...]
    photo_object_ids: tuple[int, ...]


def prepare_capture_persistence_batch(
    segment_buffer: Sequence[Any],
    photo_buffer: Sequence[Any],
    *,
    conversation_ready: bool,
    timestamp_ready: bool,
    conversation_id: str | None = None,
) -> CapturePersistenceBatch | None:
    if not conversation_ready or not timestamp_ready:
        return None

    return CapturePersistenceBatch(
        conversation_id=str(conversation_id or "").strip() or None,
        segments=tuple(deepcopy(segment_buffer)),
        photos=tuple(deepcopy(photo_buffer)),
        segment_object_ids=tuple(id(item) for item in segment_buffer),
        photo_object_ids=tuple(id(item) for item in photo_buffer),
    )


def prepare_conversation_bound_capture_batch(
    segment_buffer: Sequence[Any],
    photo_buffer: Sequence[Any],
    *,
    conversation_key: str,
    timestamp_ready: bool,
) -> CapturePersistenceBatch | None:
    """Snapshot only the leading items bound to one exact conversation."""

    first = segment_buffer[0] if segment_buffer else photo_buffer[0] if photo_buffer else None
    if not isinstance(first, Mapping):
        return None
    conversation_id = str(first.get(conversation_key) or "").strip()
    if not conversation_id:
        raise ValueError("capture_batch_conversation_missing")

    segment_prefix = []
    for item in segment_buffer:
        if not isinstance(item, Mapping) or str(item.get(conversation_key) or "").strip() != conversation_id:
            break
        segment_prefix.append(item)
    photo_prefix = []
    for item in photo_buffer:
        if not isinstance(item, Mapping) or str(item.get(conversation_key) or "").strip() != conversation_id:
            break
        photo_prefix.append(item)

    return prepare_capture_persistence_batch(
        segment_prefix,
        photo_prefix,
        conversation_ready=True,
        timestamp_ready=(not segment_prefix or timestamp_ready),
        conversation_id=conversation_id,
    )


def acknowledge_capture_persistence_batch(
    segment_buffer: MutableSequence[Any],
    photo_buffer: MutableSequence[Any],
    batch: CapturePersistenceBatch,
    *,
    segments: bool = True,
    photos: bool = True,
) -> None:
    segment_count = len(batch.segment_object_ids)
    photo_count = len(batch.photo_object_ids)
    current_segment_ids = tuple(id(item) for item in segment_buffer[:segment_count])
    current_photo_ids = tuple(id(item) for item in photo_buffer[:photo_count])
    if (segments and current_segment_ids != batch.segment_object_ids) or (
        photos and current_photo_ids != batch.photo_object_ids
    ):
        raise RuntimeError("capture_buffer_changed_before_persistence_ack")

    if segments:
        del segment_buffer[:segment_count]
    if photos:
        del photo_buffer[:photo_count]
