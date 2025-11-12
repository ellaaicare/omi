# iOS → Backend Developer Handoff

**Date**: November 11, 2025
**From**: Claude-iOS-Developer
**To**: Claude-Backend-Developer

---

## 📋 Intent Scanner Decision

**Status**: On-device intent classification **abandoned**
**Reason**: Cloud is 50x faster, more accurate, easier to maintain

### Performance Test Results (iPhone 13 Pro)
- **On-device**: 910ms → 5,381ms (thermal throttling)
- **Cloud**: <100ms ✅

### Decision Documents
1. **INTENT_SCANNER_DECISION.md** - Complete analysis and rationale
2. **BACKEND_INTENT_INTEGRATION.md** - Implementation spec for you

---

## 🎯 Your Task: Implement Cloud Intent Classification

### Endpoint to Create
```
POST /api/v1/intent/classify
```

### Request Format
```json
{
  "text": "I feel dizzy and nauseous"
}
```

### Response Format
```json
{
  "intent": "medical",
  "confidence": 0.75,
  "route": "letta",
  "is_emergency": false
}
```

### Intent Types
- `wake` - "Hey Ella" detection
- `emergency` - Critical health (chest pain, can't breathe)
- `medical` - Symptoms, medication queries
- `chitchat` - Casual conversation
- `unknown` - Unclear intent

---

## 📚 Complete Documentation

**Read these in order:**

1. **INTENT_SCANNER_DECISION.md**
   - Why we abandoned on-device
   - Performance comparison
   - Cost analysis

2. **BACKEND_INTENT_INTEGRATION.md**
   - Endpoint specification
   - Integration with WebSocket handler
   - Database schema updates
   - Sample code and queries

---

## 🔌 Integration Point

When transcript segment received via `/v4/listen`:

```python
async def handle_transcript_segment(data: dict):
    text = data['text']

    # Classify intent
    intent_data = await classify_intent(text)

    # Add to transcript metadata
    data['intent'] = intent_data['intent']
    data['intent_confidence'] = intent_data['confidence']
    data['intent_route'] = intent_data['route']
    data['is_emergency'] = intent_data['is_emergency']

    # Route based on intent
    if intent_data['is_emergency']:
        await handle_emergency(data)
    elif intent_data['route'] == 'letta':
        await escalate_to_letta(data)
```

---

## 💡 Implementation Options

### Option 1: Keyword-based (Start Here) ✅
- Fast, cheap, ~60-70% accuracy
- Good enough for MVP
- See BACKEND_INTENT_INTEGRATION.md for sample code

### Option 2: LLM-based (Upgrade Later)
- Use GPT-4o-mini
- ~90% accuracy
- ~$0.001 per request ($10-30/month)

**Recommendation**: Start with keyword-based, upgrade if needed.

---

## 🚀 Next Steps

1. Read BACKEND_INTENT_INTEGRATION.md
2. Implement `/api/v1/intent/classify` endpoint
3. Add keyword-based classification (Option 1)
4. Integrate with WebSocket `/v4/listen` handler
5. Test with sample transcripts
6. Deploy to staging

**ETA**: ~4-6 hours

---

## 📞 Questions?

Contact iOS dev via PM:
```bash
python3 /tmp/contact_pm_ios.py
```

Or check Discord #general for test results post.

---

**All docs located in**: `app/docs/`
