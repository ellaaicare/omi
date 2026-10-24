"""Bounded, explicit cached local VAD. No network, download or automatic fallback."""

import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import wave

MAX_SECONDS = 300
MAX_BYTES = 16 * 1024 * 1024
WALL_SECONDS = 30
MODEL_DIRECTORY = Path(
    Path(__file__).resolve().parents[2] / 'pretrained_models/snakers4_silero-vad_master/src/silero_vad'
)
_slot = threading.BoundedSemaphore(1)


class LocalVadError(RuntimeError):
    """Sanitized terminal failure; never interpret failure as silence."""


def validate_segments(segments, duration):
    if not isinstance(segments, list) or len(segments) > 400:
        raise LocalVadError('local_vad_invalid_response')
    result = []
    previous_end = 0.0
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {'start', 'end', 'duration'}:
            raise LocalVadError('local_vad_invalid_response')
        if any(type(segment[key]) not in (int, float) for key in segment):
            raise LocalVadError('local_vad_invalid_response')
        start, end, length = (float(segment[key]) for key in ('start', 'end', 'duration'))
        if not all(math.isfinite(value) for value in (start, end, length)):
            raise LocalVadError('local_vad_invalid_response')
        if start < previous_end or end <= start or end > duration or abs(length - (end - start)) > 1e-6:
            raise LocalVadError('local_vad_invalid_response')
        previous_end = end
        result.append({'start': start, 'end': end, 'duration': length})
    return result


def local_silero_segments(wav_path, *, model_directory=MODEL_DIRECTORY):
    """Return hosted-contract {start,end,duration} seconds for mono PCM16 8/16k.

    Existing sync decodes length-prefixed Opus to 16k mono WAV before this
    boundary. Raw PCM8, stereo, compressed WAV and other rates fail explicitly.
    It is an isolated option, not an HTTP handler or a queue admission gate.
    """
    if not _slot.acquire(blocking=False):
        raise LocalVadError('local_vad_capacity')
    descriptor = None
    worker = None
    try:
        descriptor = os.open(wav_path, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise LocalVadError('local_vad_input_size')
        with os.fdopen(os.dup(descriptor), 'rb') as audio:
            with wave.open(audio, 'rb') as reader:
                rate, frames = reader.getframerate(), reader.getnframes()
                if (reader.getnchannels(), reader.getsampwidth(), reader.getcomptype()) != (1, 2, 'NONE'):
                    raise LocalVadError('local_vad_input_format')
                if rate not in (8000, 16000) or not 0 < frames <= rate * MAX_SECONDS:
                    raise LocalVadError('local_vad_input_budget')
        os.lseek(descriptor, 0, os.SEEK_SET)
        request = {'fd': descriptor, 'model_directory': str(model_directory)}
        environment = {
            'OMP_NUM_THREADS': '1',
            'OPENBLAS_NUM_THREADS': '1',
            'MKL_NUM_THREADS': '1',
            'PYTHONNOUSERSITE': '1',
            'CUDA_VISIBLE_DEVICES': '',
        }
        worker = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name('local_silero_worker.py'))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(descriptor,),
            env=environment,
            text=True,
        )
        try:
            stdout, _stderr = worker.communicate(json.dumps(request), timeout=WALL_SECONDS)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.communicate()
            raise LocalVadError('local_vad_timeout') from None
        if worker.returncode != 0:
            raise LocalVadError('local_vad_worker_failed')
        if len(stdout) > 65536:
            raise LocalVadError('local_vad_invalid_response')
        try:
            response = json.loads(stdout)
        except ValueError:
            raise LocalVadError('local_vad_invalid_response') from None
        if not isinstance(response, dict) or set(response) != {'segments', 'metrics'}:
            raise LocalVadError('local_vad_invalid_response')
        return validate_segments(response['segments'], frames / rate)
    except (OSError, EOFError, wave.Error):
        raise LocalVadError('local_vad_input_unreadable') from None
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.communicate()
        if descriptor is not None:
            os.close(descriptor)
        _slot.release()
