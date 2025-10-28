# OMI Backend Testing Guide

## Quick Start Testing

This guide covers how to test the OMI backend with simulated device data.

### Prerequisites

1. **Backend Running**: Make sure the backend server is running:
   ```bash
   source venv/bin/activate
   python start_server.py
   ```
   Backend should be accessible at `http://localhost:8000`

2. **Environment Variables**: Ensure `.env` file has:
   - `DEEPGRAM_API_KEY` - For speech-to-text
   - `OPENAI_API_KEY` - For conversation processing
   - `FIREBASE_PROJECT_ID` - For user authentication
   - All other required API keys

3. **Test User**: You'll need a valid Firebase UID for testing. Options:
   - Set `TEST_USER_UID` in `.env` file
   - Pass `--uid` flag to test script
   - Use the UID from your Firebase dev environment

### Running the Device Simulator

The test script (`test_omi_device_simulation.py`) simulates an OMI device sending audio to the backend.

#### Option 1: Synthetic Audio (Quick Test)

Generate synthetic audio for testing:

```bash
source venv/bin/activate
python test_omi_device_simulation.py --duration 10
```

This will:
- Generate 10 seconds of synthetic speech-like audio
- Encode it as Opus frames
- Send it to the backend via WebSocket
- Display real-time transcription results

#### Option 2: Real Audio File

Test with an actual audio file:

```bash
python test_omi_device_simulation.py --audio-file path/to/your/audio.wav
```

Requirements for audio files:
- Format: WAV (will be auto-converted)
- Sample rate: Any (will be resampled to 16kHz)
- Channels: Mono or stereo (stereo will be converted to mono)

#### Option 3: Custom User ID

Test with a specific Firebase user:

```bash
python test_omi_device_simulation.py --uid "your-firebase-uid" --duration 15
```

### Expected Output

When successful, you should see:

```
==================================================================
🎧 OMI Device Simulator
==================================================================

🎤 Generating 10s of synthetic audio at 16000Hz...
🔄 Encoding PCM to Opus (frame_size=320)...
   ✅ Encoded 500 Opus frames

🔌 Connecting to WebSocket...
   URL: ws://localhost:8000/v4/listen?uid=...
   ✅ Connected!

📡 Status: initiating - Service Starting
📡 Status: stt_initiating - STT Service Starting
📡 Status: ready

🎵 Sending audio frames...
   Total frames: 500
   Frame size: 320 samples (20.0ms)

   Sent 50/500 frames (1.0s elapsed)
   Sent 100/500 frames (2.0s elapsed)
   ...

✅ Sent all 500 audio frames

🗣️  [0.0s - 2.5s] Speaker 0: Hello this is a test
🗣️  [2.5s - 5.0s] Speaker 0: of the OMI backend

💾 Memory Created!
   ID: abc123...
   Status: completed

✅ Test completed!
```

### Troubleshooting

#### Connection Refused

```
❌ Connection refused - is the backend running on localhost:8000?
```

**Solution**: Start the backend server:
```bash
source venv/bin/activate
python start_server.py
```

#### Invalid User UID

```
❌ WebSocket closed: Bad user
```

**Solution**: Provide a valid Firebase UID:
```bash
python test_omi_device_simulation.py --uid "your-valid-firebase-uid"
```

#### No Transcription Results

If audio is sent but no transcriptions appear:

1. Check Deepgram API key is valid
2. Verify the backend logs for STT errors
3. Ensure you have transcription credits in your Firebase user account

#### Module Not Found

```
ModuleNotFoundError: No module named 'websockets'
```

**Solution**: Install dependencies:
```bash
source venv/bin/activate
pip install websockets numpy opuslib
```

### Testing Different Scenarios

#### Test Speaker Diarization

Use an audio file with multiple speakers:

```bash
python test_omi_device_simulation.py --audio-file multi_speaker_conversation.wav
```

The output should show different `Speaker 0`, `Speaker 1`, etc.

#### Test Long-Form Audio

Test with longer duration to verify conversation timeout logic:

```bash
python test_omi_device_simulation.py --duration 300  # 5 minutes
```

The backend should create separate conversations based on silence detection.

#### Test Different Languages

Modify the script to test non-English languages:

```python
# In test_omi_device_simulation.py, change:
ws_url = f"{BACKEND_WS_URL}?uid={TEST_UID}&language=es&sample_rate={SAMPLE_RATE}..."
```

### Understanding the Output

**Status Messages** (`📡`): Backend service status updates
- `initiating`: WebSocket connection established
- `stt_initiating`: Speech-to-text service starting
- `ready`: Ready to receive audio

**Transcript Segments** (`🗣️`): Real-time transcription results
- Format: `[start - end] Speaker N: text`
- Start/end times in seconds
- Speaker ID from diarization

**Memory Created** (`💾`): Conversation processed and stored
- Memory ID: Unique conversation identifier
- Status: `completed` or `processing`

### Next Steps

1. **Test with Real Device**: Once simulator works, test with actual OMI hardware
2. **Verify Letta Integration**: Check that conversations are sent to Letta for processing
3. **Test Edge Cases**: Network interruptions, long silences, background noise
4. **Performance Testing**: Multiple concurrent connections, high audio quality

### Advanced Testing

For advanced testing scenarios, see:
- `utils/stt/streaming.py` - STT service configuration
- `routers/transcribe.py` - WebSocket protocol implementation
- `utils/conversations/process_conversation.py` - Conversation processing logic

### Model Downloads

Models are downloaded to:
- `~/.cache/huggingface/` - PyAnnote, Whisper models (~17GB)
- `~/.cache/torch/` - Silero VAD (~31MB)

These are automatically downloaded on first use and cached for subsequent runs.
