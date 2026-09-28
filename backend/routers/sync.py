import io
import json
import os
import re
import struct
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query, Header, Request, Response
from fastapi.responses import StreamingResponse
from opuslib import Decoder
from pydantic import BaseModel, Field
from pydub import AudioSegment

from database import conversations as conversations_db
from database import users as users_db
from database.conversations import get_closest_conversation_to_timestamps, update_conversation_segments
from models.conversation import CreateConversation, ConversationSource, Conversation, Geolocation
from models.transcript_segment import TranscriptSegment
from utils.conversations.process_conversation import process_conversation
from ella.services.ai_consent import assert_current_ai_consent, require_current_ai_consent
from utils.other import endpoints as auth
from utils.other.storage import (
    get_syncing_file_temporal_signed_url,
    delete_syncing_temporal_file,
    download_audio_chunks_and_merge,
    get_or_create_merged_audio,
    get_merged_audio_signed_url,
)
from utils.sync_capture_manifest import (
    acquire_conversation_update_lock,
    cache_sync_segment_result,
    claim_conversation_manifest,
    claim_sync_segment,
    compute_sync_segment_id,
    get_cached_sync_segment_result,
    issue_capture_manifest,
    manifest_claims_match_paths,
    release_conversation_update_lock,
    release_sync_segment_claim,
    verify_capture_manifest,
)

# Audio constants
AUDIO_SAMPLE_RATE = 16000
from utils import encryption
from utils.stt.pre_recorded import deepgram_prerecorded, postprocess_words
from utils.stt.vad import vad_is_empty

router = APIRouter()


# **********************************************
# ************ SYNC V2 WIRE MODELS *************
# **********************************************
#
# Field names/shape mirror BasedHardware/omi's `SyncLocalFilesResultResponse` /
# `SyncCaptureManifestRequest` / `SyncCaptureManifestResponse`
# (backend/routers/sync.py at commit f16699aea7fe9ba089baceb628922f2882c51153), which is what the
# vendored client's `GeneratedSyncLocalFilesResultResponse` / `GeneratedSyncCaptureManifestRequest`
# / `GeneratedSyncCaptureManifestResponse` wire models (app/lib/upstream_capture/backend/schema/gen/
# conversation_wire.g.dart on origin/release/testflight-850-necklace-recovery) encode/decode.


class SyncLocalFilesResultResponse(BaseModel):
    new_memories: List[str] = Field(default_factory=list)
    updated_memories: List[str] = Field(default_factory=list)
    failed_segments: int = 0
    total_segments: int = 0
    errors: List[str] = Field(default_factory=list)


class SyncCaptureManifestFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r'^[0-9a-fA-F]{64}$')


class SyncCaptureManifestRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    files: List[SyncCaptureManifestFile] = Field(min_length=1, max_length=20)


class SyncCaptureManifestResponse(BaseModel):
    manifest: str


# **********************************************
# ******** AUDIO FORMAT CONVERSION *************
# **********************************************


def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Convert PCM16 data to WAV format."""
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, 'wb') as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)  # 16-bit audio
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    return wav_buffer.getvalue()


def parse_range_header(range_header: str, file_size: int) -> tuple[int, int] | None:
    """
    Parse HTTP Range header and return (start, end) tuple.
    Returns None if the range is invalid.

    Example: "bytes=0-1023" -> (0, 1023)
    """
    if not range_header:
        return None

    try:
        # Parse "bytes=start-end" format
        if not range_header.startswith("bytes="):
            return None

        range_spec = range_header[6:]
        parts = range_spec.split("-")

        if len(parts) != 2:
            return None

        start_str, end_str = parts

        # Handle "bytes=start-" (from start to end of file)
        if start_str and not end_str:
            start = int(start_str)
            end = file_size - 1
        # Handle "bytes=-suffix" (last N bytes)
        elif not start_str and end_str:
            suffix_length = int(end_str)
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        # Handle "bytes=start-end"
        else:
            start = int(start_str)
            end = int(end_str)

        # RFC 7233: start must be valid, end can exceed file size and gets clamped
        if start < 0 or start >= file_size or start > end:
            return None
        end = min(end, file_size - 1)
        return (start, end)
    except (ValueError, IndexError):
        return None


# **********************************************
# ********** AUDIO PRE-CACHING *****************
# **********************************************


def _precache_audio_file(uid: str, conversation_id: str, audio_file: dict, fill_gaps: bool = True):
    """Pre-cache a single audio file."""
    try:
        audio_file_id = audio_file.get('id')
        timestamps = audio_file.get('chunk_timestamps')
        if not audio_file_id or not timestamps:
            return

        get_or_create_merged_audio(
            uid=uid,
            conversation_id=conversation_id,
            audio_file_id=audio_file_id,
            timestamps=timestamps,
            pcm_to_wav_func=pcm_to_wav,
            fill_gaps=fill_gaps,
            sample_rate=AUDIO_SAMPLE_RATE,
        )
        print(f"Pre-cached audio file: {audio_file_id}")
    except Exception as e:
        print(f"Error pre-caching audio file {audio_file.get('id')}: {e}")


@router.post("/v1/sync/audio/{conversation_id}/precache", tags=['v1'])
def precache_conversation_audio_endpoint(
    conversation_id: str,
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Warm the audio cache for a conversation.
    Returns immediately - caching happens in background.
    """
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    audio_files = conversation.get('audio_files', [])
    if not audio_files:
        return {"status": "no_audio", "message": "No audio files in conversation"}

    # Start background parallel pre-caching for all audio files
    def _precache_all_parallel():
        print(f"Pre-caching all {len(audio_files)} audio files for conversation {conversation_id} (parallel)")
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_precache_audio_file, uid, conversation_id, af) for af in audio_files]
            # Wait for all to complete
            for future in futures:
                try:
                    future.result()
                except Exception as e:
                    print(f"Error in parallel precache: {e}")
        print(f"Completed pre-cache for conversation {conversation_id}")

    thread = threading.Thread(target=_precache_all_parallel, daemon=True)
    thread.start()

    return {"status": "started", "audio_file_count": len(audio_files)}


