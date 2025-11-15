# Intent Scanner - Backend Integration Guide

**For**: Backend Developer (Claude-Backend-Developer)
**Date**: November 11, 2025
**Priority**: Medium (Feature is in testing, not production yet)

---

## 🎯 What Changed

The iOS app now sends **intent classification metadata** with every transcript segment. This enables the backend to:

1. **Route messages intelligently** (local vs Letta vs emergency)
2. **Flag emergencies** for immediate alerts
3. **Track intent analytics** over time
4. **Implement smart filtering** (skip chitchat, prioritize medical)

---

## 📦 New JSON Fields in Transcript Messages

### WebSocket Message Format

When the iOS app sends transcript segments via WebSocket (`wss://api.ella-ai-care.com/v4/listen`), it now includes these **4 new fields**:

```json
{
  "type": "transcript_segment",
  "text": "I think I'm feeling dizzy",
  "speaker": "SPEAKER_00",
  "start": 0.0,
  "end": 1699876543.21,
  "is_final": true,
  "confidence": 0.95,
  "asr_provider": "apple_speech",

  // NEW INTENT FIELDS (added November 11, 2025)
  "intent": "medical",              // string: "wake" | "emergency" | "medical" | "chitchat" | "unknown"
  "intent_confidence": 0.75,        // float: 0.0 - 1.0
  "intent_route": "letta",          // string: "local" | "letta" | "emergency"
  "is_emergency": false             // boolean: true if critical health emergency
}
```

---

## 🔍 Intent Types

### 1. `wake` - Wake Word Detection
```json
{
  "text": "Hey Ella",
  "intent": "wake",
  "intent_confidence": 0.95,
  "intent_route": "local",
  "is_emergency": false
}
```
**Backend Action**: Activate voice assistant, send acknowledgment

---

### 2. `emergency` - Critical Health Emergency ⚠️
```json
{
  "text": "I have chest pain and can't breathe",
  "intent": "emergency",
  "intent_confidence": 0.90,
  "intent_route": "emergency",
  "is_emergency": true
}
```
**Backend Action**:
- **HIGH PRIORITY**: Trigger emergency alerts
- Notify emergency contacts
- Log as critical event
- Consider 911 suggestion

---

### 3. `medical` - Health Queries
```json
{
  "text": "I feel dizzy and nauseous",
  "intent": "medical",
  "intent_confidence": 0.75,
  "intent_route": "letta",
  "is_emergency": false
}
```
**Backend Action**: Escalate to Letta AI agent for processing

---

### 4. `chitchat` - Casual Conversation
```json
{
  "text": "Hello, how are you today?",
  "intent": "chitchat",
  "intent_confidence": 0.80,
  "intent_route": "local",
  "is_emergency": false
}
```
**Backend Action**: Simple local response or skip processing

---

### 5. `unknown` - Unclear Intent
```json
{
  "text": "Random unclear text",
  "intent": "unknown",
  "intent_confidence": 0.50,
  "intent_route": "letta",
  "is_emergency": false
}
```
**Backend Action**: Default to Letta for processing

---

## 🛠️ Backend Implementation Recommendations

### 1. WebSocket Message Handling

**Update**: `/v4/listen` WebSocket handler

```python
# Example (FastAPI WebSocket handler)
async def handle_transcript_segment(data: dict):
    # Existing fields
    text = data.get('text')
    speaker = data.get('speaker')
    is_final = data.get('is_final')

    # NEW: Intent classification fields
    intent = data.get('intent')  # Optional (may be None if IntentScanner disabled)
    intent_confidence = data.get('intent_confidence')
    intent_route = data.get('intent_route')
    is_emergency = data.get('is_emergency', False)

    # Route based on intent
    if is_emergency:
        await handle_emergency(text, speaker)
    elif intent_route == 'local':
        await handle_local_response(text, intent)
    elif intent_route == 'letta':
        await escalate_to_letta(text, speaker)

    # Store intent metadata for analytics
    await store_intent_analytics(intent, intent_confidence)
```

---

### 2. Database Schema Updates

**Add to `transcript_segments` table**:

```sql
ALTER TABLE transcript_segments ADD COLUMN intent VARCHAR(20);
ALTER TABLE transcript_segments ADD COLUMN intent_confidence FLOAT;
ALTER TABLE transcript_segments ADD COLUMN intent_route VARCHAR(20);
ALTER TABLE transcript_segments ADD COLUMN is_emergency BOOLEAN DEFAULT false;

-- Index for emergency queries
CREATE INDEX idx_is_emergency ON transcript_segments(is_emergency);

-- Index for intent analytics
CREATE INDEX idx_intent ON transcript_segments(intent);
```

---

### 3. Emergency Alert Handler

```python
async def handle_emergency(text: str, speaker: str):
    """Handle emergency intents with high priority"""
    logger.critical(f"⚠️ EMERGENCY DETECTED: {text}")

    # 1. Log as critical event
    await log_emergency_event(text, speaker)

    # 2. Notify emergency contacts
    await notify_emergency_contacts(speaker)

    # 3. Send push notification to user
    await send_push_notification(
        title="Emergency Detected",
        body="We detected a potential emergency. Are you okay?",
        priority="high"
    )

    # 4. Escalate to Letta with emergency flag
    await escalate_to_letta(text, speaker, is_emergency=True)
```

---

### 4. Intent Analytics

**Track intent distribution over time**:

```python
async def store_intent_analytics(intent: str, confidence: float):
    """Store intent classification for analytics"""
    await db.execute(
        """
        INSERT INTO intent_analytics (intent, confidence, timestamp)
        VALUES ($1, $2, NOW())
        """,
        intent, confidence
    )

# Query analytics
async def get_intent_distribution(user_id: str, days: int = 7):
    """Get intent type distribution for a user"""
    return await db.fetch(
        """
        SELECT intent, COUNT(*) as count, AVG(intent_confidence) as avg_confidence
        FROM transcript_segments
        WHERE user_id = $1 AND created_at > NOW() - INTERVAL '{days} days'
        GROUP BY intent
        ORDER BY count DESC
        """,
        user_id, days=days
    )
```

---

### 5. Smart Filtering (Optional)

**Filter out low-value intents**:

```python
async def should_process_transcript(intent: str, confidence: float) -> bool:
    """Determine if transcript should be processed by Letta"""

    # Always process emergencies
    if intent == 'emergency':
        return True

    # Skip low-confidence chitchat
    if intent == 'chitchat' and confidence < 0.7:
        return False

    # Process medical and unknown
    if intent in ['medical', 'unknown']:
        return True

    # Wake words handled locally
    if intent == 'wake':
        return False

    return True
```

---

## 🧪 Testing

### Sample Messages to Expect

**Wake Word**:
```json
{"type":"transcript_segment","text":"Hey Ella","intent":"wake","intent_confidence":0.95,"intent_route":"local","is_emergency":false}
```

**Emergency**:
```json
{"type":"transcript_segment","text":"I have chest pain","intent":"emergency","intent_confidence":0.90,"intent_route":"emergency","is_emergency":true}
```

**Medical**:
```json
{"type":"transcript_segment","text":"I feel dizzy","intent":"medical","intent_confidence":0.75,"intent_route":"letta","is_emergency":false}
```

**Chitchat**:
```json
{"type":"transcript_segment","text":"Hello","intent":"chitchat","intent_confidence":0.80,"intent_route":"local","is_emergency":false}
```

---

## ⚠️ Important Notes

### 1. Optional Fields
**Intent fields are OPTIONAL**. The iOS app has a config switch to enable/disable IntentScanner:
- If disabled: No intent fields will be present
- Backend should gracefully handle missing fields

```python
# Safe field access
intent = data.get('intent')  # May be None
if intent is None:
    # IntentScanner disabled or failed - use default routing
    intent = 'unknown'
```

### 2. Performance Considerations
**Current Status**: IntentScanner is **disabled by default** due to performance issues (5+ second latency on LFM2 model).

**What this means**:
- Most production traffic will NOT have intent fields yet
- Intent fields will appear when:
  - Users manually enable it in developer settings
  - We optimize the LFM2 model and re-enable by default

**Recommendation**: Implement intent handling now, but don't rely on it for production routing yet.

### 3. Field Validation

```python
# Validate intent fields
VALID_INTENTS = ['wake', 'emergency', 'medical', 'chitchat', 'unknown']
VALID_ROUTES = ['local', 'letta', 'emergency']

def validate_intent_data(data: dict) -> bool:
    intent = data.get('intent')
    if intent and intent not in VALID_INTENTS:
        logger.warning(f"Invalid intent type: {intent}")
        return False

    route = data.get('intent_route')
    if route and route not in VALID_ROUTES:
        logger.warning(f"Invalid intent route: {route}")
        return False

    confidence = data.get('intent_confidence')
    if confidence and not (0.0 <= confidence <= 1.0):
        logger.warning(f"Invalid confidence: {confidence}")
        return False

    return True
```

---

## 📊 Analytics Queries

### Intent Distribution
```sql
SELECT intent, COUNT(*) as count
FROM transcript_segments
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY intent
ORDER BY count DESC;
```

### Emergency Events
```sql
SELECT text, speaker, created_at
FROM transcript_segments
WHERE is_emergency = true
ORDER BY created_at DESC
LIMIT 100;
```

### Low Confidence Classifications
```sql
SELECT text, intent, intent_confidence
FROM transcript_segments
WHERE intent_confidence < 0.6
ORDER BY created_at DESC
LIMIT 100;
```

---

## 🔮 Future Enhancements

### 1. Confidence Thresholds
Backend can reject low-confidence classifications:
```python
MIN_CONFIDENCE_THRESHOLD = 0.7

if intent_confidence < MIN_CONFIDENCE_THRESHOLD:
    # Treat as 'unknown' instead
    intent = 'unknown'
```

### 2. Intent-Based Rate Limiting
```python
# Allow more chitchat, throttle unknown
rate_limits = {
    'chitchat': 100,  # per hour
    'medical': 50,
    'unknown': 20,
    'emergency': 999,  # no limit
}
```

### 3. User-Specific Intent Keywords
Download custom keywords from backend:
```python
@app.get("/api/v1/intent-keywords/{user_id}")
async def get_user_keywords(user_id: str):
    return {
        "wake": ["hey ella", "ella"],
        "emergency": ["chest pain", "can't breathe"],
        # ... custom per user
    }
```

---

## ✅ Implementation Checklist

Backend Developer TODO:

- [ ] Update WebSocket handler to read new intent fields
- [ ] Add database columns for intent metadata (see SQL above)
- [ ] Implement emergency alert handler
- [ ] Add intent analytics tracking
- [ ] Test with sample messages (see Testing section)
- [ ] Deploy to staging environment
- [ ] Validate with iOS test build

---

## 📞 Questions?

Contact iOS Developer or PM for:
- Intent type definitions
- Confidence threshold recommendations
- Emergency routing requirements

---

**Status**: Intent classification implemented in iOS (disabled by default due to performance)
**ETA for Production**: TBD (pending LFM2 model optimization)
