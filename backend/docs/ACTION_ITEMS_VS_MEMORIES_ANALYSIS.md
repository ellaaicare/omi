# Action Items vs Memories - Architecture Clarification

**Date**: November 21, 2025
**Finding**: Action items and memories are SEPARATE systems with different purposes

---

## 🎯 **CRITICAL INSIGHT**

**"Buy milk tomorrow" should be an ACTION ITEM, NOT a memory!**

The user caught a critical architectural misunderstanding in our quality analysis. The OMI system has TWO separate extraction systems:

1. **Summary Agent** → Extracts action items, events, title, overview
2. **Memory Agent** → Extracts interesting/system facts worth remembering

---

## 📊 **System Architecture**

### **Summary Agent** (`/webhook/summary-agent`)

**Extracts**:
- `title`: Conversation headline
- `overview`: Brief summary
- `emoji`: Representative emoji
- `category`: Conversation category
- **`action_items`**: Todos, tasks, things to do
- **`events`**: Calendar items, appointments

**Example Action Items**:
- "Check vitamin D levels (follow-up with doctor)"
- "Buy milk tomorrow"
- "Schedule annual physical"
- "Call dentist to reschedule"

**Example Events**:
- "Annual physical — schedule" (Dec 1, 2025, 30 min)
- "Team meeting" (Nov 22, 2025, 60 min)

---

### **Memory Agent** (`/webhook/memory-agent`)

**Extracts**:
- **`interesting`**: Rare fun facts, novel insights, surprising discoveries
- **`system`**: Mundane details, routine information, context

**Example Interesting Memories**:
- "Octopuses have three hearts"
- "Microwave technology originated from WWII radar engineer's accidental discovery"
- "Merged black holes create spacetime ripples"

**Example System Memories**:
- "User and friend discussed microwave mishap involving a fork"
- "User prefers almond milk over oat milk"
- "User takes vitamin D supplements daily"

---

## ⚠️ **THE MISCLASSIFICATION PROBLEM**

### **Test 4: "Buy Milk" Extracted Incorrectly**

**Input Conversation**:
```
Friend: "Did you know that octopuses have three hearts?"  ← FUN FACT
User: "No way! That's fascinating!"
User: "By the way, I need to buy milk tomorrow."  ← TODO/TASK
Friend: "Cool. I'm going to the gym at 5pm."  ← ROUTINE INFO
```

**What Happened** (INCORRECT):
```
Memory Agent:
  ✅ Created 1 memory: "User needs to buy milk tomorrow."
  ❌ Category: "interesting" (WRONG - should be action item, not memory)
  ❌ Missed: "Octopuses have 3 hearts" (SHOULD be interesting memory)

Summary Agent:
  ❌ No action items extracted
  ❌ No summary created (or callback pending?)
```

**What SHOULD Have Happened** (CORRECT):
```
Memory Agent:
  ✅ Memory 1: "Octopuses have three hearts."
     Category: interesting
     Tags: ['biology', 'facts']

  ✅ Memory 2 (optional): "Friend goes to gym at 5pm."
     Category: system
     Tags: ['routine', 'schedule']

Summary Agent:
  ✅ Title: "Fascinating Facts and Errands"
  ✅ Overview: "Discussion about octopus biology, user has milk shopping task"
  ✅ Emoji: 🐙
  ✅ Category: other

  ✅ Action Items:
     1. Buy milk tomorrow
        Completed: false
        Due: 2025-11-22
```

---

## ✅ **Test 1: Working Correctly**

### **Input Conversation** (Health Discussion):
```
User: "I've been taking vitamin D supplements, 2000 IU daily."
Friend: "That's good! Have you had your vitamin D levels checked?"
User: "Yes, my doctor recommended it after my last checkup."
User: "I also need to schedule my annual physical for next month."
```

### **What Happened** (CORRECT Summary, WRONG Memories):

**Summary Agent** ✅:
```
Title: Vitamin D supplements — 2000 IU daily
Overview: User reports taking vitamin D supplements 2000 IU daily.
         Doctor recommended checking vitamin D levels after last checkup.
         User also needs to schedule annual physical next month.
Category: health
Emoji: 💊

Action Items: ✅
  1. Check vitamin D levels (follow-up with doctor)
     Due: 2025-12-01

Events: ✅
  1. Annual physical — schedule
     Start: 2025-12-01
     Duration: 30 min
```

**Memory Agent** ⚠️:
```
Memory 1:
  Content: User needs to schedule annual physical for next month.
  Category: interesting  ← WRONG (should be "system")
  Tags: ['appointment', 'doctor']

Memory 2:
  Content: User has been taking vitamin D supplements, 2000 IU daily;
           doctor recommended checking vitamin D levels after last checkup.
  Category: interesting  ← WRONG (should be "system")
  Tags: ['medication', 'vitamin D']
```

**Analysis**:
- ✅ Summary agent correctly extracted action items and events
- ⚠️ Memory agent marked routine health tracking as "interesting" (should be "system")
- ⚠️ Some duplication: "schedule physical" appears as both memory AND action item