@router.get("/v1/sync/audio/{conversation_id}/urls", tags=['v1'])
def get_audio_signed_urls_endpoint(
    conversation_id: str,
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Get signed URLs for all audio files in a conversation.
    Synchronously caches the first uncached file for immediate playback.
    Remaining files are cached in background.

    Returns:
        List of audio file info with signed_url (if cached) or status "pending"
    """
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    audio_files = conversation.get('audio_files', [])
    if not audio_files:
        return {"audio_files": []}

    result = []
    uncached_files = []
    first_uncached_handled = False

    for af in audio_files:
        audio_file_id = af.get('id')
        if not audio_file_id:
            continue

        signed_url = get_merged_audio_signed_url(uid, conversation_id, audio_file_id)

        if signed_url:
            result.append(
                {
                    "id": audio_file_id,
                    "status": "cached",
                    "signed_url": signed_url,
                    "duration": af.get('duration', 0),
                }
            )
        else:
            # First uncached file: cache synchronously for immediate playback
            if not first_uncached_handled:
                first_uncached_handled = True
                _precache_audio_file(uid, conversation_id, af)
                # Get signed URL after caching
                signed_url = get_merged_audio_signed_url(uid, conversation_id, audio_file_id)
                if signed_url:
                    result.append(
                        {
                            "id": audio_file_id,
                            "status": "cached",
                            "signed_url": signed_url,
                            "duration": af.get('duration', 0),
                        }
                    )
                else:
                    # Cache failed, return pending
                    result.append(
                        {
                            "id": audio_file_id,
                            "status": "pending",
                            "signed_url": None,
                            "duration": af.get('duration', 0),
                        }
                    )
            else:
                result.append(
                    {
                        "id": audio_file_id,
                        "status": "pending",
                        "signed_url": None,
                        "duration": af.get('duration', 0),
                    }
                )
                uncached_files.append(af)

    # Cache remaining files in background
    if uncached_files:

        def _cache_uncached_parallel():
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(_precache_audio_file, uid, conversation_id, af) for af in uncached_files]
                for future in futures:
                    try:
                        future.result()
                    except Exception as e:
                        print(f"Error in parallel cache: {e}")

        thread = threading.Thread(target=_cache_uncached_parallel, daemon=True)
        thread.start()

    return {"audio_files": result}


# **********************************************
# ********** AUDIO DOWNLOAD ENDPOINT ***********
# **********************************************


@router.get("/v1/sync/audio/{conversation_id}/{audio_file_id}", tags=['v1'])
def download_audio_file_endpoint(
    conversation_id: str,
    audio_file_id: str,
    request: Request,
    format: str = Query(default="wav", regex="^(wav|pcm)$"),
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Download audio file from private cloud sync in the specified format.
    Merges chunks on-demand.

    Args:
        conversation_id: ID of the conversation
        audio_file_id: ID of the audio file within the conversation
        request: FastAPI Request object (for Range header)
        format: Output format - 'wav' or 'pcm' (raw) (default: wav)
        uid: User ID (from authentication)

    Returns:
        StreamingResponse with the audio file in the requested format.
        Returns 206 Partial Content for Range requests, 200 OK for full file.
    """
    # Verify user owns the conversation
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Find the audio file in the conversation
    audio_files = conversation.get('audio_files', [])
    audio_file = None
    for af in audio_files:
        if af.get('id') == audio_file_id:
            audio_file = af
            break

    if not audio_file:
        raise HTTPException(status_code=404, detail="Audio file not found in conversation")

    # Get audio data - use cache if available, otherwise merge and cache
    try:
        if not audio_file.get('chunk_timestamps'):
            raise HTTPException(status_code=500, detail="Audio file has no chunk timestamps")

        if format == "wav":
            audio_data, was_cached = get_or_create_merged_audio(
                uid=uid,
                conversation_id=conversation_id,
                audio_file_id=audio_file_id,
                timestamps=audio_file['chunk_timestamps'],
                pcm_to_wav_func=pcm_to_wav,
                fill_gaps=True,
                sample_rate=AUDIO_SAMPLE_RATE,
            )
            content_type = "audio/wav"
            extension = "wav"
        else:
            audio_data = download_audio_chunks_and_merge(
                uid, conversation_id, audio_file['chunk_timestamps'], fill_gaps=True, sample_rate=AUDIO_SAMPLE_RATE
            )
            content_type = "application/octet-stream"
            extension = "pcm"
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Audio chunks not found in storage")
    except Exception as e:
        print(f"Error downloading audio file: {e}")
        raise HTTPException(status_code=500, detail="Failed to download audio file")

    # Create descriptive filename
    filename = f"conversation_{conversation_id}_audio_{audio_file_id}.{extension}"
    file_size = len(audio_data)

    base_headers = {
        "Content-Disposition": f"attachment; filename={filename}",
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }

    range_header = request.headers.get("Range")

    if range_header:
        # Parse the range request
        range_tuple = parse_range_header(range_header, file_size)

        if range_tuple is None:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{file_size}",
                    **base_headers,
                },
            )

        start, end = range_tuple
        content_length = end - start + 1

        # Return partial content
        return StreamingResponse(
            io.BytesIO(audio_data[start : end + 1]),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Length": str(content_length),
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                **base_headers,
            },
        )

    return StreamingResponse(
        io.BytesIO(audio_data),
        status_code=200,
        media_type=content_type,
        headers={
            "Content-Length": str(file_size),
            **base_headers,
        },
    )


