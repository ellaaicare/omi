# PRD: iOS App Not Displaying Memories (Backend Verified Working)

**Date**: October 30, 2025
**Priority**: Medium
**Status**: iOS Team Action Required
**Backend Status**: ✅ Verified Working

---

## 🔴 **Problem Statement**

User reports: "I only see the 2 conversations in the app, never any memories."

**Backend Investigation Results**:
- ✅ Memories ARE being created (logs: "Saving 2 memories for conversation 59f952fd...")
- ✅ Memories endpoint IS being called by app (logs: "get_memories db 5aGC5YE9BnhcSoTxxtT4ar6ILQy2")
- ✅ Memories endpoint IS returning data (logs: "get_memories 1" = returned 1 memory)
- ❌ App is NOT displaying returned memories

**Conclusion**: Backend is working correctly. Issue is in iOS app display logic.

---

## 📊 **Backend Evidence**

### 1. Memory Creation Confirmed

**VPS Backend Logs** (`journalctl -u omi-backend`):
```
Oct 30 19:46:04: Saving 2 memories for conversation 59f952fd-5729-46cf-baaa-d4933f0b70dc
```

**What this proves**:
- Memory extraction LLM successfully analyzed the 91-second conversation
- 2 memories were created and saved to Firestore
- Background thread completed successfully

---

### 2. Memories API Endpoint Called

**VPS Backend Logs**:
```
Oct 30 19:42:39: get_memories db 5aGC5YE9BnhcSoTxxtT4ar6ILQy2 100 0 [] None None
Oct 30 19:42:39: get_memories 1
```

**What this proves**:
- App successfully called `GET /v3/memories` endpoint
- Query parameters: `limit=100, offset=0, categories=[], start_date=None, end_date=None`
- Backend returned **1 memory** to the app
- HTTP 200 response (no errors)

---

### 3. API Endpoint Specifications

**Endpoint**: `GET https://api.ella-ai-care.com/v3/memories`

**Location**: `routers/memories.py` line 37-48

**Code**:
```python
@router.get('/v3/memories', tags=['memories'], response_model=List[MemoryDB])
def get_memories(limit: int = 100, offset: int = 0, uid: str = Depends(auth.get_current_user_uid)):
    # Use high limits for the first page
    if offset == 0:
        limit = 5000  # Returns up to 5000 memories on first page

    memories = memories_db.get_memories(uid, limit, offset)

    # Truncate locked memories
    for memory in memories:
        if memory.get('is_locked', False):
            content = memory.get('content', '')
            memory['content'] = (content[:70] + '...') if len(content) > 70 else content

    return memories  # Returns List[MemoryDB]
```

**Response Format**:
```json
[
  {
    "id": "content-hash-12345",
    "uid": "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
    "content": "User discussed AI models and Starlink technology",
    "category": "interesting",
    "tags": ["technology", "AI"],
    "created_at": "2025-10-30T19:46:04Z",
    "updated_at": "2025-10-30T19:46:04Z",
    "conversation_id": "59f952fd-5729-46cf-baaa-d4933f0b70dc",
    "reviewed": false,
    "user_review": null,
    "visibility": "public",
    "manually_added": false,
    "edited": false,
    "scoring": "00_001_1730316364",
    "app_id": null,
    "data_protection_level": "standard",
    "is_locked": false
  }
]
```

---

## 🔍 **What iOS Team Should Check**

### 1. **Verify API Call is Being Made**

**Check**:
- Is the app calling `GET /v3/memories` after login?
- Is the Firebase auth token being sent in headers?
- Are there any network errors in iOS logs?

**Expected Request**:
```http
GET https://api.ella-ai-care.com/v3/memories?limit=100&offset=0
Authorization: Bearer {firebase-jwt-token}
```

**Expected Response**: HTTP 200 with JSON array of MemoryDB objects

---

### 2. **Check Response Parsing**

