# Omi to Letta n8n Workflows

This directory contains ready-to-import n8n workflows for integrating Omi wearable device with Letta AI memory system.

## 📦 Available Workflows

### 1. **omi-to-letta-simple.json**
**Best for:** Quick setup, testing, getting started

**Features:**
- ✅ Captures all Omi memories
- ✅ Sends full transcript + metadata to Letta
- ✅ Returns success confirmation
- ✅ Minimal configuration required

**Use when:** You want to get up and running quickly without complex error handling.

---

### 2. **omi-to-letta-enhanced.json** ⭐ RECOMMENDED
**Best for:** Production use, reliability

**Features:**
- ✅ All features from Simple version
- ✅ Validates data (skips discarded/empty conversations)
- ✅ Error handling with proper HTTP codes
- ✅ Includes action items and photo descriptions
- ✅ Execution logging (saves all runs for debugging)
- ✅ Graceful failure handling

**Use when:** You want a robust, production-ready integration.

---

### 3. **omi-to-letta-with-response.json**
**Best for:** Interactive notifications, two-way communication

**Features:**
- ✅ Captures memories in Letta
- ✅ Gets Letta's response/insights
- ✅ Sends notifications back to Omi app
- ✅ Smart truncation (keeps notifications under 20 words)
- ✅ Conditional logic (only notifies when meaningful)

**Use when:** You want Letta to provide feedback or insights that appear as notifications in your Omi app.

---

### 4. **omi-to-letta-multi-agent.json** 🚀 ADVANCED
**Best for:** Multiple specialized agents, intelligent routing

**Features:**
- ✅ Routes to different Letta agents based on content type
- ✅ 7 specialized routing paths:
  - **Work Agent**: Business meetings, work conversations
  - **Health Agent**: Health logs, fitness tracking
  - **Personal Agent**: Journal entries, personal reflections
  - **Learning Agent**: Educational content, notes
  - **Visual Agent**: Photo-based memories (Omi Glass)
  - **Tasks Agent**: Action-heavy conversations (3+ tasks)
  - **General Agent**: Fallback for uncategorized content
- ✅ Smart classification based on:
  - Category (work, health, personal, etc.)
  - Action item count
  - Photo presence
  - Duration
- ✅ Customized message formatting per agent type
- ✅ Priority tagging (normal/high)

**Use when:** You have multiple Letta agents specialized for different purposes and want automatic intelligent routing.

---

## 🚀 Quick Start

### Step 1: Import Workflow into n8n

1. Open your n8n instance
2. Click **"+"** (Add workflow)
3. Click **"Import from File"**
4. Paste the contents of one of the JSON files
5. Click **"Import"**

### Step 2: Configure Letta Credentials

1. Click on the **"Letta AI Agent"** node
2. Update these values:
   - `agentId`: Replace `YOUR_AGENT_ID_HERE` with your Letta agent ID
   - Credentials: Select your existing "Letta account" or create new credentials

**How to find your Letta Agent ID:**
```bash
# Using Letta CLI
letta list agents

# Or via API
curl http://localhost:8283/v1/agents \
  -H "Authorization: Bearer YOUR_LETTA_TOKEN"
```

### Step 3: Activate Workflow

1. Toggle the workflow to **"Active"**
2. Copy the **webhook URL** from the "Omi Webhook Trigger" node
   - Example: `https://your-n8n.com/webhook/omi-memory-capture`

### Step 4: Register Webhook in Omi App

1. Open **Omi mobile app**
2. Navigate to **Settings → Apps → "+" Create App**
3. Fill in:
   - **Name**: "Letta Memory Capture"
   - **Description**: "Automatically store Omi memories in Letta AI"
   - **Capability**: Select **"Memory"** or **"Conversation Created"**
   - **Webhook URL**: Paste your n8n webhook URL from Step 3
4. **Save** and **Enable** the app

---

## 🎯 What Gets Captured

All workflows capture the following from Omi:

### Basic Data
- **Title**: Conversation title
- **Overview**: AI-generated summary
- **Category**: (work, health, personal, other, etc.)
- **Emoji**: Visual indicator
- **Transcript**: Full conversation with speaker labels
- **Duration**: Length in minutes
- **Timestamps**: Created, started, finished times

### Enhanced Data (if available)
- **Action Items**: Extracted tasks/todos
- **Photos**: Photo descriptions from Omi Glass
- **Metadata**: Segment counts, photo counts, etc.

