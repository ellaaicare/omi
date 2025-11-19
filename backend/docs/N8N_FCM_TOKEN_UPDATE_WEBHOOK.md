# N8N FCM Token Update Webhook Schema

**Endpoint**: `POST https://n8n.ella-ai-care.com/webhook/fcm-token-update`
**Purpose**: Real-time notification when user registers/updates FCM token
**Source**: OMI Backend (`routers/notifications.py` lines 63-85)
**Created**: November 19, 2025
**Status**: ✅ Implemented (ready for n8n workflow setup)

---

## ⚠️ PROBLEM THIS SOLVES

**Issue**: UID mismatch when iOS app re-registers
- User has UID `5aGC5YE9BnhcSoTxxtT4ar6ILQy2` with FCM token
- iOS app uninstalls/reinstalls or Firebase Auth resets
- User re-registers with NEW UID `HbBdbnRkPJhpYFIIsd34krM8FKD3`
- n8n/Letta still using OLD UID → notifications fail with "no_token" error

**Solution**: Backend immediately notifies n8n whenever FCM token registered
- n8n updates its database with latest UID for this user
- n8n can migrate Letta agent data to new UID (or link multiple UIDs)
- Future notifications use correct, current UID

---

## Request Schema

### Full Payload Structure

```json
{
  "uid": "string (required)",
  "fcm_token": "string (required)",
  "device_name": "string (optional)",
  "device_type": "string (optional)",
  "timestamp": "string (required, ISO 8601)"
}
```

---

## Field Definitions

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `uid` | string | ✅ Yes | Firebase User ID (may be NEW if re-registered) | "5aGC5YE9BnhcSoTxxtT4ar6ILQy2" |
| `fcm_token` | string | ✅ Yes | Firebase Cloud Messaging token for push notifications | "dA8K3mF..." |
| `device_name` | string | ❌ Optional | Human-readable device name from iOS | "John's iPhone 14 Pro" |
| `device_type` | string | ❌ Optional | Device type identifier | "ios", "android" |
| `timestamp` | string | ✅ Yes | When FCM token was registered (UTC, ISO 8601) | "2025-11-19T20:55:37.123456" |

---

## Real-World Examples

### Example 1: Initial Registration

```json
{
  "uid": "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
  "fcm_token": "dA8K3mF2pQ4nRs7vXz1cYb6jHl9tWe5kPo8mNr3qGu4hVx2fCy7sAz0wDe1lBn6jMp9oTr3",
  "device_name": "Greg's iPhone 15 Pro",
  "device_type": "ios",
  "timestamp": "2025-11-19T20:55:37.456789"
}
```

**Context**:
- User first time opening iOS app
- Firebase Auth creates new user account
- iOS app registers for push notifications
- Backend sends webhook to n8n

**n8n Action**: Store UID → FCM token mapping in database

---

### Example 2: Re-Registration with NEW UID (Critical Case)

```json
{
  "uid": "HbBdbnRkPJhpYFIIsd34krM8FKD3",
  "fcm_token": "eB9L4nG3qR5oSt8wYa2dZc7kIm0uXf6lQp9nOs4rHv5iWy3gDz8tBa1xEf2mCo7kNq0pUs4",
  "device_name": "Greg's iPhone 15 Pro",
  "device_type": "ios",
  "timestamp": "2025-11-19T21:30:12.789012"
}
```

**Context**:
- Same physical device (same device_name)
- BUT different UID (Firebase Auth created new account)
- Old UID: `5aGC5YE9BnhcSoTxxtT4ar6ILQy2`
- New UID: `HbBdbnRkPJhpYFIIsd34krM8FKD3`

**n8n Action**:
1. Detect device_name matches existing device
2. Migrate Letta agent data from old UID to new UID
3. Update notification callbacks to use new UID
4. Optionally: Link old UID → new UID for conversation history

---

### Example 3: Token Refresh (Same UID)

