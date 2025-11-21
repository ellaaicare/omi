# Advanced Audio Processing Features - Setup Guide

**Date**: October 30, 2025
**Status**: Optional Features - Currently Disabled
**Priority**: Low (system works without them)

---

## 🎯 **TL;DR - Do You Need These?**

**Current Status**: ✅ System working without these features
- Deepgram Nova 3 transcription: **ENABLED** ✅
- VAD (Voice Activity Detection): **DISABLED** ⚠️
- Speaker Diarization: **DISABLED** ⚠️

**Quick Decision Matrix**:

| Feature | Benefit | Best For | Skip If |
|---------|---------|----------|---------|
| **VAD (Silero)** | Filters non-speech noise | Necklace with noisy environment | Using phone/quiet environment |
| **Speaker Diarization** | Identifies different speakers | Multi-person conversations | Solo recordings only |

**Recommendation**: Enable VAD for necklace, skip diarization for now.

---

## 📊 **Feature Comparison**

### Current System (Enabled)

**What's Working**:
- ✅ Deepgram Nova 3 STT (cloud-based, real-time)
- ✅ WebSocket streaming
- ✅ Conversation processing & memory extraction
- ✅ Discard logic (filters test/noise)

**Quality**: Excellent for clean audio (phone calls, quiet rooms)

**Cost**: ~$0.005 per conversation minute (Deepgram)

---

### VAD (Voice Activity Detection) - Silero

**What It Does**:
- Filters out segments with no speech (silence, background noise, rustling)
- Only sends speech audio to Deepgram
- Saves transcription costs

**Benefits for Necklace**:
- ✅ Reduces noise from clothing rustling, wind, ambient sounds
- ✅ Saves Deepgram costs (only transcribe speech)
- ✅ Improves transcript quality (no false transcriptions of noise)
- ✅ Local ML model (17MB, runs on backend CPU)

**Costs**:
- ❌ Adds 50-100ms latency per chunk (CPU processing)
- ❌ Requires 17MB model download
- ❌ Slight CPU overhead

**When to Enable**:
- Using necklace/wearable device (lots of ambient noise)
- Recording in noisy environments (outdoors, crowds)
- Want to reduce Deepgram costs

**When to Skip**:
- Using phone/headset (clean audio)
- Recording in quiet rooms
- Real-time latency is critical (<500ms)

---

### Speaker Diarization - PyAnnote

**What It Does**:
- Identifies different speakers in multi-person conversations
- Labels transcript segments with speaker IDs (SPEAKER_00, SPEAKER_01, etc.)
- Helps memory extraction understand who said what

**Benefits**:
- ✅ Multi-person conversation clarity
- ✅ Better context for memory extraction ("User said X, other person said Y")
- ✅ Voice profile learning (future feature)

**Costs**:
- ❌ 17GB model download (HUGE!)
- ❌ Adds 2-3 seconds latency per conversation
- ❌ Requires GPU for real-time (or slow on CPU)
- ❌ High memory usage (~4GB RAM during processing)

**When to Enable**:
- Recording multi-person conversations (meetings, group discussions)
- Need to attribute statements to specific speakers
- Have GPU available (CUDA/MPS)

**When to Skip**:
- Solo recordings only (podcasts, personal notes)
- Limited compute resources
- Real-time latency is critical

---

## 🔧 **How to Enable VAD (Recommended for Necklace)**

### Step 1: Verify Model Download

**Check if Silero VAD model is cached**:
```bash
ssh root@100.101.168.91
ls -lh ~/.cache/torch/hub/ | grep silero
```

**Expected Output**:
```
drwxr-xr-x 3 root root 4.0K Oct 28 snakers4_silero-vad_master
```

**If not present**, model will auto-download on first backend start (~17MB).

---

### Step 2: Enable VAD in Code

**File**: `/root/omi/backend/routers/transcribe.py`

**Find line ~720** (VAD initialization):
```python
# Currently commented out or disabled
# vad_model = load_silero_vad_model()
```

**Change to**:
```python
vad_model = load_silero_vad_model()
vad_enabled = True  # Add this flag
```

**Find line ~850** (audio processing loop):
```python
# Currently sending all audio to Deepgram
audio_buffer.extend(audio_chunk)
deepgram.send(audio_chunk)
```

**Change to**:
```python
# Add VAD filtering
audio_buffer.extend(audio_chunk)

if vad_enabled:
    # Check if chunk contains speech
    has_speech = vad_model.detect_speech(audio_chunk)
    if has_speech:
        deepgram.send(audio_chunk)  # Only send speech
    else:
        print(f"VAD: Skipped non-speech chunk at {current_time}s")
else:
    deepgram.send(audio_chunk)  # Send all audio
```

---

### Step 3: Make VAD Optional (Environment Variable)

**File**: `/root/omi/backend/.env`

**Add**:
```bash
# Audio Processing Features
ENABLE_VAD=true
ENABLE_SPEAKER_DIARIZATION=false
```

**File**: `/root/omi/backend/routers/transcribe.py`

