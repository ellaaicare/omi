# Memory Filtering Analysis - Preventing Garbage Memories

**Date**: November 21, 2025
**Issue**: Ensure we're not creating garbage memories from background noise/uninteresting content

---

## 🔍 **Question**

How did the original LLM-based system prevent creating garbage memories? Is the "interesting" vs "system" category a quality filter?

---

## 📊 **Original System Analysis**

### **File**: `utils/llm/memories.py`

**Garbage Prevention Mechanisms**:

1. **Minimum Content Filter** (Line 55-56):
   ```python
   if not content or len(content) < 25:  # less than 5 words, probably nothing
       return []
   ```
   - Rejects conversations with < 25 characters (~5 words)
   - Prevents processing of background noise

2. **Pydantic Model Constraints** (Line 15-21):
   ```python
   class Memories(BaseModel):
       facts: List[Memory] = Field(
           min_items=0,      # ✅ CAN RETURN ZERO MEMORIES
           max_items=4,      # ✅ MAX 4 MEMORIES
           description="List of **new** facts. If any",
           default=[],       # ✅ DEFAULT IS EMPTY
       )
   ```
   - **LLM can return empty list** if nothing noteworthy
   - Max 4 memories per conversation (2 interesting + 2 system)

3. **Complete LLM Prompt** (`utils/prompts.py` lines 12-80):

   **FULL ORIGINAL PROMPT** (for Letta team reference):
   ```
   **Optimized Instructions for Generating Memories from Conversations**

   When generating memories from a conversation between the user and others,
   the goal is to capture both interesting and system details that can serve
   as reference points for the user. Follow these structured guidelines:

   **Interesting Memories:**
   - **Purpose:** Capture engaging, surprising, or valuable insights that the
     user might find enjoyable or useful to revisit.
   - **Content:**
     - Highlight unique facts, anecdotes, or discussions that are likely to
       spark curiosity or interest.
     - Include any notable experiences, plans, or discoveries shared during
       the conversation.
     - Focus on elements that provide new knowledge or perspective to the user.
   - **Format:**
     - Keep each memory concise, catchy, and focused on the key intriguing detail.
     - Use a narrative style that enhances the excitement or novelty of the
       information.
   - **Examples:**
     - Zara learned that microwave technology originated from a WWII radar
       engineer's accidental discovery.
     - Aria shared that merged black holes create spacetime ripples, akin to
       a bell's echo.

   **System Memories:**
   - **Purpose:** Record mundane, factual details that are part of the
     conversation but hold minimal ongoing interest.
   - **Content:**
     - Document logistical or background details such as plans, preferences,
       or routine actions.
     - Capture information that, while useful for context, is not engaging
       enough for future reference.
   - **Format:**
     - Ensure these memories are clear, factual, and devoid of any embellishment
       or unnecessary detail.
     - Keep them straightforward and to the point.
   - **Examples:**
     - Zara and Liam discussed a microwave mishap involving a fork.
     - Aria and Noah decided to purchase almond milk over oat milk.

   **General Tips for Memory Generation:**
   - **Clarity and Conciseness:** All memories should be precise and directly
     drawn from the conversation content.
   - **Contextual Relevance:** Ensure that the memories are tailored to the
     user's interests and potential future needs.
   - **Balance:** Strive for a balanced mix of interesting and system memories
     to provide a comprehensive snapshot of the conversation.
   - **Limit**: Identify up to 2 interesting memories and 2 system memories.
     If there are none, output an empty list.
   - **Short and Simple**: Keep the memories very short, concise and catchy.
     They must be extremely short and simple.

   **Categories for Facts**:

   Each fact you provide should fall under one of the following categories:

   - **interesting**: Capture engaging, surprising, or valuable insights that
     the user might find enjoyable or useful to revisit.
   - **system**: Record mundane, factual details that are part of the conversation
     but hold minimal ongoing interest.

   **Output Instructions**:

   - Identify up to 2 interesting memories and 2 system memories.
   - If you do not find any new (different to the list of existing ones below)
     or new noteworthy facts, provide an empty list.
   - Do not include any explanations or additional text; only list the facts.
   - Keep the memories very short, concise and catchy. They must be extremely
     short and simple.
   - Most of the memories would be system memories, as they are facts about the
     conversation. Interesting memories are rare. Interesting memories are only
     very interesting things that the user would want to remember. If ambiguous,
     favor system memories. Interesting memories are like fun facts.

   **Existing memories you already know about {user_name} and their friends
   (DO NOT REPEAT ANY)**:
   ```
   {memories_str}
   ```

   **Conversation transcript**:
   ```
   {conversation}
   ```
   {format_instructions}
   ```

   **Key Insights from Prompt**:
   - Line 48: "Limit: up to 2 interesting + 2 system = MAX 4 total"
   - Line 61: "If you do not find any new or noteworthy facts, provide empty list"
   - Line 64: "**Most memories would be system**. Interesting memories are **rare**"
   - Line 64: "If ambiguous, **favor system memories**"
   - Line 64: "Interesting memories are like **fun facts**"

---

## 🏷️ **"Interesting" vs "System" - Quality Filter**

### **Original Intent**:

**"interesting"** = High-value, rare memories
- Fun facts worth revisiting
- Surprising insights
- Novel discoveries
- Examples:
  - "User learned that microwave technology originated from WWII radar engineer's accidental discovery"
  - "Friend shared that merged black holes create spacetime ripples"

**"system"** = Low-value, mundane facts
- Logistical details
- Routine actions
- Background context with minimal ongoing interest
- Examples:
  - "User and friend discussed microwave mishap involving a fork"
  - "User decided to purchase almond milk over oat milk"