# **********************************************
# ************ SYNC LOCAL FILES ****************
# **********************************************


import shutil
import wave


def decode_opus_file_to_wav(opus_file_path, wav_file_path, sample_rate=16000, channels=1, frame_size: int = 160):
    """Decode an Opus file with length-prefixed frames to WAV format.

    Writes directly to WAV file to avoid accumulating all PCM data in memory.
    """
    if not os.path.exists(opus_file_path):
        print(f"File not found: {opus_file_path}")
        return False

    decoder = Decoder(sample_rate, channels)
    frame_count = 0

    try:
        with open(opus_file_path, 'rb') as f, wave.open(wav_file_path, 'wb') as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(2)  # 16-bit audio
            wav_file.setframerate(sample_rate)

            while True:
                length_bytes = f.read(4)
                if not length_bytes:
                    print("End of file reached.")
                    break
                if len(length_bytes) < 4:
                    print("Incomplete length prefix at the end of the file.")
                    break

                frame_length = struct.unpack('<I', length_bytes)[0]
                opus_data = f.read(frame_length)
                if len(opus_data) < frame_length:
                    print(f"Unexpected end of file at frame {frame_count}.")
                    break
                try:
                    pcm_frame = decoder.decode(opus_data, frame_size=frame_size)
                    wav_file.writeframes(pcm_frame)  # Write directly to file
                    frame_count += 1
                except Exception as e:
                    print(f"Error decoding frame {frame_count}: {e}")
                    break

        if frame_count > 0:
            print(f"Decoded audio saved to {wav_file_path}")
            return True
        else:
            print("No PCM data was decoded.")
            # Clean up empty/invalid wav file
            if os.path.exists(wav_file_path):
                os.remove(wav_file_path)
            return False
    except Exception as e:
        print(f"Error during decode: {e}")
        # Clean up on error
        if os.path.exists(wav_file_path):
            os.remove(wav_file_path)
        return False


def get_timestamp_from_path(path: str):
    timestamp = int(path.split('/')[-1].split('_')[-1].split('.')[0])
    if timestamp > 1e10:
        return int(timestamp / 1000)
    return timestamp