```json
{
  "uid": "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
  "fcm_token": "fC0M5oH4rS6pTu9xZb3eAd8lJn1vYg7mRq0oPs5sIw6jXz4hEa9uCb2yFg3nDp8lOr1qVt5",
  "device_name": "Greg's iPhone 15 Pro",
  "device_type": "ios",
  "timestamp": "2025-11-20T08:15:42.123456"
}
```

**Context**:
- Same UID as before
- NEW FCM token (FCM token rotated by Firebase)
- Device name unchanged

**n8n Action**: Update FCM token for existing UID (simple update, no migration)

---

## Integration Flow

```
iOS App Opens
      ↓
Firebase Auth (may create NEW UID if fresh install)
      ↓
iOS requests FCM token
      ↓
iOS calls: POST /v1/users/fcm-token
      ↓
Backend stores in Firestore
      ↓
Backend → POST https://n8n.ella-ai-care.com/webhook/fcm-token-update ← YOU ARE HERE
      ↓
n8n Workflow:
  1. Check if device_name already known
  2. If known + different UID → MIGRATION NEEDED
  3. If known + same UID → UPDATE FCM token
  4. If unknown → NEW user, store mapping
      ↓
Letta Database:
  - users/{uid}/fcm_token → Updated
  - users/{uid}/device_info → Updated
  - (If migration) agent_data migrated from old_uid to new_uid
      ↓
Future notifications use correct UID
```

---

## Processing Guidelines for n8n

### Detection Logic

**New User** (device_name never seen before):
```javascript
// n8n workflow pseudocode
if (!existingDevice) {
  // Store new user
  db.upsert({
    uid: request.uid,
    fcm_token: request.fcm_token,
    device_name: request.device_name,
    first_seen: request.timestamp,
    last_seen: request.timestamp
  });
}
```

**UID Migration** (device_name exists but UID changed):
```javascript
if (existingDevice && existingDevice.uid !== request.uid) {
  console.log(`🔄 UID MIGRATION DETECTED`);
  console.log(`  Device: ${request.device_name}`);
  console.log(`  Old UID: ${existingDevice.uid}`);
  console.log(`  New UID: ${request.uid}`);

  // Migrate Letta agent data
  await migrateLettaAgent(existingDevice.uid, request.uid);

  // Update database
  db.upsert({
    uid: request.uid,
    fcm_token: request.fcm_token,
    device_name: request.device_name,
    previous_uid: existingDevice.uid,
    migrated_at: request.timestamp
  });
}
```

**Token Refresh** (same UID, new FCM token):
```javascript
if (existingDevice && existingDevice.uid === request.uid) {
  // Simple update
  db.update({
    uid: request.uid,
    fcm_token: request.fcm_token,
    last_seen: request.timestamp
  });
}
```

### Device Identification Strategy

**Primary Key**: `device_name` (human-readable, stable across reinstalls)

**Why not FCM token?**
- FCM tokens change frequently (rotation, expiration)
- Can't use as stable identifier

**Why not UID?**
- UID can change if user reinstalls app (Firebase Anonymous Auth creates new account)
- This is the PROBLEM we're solving

**Why device_name?**
- User-provided, stable across reinstalls
- e.g., "Greg's iPhone 15 Pro" stays the same
- iOS typically keeps same device name unless user manually changes

**Alternative**: Use device hardware identifier if iOS team can provide
- e.g., IDFV (Identifier for Vendor) - stable per app vendor
- More reliable than device_name

---

## Error Handling

### Backend Behavior

- **Timeout**: 2 seconds (non-blocking)
- **Failure Mode**: Silent (does not break FCM registration)
- **Retry Logic**: None (fire-and-forget)
- **Success**: Logs `📱 Notified n8n of FCM token update for uid=...`
- **Failure**: Logs `⚠️  Failed to notify n8n of FCM token update: {error}`

**Important**: Webhook failure does NOT prevent FCM token registration. iOS app will work even if n8n is down.

