# Quick Testing Guide - OMI Backend

## 🚀 Ready to Test!

We have **4 test audio files** ready for testing the complete pipeline.

## Test Audio Files Available

✅ **silero_test.wav** (60s, 16kHz mono) - Best for full pipeline test
✅ **pyannote_sample.wav** (30s, 16kHz mono) - Best for speaker diarization
✅ **librivox_sample.wav** (38.8s, 48kHz stereo) - Tests audio conversion
✅ **conversation_sample.wav** (9.2s, 44.1kHz stereo) - Quick smoke test

All files are in `backend/test_audio/` directory.

## Quick Start - 3 Commands

### 1️⃣ Start the Backend (Terminal 1)
```bash
cd /Users/greg/repos/omi/backend
source venv/bin/activate
python start_server.py
```

Wait for: `Application startup complete` and `Uvicorn running on http://0.0.0.0:8000`

### 2️⃣ Run Test with Real Audio (Terminal 2)
```bash
cd /Users/greg/repos/omi/backend
source venv/bin/activate

# Option A: Quick 30-second test with speaker diarization
python test_omi_device_simulation.py --audio-file test_audio/pyannote_sample.wav

# Option B: Full 60-second pipeline test
python test_omi_device_simulation.py --audio-file test_audio/silero_test.wav

# Option C: Quick synthetic audio test (no file needed)
python test_omi_device_simulation.py --duration 10
```

### 3️⃣ Watch the Magic ✨

You should see:
```
🎧 OMI Device Simulator
======================================================================

📂 Loading audio from test_audio/pyannote_sample.wav...
   Original: 1ch, 16bit, 16000Hz, 480000 frames
🔄 Encoding PCM to Opus (frame_size=320)...
   ✅ Encoded 1500 Opus frames

🔌 Connecting to WebSocket...
   ✅ Connected!

📡 Status: ready

🎵 Sending audio frames...
   Sent 50/1500 frames (1.0s elapsed)
   Sent 100/1500 frames (2.0s elapsed)
   ...

🗣️  [0.0s - 2.5s] Speaker 0: The quick brown fox...
🗣️  [2.5s - 5.0s] Speaker 1: Jumped over the lazy dog...

💾 Memory Created!
   ID: abc123-def456-...
   Status: completed

✅ Test completed!
```

## Troubleshooting

### Backend won't start?
```bash
# Check if port 8000 is already in use
lsof -ti:8000 | xargs kill -9

# Restart backend
python start_server.py
```

### Test script errors?
```bash
# Make sure you're in the backend directory
cd /Users/greg/repos/omi/backend

# Activate virtual environment
source venv/bin/activate

# Check dependencies
python -c "import websockets, numpy, opuslib; print('✅ OK')"
```

### No transcription results?
1. Check `DEEPGRAM_API_KEY` in `.env` file
2. Verify backend logs for errors
3. Try with synthetic audio first: `python test_omi_device_simulation.py --duration 5`

## What's Being Tested

This test validates:
- ✅ Audio encoding (PCM → Opus)
- ✅ WebSocket connection to `/v4/listen`
- ✅ Real-time audio streaming
- ✅ Speech-to-text (Deepgram)
- ✅ Voice activity detection (Silero VAD)
- ✅ Speaker diarization (PyAnnote)
- ✅ Conversation processing
- ✅ Database storage

## Next Steps After Testing

Once testing works:
1. Test with real OMI hardware
2. Integrate Letta for memory processing
3. Deploy to M1 iMac for 24/7 operation
4. Connect to VPS (Redis + Postgres + Tailscale)

## Files Created This Session

```
backend/
├── .env                              # API keys and credentials ✅
├── start_server.py                   # Helper to start backend ✅
├── download_models.py                # PyAnnote downloader ✅
├── download_whisper_models.py        # Whisper downloader ✅
├── test_omi_device_simulation.py     # Device simulator ✅
├── README_TESTING.md                 # Full testing docs ✅
├── QUICK_TEST.md                     # This file ✅
└── test_audio/
    ├── README.md                     # Audio file docs ✅
    ├── silero_test.wav              # 60s test audio ✅
    ├── pyannote_sample.wav          # 30s with speakers ✅
    ├── librivox_sample.wav          # 38.8s high quality ✅
    └── conversation_sample.wav      # 9.2s quick test ✅
```

All ready to go! 🎉
