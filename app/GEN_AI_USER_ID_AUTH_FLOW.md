# Gen AI Authentication & user_id Flow

**Date**: November 2, 2025
**Developer**: Claude-iOS-Developer
**Status**: ✅ Implemented - Ready for Backend Integration

---

## 📋 Summary

Added "Gen AI Test" feature to developer settings that enables proper user identification for backend AI routing.

---

## ❓ Original Question

**User asked**: "Does iOS already send user_id to backend for proper agent routing?"

**Answer**: **NO, but it sends something better** - a Firebase JWT token that contains the user_id.

---

## 🔐 How Authentication Works Currently

### **Production API Calls** (Memories, Conversations)

**iOS Side** (`lib/backend/http/shared.dart`):
```dart
// Line 44: getAuthHeader() returns Firebase JWT
return 'Bearer ${SharedPreferencesUtil().authToken}';

// Lines 50-66: buildHeaders() adds to all API requests
headers['Authorization'] = await getAuthHeader();
```

**What's sent**:
```http
GET /v3/memories HTTP/1.1
Host: api.ella-ai-care.com
Authorization: Bearer eyJhbGciOiJSUzI1NiIsImtpZCI6IjFlOWdkazcifQ...  (Firebase JWT)
X-App-Platform: ios
X-App-Version: 1.0.0
```

**Backend Must Do**:
1. Decode the JWT token (using Firebase Admin SDK)
2. Extract `user_id` from the token payload:
   ```python
   decoded_token = auth.verify_id_token(jwt_token)
   user_id = decoded_token['uid']
   ```

### **TTS Testing (Previous Implementation)**

**What was sent**:
```http
POST /v1/tts/generate HTTP/1.1
Authorization: Bearer test-admin-key-local-dev-only  (ADMIN_KEY bypass)
```

**Problem**: ADMIN_KEY has no user_id → Backend can't route to user-specific agents!

---

## ✅ New Implementation: Gen AI Test Checkbox

### **Changes Made**

#### **1. Developer Settings UI** (`lib/pages/settings/developer.dart`)

Added new checkbox after "Force Generate":

```dart
CheckboxListTile(
  title: const Text('Gen AI Test (Advanced)'),
  subtitle: Text(
    'Enable AI-powered responses. Backend routes to OpenAI/Claude/Letta based on your user_id.',
  ),
  value: _genAiEnabled,
  onChanged: (value) {
    setState(() => _genAiEnabled = value ?? false);
  },
)
```

#### **2. TTS Service** (`lib/services/audio/ella_tts_service.dart`)

Updated `speakFromBackend()` method:

```dart
Future<void> speakFromBackend(
  String text, {
  String voice = 'nova',
  bool forceGenerate = false,
  bool useRealAuth = false,      // NEW: Use Firebase JWT
  bool genAiEnabled = false,     // NEW: Enable AI routing
}) async {
  // Determine auth header
  String authHeader;
  if (useRealAuth) {
    // Use real Firebase JWT (so backend can extract user_id)
    authHeader = 'Bearer ${SharedPreferencesUtil().authToken}';
  } else {
    // Use ADMIN_KEY for testing
    authHeader = 'Bearer test-admin-key-local-dev-only';
  }

  // Call backend with gen_ai_enabled flag
  final response = await http.post(
    Uri.parse('$baseUrl/v1/tts/generate'),
    headers: {
      'Authorization': authHeader,
      'Content-Type': 'application/json',
    },
    body: jsonEncode({
      'text': text,
      'voice': voice,
      'model': 'hd',
      'cache_key': forceGenerate ? null : text.hashCode.toString(),
      'gen_ai_enabled': genAiEnabled,  // Tell backend to use AI routing
    }),
  );
}
```

---

## 🎯 How It Works Now