### Expected n8n Response

- **Status Code**: 200 OK (any response is accepted)
- **Response Body**: Ignored by backend
- **Processing Time**: Should be < 1 second (no blocking operations)

---

## Testing

### Manual Test (Simulate iOS Re-Registration)

```bash
# Test 1: Initial registration
curl -X POST "https://api.ella-ai-care.com/v1/users/fcm-token" \
  -H "Authorization: Bearer {FIREBASE_ID_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "fcm_token": "test_token_abc123",
    "device_name": "Test iPhone",
    "device_type": "ios"
  }'

# Test 2: Re-register with DIFFERENT UID (new Firebase Auth)
# (Requires creating new Firebase user account)
curl -X POST "https://api.ella-ai-care.com/v1/users/fcm-token" \
  -H "Authorization: Bearer {NEW_FIREBASE_ID_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "fcm_token": "test_token_xyz789",
    "device_name": "Test iPhone",
    "device_type": "ios"
  }'
```

**Expected n8n behavior**: Detect device_name match, migrate from old UID to new UID

---

## Monitoring

### Backend Logs

```bash
# Watch FCM token registrations in production
ssh root@100.101.168.91 'journalctl -u omi-backend -f | grep "📱 Notified n8n"'

# Example output:
# 📱 Notified n8n of FCM token update for uid=5aGC5YE9BnhcSoTxxtT4ar6ILQy2: 200
# ⚠️  Failed to notify n8n of FCM token update: Connection timeout
```

### n8n Workflow Logs

Check n8n workflow execution logs for:
- Successful webhook reception
- UID migration detection
- Letta agent data migration status

---

## Migration Strategy for Letta Agents

When UID changes, n8n should migrate:

1. **Conversation History**:
   ```sql
   UPDATE conversations SET uid = new_uid WHERE uid = old_uid
   ```

2. **Agent State**:
   ```sql
   UPDATE letta_agents SET uid = new_uid WHERE uid = old_uid
   ```

3. **Memory Store**:
   ```sql
   UPDATE memories SET uid = new_uid WHERE uid = old_uid
   ```

4. **Notification Preferences**:
   ```sql
   UPDATE notification_settings SET uid = new_uid WHERE uid = old_uid
   ```

5. **Link Old UID → New UID** (for debugging):
   ```sql
   INSERT INTO uid_migrations (old_uid, new_uid, device_name, migrated_at)
   VALUES (old_uid, new_uid, device_name, timestamp)
   ```

---

## iOS App Fix (Long-Term Solution)

**Root Cause**: UIDs shouldn't change for same user

**iOS Team TODO**:
1. Use Firebase **Persistent Auth** instead of Anonymous Auth
2. Store Firebase Auth credentials in iOS Keychain (survives app uninstall)
3. Re-authenticate on app launch instead of creating new account
4. Provide stable device identifier (IDFV) in registration payload

**With iOS fix**: UIDs will be stable, migration won't be needed

---

## Deployment Status

**Backend**: ✅ Implemented (lines 63-85 in `routers/notifications.py`)
**n8n Workflow**: ⏳ Pending (needs workflow creation)
**Webhook URL**: `https://n8n.ella-ai-care.com/webhook/fcm-token-update`

---

## Change History

| Date | Version | Changes |
|------|---------|---------|
| 2025-11-19 | 1.0 | Initial implementation - FCM token update webhook |

---

## Contact

- **Backend Team**: See `backend/CLAUDE.md` for architecture details
- **iOS Team**: See `app/CLAUDE.md` for Firebase Auth best practices
- **n8n Team**: Create workflow for `/webhook/fcm-token-update` endpoint

---

**Production URL**: https://n8n.ella-ai-care.com/webhook/fcm-token-update
**Status**: ⏳ Backend ready, n8n workflow pending
**Purpose**: Solve UID mismatch issue when iOS app re-registers