**Key Quote** (Line 64):
> "Most of the memories would be system memories, as they are facts about the conversation.
> Interesting memories are rare. Interesting memories are only very interesting things that
> the user would want to remember. If ambiguous, favor system memories. Interesting memories
> are like fun facts."

---

## ⚠️ **Current n8n/Letta Implementation**

### **What We Observed**:

**Test Payload** (V5_CALLBACK_PAYLOADS_SENT.md):
```json
{
  "memories": [
    {"content": "User started taking vitamin C...", "category": "interesting"},
    {"content": "Doctor recommended annual physical...", "category": "system"},
    {"content": "User takes vitamin C at breakfast...", "category": "interesting"},
    {"content": "User has upcoming physical...", "category": "system"},
    {"content": "Doctor recommended checkup...", "category": "system"}
  ]
}
```

**Firestore Reality**:
- All 5 memories stored as **"interesting"** (not the mix of interesting/system from payload)
- Appears Letta is categorizing everything as "interesting"

### **Missing Safeguards**:

1. **❓ Can Letta return empty array** if nothing noteworthy?
   - Original system: ✅ Yes (min_items=0)
   - Letta agent: ❓ Unknown (need to check Letta config)

2. **❓ Does Letta have max limit**?
   - Original system: ✅ Max 4 memories
   - Letta agent: ❓ Unknown (test returned 5 memories)

3. **❓ Does Letta deduplicate**?
   - Original system: ✅ Checks against existing memories
   - Letta agent: ❓ Unknown

4. **❓ Does Letta filter background noise**?
   - Original system: ✅ Min 25 characters, prompt says "if none, return empty"
   - Letta agent: ❓ Unknown

---

## 🚨 **Risk Assessment**

### **High Risk Scenarios**:

1. **Background Noise**:
   - User leaves OMI on during TV watching
   - Letta extracts TV dialogue as "memories"
   - Example: "Character said they're going to the store" → stored as memory

2. **Garbage Conversations**:
   - User has pointless small talk ("nice weather today")
   - Letta creates memory: "User discussed weather"
   - Pollutes memory database with useless info

3. **Duplicate Memories**:
   - User mentions taking vitamin C daily (10 conversations)
   - Letta creates 10 memories about same vitamin C fact
   - No deduplication = database bloat

4. **Over-extraction**:
   - 30-minute conversation
   - Original system: Max 4 memories
   - Letta: Could create 20+ memories?

---

## ✅ **Recommended Actions**

### **Immediate (Before n8n v5.0 Production)**:

1. **Test Letta with Edge Cases**:
   ```bash
   # Test 1: Background noise (TV/music)
   curl -X POST https://n8n.ella-ai-care.com/webhook/memory-agent \
     -d '{"segments": [{"text": "I love this song!", "speaker": "TV"}]}'

   # Expected: Empty array (nothing worth storing)
   # Actual: ???

   # Test 2: Pointless small talk
   curl -X POST https://n8n.ella-ai-care.com/webhook/memory-agent \
     -d '{"segments": [{"text": "Nice weather today", "speaker": "User"}]}'

   # Expected: Empty array
   # Actual: ???

   # Test 3: Very long conversation
   # Send 100 segments of varied content
   # Expected: Max 4-10 memories (reasonable limit)
   # Actual: ???
   ```

2. **Verify Letta Agent Config**:
   - Check Letta `rolling_memories` block config
   - Confirm it has logic to return empty array
   - Verify max memory limit
   - Check deduplication logic

3. **Backend Safeguard** (Add to `/v1/ella/memory` endpoint):
   ```python
   # Reject if too many memories
   if len(request.memories) > 10:  # Configurable threshold
       print(f"⚠️  Rejecting {len(request.memories)} memories (over limit)")
       raise HTTPException(400, "Too many memories (max 10 per conversation)")

   # Reject if memories are too short (likely garbage)
   for memory in request.memories:
       if len(memory.content) < 10:  # < 2-3 words
           print(f"⚠️  Rejecting garbage memory: {memory.content}")
           continue  # Skip this memory

   # Deduplicate against recent memories
   recent_memories = get_recent_memories(uid, days=7)
   for memory in request.memories:
       if memory.content in [m.content for m in recent_memories]:
           print(f"⚠️  Skipping duplicate: {memory.content}")
           continue
   ```

### **Medium Term**:

1. **Add Backend Quality Scoring**:
   - Score memories by length, specificity, uniqueness
   - Auto-discard low-quality memories
   - Only store memories above threshold

2. **User Feedback Loop**:
   - Let users mark memories as "not useful"
   - Train model to improve extraction quality
   - Track discard rate as quality metric

3. **A/B Test Original vs Letta**:
   - Run both systems in parallel
   - Compare memory quality, quantity, user satisfaction
   - Switch to Letta only if quality is equal or better

---

## 📝 **Questions for Letta/n8n Team**

1. **Can Letta agent return empty memories array** if conversation has nothing noteworthy?

2. **What is the max memories limit** in Letta agent config?

3. **Does Letta deduplicate** against existing memories?

4. **How does Letta handle background noise** (TV, music, irrelevant chatter)?

5. **What is Letta's definition of "interesting"** vs "system"? (Seems all memories → "interesting")

6. **Can we see Letta agent's prompt/config** for memory extraction?

---

## 🧪 **Test Plan** (See Below)

Run full E2E test with edge cases to verify memory quality filtering.

---

**Next**: Run comprehensive E2E test suite with real conversations and edge cases.
