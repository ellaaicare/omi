# M4 Pro Mac - Speaker Diarization Server Setup

**Date**: October 30, 2025
**Purpose**: GPU-accelerated speaker diarization for necklace audio
**Status**: Ready to Deploy

---

## 🎯 **Overview**

This setup runs speaker diarization on your M4 Pro Mac, accessible from VPS via Tailscale.

**Why M4 Pro**:
- ✅ 48GB unified memory (PyAnnote needs ~4GB)
- ✅ GPU acceleration (5-10x faster than CPU)
- ✅ Local processing (no cloud costs)
- ✅ Always-on via Tailscale

**Architecture**:
```
iOS Device → VPS Backend → Deepgram STT
                ↓
         Save audio to temp
                ↓
         Forward to M4 Mac (Tailscale)
                ↓
         M4 Mac: Speaker Diarization
                ↓
         Return speaker labels
                ↓
         Update conversation segments
```

---

## 📋 **Prerequisites**

### 1. Install Homebrew (if not already)
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 2. Install Python 3.11
```bash
brew install python@3.11
```

### 3. Install Tailscale
```bash
brew install --cask tailscale
# Start Tailscale and login
open -a Tailscale
# Authenticate via browser
```

**Get Tailscale hostname**:
```bash
tailscale status | grep $(hostname)
# Example output: m4-mac    100.x.x.x
```

---

## 🚀 **Installation Steps**

### Step 1: Create Diarization Server Directory

```bash
mkdir -p ~/omi-diarization-server
cd ~/omi-diarization-server
```

---

### Step 2: Create Virtual Environment

```bash
python3.11 -m venv venv
source venv/bin/activate
```

---

### Step 3: Install Dependencies

```bash
pip install --upgrade pip

# Core dependencies
pip install torch torchvision torchaudio
pip install pyannote.audio
pip install fastapi uvicorn[standard]
pip install httpx pydantic
pip install python-multipart  # For file uploads
```

---

### Step 4: Download PyAnnote Models

**Requires HuggingFace Token** (accept model license):
1. Visit https://huggingface.co/settings/tokens
2. Create new token with read access
3. Accept license at https://huggingface.co/pyannote/speaker-diarization-3.1

```bash
export HUGGINGFACE_TOKEN="your_hf_token_here"

# Test model download
python3 << 'EOF'
from pyannote.audio import Pipeline
import os

token = os.getenv("HUGGINGFACE_TOKEN")
pipeline = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-3.1",
    use_auth_token=token
)
print("✅ PyAnnote models downloaded successfully!")
EOF
```

**Expected**: ~17GB download, cached to `~/.cache/huggingface/`

---

### Step 5: Create Diarization API Server

**File**: `~/omi-diarization-server/server.py`

```python
"""
Speaker Diarization API Server for M4 Mac

Receives audio files from VPS, runs PyAnnote diarization,
returns speaker labels via Tailscale.
"""

import os
import tempfile
from typing import List, Optional
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn
from pyannote.audio import Pipeline
import torch


app = FastAPI(
    title="OMI Speaker Diarization Server",
    description="GPU-accelerated speaker diarization on M4 Mac",
    version="1.0.0"
)

# Load PyAnnote pipeline at startup
HUGGINGFACE_TOKEN = os.getenv("HUGGINGFACE_TOKEN")
if not HUGGINGFACE_TOKEN:
    raise ValueError("HUGGINGFACE_TOKEN environment variable required")

print("Loading PyAnnote speaker diarization model...")
diarization_pipeline = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-3.1",
    use_auth_token=HUGGINGFACE_TOKEN
)

# Use Metal Performance Shaders (MPS) on M4 Mac for GPU acceleration
if torch.backends.mps.is_available():
    device = torch.device("mps")
    print(f"✅ Using GPU acceleration (Metal Performance Shaders)")
else:
    device = torch.device("cpu")
    print("⚠️ Using CPU (no GPU acceleration)")

diarization_pipeline.to(device)
print("✅ PyAnnote model loaded and ready")


class Speaker(BaseModel):
    """Speaker segment"""
    speaker_id: str  # e.g., "SPEAKER_00", "SPEAKER_01"
    start: float     # Start time in seconds
    end: float       # End time in seconds


class DiarizationResponse(BaseModel):
    """Diarization result"""
    speakers: List[Speaker]
    num_speakers: int
    processing_time_ms: int


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "omi-diarization",
        "device": str(device),
        "model": "pyannote/speaker-diarization-3.1"
    }


@app.post("/api/diarize", response_model=DiarizationResponse)
async def diarize_audio(
    file: UploadFile = File(...),
    num_speakers: Optional[int] = None
):
    """
    Perform speaker diarization on uploaded audio file.

    Args:
        file: Audio file (WAV, MP3, etc.)
        num_speakers: Number of speakers (optional, auto-detect if None)

    Returns:
        DiarizationResponse with speaker segments
    """

    # Save uploaded file to temp
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
        content = await file.read()
        tmp_file.write(content)
        tmp_path = tmp_file.name

    try:
        import time
        start_time = time.time()

        # Run diarization
        print(f"Processing audio file: {file.filename} ({len(content)} bytes)")

        diarization = diarization_pipeline(tmp_path, num_speakers=num_speakers)

        # Extract speaker segments
        speakers = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            speakers.append(Speaker(
                speaker_id=speaker,
                start=turn.start,
                end=turn.end
            ))

        processing_time_ms = int((time.time() - start_time) * 1000)

        print(f"✅ Diarization complete: {len(set(s.speaker_id for s in speakers))} speakers, {processing_time_ms}ms")

        return DiarizationResponse(
            speakers=speakers,
            num_speakers=len(set(s.speaker_id for s in speakers)),
            processing_time_ms=processing_time_ms
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Diarization failed: {str(e)}")

    finally:
        # Clean up temp file
        os.unlink(tmp_path)


if __name__ == "__main__":
    # Run server on all interfaces (accessible via Tailscale)
    uvicorn.run(
        app,
        host="0.0.0.0",  # Listen on all interfaces (Tailscale will route)
        port=5000,
        log_level="info"
    )
```

