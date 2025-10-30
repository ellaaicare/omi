# Product Requirements Document: Backend TTS Audio Generation Service

**Project**: Ella AI Care - Text-to-Speech Audio Generation
**Date**: October 30, 2025
**Status**: Ready for Implementation
**Priority**: High
**Owner**: Backend Team
**App Integration**: iOS Team (Claude Code)

---

## 1. Executive Summary

**Objective**: Implement a backend TTS (Text-to-Speech) service that generates high-quality audio for patient notifications, replacing low-quality native iOS voices with OpenAI's neural TTS.

**Why**:
- Current iOS native TTS sounds robotic and unprofessional for healthcare
- Backend generation enables caching, reducing costs by 90%+
- Centralized control allows voice provider changes without app updates
- Secure (no API keys in mobile apps)

**Expected Impact**:
- Better patient experience (ChatGPT-quality voices)
- Cost: ~$0.30/month (vs $112/month without caching)
- Latency: <500ms for cached messages, ~1-2s for new messages

---

## 2. Technical Requirements

### 2.1 Core Functionality

**Endpoint**: `POST /api/v1/tts/generate`

**Request**:
```json
{
  "text": "Hello, it's time to take your blood pressure medication.",
  "voice": "nova",
  "cache_key": "medication_reminder_bp",
  "model": "tts-1-hd"
}
```

**Response (Success)**:
```json
{
  "audio_url": "https://cdn.ella-ai-care.com/audio/abc123def456.mp3",
  "duration_ms": 2500,
  "cached": true,
  "voice": "nova",
  "size_bytes": 45678,
  "expires_at": "2025-11-30T00:00:00Z"
}
```

**Response (Error)**:
```json
{
  "error": "InvalidVoice",
  "message": "Voice 'invalid' not supported. Use: alloy, echo, fable, onyx, nova, shimmer",
  "supported_voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
}
```

### 2.2 OpenAI Integration

**API**: OpenAI Audio API
**Endpoint**: `https://api.openai.com/v1/audio/speech`
**Models**:
- `tts-1-hd`: High quality, ~1-2s latency (RECOMMENDED)
- `tts-1`: Standard quality, ~500ms latency (fallback)

**Voices** (for healthcare, recommend `nova` - warm, caring female voice):
- `alloy` - Neutral, balanced
- `echo` - Male, authoritative
- `fable` - British accent, warm
- `onyx` - Deep male, confident
- `nova` - **Female, warm, caring** (BEST for healthcare)
- `shimmer` - Soft female, friendly

**Python Implementation Example**:
```python
from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

response = client.audio.speech.create(
    model="tts-1-hd",
    voice="nova",
    input="Hello, it's time to take your medication.",
    response_format="mp3"  # or "opus", "aac", "flac"
)

audio_bytes = response.content
```

### 2.3 Audio Storage

**Storage Provider**: CloudFlare R2 (or AWS S3)
**CDN**: CloudFlare CDN for global distribution
**File Format**: MP3 (universal compatibility)
**Naming Convention**: `{hash(text + voice)}.mp3`
**Lifecycle**: 30-day expiration for non-cached, permanent for cached

**Example Storage Structure**:
```
ella-tts-audio/
  ├── cached/
  │   ├── medication_reminder_bp_nova.mp3
  │   ├── appointment_reminder_nova.mp3
  │   └── welcome_message_nova.mp3
  └── temp/
      ├── abc123def456.mp3 (expires in 30 days)
      └── xyz789ghi012.mp3 (expires in 30 days)
```

### 2.4 Caching Strategy

**Cache Layer**: Redis or in-memory cache
**Cache Key Format**: `tts:{voice}:{hash(text)}`
**Cache TTL**:
- Common messages (with `cache_key`): Permanent
- Ad-hoc messages: 30 days
- Cache size limit: 1GB (auto-evict LRU)