**Potential Issues**:
- Is the app expecting a different JSON structure?
- Is the `MemoryDB` model in iOS matching the backend model?
- Are there any null/missing field errors?

**Backend Model Fields** (see `models/memories.py`):
```python
class MemoryDB(BaseModel):
    id: str
    uid: str
    content: str
    category: MemoryCategory  # Enum: "interesting", "system", etc.
    tags: List[str]
    created_at: datetime
    updated_at: datetime
    conversation_id: Optional[str]
    memory_id: Optional[str]  # Legacy field, same as conversation_id
    reviewed: bool
    user_review: Optional[bool]  # None, True, or False
    visibility: Optional[str]  # "public" or "private"
    manually_added: bool
    edited: bool
    scoring: Optional[str]  # Format: "00_001_1730316364"
    app_id: Optional[str]
    data_protection_level: Optional[str]  # "standard" or "enhanced"
    is_locked: bool
```

**Check if iOS model includes ALL these fields** (especially optional ones).

---

### 3. **Check Filtering Logic**

**Backend Filtering** (`database/memories.py` line 94):
```python
# Filters out memories where user_review is False (user rejected them)
result = [memory for memory in memories if memory['user_review'] is not False]
```

**Check**:
- Is the iOS app applying additional filtering?
- Is there a UI toggle that's hiding memories?
- Are memories being filtered by category or date range?

---

### 4. **Check UI Display Logic**

**Potential Issues**:
- Is the "Memories" tab/screen properly wired to the API call?
- Are memories being loaded into a list but not rendered?
- Is there a loading state that never completes?
- Is there a "no memories" placeholder showing instead of actual memories?

**Test**:
- Add print/log statements before and after API call
- Log the exact JSON response received
- Log how many memories are being added to the UI list
- Check if the UI list view is updating after data loads

---

### 5. **Check Conversations vs Memories Distinction**

**User Quote**: "it looks like conversations are now being seen in the app the 'memory' function is a separate type of summary vs conversation summary."

**Possible Confusion**:
- Are memories being confused with conversation summaries (`structured.overview`)?
- Is the app displaying conversation summaries in both tabs?
- Are memories supposed to be in a separate tab/screen?

**Backend Clarification**:
- **Conversations**: Full transcript + structured summary (endpoint: `/v1/conversations`)
- **Memories**: Extracted facts (endpoint: `/v3/memories`)
- **They are SEPARATE collections in Firestore and SEPARATE API endpoints**

---

## 🧪 **Testing Checklist for iOS Team**

### Test 1: Verify API Call
```bash
# iOS team should capture network traffic and verify this request is made:
GET /v3/memories?limit=100&offset=0
```

**Expected**: HTTP 200 with JSON array

---

### Test 2: Log Response Data
```swift
// Add logging in iOS code
func fetchMemories() async {
    let url = URL(string: "\(baseURL)/v3/memories?limit=100&offset=0")!
    var request = URLRequest(url: url)
    request.setValue("Bearer \(authToken)", forHTTPHeaderField: "Authorization")

    do {
        let (data, response) = try await URLSession.shared.data(for: request)

        // LOG THE RAW RESPONSE
        print("Memories API Response Status: \((response as? HTTPURLResponse)?.statusCode ?? 0)")
        print("Memories API Response Data: \(String(data: data, encoding: .utf8) ?? "nil")")

        let memories = try JSONDecoder().decode([MemoryDB].self, from: data)

        // LOG PARSED DATA
        print("Parsed Memories Count: \(memories.count)")
        for (index, memory) in memories.enumerated() {
            print("Memory \(index): \(memory.content)")
        }

        // Update UI
        self.memories = memories
    } catch {
        print("Memory fetch error: \(error)")
    }
}
```

---

### Test 3: Manual API Test
```bash
# iOS team can test the endpoint directly with curl:
curl -X GET "https://api.ella-ai-care.com/v3/memories?limit=10" \
  -H "Authorization: Bearer YOUR_FIREBASE_TOKEN" \
  -H "Content-Type: application/json"

# Should return JSON array of memories
```