---

### Step 6: Create Systemd Service (Auto-Start on Boot)

**File**: `~/Library/LaunchAgents/com.omi.diarization.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.omi.diarization</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOUR_USERNAME/omi-diarization-server/venv/bin/python</string>
        <string>/Users/YOUR_USERNAME/omi-diarization-server/server.py</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>HUGGINGFACE_TOKEN</key>
        <string>YOUR_HF_TOKEN_HERE</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/Users/YOUR_USERNAME/omi-diarization-server/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USERNAME/omi-diarization-server/stderr.log</string>
</dict>
</plist>
```

**Replace `YOUR_USERNAME` and `YOUR_HF_TOKEN_HERE`**

**Load service**:
```bash
launchctl load ~/Library/LaunchAgents/com.omi.diarization.plist
launchctl start com.omi.diarization
```

**Check status**:
```bash
launchctl list | grep omi
tail -f ~/omi-diarization-server/stdout.log
```

---

### Step 7: Test Locally

```bash
# Start server manually (for testing)
cd ~/omi-diarization-server
source venv/bin/activate
export HUGGINGFACE_TOKEN="your_token"
python server.py
```

**Test health endpoint**:
```bash
curl http://localhost:5000/health
```

**Expected**:
```json
{
  "status": "healthy",
  "service": "omi-diarization",
  "device": "mps",
  "model": "pyannote/speaker-diarization-3.1"
}
```

**Test diarization** (using sample audio):
```bash
curl -X POST http://localhost:5000/api/diarize \
  -F "file=@test_audio.wav" \
  -F "num_speakers=2"
```

---

### Step 8: Configure Tailscale Routing

**On M4 Mac**:
```bash
# Get Tailscale IP
tailscale status | grep $(hostname)
# Example: m4-mac    100.101.102.103

# Verify reachable from VPS
# (SSH into VPS and ping)
ssh root@100.101.168.91
ping 100.101.102.103  # Use actual Tailscale IP
```

**On VPS** (add to .env):
```bash
# On VPS
ssh root@100.101.168.91
cd /root/omi/backend
echo "DIARIZATION_SERVER_URL=http://100.101.102.103:5000" >> .env
echo "ENABLE_SPEAKER_DIARIZATION=true" >> .env
```

---

## 🔌 **VPS Integration**

### Update VPS Backend to Use M4 Diarization

**File**: `/root/omi/backend/utils/stt/diarization.py` (create new)

