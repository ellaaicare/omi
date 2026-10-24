"""One owned CPU process; pinned cached assets and inherited WAV descriptor only."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys
import time
import wave

MODEL_SHA = 'e1122837f4154c511485fe0b9c64455f7b929c96fbb8d79fbdb336383ebd3720'
UTILITY_SHA = '7e0f9b3334ee4feb79abcf2251be12458329077bce28c8540878ed9d35e5aa6b'


def deny_network_and_children(event, arguments):
    if event.startswith('socket.') or event in {'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
        raise RuntimeError('local_vad_forbidden_io')


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (25, 28))
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.nice(10)
    sys.addaudithook(deny_network_and_children)
    request = json.loads(sys.stdin.read(4097))
    if set(request) != {'fd', 'model_directory'} or type(request['fd']) is not int:
        raise RuntimeError('local_vad_invalid_request')
    root = Path(request['model_directory'])
    model_path, utility_path = root / 'data/silero_vad.jit', root / 'utils_vad.py'
    if hashlib.sha256(model_path.read_bytes()).hexdigest() != MODEL_SHA:
        raise RuntimeError('local_vad_asset_mismatch')
    if hashlib.sha256(utility_path.read_bytes()).hexdigest() != UTILITY_SHA:
        raise RuntimeError('local_vad_asset_mismatch')
    started = time.monotonic()
    # Torch is imported only inside this bounded worker, never in the service.
    torch = importlib.import_module('torch')
    torch.set_num_threads(1)
    spec = importlib.util.spec_from_file_location('ella_pinned_cached_silero_utils', utility_path)
    utility = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(utility)
    model = torch.jit.load(str(model_path), map_location='cpu')
    model.eval()
    with os.fdopen(request['fd'], 'rb') as audio:
        with wave.open(audio, 'rb') as reader:
            rate, frames = reader.getframerate(), reader.getnframes()
            if (reader.getnchannels(), reader.getsampwidth(), reader.getcomptype()) != (1, 2, 'NONE'):
                raise RuntimeError('local_vad_input_format')
            if rate not in (8000, 16000) or not 0 < frames <= rate * 300:
                raise RuntimeError('local_vad_input_budget')
            raw = reader.readframes(frames)
            if len(raw) != frames * 2:
                raise RuntimeError('local_vad_input_truncated')
    samples = torch.frombuffer(bytearray(raw), dtype=torch.int16).to(torch.float32).div_(32768.0)
    del raw
    with torch.inference_mode():
        spans = utility.get_speech_timestamps(samples, model, sampling_rate=rate, return_seconds=False)
    segments = [
        {'start': span['start'] / rate, 'end': span['end'] / rate, 'duration': (span['end'] - span['start']) / rate}
        for span in spans
    ]
    metrics = {
        'wall_seconds': round(time.monotonic() - started, 6),
        'cpu_seconds': round(
            resource.getrusage(resource.RUSAGE_SELF).ru_utime + resource.getrusage(resource.RUSAGE_SELF).ru_stime, 6
        ),
        'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'address_space_limit_bytes': resource.getrlimit(resource.RLIMIT_AS)[0],
        'cpu_limit_seconds': resource.getrlimit(resource.RLIMIT_CPU)[0],
    }
    print(json.dumps({'segments': segments, 'metrics': metrics}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('local_vad_worker_failed:' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