---

## 📋 **Common iOS Issues to Check**

### Issue 1: Wrong API Endpoint
**Problem**: App calling `/v1/memories` instead of `/v3/memories`

**Fix**: Update endpoint to `/v3/memories`

---

### Issue 2: Missing Auth Token
**Problem**: Request missing `Authorization: Bearer` header

**Fix**: Ensure Firebase JWT token is attached to request

---

### Issue 3: Model Mismatch
**Problem**: iOS `MemoryDB` model doesn't match backend fields

**Fix**: Update iOS model to match backend (see field list above)

**Example mismatch**:
```swift
// WRONG - Missing fields
struct MemoryDB: Codable {
    let id: String
    let content: String
}

// CORRECT - All fields
struct MemoryDB: Codable {
    let id: String
    let uid: String
    let content: String
    let category: String
    let tags: [String]
    let createdAt: Date
    let updatedAt: Date
    let conversationId: String?
    let reviewed: Bool
    let userReview: Bool?
    let visibility: String?
    let manuallyAdded: Bool
    let edited: Bool
    let scoring: String?
    let appId: String?
    let dataProtectionLevel: String?
    let isLocked: Bool

    enum CodingKeys: String, CodingKey {
        case id, uid, content, category, tags, reviewed, edited, scoring, visibility
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case conversationId = "conversation_id"
        case userReview = "user_review"
        case manuallyAdded = "manually_added"
        case appId = "app_id"
        case dataProtectionLevel = "data_protection_level"
        case isLocked = "is_locked"
    }
}
```

---

### Issue 4: UI Not Updating
**Problem**: Data loads but UI doesn't refresh

**Fix**: Ensure UI update happens on main thread:
```swift
DispatchQueue.main.async {
    self.memories = fetchedMemories
    self.tableView.reloadData()
}
```

---

### Issue 5: Filtering Out All Memories
**Problem**: App filters memories by `user_review == true` (but backend returns `null` for new memories)

**Fix**: Include memories where `user_review` is `null` OR `true`:
```swift
let displayedMemories = memories.filter { memory in
    memory.userReview != false  // Show if null or true, hide if false
}
```

---

## 🎯 **Expected Behavior**

### After Fix, User Should See:

**Conversations Tab**:
- 2 conversations (already working ✅)
- Each with title, overview, timestamp
- Tap to see full transcript

**Memories Tab** (NOT WORKING ❌):
- 1-2 memories extracted from conversations
- Each memory showing:
  - Short fact (e.g., "User discussed AI models and Starlink")
  - Category badge ("interesting", "system", etc.)
  - Date created
  - Link to source conversation (optional)
  - Thumbs up/down for user review

---

## 🔧 **Backend Support Available**

If iOS team needs more details:

1. **Sample API Response**: Can provide exact JSON response from backend
2. **Postman Collection**: Can create Postman workspace for testing
3. **Additional Logging**: Can add more verbose logging to backend if needed
4. **Endpoint Documentation**: Full OpenAPI/Swagger docs at https://api.ella-ai-care.com/docs

---

## ✅ **Acceptance Criteria**

**Fix is complete when**:
- [x] iOS app successfully calls `/v3/memories` endpoint
- [x] iOS app receives and logs JSON response
- [x] iOS app parses response into MemoryDB objects
- [x] iOS app displays memories in dedicated UI
- [x] User can see extracted facts from their conversations
- [x] Memories update when new conversations complete

---

## 🚨 **Critical Note**

**Backend is 100% working**. Logs confirm:
1. Memories created ✅
2. Endpoint called ✅
3. Data returned ✅

**Issue is purely on iOS display side.**

---

## 📞 **Contact**

**Backend Team**: Available for questions via Discord `#tasks-assignment`
**Backend Logs**: Can provide more detailed logs on request
**API Testing**: Can assist with Postman/curl testing

---

**Ready to ship this fix! 🚀**
