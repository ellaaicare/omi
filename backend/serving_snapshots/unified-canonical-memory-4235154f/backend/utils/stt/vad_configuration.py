"""Normal sync VAD admission: configured hosted service or pinned cached CPU model."""
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import HTTPException

ASSETS={'data/silero_vad.jit':'e1122837f4154c511485fe0b9c64455f7b929c96fbb8d79fbdb336383ebd3720','utils_vad.py':'7e0f9b3334ee4feb79abcf2251be12458329077bce28c8540878ed9d35e5aa6b'}
MODEL_DIRECTORY=Path(__file__).resolve().parents[2]/'pretrained_models/snakers4_silero-vad_master/src/silero_vad'

def hosted_vad_configured():
    raw=os.getenv('HOSTED_VAD_API_URL','').strip()
    if not raw:return False
    try:
        parsed=urlsplit(raw);parsed.port
        if parsed.scheme in {'http','https'} and parsed.hostname:return True
    except ValueError:pass
    raise HTTPException(status_code=503,detail={'code':'sync_vad_endpoint_invalid','retryable':True})

def require_sync_vad_configuration():
    if hosted_vad_configured():return
    try:
        if all(hashlib.sha256((MODEL_DIRECTORY/name).read_bytes()).hexdigest()==pin for name,pin in ASSETS.items()):return
    except (OSError,ValueError):pass
    raise HTTPException(status_code=429,detail={'code':'sync_vad_unconfigured','retryable':True},headers={'Retry-After':'300','X-Omi-Rate-Limit-Reason':'sync_vad_unconfigured'})