### **When "Gen AI Test" is UNCHECKED** (Default)
```
iOS sends:
  Authorization: Bearer test-admin-key-local-dev-only
  Body: { "gen_ai_enabled": false, ... }

Backend behavior:
  - Recognizes ADMIN_KEY bypass
  - No user_id available
  - Uses default TTS (simple OpenAI TTS, no AI routing)
```

### **When "Gen AI Test" is CHECKED**
```
iOS sends:
  Authorization: Bearer eyJhbGciOiJSUzI1NiIsImtpZCI...  (Firebase JWT)
  Body: { "gen_ai_enabled": true, ... }

Backend behavior:
  1. Decodes JWT → extracts user_id
  2. Sees gen_ai_enabled = true
  3. Routes to AI based on user_id:
     - Option A: OpenAI completion (fast, simple)
     - Option B: Claude API (high quality)
     - Option C: Letta agent (stateful, memory)
  4. Generates TTS from AI response
  5. Returns audio URL
```

---

## 🔧 Backend Integration Required

### **Endpoint**: `POST /v1/tts/generate`

**Current Implementation** (before Gen AI):
```python
@router.post("/v1/tts/generate")
async def generate_tts(
    request: TTSRequest,
    uid: str = Depends(get_user_id),  # Extracted from JWT
):
    # Generate TTS from text
    audio_url = await tts_provider.generate(request.text, request.voice)
    return {"audio_url": audio_url}
```

**NEW Implementation** (with Gen AI):
```python
@router.post("/v1/tts/generate")
async def generate_tts(
    request: TTSRequest,
    uid: str = Depends(get_user_id),  # Extracted from JWT
):
    text = request.text

    # NEW: Check if Gen AI is enabled
    if request.gen_ai_enabled:
        # Route to AI based on user preferences
        user_prefs = await get_user_ai_preferences(uid)

        if user_prefs.ai_provider == "letta":
            # Use Letta agent with memory
            agent_response = await letta_client.send_message(
                agent_id=user_prefs.letta_agent_id,
                message=text,
            )
            text = agent_response.content

        elif user_prefs.ai_provider == "claude":
            # Use Claude API
            response = await anthropic.messages.create(
                model="claude-3-5-sonnet-20241022",
                messages=[{"role": "user", "content": text}],
            )
            text = response.content[0].text

        else:  # Default to OpenAI
            response = await openai.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": text}],
            )
            text = response.choices[0].message.content

    # Generate TTS from text (original or AI-generated)
    audio_url = await tts_provider.generate(text, request.voice)
    return {"audio_url": audio_url, "ai_processed": request.gen_ai_enabled}
```

### **User AI Preferences** (Backend Needs to Implement)

**Endpoint**: `GET /v1/users/ai-preferences`

```python
{
  "user_id": "abc123",
  "ai_provider": "letta",  # or "openai", "claude"
  "letta_agent_id": "agent-xyz",  # if using Letta
  "openai_model": "gpt-4",
  "claude_model": "claude-3-5-sonnet-20241022"
}
```

**Endpoint**: `PATCH /v1/users/ai-preferences`

Allow users to configure which AI they want to use.

---

## 🧪 Testing Scenarios

### **Test 1: Basic TTS (No Gen AI)**
```
1. Open app → Settings → Developer Settings
2. Enter test text: "Hello, this is a test."
3. Ensure "Gen AI Test" is UNCHECKED
4. Click "Test Cloud TTS"

Expected:
  - Uses ADMIN_KEY auth
  - Backend returns direct TTS (no AI processing)
  - Audio plays immediately
```

### **Test 2: Gen AI with Firebase Auth**
```
1. Ensure user is logged in (Firebase authenticated)
2. Open app → Settings → Developer Settings
3. Enter test text: "Tell me about my health today."
4. CHECK "Gen AI Test" checkbox
5. Click "Test Cloud TTS"

Expected:
  - Uses Firebase JWT auth (user_id extracted)
  - Backend routes to AI (OpenAI/Claude/Letta)
  - AI generates response based on user context
  - TTS audio of AI response plays
```