---

## 📊 Sample Data Structure

Here's what Omi sends to your webhook:

```json
{
  "created_at": "2025-01-16T10:30:00Z",
  "started_at": "2025-01-16T10:30:00Z",
  "finished_at": "2025-01-16T10:45:00Z",
  "transcript_segments": [
    {
      "text": "Let's discuss the new product launch",
      "speaker": "SPEAKER_00",
      "is_user": true,
      "start": 0.0,
      "end": 3.5
    },
    {
      "text": "Sure, I think we should target Q2",
      "speaker": "SPEAKER_01",
      "is_user": false,
      "start": 4.0,
      "end": 7.2
    }
  ],
  "structured": {
    "title": "Product Launch Planning",
    "overview": "Discussion about Q2 product launch strategy and timeline",
    "emoji": "🚀",
    "category": "work",
    "action_items": [
      {
        "description": "Schedule Q2 launch meeting"
      }
    ]
  },
  "photos": [],
  "discarded": false
}
```

---

## 🔧 Customization Options

### Filter by Category

Add this to the "Validate Omi Data" node (Enhanced workflow):

```javascript
const omiData = $input.item.json;

// Only capture work-related conversations
const allowedCategories = ['work', 'business'];
if (!allowedCategories.includes(omiData.structured?.category)) {
  throw new Error(`Skipping category: ${omiData.structured?.category}`);
}

return { json: omiData };
```

### Add Database Logging

Insert a new node between "Letta AI Agent" and "Respond":

1. Add **"Postgres"** or **"MySQL"** node
2. Configure to insert into your database:
   ```sql
   INSERT INTO omi_memories (title, category, transcript, letta_response, created_at)
   VALUES (
     '{{ $node["Transform Omi Data"].json.metadata.title }}',
     '{{ $node["Transform Omi Data"].json.metadata.category }}',
     '{{ $node["Transform Omi Data"].json.message }}',
     '{{ $json.messages[1].content }}',
     NOW()
   );
   ```

### Change Notification Format

Edit the "Format Letta Response" node in the bidirectional workflow:

```javascript
// Custom notification format
const notification = `💡 ${metadata.emoji} ${responseText.substring(0, 50)}...`;

return {
  json: {
    message: notification,
    // ... rest of the fields
  }
};
```

---

## 🐛 Troubleshooting

### Workflow not triggering?
1. Check workflow is **Active** (toggle in top right)
2. Verify webhook URL is correct in Omi app
3. Check n8n logs: Settings → Log Streaming

### Letta agent not found?
```bash
# List all agents
letta list agents

# Get specific agent details
letta agent info <agent-name>
```

### Empty/missing data?
- Enable **execution logging** in workflow settings
- Check the "Executions" tab to see actual data received
- Verify Omi app has permissions to send webhooks

### Notifications not appearing?
- Omi only shows notifications with content >5 characters
- Check the "message" field in your response
- Try the bidirectional workflow for better notification control

---

## 📚 Additional Resources

- **Omi Documentation**: https://docs.omi.me/
- **Letta Documentation**: https://docs.letta.ai/
- **n8n Documentation**: https://docs.n8n.io/
- **Omi Plugin Development**: https://docs.omi.me/docs/developer/apps/Introduction

---

## 🤝 Contributing

Have improvements or additional workflows? Submit them to:
- GitHub: https://github.com/BasedHardware/omi
- Discord: http://discord.omi.me

---

## 📝 License

These workflows are part of the Omi project and are licensed under the MIT License.

---

## ⚡ Quick Reference

| Workflow | Complexity | Features | Best For |
|----------|-----------|----------|----------|
| Simple | ⭐ | Basic capture | Testing, learning |
| Enhanced | ⭐⭐⭐ | Validation, error handling | Production |
| Bidirectional | ⭐⭐ | Two-way communication | Interactive insights |
| Multi-Agent | ⭐⭐⭐⭐ | 7 specialized agents, smart routing | Advanced setups |

## 📖 Additional Documentation

- **OMI_PAYLOAD_DOCUMENTATION.md**: Complete guide to Omi webhook payloads with sample data
  - Memory vs Realtime payloads explained
  - All field descriptions
  - Sample payloads for testing
  - Routing strategies for multi-agent setups

**Need help?** Join the [Omi Discord](http://discord.omi.me) or check the [documentation](https://docs.omi.me/).
