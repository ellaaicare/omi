# Original OMI Prompts Reference

**Purpose**: Reference document for comparing Letta agent outputs with original OMI prompt behavior.
**Generated**: November 25, 2025

---

## Memory Extraction Prompt

**Location**: `utils/prompts.py` - `extract_memories_prompt`

**Key Design Principles**:
- **Two Categories**: `interesting` (fun facts, surprising insights) vs `system` (mundane, factual details)
- **Limit**: Up to 2 interesting + 2 system memories per conversation
- **Deduplication**: Checks against existing memories to avoid repetition
- **Short & Concise**: Memories must be "extremely short and simple"

### Full Prompt:

```
**Optimized Instructions for Generating Memories from Conversations**

When generating memories from a conversation between the user and others, the goal is to capture both interesting and system details that can serve as reference points for the user. Follow these structured guidelines:

**Interesting Memories:**
- **Purpose:** Capture engaging, surprising, or valuable insights that the user might find enjoyable or useful to revisit.
- **Content:**
  - Highlight unique facts, anecdotes, or discussions that are likely to spark curiosity or interest.
  - Include any notable experiences, plans, or discoveries shared during the conversation.
  - Focus on elements that provide new knowledge or perspective to the user.
- **Format:**
  - Keep each memory concise, catchy, and focused on the key intriguing detail.
  - Use a narrative style that enhances the excitement or novelty of the information.
- **Examples:**
  - Zara learned that microwave technology originated from a WWII radar engineer's accidental discovery.
  - Aria shared that merged black holes create spacetime ripples, akin to a bell's echo.

**System Memories:**
- **Purpose:** Record mundane, factual details that are part of the conversation but hold minimal ongoing interest.
- **Content:**
  - Document logistical or background details such as plans, preferences, or routine actions.
  - Capture information that, while useful for context, is not engaging enough for future reference.
- **Format:**
  - Ensure these memories are clear, factual, and devoid of any embellishment or unnecessary detail.
  - Keep them straightforward and to the point.
- **Examples:**
  - Zara and Liam discussed a microwave mishap involving a fork.
  - Aria and Noah decided to purchase almond milk over oat milk.

**General Tips for Memory Generation:**
- **Clarity and Conciseness:** All memories should be precise and directly drawn from the conversation content.
- **Contextual Relevance:** Ensure that the memories are tailored to the user's interests and potential future needs.
- **Balance:** Strive for a balanced mix of interesting and system memories to provide a comprehensive snapshot of the conversation.
- **Limit**: Identify up to 2 interesting memories and 2 system memories. If there are none, output an empty list.
- **Short and Simple**: Keep the memories very short, concise and catchy. They must be extremely short and simple.

**Categories for Facts**:

Each fact you provide should fall under one of the following categories:

- **interesting**: Capture engaging, surprising, or valuable insights that the user might find enjoyable or useful to revisit.
- **system**: Record mundane, factual details that are part of the conversation but hold minimal ongoing interest.

**Output Instructions**:

- Identify up to 2 interesting memories and 2 system memories.
- If you do not find any new (different to the list of existing ones below) or new noteworthy facts, provide an empty list.
- Do not include any explanations or additional text; only list the facts.
- Keep the memories very short, concise and catchy. They must be extremely short and simple.
- Most of the memories would be system memories, as they are facts about the conversation. Interesting memories are rare. Interesting memories are only very interesting things that the user would want to remember. If ambiguous, favor system memories. Interesting memories are like fun facts.
```

### Memory Model Categories (from `models/memories.py`):
- `interesting` - Engaging, surprising insights
- `system` - Mundane, factual details

---

## Summary/Structured Generation Prompt

**Location**: `utils/llm/conversation_processing.py` - `get_transcript_structure()`

**Key Design Principles**:
- **Title**: Clear headline (≤10 words), Title Case, key noun + verb
- **Overview**: Condensed summary with main topics and key points
- **Emoji**: Single emoji reflecting core subject/mood (specific, not generic)
- **Category**: Classify into predefined categories
- **Events**: Only CONFIRMED commitments with specific timing
- **Action Items**: Extracted separately with strict filtering rules

### Full Summary Prompt:

```
You are an expert content analyzer. Your task is to analyze the provided content (which could be a transcript, a series of photo descriptions from a wearable camera, or both) and provide structure and clarity.
The content language is {language_code}. Use the same language {language_code} for your response.

For the title, Write a clear, compelling headline (≤ 10 words) that captures the central topic and outcome. Use Title Case, avoid filler words, and include a key noun + verb where possible (e.g., "Team Finalizes Q2 Budget" or "Family Plans Weekend Road Trip")
For the overview, condense the content into a summary with the main topics discussed or scenes observed, making sure to capture the key points and important details.
For the emoji, select a single emoji that vividly reflects the core subject, mood, or outcome of the content. Strive for an emoji that is specific and evocative, rather than generic (e.g., prefer 🎉 for a celebration over 👍 for general agreement, or 💡 for a new idea over 🧠 for general thought).

For the category, classify the content into one of the available categories.

For Calendar Events, apply strict filtering to include ONLY events that meet ALL these criteria:
• **Confirmed commitment**: Not suggestions or "maybe" - actual scheduled events
• **User involvement**: The user is expected to attend, participate, or take action
• **Specific timing**: Has concrete date/time, not vague references like "sometime" or "soon"
• **Important/actionable**: Missing it would have real consequences or impact

INCLUDE these event types:
• Meetings & appointments (business meetings, doctor visits, interviews)
• Hard deadlines (project due dates, payment deadlines, submission dates)
• Personal commitments (family events, social gatherings user committed to)
• Travel & transportation (flights, trains, scheduled pickups)
• Recurring obligations (classes, regular meetings, scheduled calls)

EXCLUDE these:
• Casual mentions ("we should meet sometime", "maybe next week")
• Historical references (past events being discussed)
• Other people's events (events user isn't involved in)
• Vague suggestions ("let's grab coffee soon")
• Hypothetical scenarios ("if we meet Tuesday...")

For date context, this content was captured on {started_at}. {tz} is the user's timezone; convert all event times to UTC and respond in UTC.
```

### Available Categories (from `models/conversation.py` - `CategoryEnum`):
- `personal`
- `education`
- `health`
- `finance`
- `legal`
- `philosophy`
- `spiritual`
- `science`
- `entrepreneurship`
- `parenting`
- `romance`
- `travel`
- `inspiration`
- `technology`
- `business`
- `social`
- `work`
- `sports`
- `politics`
- `entertainment`
- `other`

---

## Action Items Extraction Prompt

**Location**: `utils/llm/conversation_processing.py` - `extract_action_items()`

**Key Design Principles**:
- **Quality over Quantity**: Better 0 items than flooding with unnecessary ones
- **Strict Filtering**: Clear ownership, concrete action, timing signal, real importance
- **NOT Already Being Done**: Skip tasks user is actively working on
- **Deduplication**: Check against existing action items from past 2 days
- **Format**: Short (max 15 words), start with verb, no time references in description

### Key Filtering Rules:

**INCLUDE if ALL criteria met:**
1. Clear Ownership & Relevance to Primary User
2. Concrete Action (specific, actionable next step)
3. Timing Signal (explicit dates, relative timing, urgency markers)
4. Real Importance (financial impact, health/safety, hard deadlines, commitments)
5. NOT Already Being Done

**EXCLUDE:**
- Things user is ALREADY doing
- Casual mentions or updates
- Vague suggestions without commitment
- General goals without specific next steps
- Past actions being discussed
- Hypothetical scenarios
- Trivial tasks with no consequences
- Tasks assigned to others that don't impact primary user

---

## Learnings Extraction Prompt

**Location**: `utils/prompts.py` - `extract_learnings_prompt`

**Purpose**: Extract key learnings and valuable facts that can make user more knowledgeable.

**Categories**:
- Life Lessons
- World Facts
- Motivational Insights
- Historical Facts
- Scientific Facts
- Practical Advice
- Other

**Limit**: Up to 5 learnings per conversation

---

## Comparison: Original vs Letta Agent Output

### Memory Example from Letta Agent:
```
Content: User had lunch with Sarah at noon — mission: tame the Q2 project timeline
         (sandwiches were consumed, deadlines negotiated).
Category: interesting
```

### How Original Prompt Would Handle This:

The original prompt would likely produce:
- **System Memory**: "User had lunch with Sarah at noon to discuss Q2 project timeline."
- **System Memory**: "User agreed to push Q2 deadline back by two weeks."

The Letta agent is adding more personality/humor ("sandwiches were consumed, deadlines negotiated") which isn't in the original prompt style. The original emphasizes:
- "Clear, factual, and devoid of any embellishment"
- "Extremely short and simple"

### Recommendation for Letta Agent Configuration:

If you want Letta to match the original behavior more closely:
1. **System memories should be factual and plain** - no embellishment
2. **Interesting memories should be reserved for genuinely surprising facts** - like scientific discoveries, not meeting summaries
3. **Keep memories shorter** - the Letta output is longer than original style

---

## Files Reference

| File | Purpose |
|------|---------|
| `utils/prompts.py` | Memory extraction prompts |
| `utils/llm/memories.py` | Memory extraction logic |
| `utils/llm/conversation_processing.py` | Summary and action item extraction |
| `models/memories.py` | Memory data models |
| `models/conversation.py` | Structured summary data models |

---

**Last Updated**: November 25, 2025