**Add at top**:
```python
import os

ENABLE_VAD = os.getenv('ENABLE_VAD', 'false').lower() == 'true'
ENABLE_DIARIZATION = os.getenv('ENABLE_SPEAKER_DIARIZATION', 'false').lower() == 'true'
```

**Update processing logic**:
```python
if ENABLE_VAD:
    vad_model = load_silero_vad_model()
    print("VAD enabled for noise filtering")
else:
    print("VAD disabled - processing all audio")
```

---

### Step 4: Restart Backend

```bash
ssh root@100.101.168.91
systemctl restart omi-backend
journalctl -u omi-backend -f  # Watch logs
```

**Expected logs**:
```
INFO: Loading Silero VAD model...
INFO: VAD enabled for noise filtering
INFO: WebSocket endpoint ready
```

---

### Step 5: Test with Noisy Audio

**Record 30-second necklace audio** with:
- 10 seconds of speech
- 10 seconds of silence
- 10 seconds of rustling/noise

**Expected Behavior**:
- VAD detects speech segments
- Only speech sent to Deepgram (saves ~60% cost)
- Transcript only contains actual speech

**Check logs**:
```bash
journalctl -u omi-backend -f | grep VAD
```

**Expected**:
```
VAD: Speech detected at 0.0-4.5s
VAD: Skipped non-speech chunk at 5.0s
VAD: Skipped non-speech chunk at 6.0s
VAD: Speech detected at 10.2-15.8s
```

---

## 🎤 **How to Enable Speaker Diarization (Optional)**

### Step 1: Check Model Download

**Verify PyAnnote models** (~17GB):
```bash
ssh root@100.101.168.91
du -sh ~/.cache/huggingface/ | grep GB
```

**Expected**: ~17GB cached

**If not present**:
```bash
cd /root/omi/backend
source venv/bin/activate
python download_models.py  # Downloads PyAnnote models
```

---

### Step 2: Enable in Environment

**File**: `/root/omi/backend/.env`

**Add**:
```bash
ENABLE_SPEAKER_DIARIZATION=true
HUGGINGFACE_TOKEN=your_hf_token_here  # Required for PyAnnote
```

**Get HuggingFace token**:
1. Visit https://huggingface.co/settings/tokens
2. Create new token with read access
3. Accept PyAnnote model license at https://huggingface.co/pyannote/speaker-diarization-3.1

---

### Step 3: Update Processing Code

**File**: `/root/omi/backend/routers/transcribe.py`

**Add diarization processing** (after transcription completes):
```python
if ENABLE_DIARIZATION and len(transcript_segments) > 0:
    print("Running speaker diarization...")
    from utils.stt.diarization import run_diarization

    # Run diarization on full audio
    diarization_results = run_diarization(
        audio_file_path=f"/tmp/{session_id}.wav",
        num_speakers=None  # Auto-detect
    )

    # Merge diarization with transcript
    for segment in transcript_segments:
        speaker_id = find_speaker_for_timestamp(
            diarization_results,
            segment['start'],
            segment['end']
        )
        segment['speaker'] = speaker_id
        segment['is_user'] = (speaker_id == 'SPEAKER_00')  # First speaker = user

    print(f"Diarization complete: {len(set(s['speaker'] for s in transcript_segments))} speakers")
```

---

### Step 4: Performance Considerations