### **Test 3: User-Specific AI Routing**
```
Scenario: User A prefers Letta, User B prefers Claude

User A:
  - user_id: "user_a_123"
  - Preferences: { "ai_provider": "letta", "letta_agent_id": "agent-A" }
  - Input: "What's my medication schedule?"
  - Expected: Letta agent responds with user's actual schedule

User B:
  - user_id: "user_b_456"
  - Preferences: { "ai_provider": "claude" }
  - Input: "What's my medication schedule?"
  - Expected: Claude responds (may be generic if no memory)
```

---

## 📊 Authentication Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         iOS APP                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  User Taps "Test Cloud TTS" with "Gen AI Test" Checked         │
│                           ↓                                      │
│  speakFromBackend(text, useRealAuth: true, genAiEnabled: true) │
│                           ↓                                      │
│  authHeader = SharedPreferencesUtil().authToken                 │
│  (Firebase JWT: "eyJhbGciOiJSUzI1NiIsImtpZCI6...")             │
│                           ↓                                      │
│  POST /v1/tts/generate                                          │
│  Headers:                                                        │
│    Authorization: Bearer eyJhbGciOiJSUzI1NiIsImtpZCI...         │
│  Body:                                                           │
│    { "text": "...", "gen_ai_enabled": true, ... }              │
│                                                                  │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ↓ HTTPS Request

┌─────────────────────────────────────────────────────────────────┐
│                      BACKEND API                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Middleware: Decode JWT Token                                   │
│  ↓                                                               │
│  decoded_token = auth.verify_id_token(jwt_token)                │
│  user_id = decoded_token['uid']  → "user_a_123"                 │
│                                                                  │
│  ↓                                                               │
│  Endpoint: /v1/tts/generate                                     │
│  ↓                                                               │
│  if request.gen_ai_enabled:                                     │
│    ↓                                                             │
│    user_prefs = get_user_ai_preferences(user_id)                │
│    → { "ai_provider": "letta", "letta_agent_id": "agent-A" }   │
│    ↓                                                             │
│    Route to Letta Agent "agent-A"                               │
│    ↓                                                             │
│    AI Response: "You have medication due at 2pm today."         │
│  ↓                                                               │
│  Generate TTS from AI response                                  │
│  ↓                                                               │
│  Return: { "audio_url": "https://...", "ai_processed": true }  │
│                                                                  │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ↓ JSON Response

┌─────────────────────────────────────────────────────────────────┐
│                         iOS APP                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Receives audio_url                                             │
│  ↓                                                               │
│  Downloads audio file                                           │
│  ↓                                                               │
│  Plays audio: "You have medication due at 2pm today."          │
│  ↓                                                               │
│  User hears AI-powered TTS response                             │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## ✅ Summary

### **iOS Changes** (Completed)
- ✅ Added "Gen AI Test" checkbox to developer settings
- ✅ Updated `speakFromBackend()` to support real Firebase auth
- ✅ Pass `gen_ai_enabled` flag to backend
- ✅ Logs show which auth method is used

### **Backend Changes** (Required)
- ⏳ Update `/v1/tts/generate` endpoint to handle `gen_ai_enabled` flag
- ⏳ Implement AI routing logic (OpenAI, Claude, Letta)
- ⏳ Create `/v1/users/ai-preferences` endpoint
- ⏳ Allow users to configure AI provider preference

### **Benefits**
- ✅ Simple checkbox for advanced testing
- ✅ Backend gets real user_id from JWT
- ✅ Backend can route to user-specific agents
- ✅ No complex iOS-side configuration needed
- ✅ All routing logic handled by backend
- ✅ Easy to add more AI providers later

---

**Files Modified**:
- `lib/pages/settings/developer.dart` - Added Gen AI checkbox
- `lib/services/audio/ella_tts_service.dart` - Updated auth flow

**Ready for**: Backend integration and testing
**Next Steps**: Backend team implements AI routing logic