**Pre-cached Common Messages** (generate on deployment):
```python
COMMON_MESSAGES = [
    ("medication_reminder_bp", "It's time to take your blood pressure medication."),
    ("medication_reminder_general", "Time to take your medication."),
    ("appointment_reminder", "You have a doctor's appointment tomorrow."),
    ("daily_checkin", "Good morning! How are you feeling today?"),
    ("welcome", "Hello, this is Ella AI Care."),
    ("emergency_contact", "We noticed something concerning. Please contact your doctor."),
    # Add more as needed
]
```

**Cache Hit Rate Target**: >90% (most notifications use common templates)

### 2.5 Database Schema

**Table**: `tts_audio_cache`

```sql
CREATE TABLE tts_audio_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cache_key VARCHAR(255) UNIQUE,
    text TEXT NOT NULL,
    voice VARCHAR(50) NOT NULL,
    model VARCHAR(50) DEFAULT 'tts-1-hd',
    audio_url TEXT NOT NULL,
    cdn_url TEXT NOT NULL,
    duration_ms INTEGER,
    size_bytes INTEGER,
    created_at TIMESTAMP DEFAULT NOW(),
    last_accessed_at TIMESTAMP DEFAULT NOW(),
    access_count INTEGER DEFAULT 0,
    expires_at TIMESTAMP,
    INDEX idx_cache_key (cache_key),
    INDEX idx_voice (voice),
    INDEX idx_created_at (created_at)
);
```

---

## 3. API Specifications

### 3.1 Generate TTS Audio

**Endpoint**: `POST /api/v1/tts/generate`
**Authentication**: Bearer token (same as existing API)
**Rate Limit**: 100 requests/minute per user

**Request Body**:
| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `text` | string | Yes | - | Text to convert (max 4096 chars) |
| `voice` | string | No | `"nova"` | Voice ID (see supported voices) |
| `cache_key` | string | No | `null` | Optional cache identifier for reuse |
| `model` | string | No | `"tts-1-hd"` | OpenAI model (`tts-1` or `tts-1-hd`) |

**Response Fields**:
| Field | Type | Description |
|-------|------|-------------|
| `audio_url` | string | CDN URL to audio file (HTTPS) |
| `duration_ms` | integer | Audio duration in milliseconds |
| `cached` | boolean | Whether audio was served from cache |
| `voice` | string | Voice used for generation |
| `size_bytes` | integer | File size in bytes |
| `expires_at` | string (ISO 8601) | When audio URL will expire |

**Error Codes**:
| Code | Status | Description |
|------|--------|-------------|
| `TextTooLong` | 400 | Text exceeds 4096 characters |
| `InvalidVoice` | 400 | Voice not in supported list |
| `InvalidModel` | 400 | Model not `tts-1` or `tts-1-hd` |
| `OpenAIError` | 502 | OpenAI API failure |
| `StorageError` | 500 | Failed to upload audio |
| `RateLimited` | 429 | Too many requests |

### 3.2 Get Cached Audio (Optional Optimization)

**Endpoint**: `GET /api/v1/tts/cached/{cache_key}`
**Purpose**: Check if audio exists before generating

**Response**:
```json
{
  "exists": true,
  "audio_url": "https://cdn.ella-ai-care.com/audio/cached/medication_reminder_bp_nova.mp3",
  "duration_ms": 2500
}
```

### 3.3 List Supported Voices (Optional)

**Endpoint**: `GET /api/v1/tts/voices`

**Response**:
```json
{
  "voices": [
    {
      "id": "nova",
      "name": "Nova",
      "description": "Warm, caring female voice - Best for healthcare",
      "gender": "female",
      "recommended_for": ["healthcare", "elderly_care", "general"]
    },
    {
      "id": "onyx",
      "name": "Onyx",
      "description": "Deep, confident male voice",
      "gender": "male",
      "recommended_for": ["professional", "announcements"]
    }
    // ... other voices
  ],
  "default": "nova"
}
```

---

## 4. Implementation Phases

### Phase 1: Core TTS Generation (Week 1)
**Goal**: Basic TTS endpoint working

**Tasks**:
- [ ] Set up OpenAI API client with credentials
- [ ] Implement `POST /api/v1/tts/generate` endpoint
- [ ] Upload audio to R2/S3
- [ ] Return audio URL in response
- [ ] Add basic error handling
- [ ] Write unit tests

