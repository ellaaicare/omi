# Testing Setup Issue - Firebase Configuration

## Current Blocker

The backend test is failing because **Firestore API is not enabled** for the Firebase project `omi-dev-ca005`.

### Error Message
```
Cloud Firestore API has not been used in project omi-dev-ca005 before or it is disabled.
```

## Solutions (Pick One)

### Option 1: Enable Firestore API (Recommended)

**Step 1: Enable the API**
1. Visit: https://console.developers.google.com/apis/api/firestore.googleapis.com/overview?project=omi-dev-ca005
2. Click "Enable API"
3. Wait 2-5 minutes for the change to propagate

**Step 2: Create the Database**
1. Visit: https://console.cloud.google.com/datastore/setup?project=omi-dev-ca005
2. Choose "Firestore Native Mode" (recommended)
3. Select region (e.g., us-central1)
4. Click "Create Database"
5. Wait ~30 seconds for provisioning

**Step 3: Create Test User**
```bash
python create_test_user.py
```

**Step 4: Run Test**
```bash
python test_omi_device_simulation.py --audio-file test_audio/pyannote_sample.wav
```

### Option 2: Test Audio Processing Only (Quick Validation)

Test just the audio encoding/decoding without the full backend:

```bash
# Test that audio loading and Opus encoding works
python -c "
from test_omi_device_simulation import load_audio_file, encode_pcm_to_opus
import os
os.chdir('backend')

pcm = load_audio_file('test_audio/pyannote_sample.wav')
frames = encode_pcm_to_opus(pcm)
print(f'✅ Successfully encoded {len(frames)} Opus frames')
"
```

### Option 3: Use Different Firebase Project

If you have another Firebase project with Firestore already enabled:

1. Replace `google-credentials.json` with credentials from that project
2. Update `FIREBASE_PROJECT_ID` in `.env`
3. Run `python create_test_user.py`
4. Test again

### Option 4: Real OMI Device Testing

If you have a real OMI device with a valid Firebase user:
- Get the Firebase UID from your app
- Use `--uid your-real-firebase-uid` when running the test script

## What We've Verified So Far

✅ All ML models downloaded correctly (~17GB)
✅ Backend server starts successfully
✅ Audio file loading works (tested 4 files)
✅ Opus encoding works (1500 frames from 30s audio)
✅ WebSocket connection attempt successful
✅ LOCAL_DEVELOPMENT mode configured

❌ Firestore API not enabled (blocks user validation)

## Next Steps After Firestore is Enabled

Once Firestore is enabled and test user created:

1. The full test will work end-to-end
2. You'll see transcription results in real-time
3. Speaker diarization will identify multiple speakers
4. Conversations will be saved to Firestore

## Alternative: Skip Firebase for Local Testing

If you want to test WITHOUT Firebase temporarily, you could:
1. Comment out the user validation in `routers/transcribe.py` lines 281-284
2. This is NOT recommended for production but works for local audio processing tests

```python
# TEMPORARY BYPASS FOR LOCAL TESTING ONLY
# if not user_db.is_exists_user(uid):
#     websocket_active = False
#     await websocket.close(code=1008, reason="Bad user")
#     return
```

**Note**: This bypasses authentication and should NEVER be deployed to production!