---

## 📝 **REVISED QUALITY ISSUES**

### **Issue 1: Memory Agent Extracting Action Items**

**Problem**: Memory agent is extracting todos/tasks as memories instead of leaving them for summary agent.

**Example**:
- "Buy milk tomorrow" → Extracted as MEMORY (wrong)
- Should be: Extracted as ACTION ITEM by summary agent

**Impact**:
- Duplication between memories and action items
- Wrong category ("interesting" instead of action item)
- Clutters memory database with todos

---

### **Issue 2: Memory Agent Missing Interesting Facts**

**Problem**: Memory agent prioritizes mundane tasks over fascinating facts.

**Example**:
- "Octopuses have 3 hearts" → NOT extracted
- "Buy milk" → Extracted instead

**Impact**:
- Database fills with routine tasks instead of knowledge worth remembering
- User loses interesting facts they wanted to recall later

---

### **Issue 3: Category Distribution (Still Wrong)**

**Problem**: All memories marked "interesting" when most should be "system".

**Current**: 100% interesting, 0% system
**Expected**: 25% interesting, 75% system

---

## 🎯 **RECOMMENDATIONS FOR LETTA TEAM**

### **Priority 1: Clarify Memory vs Action Item Boundary**

**Memory Agent Prompt Should Say**:
```
DO NOT extract action items, todos, or tasks. These are handled by the summary agent.

Examples of what NOT to extract as memories:
- "User needs to buy milk tomorrow" ❌ (action item)
- "User should call dentist" ❌ (action item)
- "User wants to schedule appointment" ❌ (action item)

Examples of what TO extract as memories:
- "Octopuses have three hearts" ✅ (interesting fact)
- "User takes vitamin D supplements daily" ✅ (system detail)
- "Friend prefers almond milk" ✅ (system preference)
```

---

### **Priority 2: Fix Memory Selection Logic**

When conversation contains BOTH interesting facts AND mundane tasks:
- **Prioritize**: Novel, educational, surprising facts (octopus hearts)
- **Deprioritize**: Routine tasks/todos (buy milk) ← These go to action items

---

### **Priority 3: Fix Category Distribution**

**Interesting** (25%):
- Rare fun facts
- Surprising discoveries
- Novel insights worth sharing

**System** (75%):
- Routine health tracking ("takes vitamin D daily")
- Preferences ("prefers almond milk")
- Mundane details ("discussed microwave mishap")

---

## 📊 **EVIDENCE FROM TESTS**

### **Test 1: Action Items Working**

Summary agent correctly extracted:
```json
{
  "action_items": [
    {
      "description": "Check vitamin D levels (follow-up with doctor)",
      "completed": false,
      "due_at": "2025-12-01T00:00:00+00:00"
    }
  ],
  "events": [
    {
      "title": "Annual physical — schedule",
      "start": "2025-12-01T00:00:00+00:00",
      "duration": 30
    }
  ]
}
```

This proves:
- ✅ Summary agent CAN extract action items
- ✅ Events extraction working
- ✅ Due dates being set correctly

---

### **Test 4: Summary Agent Failure**

Summary agent returned:
```
Title: None
Overview: None
Action Items: []
Events: []
```

This means either:
1. Summary agent wasn't called
2. Summary callback is still pending (>35s delay unlikely)
3. Summary agent returned empty response

Need to verify if Test 4 called summary agent at all.

---

## 🔍 **NEXT STEPS**

### **For Backend Team**:
1. ✅ Verify Test 4 called summary agent (check logs)
2. Check if summary callback arrived late
3. Test if summary agent returns empty for short conversations

### **For Letta Team**:
1. ⚠️ Update memory agent prompt: "DO NOT extract action items/todos"
2. ⚠️ Fix memory selection: Prioritize interesting facts over mundane tasks
3. ⚠️ Fix category distribution: 25% interesting, 75% system
4. ⚠️ Verify summary agent extracts todos as action items (not memories)

### **For iOS Team**:
- ℹ️ Action items are displayed in app UI
- ℹ️ Users can mark action items as completed
- ℹ️ Memories are separate (knowledge database)
- ℹ️ Both systems should work independently

---

## 📚 **RELATED FILES**

**Backend Models**:
- `models/conversation.py` lines 102-111: ActionItem model
- `models/conversation.py` lines 169-181: Structured model with action_items
- `models/memories.py`: Memory model (separate from action items)

**Backend Processing**:
- `utils/llm/conversation_processing.py` lines 404-411: Action item parsing from summary
- `utils/llm/memories.py`: Memory extraction (should NOT extract action items)

**n8n Endpoints**:
- `/webhook/summary-agent`: Returns action_items, events, title, overview
- `/webhook/memory-agent`: Returns memories (interesting/system facts)

---

**Conclusion**: The user's observation is correct and critical. "Buy milk" should be an ACTION ITEM (extracted by summary agent), NOT a memory (extracted by memory agent). This is a fundamental architectural distinction that needs to be enforced in the Letta agent prompts.