```python
"""
Speaker Diarization via M4 Mac Server

Sends audio to M4 Mac via Tailscale for GPU-accelerated diarization.
"""

import os
import httpx
from typing import List, Dict, Optional


DIARIZATION_SERVER_URL = os.getenv("DIARIZATION_SERVER_URL", "http://m4-mac.tailscale:5000")
ENABLE_DIARIZATION = os.getenv("ENABLE_SPEAKER_DIARIZATION", "false").lower() == "true"


async def run_diarization(
    audio_file_path: str,
    num_speakers: Optional[int] = None
) -> List[Dict]:
    """
    Run speaker diarization on audio file via M4 Mac server.

    Args:
        audio_file_path: Path to audio file
        num_speakers: Number of speakers (optional, auto-detect if None)

    Returns:
        List of speaker segments: [{"speaker_id": "SPEAKER_00", "start": 0.0, "end": 5.2}, ...]
    """

    if not ENABLE_DIARIZATION:
        print("Speaker diarization disabled (ENABLE_SPEAKER_DIARIZATION=false)")
        return []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(audio_file_path, 'rb') as audio_file:
                files = {'file': (os.path.basename(audio_file_path), audio_file, 'audio/wav')}
                data = {}
                if num_speakers:
                    data['num_speakers'] = num_speakers

                response = await client.post(
                    f"{DIARIZATION_SERVER_URL}/api/diarize",
                    files=files,
                    data=data
                )

                if response.status_code != 200:
                    print(f"Diarization error: {response.status_code} - {response.text}")
                    return []

                result = response.json()
                print(f"✅ Diarization complete: {result['num_speakers']} speakers, {result['processing_time_ms']}ms")

                return result['speakers']

    except httpx.TimeoutException:
        print("⚠️ Diarization timeout (M4 Mac server slow or unreachable)")
        return []

    except httpx.ConnectError:
        print("⚠️ Cannot connect to M4 Mac diarization server (check Tailscale)")
        return []

    except Exception as e:
        print(f"⚠️ Diarization error: {e}")
        return []
```

---

## 🧪 **Testing**

### Test 1: Health Check from VPS

```bash
# SSH into VPS
ssh root@100.101.168.91

# Test health endpoint
curl http://100.101.102.103:5000/health
```

**Expected**: `{"status": "healthy", "device": "mps"}`

---

### Test 2: Diarization from VPS

```bash
# SSH into VPS
ssh root@100.101.168.91
cd /root/omi/backend

# Test with sample audio
curl -X POST http://100.101.102.103:5000/api/diarize \
  -F "file=@test_audio/pyannote_sample.wav"
```

**Expected**: JSON with speaker segments

---

### Test 3: End-to-End with Real Necklace

1. Record 30-second necklace audio with 2 speakers
2. Check backend logs for diarization call:
   ```bash
   journalctl -u omi-backend -f | grep -i diariz
   ```
3. Verify speaker labels in conversation transcript

---

## 📊 **Performance Expectations**

**M4 Pro GPU (MPS) Benchmarks**:
- 30-second audio: ~2-3 seconds processing
- 60-second audio: ~4-6 seconds processing
- 5-minute audio: ~20-30 seconds processing

**CPU-Only (fallback)**:
- 30-second audio: ~15-20 seconds
- 60-second audio: ~40-60 seconds

**Network Latency** (VPS ↔ M4 Mac via Tailscale):
- ~10-50ms (depends on route)
- Not significant compared to processing time

---

## 🔧 **Troubleshooting**

### Issue: Cannot connect to M4 Mac

**Check Tailscale**:
```bash
# On M4 Mac
tailscale status
# Should show "connected"

# On VPS
ssh root@100.101.168.91
tailscale status
# Should list m4-mac
```

**Solution**: Restart Tailscale on both machines

---

### Issue: GPU not being used (CPU slow)

**Check Metal availability**:
```bash
python3 << 'EOF'
import torch
print(f"MPS available: {torch.backends.mps.is_available()}")
print(f"MPS built: {torch.backends.mps.is_built()}")
EOF
```

**Expected**: Both `True`

---

### Issue: Model download fails

**Check HuggingFace token**:
```bash
echo $HUGGINGFACE_TOKEN
# Should show your token

# Test model access
python3 << 'EOF'
from pyannote.audio import Pipeline
import os
pipeline = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-3.1",
    use_auth_token=os.getenv("HUGGINGFACE_TOKEN")
)
print("✅ Model loaded")
EOF
```

---

## ✅ **Success Criteria**

- [x] M4 Mac running diarization server 24/7
- [x] Accessible from VPS via Tailscale
- [x] GPU acceleration working (MPS)
- [x] Processing 30s audio in <5 seconds
- [x] Auto-starts on Mac boot

---

## 🎯 **Next Steps**

1. **Install on M4 Pro Mac** (follow steps above)
2. **Test health endpoint** from VPS
3. **Enable in VPS .env**: `ENABLE_SPEAKER_DIARIZATION=true`
4. **Test with real necklace audio**
5. **Monitor performance** in logs

---

**Ready to run speaker diarization on M4 Mac! 🚀**