**Deliverable**: Working endpoint that generates audio (no caching yet)

**Testing**:
```bash
curl -X POST https://api.ella-ai-care.com/api/v1/tts/generate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hello, this is a test.",
    "voice": "nova"
  }'
```

### Phase 2: Caching Layer (Week 2)
**Goal**: Implement Redis caching for cost optimization

**Tasks**:
- [ ] Set up Redis cache
- [ ] Create `tts_audio_cache` database table
- [ ] Implement cache lookup before generation
- [ ] Pre-generate common messages
- [ ] Add cache statistics endpoint
- [ ] Monitor cache hit rate

**Deliverable**: 90%+ cache hit rate on production traffic

### Phase 3: Production Optimization (Week 3)
**Goal**: Polish for production deployment

**Tasks**:
- [ ] Add rate limiting
- [ ] Implement retry logic for OpenAI failures
- [ ] Set up monitoring/alerting
- [ ] Add CDN configuration
- [ ] Optimize audio file sizes
- [ ] Add analytics tracking
- [ ] Write integration tests

**Deliverable**: Production-ready service with monitoring

### Phase 4: Advanced Features (Future)
**Goal**: Enhanced capabilities

**Optional Tasks**:
- [ ] Support for SSML (Speech Synthesis Markup Language)
- [ ] Voice cloning for brand voice
- [ ] Real-time streaming for long text
- [ ] Multi-language support
- [ ] A/B testing different voices
- [ ] Emotion/tone parameters

---

## 5. Cost Analysis

### 5.1 OpenAI Pricing
- **Model**: `tts-1-hd` (high quality)
- **Cost**: $15.00 per 1 million characters
- **Per Character**: $0.000015

### 5.2 Expected Usage
**Assumptions**:
- 1,000 active patients
- 5 notifications per day per patient = 5,000 notifications/day
- Average message length: 50 characters
- Cache hit rate: 90% (after Phase 2)

**Monthly Calculations**:
```
Without Caching:
5,000 notifications/day × 30 days × 50 chars × $0.000015/char
= 7,500,000 chars/month × $0.000015
= $112.50/month

With 90% Cache Hit Rate:
7,500,000 chars × 10% × $0.000015
= $11.25/month (OpenAI costs)
+ ~$5/month (R2 storage)
= ~$16/month total

With Pre-generated Common Messages:
Initial generation: ~10 messages × 50 chars × $0.000015 = $0.01
Monthly new messages: ~100 messages × 50 chars × $0.000015 = $0.08
= ~$0.30/month (OpenAI costs)
+ ~$2/month (R2 storage)
= ~$2.50/month total 🎉
```

### 5.3 Storage Costs
**CloudFlare R2**: $0.015 per GB/month (first 10GB free)
**Estimated Storage**: ~2-3 GB for 1000 cached messages = **FREE**

---

## 6. Monitoring & Analytics

### 6.1 Metrics to Track

**Performance**:
- API response time (p50, p95, p99)
- OpenAI API latency
- Cache hit rate
- Audio generation success rate

**Business**:
- Total audio generations per day
- Cost per generation (with/without cache)
- Most frequently generated messages
- Voice usage distribution

**Errors**:
- OpenAI API errors (rate limits, timeouts)
- Storage upload failures
- Cache misses

### 6.2 Alerts

**Critical**:
- OpenAI API failure rate > 5%
- Cache hit rate < 80%
- Average latency > 3 seconds

**Warning**:
- Daily cost > $10
- Storage usage > 8GB (approaching limit)
- Cache hit rate < 90%

---

## 7. Security & Privacy

### 7.1 Data Handling
- **No PII in cache keys**: Use generic templates only
- **Patient-specific data**: Generate on-demand, don't cache
- **Audio retention**: Max 30 days for non-cached messages
- **Secure storage**: HTTPS-only audio URLs

### 7.2 API Security
- **Authentication**: Require valid bearer token
- **Rate limiting**: Prevent abuse
- **Input validation**: Sanitize text input
- **CORS**: Restrict to app domains

