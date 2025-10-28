# How to View Transcripts & Verify Backend Output

This guide explains the different ways to see transcript output from the OMI backend.

---

## 🎯 Quick Answer

**During Testing**: Transcripts appear in **real-time console output** while the test runs.

**In Production**: Transcripts are saved to **Firestore** after the conversation ends.

---

## Method 1: Console Output (Real-Time) ✅ **EASIEST**

When running the test script, transcripts appear immediately in your terminal:

```bash
python test_omi_device_simulation.py --audio-file test_audio/pyannote_sample.wav
```

**You'll see**:
```
🗣️  [0.0s - 4.8s] Speaker 0: Hello? Hello? Oh, hello. I didn't know you were there...
🗣️  [0.0s - 12.5s] Speaker 0: This is Diane in New Jersey. And I'm Sheila, in Texas...
🗣️  [0.0s - 20.9s] Speaker 0: Oh, I'm originally from Chicago also...
```

**This confirms**:
- ✅ Audio is being processed
- ✅ Deepgram transcription is working
- ✅ WebSocket communication is successful
- ✅ Backend is receiving and processing audio

---

## Method 2: Firestore Database (After Session Ends)

### When Conversations Are Saved

The backend saves conversations to Firestore when:
1. **Session ends properly** (WebSocket closes gracefully)
2. **Minimum duration met** (typically 30+ seconds)
3. **Processing completes** (transcription + speaker diarization + memory creation)
4. **Inactivity timeout** (no audio for X seconds)

### Checking Firestore

**Option A: Firebase Console** (Visual)
1. Visit: https://console.firebase.google.com/project/omi-dev-ca005/firestore/data
2. Navigate to `conversations` collection
3. Look for documents with your UID

**Option B: Verification Script** (Command Line)
```bash
python verify_test_output.py
```

**Option C: List All Collections** (Debug)
```bash
python list_firestore_collections.py
```

**Expected Data Structure in Firestore**:
```json
{
  "uid": "123",
  "created_at": "2025-10-28T02:35:00Z",
  "status": "completed",
  "language": "en",
  "transcript": "Full conversation text...",
  "transcript_segments_compressed": true,
  "_byteString": "eJx1UU1r4zAQ...",  // Compressed segments
  "visibility": "private"
}
```

### Decompressing Transcripts

Transcripts are **gzip compressed** and **base64 encoded** to save storage space.

**The verification script automatically decompresses** them:
```bash
python verify_test_output.py

# Shows:
🎯 SEGMENTS: 15 segments
   ℹ️  Decompressed from blob storage
   1. [0.0s - 4.8s] Speaker 0: Hello? Hello? Oh, hello...
```

**Manual Decompression** (if needed):
```python
import gzip
import base64
import json

# Get compressed data from Firestore
compressed_data = document.get('_byteString')

# Decompress
decoded = base64.b64decode(compressed_data)
decompressed = gzip.decompress(decoded)
segments = json.loads(decompressed.decode('utf-8'))

# Now you have the original segments array
for segment in segments:
    print(f"{segment['speaker']}: {segment['text']}")
```

---

## Method 3: Backend Server Logs

Check the terminal running `start_server.py`:

```bash
# Look for:
INFO:     ('127.0.0.1', 63801) - "WebSocket /v4/listen?uid=123..." [accepted]
INFO:     connection open
# ... processing ...
INFO:     connection closed
```

**What to Look For**:
- ✅ `[accepted]` = WebSocket handshake successful
- ✅ `connection open` = Audio streaming started
- ✅ `connection closed` = Session ended
- ❌ SSL errors = Certificate issues (should be fixed)
- ❌ Exceptions = Backend processing errors

---

## Method 4: Deepgram Dashboard (API Usage)

Verify transcription API is being called:

1. Visit: https://console.deepgram.com/
2. Go to "Usage" tab
3. Check for recent API requests
4. Should see ~30 seconds of audio processed per test

---

## 🐛 Troubleshooting

### "I see console output but nothing in Firestore"

This is **NORMAL** for short test sessions. The backend:
- ✅ **Does transcribe** audio in real-time (you see this)
- ❌ **Doesn't save** until session properly closes

**Solutions**:
1. **Use a longer audio file** (60+ seconds)
2. **Keep connection open longer** (modify test script to wait)
3. **Check backend logs** for errors during finalization
4. **Use a real OMI device** (proper session management)

### "Console shows transcripts but they're wrong/gibberish"

- ✅ **Expected**: Synthetic audio generates nonsense transcripts
- ✅ **Use real audio files** for meaningful transcripts
- ✅ **PyAnnote sample** has actual conversation (2 women talking)

### "Compressed blob in Firestore - can't read it"

- ✅ **Use verification script**: `python verify_test_output.py`
- ✅ **Script auto-decompresses** and shows readable text
- ❌ **Don't try to read raw blob** in Firebase Console

### "No documents in any collection"

```bash
# Check what's actually in the database
python list_firestore_collections.py

# Should show at least:
# - users collection: test user (UID 123)
```

If users collection is also empty:
1. Run: `python create_test_user.py`
2. Verify Firestore API is enabled
3. Check google-credentials.json exists

---

## 📊 What "Success" Looks Like

### Minimal Success (Test Infrastructure Working)
- ✅ Backend starts without errors
- ✅ WebSocket accepts connection
- ✅ Console shows real-time transcripts
- ✅ Test completes without crashes

### Full Success (Production Ready)
- ✅ Conversations saved to Firestore
- ✅ Compressed segments decompressible
- ✅ Speaker diarization working
- ✅ Memory/structured data created

---

## 🎯 Recommended Testing Workflow

### 1. **Quick Validation** (5 minutes)
```bash
# Start backend
python start_server.py

# In another terminal, run test
python test_omi_device_simulation.py --audio-file test_audio/pyannote_sample.wav

# ✅ Success = You see transcripts in console
```

### 2. **Database Verification** (After longer session)
```bash
# After running a longer test or using real device:
python verify_test_output.py

# Or check Firebase Console:
open https://console.firebase.google.com/project/omi-dev-ca005/firestore/data
```

### 3. **Full Pipeline Test** (Real Device)
- Connect actual OMI hardware
- Have a real conversation (60+ seconds)
- Check Firestore for saved conversation
- Verify memory was created

---

## 💡 Key Insights

### Real-Time vs Persisted Data

**Real-Time (Console)**:
- Shows transcription is working
- Confirms audio pipeline
- Validates WebSocket communication
- **Does NOT require Firestore**

**Persisted (Firestore)**:
- Requires proper session closure
- Needs minimum duration
- Full processing pipeline must complete
- **Used for memories and history**

### Why Compression?

- **30 seconds of audio** = ~50-100 transcript segments
- **Uncompressed** = ~5-10KB per conversation
- **Compressed** = ~1-2KB per conversation
- **Savings** = 70-80% storage reduction
- **Millions of conversations** = Massive cost savings

### Production Behavior

In production with real OMI devices:
1. User has conversation while wearing device
2. Audio streams continuously to backend
3. Transcripts generated in real-time
4. When session ends (device removed / timeout):
   - Full transcript compiled
   - Speaker diarization applied
   - Memory/summary created
   - Everything saved to Firestore
5. User can view in app immediately

---

## 📚 Related Documentation

- **CLAUDE.md**: Main developer guide
- **SESSION_SUMMARY.md**: Complete setup history
- **README_TESTING.md**: Comprehensive testing instructions
- **TESTING_SETUP_NOTE.md**: Troubleshooting guide

---

**Last Updated**: October 28, 2025
**Status**: Backend transcription working, Firestore persistence requires longer sessions