**Diarization is SLOW** (2-3 seconds per minute of audio):
- Run in background thread (don't block response)
- OR run as post-processing job
- OR skip for real-time use cases

**Recommended Approach** (async processing):
```python
# Don't run diarization during WebSocket
# Instead, run it after conversation completes

def process_conversation_with_diarization(session_id, audio_path):
    # This runs in background thread
    diarization = run_diarization(audio_path)
    update_conversation_speakers(session_id, diarization)
```

---

## 🌐 **Deployment Architecture Options**

### Option 1: Backend-Only (Current)

```
iOS Device → VPS Backend → Deepgram API
              ↓
         [VAD Filter]
              ↓
         [Speaker Diarization (optional)]
```

**Pros**:
- ✅ Simple deployment
- ✅ No additional infrastructure
- ✅ Works with current Tailscale setup

**Cons**:
- ❌ VAD/diarization adds CPU load to backend
- ❌ Limited to backend CPU (no GPU acceleration)

---

### Option 2: Separate Processing Server (M4 Pro / M1 Mac)

```
iOS Device → VPS Backend → Deepgram API
              ↓
         [Forward to Processing Server via Tailscale]
              ↓
         M4 Pro Mac
         - VAD processing
         - Speaker diarization (GPU-accelerated)
         - WhisperX (local STT)
              ↓
         Results back to VPS
```

**Pros**:
- ✅ GPU acceleration for diarization (5x faster)
- ✅ Can run local WhisperX for HIPAA compliance
- ✅ Offload compute from VPS
- ✅ Already on Tailscale (easy routing)

**Cons**:
- ❌ Additional infrastructure complexity
- ❌ Network latency (VPS ↔ Mac)
- ❌ Requires always-on Mac

---

### Option 3: Cloud GPU Instance (Future)

```
iOS Device → VPS Backend → GPU Instance (Lambda Labs, RunPod)
              ↓
         - Fast diarization
         - Local STT
         - Audio processing
```

**Pros**:
- ✅ Scalable compute
- ✅ GPU acceleration
- ✅ No Mac setup needed

**Cons**:
- ❌ Additional cost (~$0.30/hour for GPU)
- ❌ More complex architecture

---

## 🎛️ **Configuration via Environment Flags**

### Recommended `.env` Setup

```bash
# === Audio Processing Features ===

# Voice Activity Detection (Silero)
# Filters non-speech audio before transcription
# Benefits: Reduces cost, improves quality
# Cost: +50ms latency, 17MB model
ENABLE_VAD=true  # Recommended for necklace

# Speaker Diarization (PyAnnote)
# Identifies different speakers
# Benefits: Multi-person conversation clarity
# Cost: +2-3s latency, 17GB model, high CPU/GPU
ENABLE_SPEAKER_DIARIZATION=false  # Enable only if needed

# HuggingFace Token (required for diarization)
HUGGINGFACE_TOKEN=your_hf_token_here

# === STT Provider ===

# Primary transcription service
STT_PROVIDER=deepgram  # Options: deepgram, whisperx, soniox
DEEPGRAM_API_KEY=your_deepgram_key
DEEPGRAM_MODEL=nova-3-general  # Options: nova-3-general, nova-2-enhanced, nova-3-meeting

# === Performance Tuning ===

# Chunk size for VAD processing (ms)
VAD_CHUNK_SIZE=600  # 600ms chunks (default)

# Minimum speech duration to transcribe (ms)
MIN_SPEECH_DURATION=500  # Skip very short speech segments

# Number of speakers (for diarization, or null for auto-detect)
MAX_SPEAKERS=null  # Auto-detect speaker count
```

---

## 📊 **Cost & Performance Comparison**

### Scenario: 30-minute noisy necklace recording

| Setup | Transcription Cost | Processing Time | Quality |
|-------|-------------------|----------------|---------|
| **Deepgram only** | $0.15 (30 min × $0.005) | ~30 seconds | Good (some noise transcribed) |
| **Deepgram + VAD** | $0.05 (10 min speech × $0.005) | ~35 seconds | Excellent (noise filtered) |
| **Deepgram + VAD + Diarization** | $0.05 | ~95 seconds | Excellent + speaker IDs |
| **Local WhisperX + VAD** | $0 | ~45 seconds | Very good (offline) |

**Best for Necklace**: Deepgram + VAD ($0.05, 35s, excellent quality)

---

## 🚀 **Quick Start: Enable VAD for Necklace**

**1-minute setup**:

```bash
# SSH into VPS
ssh root@100.101.168.91

# Add VAD flag to environment
cd /root/omi/backend
echo "ENABLE_VAD=true" >> .env

# Restart backend
systemctl restart omi-backend

# Verify enabled
journalctl -u omi-backend -n 50 | grep VAD
```

**Expected**: "VAD enabled for noise filtering"

**Test**: Record 30s necklace audio, check logs for "VAD: Speech detected" messages

---

## 🔍 **Monitoring & Debugging**

### Check VAD Performance

**Logs to monitor**:
```bash
journalctl -u omi-backend -f | grep -E "VAD|speech"
```

**Expected output**:
```
VAD: Speech detected at 0.0-4.5s (confidence: 0.95)
VAD: Skipped non-speech chunk at 5.0s
VAD: Speech detected at 10.2-15.8s (confidence: 0.88)
```

**Metrics to track**:
- Speech detection rate (% of chunks with speech)
- False positives (noise transcribed as speech)
- False negatives (speech not detected)
- Deepgram cost savings (before/after VAD)

---

### Check Diarization Performance

**Logs to monitor**:
```bash
journalctl -u omi-backend -f | grep -E "diarization|speakers"
```

**Expected output**:
```
Running speaker diarization...
Diarization complete: 2 speakers detected
SPEAKER_00: 65% of conversation
SPEAKER_01: 35% of conversation
```

---

## 🎯 **Summary & Recommendations**

### For Necklace Audio (Your Use Case)

**Enable**:
- ✅ VAD (Silero) - Critical for filtering noise

**Skip** (for now):
- ❌ Speaker Diarization - Overkill for solo recordings

**Rationale**:
- Necklace has lots of ambient noise → VAD filters it
- Solo recordings → Don't need speaker IDs
- Cost savings: 50-70% reduction in Deepgram charges

---

### For Future 2-Way Phone Calls

**Enable**:
- ✅ VAD - Still helpful for filtering silence
- ✅ Speaker Diarization - Distinguish user vs caller

**Architecture**:
- Real-time STT (Deepgram) + VAD
- Background diarization (post-call)
- M4 Pro Mac for GPU-accelerated processing (via Tailscale)

---

## 📞 **Setup Support**

**Need help enabling**:
- Discord: `#tasks-assignment`
- Can provide step-by-step walkthrough
- Can test VAD/diarization on sample audio

---

**Ready to enable VAD? Let's filter that necklace noise! 🎙️**