---

## 8. Testing Requirements

### 8.1 Unit Tests
```python
def test_generate_tts_success():
    """Test successful TTS generation"""
    response = client.post("/api/v1/tts/generate", json={
        "text": "Hello world",
        "voice": "nova"
    })
    assert response.status_code == 200
    assert "audio_url" in response.json()

def test_generate_tts_cached():
    """Test cache hit"""
    # Generate first time
    response1 = client.post("/api/v1/tts/generate", json={
        "text": "Test message",
        "cache_key": "test_cache"
    })

    # Generate second time (should be cached)
    response2 = client.post("/api/v1/tts/generate", json={
        "text": "Test message",
        "cache_key": "test_cache"
    })

    assert response2.json()["cached"] == True
    assert response1.json()["audio_url"] == response2.json()["audio_url"]

def test_invalid_voice():
    """Test invalid voice rejection"""
    response = client.post("/api/v1/tts/generate", json={
        "text": "Hello",
        "voice": "invalid_voice"
    })
    assert response.status_code == 400
```

### 8.2 Integration Tests
- Test OpenAI API connectivity
- Test R2/S3 upload and CDN access
- Test end-to-end flow: request → generate → upload → return URL
- Test audio playback on iOS device

---

## 9. Documentation

### 9.1 API Documentation
- Add OpenAPI/Swagger specs
- Include code examples (Python, JavaScript, cURL)
- Document all error codes

### 9.2 Internal Documentation
- Architecture diagrams
- Deployment procedures
- Troubleshooting guide
- Cost monitoring guide

---

## 10. Success Criteria

**Phase 1 Complete When**:
- ✅ Basic TTS endpoint returns valid audio URLs
- ✅ Audio plays successfully on iOS app
- ✅ Unit tests passing (>80% coverage)

**Phase 2 Complete When**:
- ✅ Cache hit rate > 90%
- ✅ Response time < 500ms for cached messages
- ✅ Common messages pre-generated

**Production Ready When**:
- ✅ All phases complete
- ✅ Monitoring and alerts configured
- ✅ Load tested (1000 req/min)
- ✅ Documentation complete
- ✅ Monthly cost < $5

---

## 11. App Integration (iOS Team Reference)

**iOS Team will**:
1. Call `POST /api/v1/tts/generate` when notification received
2. Download audio file from returned URL
3. Play audio via AVAudioPlayer with Bluetooth routing
4. Fallback to native iOS TTS if backend unavailable

**App Changes Needed** (handled by iOS team):
```dart
// lib/services/audio/ella_tts_service.dart
Future<void> speakFromBackend(String text) async {
  try {
    // Call backend TTS endpoint
    final response = await http.post(
      Uri.parse('$baseUrl/api/v1/tts/generate'),
      headers: {'Authorization': 'Bearer $token'},
      body: json.encode({'text': text, 'voice': 'nova'}),
    );

    if (response.statusCode == 200) {
      final data = json.decode(response.body);
      await _audioPlayer.play(UrlSource(data['audio_url']));
    } else {
      // Fallback to native iOS TTS
      await _speakNative(text);
    }
  } catch (e) {
    // Fallback to native iOS TTS
    await _speakNative(text);
  }
}
```

---

## 12. Appendix

### 12.1 OpenAI API Reference
**Documentation**: https://platform.openai.com/docs/guides/text-to-speech
**Rate Limits**: 50 requests/minute (can request increase)
**Character Limits**: 4096 characters per request

### 12.2 Voice Samples
Test different voices at: https://platform.openai.com/docs/guides/text-to-speech/voice-options

### 12.3 Alternative Providers (Future Consideration)
- **Cartesia**: Lower latency (300ms), $0.05/1K requests
- **ElevenLabs**: Better quality, $0.30/1K chars (expensive)
- **Azure Neural TTS**: $15/1M chars (same as OpenAI)

---

## Questions for Backend Team?

Contact: iOS Team (Claude Code) via Discord `#tasks-assignment`

**Ready to implement? Let's ship this! 🚀**