def retrieve_file_paths(files: List[UploadFile], uid: str):
    directory = f'syncing/{uid}/'
    os.makedirs(directory, exist_ok=True)
    paths = []
    for file in files:
        filename = file.filename
        # Validate the file is .bin and contains a _$timestamp.bin, if not, 400 bad request
        if not filename.endswith('.bin'):
            raise HTTPException(status_code=400, detail=f"Invalid file format {filename}")
        if '_' not in filename:
            raise HTTPException(status_code=400, detail=f"Invalid file format {filename}, missing timestamp")
        try:
            timestamp = get_timestamp_from_path(filename)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid file format {filename}, invalid timestamp")

        time = datetime.fromtimestamp(timestamp)
        if time > datetime.now() or time < datetime(2024, 1, 1):
            raise HTTPException(status_code=400, detail=f"Invalid file format {filename}, invalid timestamp")

        path = f"{directory}{filename}"
        try:
            with open(path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            paths.append(path)
        except Exception as e:
            if os.path.exists(path):
                os.remove(path)
            raise HTTPException(status_code=500, detail=f"Failed to write file {filename}: {str(e)}")
    return paths


def get_wav_duration(wav_path: str) -> float:
    """Get WAV file duration without loading entire file into memory."""
    try:
        with wave.open(wav_path, 'rb') as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate()
            return frames / float(rate)
    except Exception as e:
        print(f"Error reading WAV duration: {e}")
        return 0.0


def decode_files_to_wav(files_path: List[str]):
    wav_files = []
    for path in files_path:
        wav_path = path.replace('.bin', '.wav')
        filename = os.path.basename(path)
        frame_size = 160  # Default frame size
        match = re.search(r'_fs(\d+)', filename)
        if match:
            try:
                frame_size = int(match.group(1))
                print(f"Found frame size {frame_size} in filename: {filename}")
            except ValueError:
                print(f"Invalid frame size format in filename: {filename}, using default {frame_size}")

        success = decode_opus_file_to_wav(path, wav_path, frame_size=frame_size)
        if not success:
            # Clean up .bin file even on decode failure
            if os.path.exists(path):
                os.remove(path)
            continue

        # Always remove .bin file after decode attempt
        if os.path.exists(path):
            os.remove(path)

        # Check duration without loading entire file into memory
        duration = get_wav_duration(wav_path)
        if duration == 0:
            # Invalid WAV file
            if os.path.exists(wav_path):
                os.remove(wav_path)
            raise HTTPException(status_code=400, detail=f"Invalid file format {path}")

        if duration < 1:
            os.remove(wav_path)
            continue
        wav_files.append(wav_path)
    return wav_files


def retrieve_vad_segments(path: str, segmented_paths: set, errors: list = None):
    try:
        start_timestamp = get_timestamp_from_path(path)
        voice_segments = vad_is_empty(path, return_segments=True, cache=True)
    except Exception as e:
        error_msg = f"VAD failed for {path}: {str(e)}"
        print(error_msg)
        if errors is not None:
            errors.append(error_msg)
        raise  # Re-raise to ensure thread failure is visible

    segments = []
    # should we merge more aggressively, to avoid too many small segments? ~ not for now
    # Pros -> lesser segments, faster, less concurrency
    # Cons -> less accuracy.

    # edge case, multiple small segments that map towards the same memory .-.
    # so ... let's merge them if distance < 120 seconds
    # a better option would be to keep here 1s, and merge them like that after transcribing
    # but FAL has 10 RPS limit, **let's merge it here for simplicity for now**

    for i, segment in enumerate(voice_segments):
        if segments and (segment['start'] - segments[-1]['end']) < 120:
            segments[-1]['end'] = segment['end']
        else:
            segments.append(segment)

    print(path, len(segments))

    aseg = AudioSegment.from_wav(path)
    path_dir = '/'.join(path.split('/')[:-1])

    try:
        for i, segment in enumerate(segments):
            if (segment['end'] - segment['start']) < 1:
                continue
            segment_timestamp = start_timestamp + segment['start']
            segment_path = f'{path_dir}/{segment_timestamp}.wav'
            segment_aseg = aseg[segment['start'] * 1000 : segment['end'] * 1000]
            segment_aseg.export(segment_path, format='wav')
            segmented_paths.add(segment_path)
            # Explicitly delete segment to free memory immediately
            del segment_aseg
    finally:
        # Explicitly delete main audio to free memory
        del aseg


def _reprocess_conversation_after_update(uid: str, conversation_id: str, language: str):
    """
    Reprocess a conversation after new segments have been added.
    This checks if the conversation should still be discarded and regenerates
    the summary/structured data if it now has sufficient content.
    """
    # Fetch the updated conversation with all segments
    conversation_data = conversations_db.get_conversation(uid, conversation_id)
    if not conversation_data:
        print(f'Conversation {conversation_id} not found for reprocessing')
        return

    # Convert to Conversation object
    conversation = Conversation(**conversation_data)

    process_conversation(
        uid=uid,
        language_code=language or 'en',
        conversation=conversation,
        force_process=True,
        is_reprocess=True,
    )

    print(f'Successfully reprocessed conversation {conversation_id}')


def process_segment(
    path: str,
    uid: str,
    response: dict,
    source: ConversationSource = ConversationSource.omi,
    target_conversation_id: Optional[str] = None,
    geolocation: Optional[Geolocation] = None,
):
    """Transcribe one VAD-segmented audio file and attach it to a conversation.

    [target_conversation_id], when given, is looked up with `conversations_db.get_conversation(uid,
    target_conversation_id)` — a uid-scoped Firestore read, so it can never resolve a conversation
    owned by a different uid — and used instead of nearest-by-timestamp matching. It falls back to
    the timestamp heuristic (and ultimately to creating a new conversation) whenever it does not
    resolve, so an unknown/foreign/already-deleted id never fails the sync and never touches another
    account's data. [geolocation], when given, is only stamped on a newly created conversation.

    Returns `(kind, conversation_id)` — `kind` is `'new_memories'` or `'updated_memories'` — for the
    conversation this segment ended up in, or `None` when nothing was transcribed. `/v2/sync-local-files`
    uses this to cache the outcome per segment content id for idempotent replay; `/v1/sync-local-files`
    (and any other existing caller) ignores the return value, so this is backward compatible.
    """
    assert_current_ai_consent(uid)
    url = get_syncing_file_temporal_signed_url(path)

    def delete_file():
        time.sleep(480)
        delete_syncing_temporal_file(path)

    threading.Thread(target=delete_file).start()

    words, language = deepgram_prerecorded(url, speakers_count=3, attempts=0, return_language=True)
    transcript_segments: List[TranscriptSegment] = postprocess_words(words, 0)
    if not transcript_segments:
        print('failed to get deepgram segments')
        return None

    timestamp = get_timestamp_from_path(path)
    segment_end_timestamp = timestamp + transcript_segments[-1].end
    closest_memory = None
    if target_conversation_id:
        closest_memory = conversations_db.get_conversation(uid, target_conversation_id)
    if not closest_memory:
        closest_memory = get_closest_conversation_to_timestamps(uid, timestamp, segment_end_timestamp)

    if not closest_memory:
        started_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        finished_at = datetime.fromtimestamp(segment_end_timestamp, tz=timezone.utc)
        create_memory = CreateConversation(
            started_at=started_at,
            finished_at=finished_at,
            transcript_segments=transcript_segments,
            source=source,
            geolocation=geolocation,
        )
        created = process_conversation(uid, language, create_memory)
        response['new_memories'].add(created.id)
        return ('new_memories', created.id)
    else:
        # Parallel VAD segments can explicitly target the same conversation (`target_conversation_id`,
        # e.g. several segments from one live-capture batch). The merge below is a read-modify-replace
        # of the conversation's full segment list, so two concurrent segments for that conversation
        # would otherwise both read the same snapshot and the second write would silently drop the
        # first's segments. Serialize the read-merge-write on that conversation with a short-lived
        # Redis lock (this never engages for `/v1/sync-local-files`, which never passes
        # target_conversation_id, or for the nearest-by-timestamp fallback match).
        lock_conversation_id = target_conversation_id if target_conversation_id == closest_memory.get('id') else None
        lock_claimant = uuid.uuid4().hex
        if lock_conversation_id:
            if not acquire_conversation_update_lock(uid, lock_conversation_id, lock_claimant):
                raise RuntimeError(f'timed out waiting to update conversation {lock_conversation_id}')
            # Re-read now that we hold the lock: another segment may have merged its own segments
            # into this conversation while we were waiting.
            refreshed = conversations_db.get_conversation(uid, lock_conversation_id)
            if refreshed:
                closest_memory = refreshed

        try:
            transcript_segments = [s.dict() for s in transcript_segments]

            # assign timestamps to each segment
            for segment in transcript_segments:
                segment['timestamp'] = timestamp + segment['start']
            for segment in closest_memory['transcript_segments']:
                segment['timestamp'] = closest_memory['started_at'].timestamp() + segment['start']

            # merge and sort segments by start timestamp
            segments = closest_memory['transcript_segments'] + transcript_segments
            segments.sort(key=lambda x: x['timestamp'])

            # fix segment.start .end to be relative to the memory
            for i, segment in enumerate(segments):
                duration = segment['end'] - segment['start']
                segment['start'] = segment['timestamp'] - closest_memory['started_at'].timestamp()
                segment['end'] = segment['start'] + duration

            # Calculate new finished_at based on the latest segment
            last_segment_end = segments[-1]['end'] if segments else 0
            new_finished_at = datetime.fromtimestamp(
                closest_memory['started_at'].timestamp() + last_segment_end, tz=timezone.utc
            )

            # Ensure finished_at doesn't go backwards
            if new_finished_at < closest_memory['finished_at']:
                new_finished_at = closest_memory['finished_at']

            # remove timestamp field
            for segment in segments:
                segment.pop('timestamp')

            # save with updated finished_at
            response['updated_memories'].add(closest_memory['id'])
            update_conversation_segments(uid, closest_memory['id'], segments, finished_at=new_finished_at)
        finally:
            if lock_conversation_id:
                release_conversation_update_lock(uid, lock_conversation_id, lock_claimant)

        # If the conversation was previously discarded, reprocess it with the new segments
        if closest_memory.get('discarded', False):
            print(f'Conversation {closest_memory["id"]} was discarded, checking if it should be reprocessed')
            _reprocess_conversation_after_update(uid, closest_memory['id'], language)

        return ('updated_memories', closest_memory['id'])


def _cleanup_files(file_paths):
    """Helper to clean up temporary files."""
    for path in file_paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print(f"Failed to cleanup file {path}: {e}")


@router.post("/v1/sync-local-files")
async def sync_local_files(files: List[UploadFile] = File(...), uid: str = Depends(require_current_ai_consent)):
    # Improve a version without timestamp, to consider uploads from the stored in v2 device bytes.
    # Detect source from filenames
    source = ConversationSource.omi
    for f in files:
        if f.filename and 'limitless' in f.filename.lower():
            source = ConversationSource.limitless
            break

    paths = []
    wav_paths = []
    segmented_paths = set()

    try:
        paths = retrieve_file_paths(files, uid)
        wav_paths = decode_files_to_wav(paths)

        def chunk_threads(threads):
            chunk_size = 5
            for i in range(0, len(threads), chunk_size):
                [t.start() for t in threads[i : i + chunk_size]]
                [t.join() for t in threads[i : i + chunk_size]]

        vad_errors = []
        threads = [
            threading.Thread(target=retrieve_vad_segments, args=(path, segmented_paths, vad_errors))
            for path in wav_paths
        ]
        chunk_threads(threads)

        # Clean up original wav files after VAD segmentation (segments are now in segmented_paths)
        _cleanup_files(wav_paths)
        wav_paths = []  # Clear to avoid double cleanup in finally

        # Check for VAD errors - if any failed, abort to prevent data loss
        if vad_errors:
            error_detail = f"VAD processing failed for {len(vad_errors)} file(s): {'; '.join(vad_errors[:3])}"
            if len(vad_errors) > 3:
                error_detail += f" (and {len(vad_errors) - 3} more)"
            raise HTTPException(status_code=500, detail=error_detail)

        print('sync_local_files len(segmented_paths)', len(segmented_paths))

        response = {'updated_memories': set(), 'new_memories': set()}
        threads = [
            threading.Thread(
                target=process_segment,
                args=(
                    path,
                    uid,
                    response,
                    source,
                ),
            )
            for path in segmented_paths
        ]
        chunk_threads(threads)

        # notify through FCM too ?
        return response
    finally:
        # Clean up any remaining temporary files
        _cleanup_files(paths)  # .bin files (in case decode_files_to_wav didn't finish)
        _cleanup_files(wav_paths)  # Original wav files (if VAD didn't complete)
        _cleanup_files(segmented_paths)  # Segmented wav files after processing


# **********************************************
# ************ SYNC LOCAL FILES V2 *************
# **********************************************
#
# Wire contract ported from BasedHardware/omi (upstream commit
# f16699aea7fe9ba089baceb628922f2882c51153), `backend/routers/sync.py`'s `/v2/sync-capture-manifest`
# and `/v2/sync-local-files` routes, cross-checked against the vendored client's request/response
# handling in `app/lib/upstream_capture/backend/http/api/conversations.dart` (`uploadLocalFilesV2`,
# `_createSyncCaptureManifest`) on `origin/release/testflight-850-necklace-recovery`.
#
# Deliberate deviations from upstream's implementation (the *wire contract* — request/response
# shape and status codes the client actually sends/parses — is kept intact; upstream's *internal*
# architecture is not, since this fork has no Cloud Tasks / fair-use / backfill-lane / Firestore
# sync-ledger subsystem, and its v1 sibling already processes uploads synchronously):
#   * /v2/sync-local-files always processes synchronously and returns 200 with a
#     SyncLocalFilesResultResponse body (upstream's async 202 job_id + GET
#     /v2/sync-local-files/{job_id} polling contract is not implemented). The vendored client
#     explicitly supports this as its "fast path" (see `UploadFilesResult.done` /
#     `uploadLocalFilesV2`'s 200 branch), so this is a spec-compliant subset, not a break.
#   * The capture-manifest token is not bound to a verified client_device_id (this fork doesn't
#     resolve one for sync requests yet) — see utils/sync_capture_manifest.py's module docstring.
#   * Idempotent replay is enforced per VAD-segment (content-hash keyed), not per upload batch via
#     a Firestore job ledger — see utils/sync_capture_manifest.py's module docstring.
#   * No fair-use / daily-audio-ceiling / backfill-lane / rate-limit gating: none of that subsystem
#     exists in this fork, and the client treats any non-recognized status as a generic retryable
#     failure, so omitting it does not desync the client.
#   * X-Omi-Conversation-Geolocation is parsed best-effort into this fork's narrower `Geolocation`
#     model (latitude/longitude/google_place_id/address/location_type only — upstream's richer
#     altitude/accuracy/capture_source/captured_at fields have no equivalent field here) and is
#     only ever attached to a newly created conversation; a malformed header is ignored rather than
#     failing the upload.


def _run_threads_in_chunks(threads: List[threading.Thread], chunk_size: int = 5):
    for i in range(0, len(threads), chunk_size):
        [t.start() for t in threads[i : i + chunk_size]]
        [t.join() for t in threads[i : i + chunk_size]]


def _parse_conversation_geolocation_header(raw: Optional[str]) -> Optional[Geolocation]:
    """Best-effort parse of X-Omi-Conversation-Geolocation. Never raises: a malformed/partial
    header just means the new conversation is created without a geolocation stamp."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        if latitude is None or longitude is None:
            return None
        return Geolocation(
            latitude=float(latitude),
            longitude=float(longitude),
            google_place_id=data.get('google_place_id'),
            address=data.get('address'),
            location_type=data.get('location_type'),
        )
    except Exception as e:
        print(f'Failed to parse X-Omi-Conversation-Geolocation header: {e}')
        return None


@router.post('/v2/sync-capture-manifest', response_model=SyncCaptureManifestResponse)
async def create_sync_capture_manifest(
    payload: SyncCaptureManifestRequest,
    uid: str = Depends(require_current_ai_consent),
):
    """Issue a short-lived, HMAC-signed proof binding [payload.files] (name + sha256) to
    [payload.conversation_id] for this uid. The vendored client only requests this before a fresh
    (live-capture) upload and treats any non-200 response as "no manifest available" — it still
    uploads the audio without one — so failures here are conservative (409/503) rather than fatal.
    """
    claims = [item.model_dump() for item in payload.files]
    try:
        claimed = claim_conversation_manifest(uid, payload.conversation_id, claims)
    except Exception as e:
        print(f'sync capture manifest claim unavailable uid={uid} error={e}')
        raise HTTPException(status_code=503, detail={'code': 'sync_capture_manifest_unavailable', 'retryable': True})
    if not claimed:
        raise HTTPException(status_code=409, detail={'code': 'sync_capture_manifest_conflict'})
    manifest = issue_capture_manifest(uid, payload.conversation_id, claims)
    return SyncCaptureManifestResponse(manifest=manifest)


@router.post('/v2/sync-local-files', response_model=SyncLocalFilesResultResponse)
async def sync_local_files_v2(
    files: List[UploadFile] = File(...),
    uid: str = Depends(require_current_ai_consent),
    conversation_id: Optional[str] = Query(
        None, description="Target conversation ID to attach audio to (auto-sync from live capture)"
    ),
    x_omi_sync_capture_manifest: Optional[str] = Header(None, alias='X-Omi-Sync-Capture-Manifest'),
    x_omi_conversation_geolocation: Optional[str] = Header(None, alias='X-Omi-Conversation-Geolocation'),
):
    """Synchronous v2 upload. Same VAD -> STT -> conversation-assignment pipeline as
    `/v1/sync-local-files`, plus: optional fresh-capture manifest verification, optional explicit
    `conversation_id` targeting (uid-scoped; never resolves another account's conversation), and
    per-segment idempotent replay so re-uploading the same audio (e.g. after a dropped response)
    never creates or updates a conversation twice.
    """
    source = ConversationSource.omi
    for f in files:
        if f.filename and 'limitless' in f.filename.lower():
            source = ConversationSource.limitless
            break

    filenames = [f.filename or '' for f in files]
    manifest_claims = None
    if x_omi_sync_capture_manifest:
        manifest_claims = verify_capture_manifest(x_omi_sync_capture_manifest, uid, conversation_id, filenames)

    geolocation = _parse_conversation_geolocation_header(x_omi_conversation_geolocation)

    paths = []
    wav_paths = []
    segmented_paths = set()

    try:
        paths = retrieve_file_paths(files, uid)

        if manifest_claims is not None and not manifest_claims_match_paths(manifest_claims, paths):
            raise HTTPException(
                status_code=422,
                detail={
                    'code': 'capture_manifest_mismatch',
                    'detail': 'Fresh capture manifest did not match the uploaded audio',
                },
            )

        wav_paths = decode_files_to_wav(paths)

        vad_errors = []
        threads = [
            threading.Thread(target=retrieve_vad_segments, args=(path, segmented_paths, vad_errors))
            for path in wav_paths
        ]
        _run_threads_in_chunks(threads)

        # Clean up original wav files after VAD segmentation (segments are now in segmented_paths)
        _cleanup_files(wav_paths)
        wav_paths = []  # Clear to avoid double cleanup in finally

        if vad_errors:
            error_detail = f"VAD processing failed for {len(vad_errors)} file(s): {'; '.join(vad_errors[:3])}"
            if len(vad_errors) > 3:
                error_detail += f" (and {len(vad_errors) - 3} more)"
            raise HTTPException(status_code=500, detail=error_detail)

        print('sync_local_files_v2 len(segmented_paths)', len(segmented_paths))

        response = {'updated_memories': set(), 'new_memories': set()}
        errors: List[str] = []
        result_lock = threading.Lock()
        total_segments = len(segmented_paths)

        def _run_segment(path: str):
            # Everything below — the cache read, the claim, processing, and the cache write — is
            # inside this single try. A Redis outage on any of them (SYNC-V2-001) is a worker
            # failure like any other: it lands in [errors] and counts against failed_segments,
            # rather than silently vanishing (a thread's uncaught exception never reaches the
            # caller) and being reported back as a false-success 200.
            try:
                segment_id = compute_sync_segment_id(uid, path)
                cached = get_cached_sync_segment_result(segment_id)
                if cached is not None:
                    # Replay of already-durably-processed audio: no STT/LLM re-run, no duplicate
                    # conversation write. Just report the same outcome as the first successful call.
                    with result_lock:
                        response[cached['kind']].add(cached['conversation_id'])
                    return

                # Atomic claim (SYNC-V2-002): the prior get/process/set was non-atomic, so two
                # concurrent retries of the same segment could both miss the cache and both run
                # STT/LLM/persistence. Only the claimant may process this segment id; a concurrent
                # retry that loses the race backs off as retryable instead of racing it.
                claimant = uuid.uuid4().hex
                if not claim_sync_segment(segment_id, claimant):
                    with result_lock:
                        errors.append(f'{os.path.basename(path)}: segment is already being processed, retry')
                    return

                try:
                    # Re-check: a concurrent execution may have finished and cached the result
                    # while this one was racing for the claim.
                    cached = get_cached_sync_segment_result(segment_id)
                    if cached is not None:
                        with result_lock:
                            response[cached['kind']].add(cached['conversation_id'])
                        return

                    outcome = process_segment(
                        path,
                        uid,
                        response,
                        source,
                        target_conversation_id=conversation_id,
                        geolocation=geolocation,
                    )
                    if outcome is not None:
                        kind, result_conversation_id = outcome
                        cache_sync_segment_result(segment_id, kind, result_conversation_id)
                finally:
                    release_sync_segment_claim(segment_id, claimant)
            except Exception as e:
                with result_lock:
                    errors.append(f'{os.path.basename(path)}: {e}')

        threads = [threading.Thread(target=_run_segment, args=(path,)) for path in segmented_paths]
        _run_threads_in_chunks(threads)

        failed_segments = len(errors)
        successful_segments = total_segments - failed_segments

        if total_segments > 0 and successful_segments == 0:
            raise HTTPException(
                status_code=500,
                detail=f"All {total_segments} segment(s) failed processing: {'; '.join(errors[:3])}",
            )

        return SyncLocalFilesResultResponse(
            new_memories=sorted(response['new_memories']),
            updated_memories=sorted(response['updated_memories']),
            failed_segments=failed_segments,
            total_segments=total_segments,
            errors=errors[:10],
        )
    finally:
        _cleanup_files(paths)
        _cleanup_files(wav_paths)
        _cleanup_files(segmented_paths)
