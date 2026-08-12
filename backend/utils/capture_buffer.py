from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, MutableSequence, Sequence


@dataclass(frozen=True)
class CapturePersistenceBatch:
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
) -> CapturePersistenceBatch | None:
    if not conversation_ready or not timestamp_ready:
        return None

    return CapturePersistenceBatch(
        segments=tuple(deepcopy(segment_buffer)),
        photos=tuple(deepcopy(photo_buffer)),
        segment_object_ids=tuple(id(item) for item in segment_buffer),
        photo_object_ids=tuple(id(item) for item in photo_buffer),
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
