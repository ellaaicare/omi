# Ella Test Suite Design

A comprehensive testing strategy for the Ella voice AI flow.

## Overview

The Ella system has multiple components that need to work together. This test suite ensures each component works in isolation (unit tests) and together (integration tests).

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           ELLA VOICE FLOW                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌────────┐│
│  │  Wake   │───▶│  Voice  │───▶│   LLM   │───▶│   TTS   │───▶│  Push  ││
│  │  Word   │    │  Input  │    │  Agent  │    │ Service │    │ Notify ││
│  └─────────┘    └─────────┘    └─────────┘    └─────────┘    └────────┘│
│       │              │              │              │              │     │
│       ▼              ▼              ▼              ▼              ▼     │
│    [Test 1]      [Test 2]      [Test 3]      [Test 4]      [Test 5]   │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Test Categories

### 1. Unit Tests (Fast, No External Dependencies)

| Test | What it validates | Mock required |
|------|-------------------|---------------|
| `test_tts_notification_payload` | Notification data structure is correct | FCM send |
| `test_transcript_segment_schema` | Transcript segments validate properly | None |
| `test_memory_schema_validation` | Memory objects are schema-compliant | Firestore |
| `test_conversation_schema` | Conversation objects pass Pydantic | Firestore |

### 2. Integration Tests (External Services, Slow)

| Test | What it validates | Services used |
|------|-------------------|---------------|
| `test_fcm_token_lookup` | User FCM tokens can be retrieved | Firestore |
| `test_fcm_notification_delivery` | FCM accepts and delivers notification | FCM |
| `test_tts_audio_generation` | TTS service generates audio | TTS API |
| `test_tts_audio_playback` | iOS app plays audio from notification | iOS device |

### 3. End-to-End Tests (Full Flow)

| Test | What it validates |
|------|-------------------|
| `test_voice_to_notification` | Full flow from voice input to push notification |
| `test_wake_word_to_response` | Wake word triggers voice agent and response |
| `test_memory_extraction_flow` | Voice → LLM → Memory extraction → Firestore |

---

## Test Endpoints

Located in `routers/testing.py`. Enable with `TESTING_ENDPOINTS_ENABLED=true`.

```bash
# Health check
GET /v1/test/health

# Send TTS notification
POST /v1/test/tts-notification
{
  "uid": "firebase-uid",
  "preset": "bell",  # or audio_url
  "title": "Ella",
  "body": "Test message"
}

# Send plain notification
POST /v1/test/notification
{
  "uid": "firebase-uid",
  "title": "Test",
  "body": "Message"
}

# List user tokens
GET /v1/test/user-tokens?uid=firebase-uid
```

---

## Test Scripts

### Quick CLI Testing

```bash
# From backend directory
./scripts/test-push.sh health                    # Check endpoints enabled
./scripts/test-push.sh tokens YOUR_UID           # List FCM tokens
./scripts/test-push.sh tts YOUR_UID              # Send TTS with bell
./scripts/test-push.sh tts YOUR_UID chime        # Send TTS with chime
./scripts/test-push.sh notify YOUR_UID "Hi" "Test"  # Plain notification
```

### Full Python Test

```bash
# Requires full environment
export TEST_USER_ID="your-firebase-uid"
python scripts/test_tts_push_notification.py --preset bell
```

---

## Test Implementation Plan

### Phase 1: Notification Tests (Current)

- [x] `test_tts_push_notification.py` - Manual TTS test
- [x] Test endpoints in `routers/testing.py`
- [x] Shell script `scripts/test-push.sh`
- [ ] Pytest wrapper for automated runs

### Phase 2: Schema Validation Tests

```python
# tests/test_schemas.py
def test_transcript_segment_requires_is_user():
    """TranscriptSegment must have is_user field."""
    with pytest.raises(ValidationError):
        TranscriptSegment(text="hello", start=0.0, end=1.0)  # Missing is_user

def test_memory_schema_validation():
    """Memory must follow official schema."""
    memory = Memory(content="Test", category="interesting")
    assert memory.visibility == "private"  # Default
```

### Phase 3: Integration Tests

```python
# tests/integration/test_notifications.py
@pytest.mark.integration
def test_fcm_delivery(test_user_uid):
    """Verify FCM accepts and delivers notification."""
    from utils.notifications import send_notification
    # This requires a real FCM token
    send_notification(test_user_uid, "Test", "Integration test")
    # Manual verification on device required
```

### Phase 4: End-to-End Tests

```python
# tests/e2e/test_voice_flow.py
@pytest.mark.e2e
async def test_voice_to_memory():
    """Full flow: voice input → LLM → memory extraction."""
    # 1. Send test audio to transcription
    # 2. Verify transcription response
    # 3. Trigger memory extraction
    # 4. Verify memory in Firestore
```

---

## Environment Setup

### Required Environment Variables

```bash
# .env for testing
TESTING_ENDPOINTS_ENABLED=true
GOOGLE_APPLICATION_CREDENTIALS=google-credentials.json

# Optional: Override API URL
API_URL=http://localhost:8000
```

### Test User Setup

1. Get your Firebase UID from the app:
   - iOS: Settings → Developer → Debug Info → User ID
   - Or from Firestore Console

2. Set as environment variable:
   ```bash
   export TEST_USER_ID="your-firebase-uid"
   ```

3. Ensure app has been opened at least once (FCM token registered)

---

## Debugging Checklist

When a test fails, check:

### FCM/Push Issues
- [ ] User has FCM tokens? (`/v1/test/user-tokens?uid=...`)
- [ ] Token is valid? (not expired/uninstalled)
- [ ] APNS certificate configured? (iOS only)
- [ ] Notification permissions granted in app?

### Schema Issues
- [ ] All required fields present? (`is_user`, `start`, `end`)
- [ ] No old-format data? (`role` instead of `is_user`)
- [ ] Pydantic validation passing?

### Audio Issues
- [ ] Audio URL is HTTPS?
- [ ] Audio URL is publicly accessible?
- [ ] Audio format supported? (MP3, M4A, WAV)
- [ ] iOS audio session configured?

---

## CI/CD Integration (Future)

```yaml
# .github/workflows/test.yml
name: Test Suite

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Run unit tests
        run: pytest tests/unit -v

  integration-tests:
    runs-on: ubuntu-latest
    needs: unit-tests
    steps:
      - uses: actions/checkout@v3
      - name: Run integration tests
        env:
          GOOGLE_APPLICATION_CREDENTIALS: ${{ secrets.GCP_CREDENTIALS }}
        run: pytest tests/integration -v -m integration
```

---

## References

- [DATA_SCHEMA_REQUIREMENTS.md](./DATA_SCHEMA_REQUIREMENTS.md) - Schema rules
- [N8N_MIGRATION_GUIDE.md](./N8N_MIGRATION_GUIDE.md) - API endpoint mapping
- iOS TTS Handler: `app/ios/Runner/TtsPushNotificationHandler.swift`

---

**Last Updated**: January 2026
