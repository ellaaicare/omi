# Custom Backend Infrastructure Setup

This guide explains how to configure the Omi Flutter app to use your own custom backend infrastructure instead of the default Omi servers.

## Overview

The app now supports runtime configuration of the API base URL through the Developer Settings interface. This allows you to:

- Point the app to your own self-hosted backend
- Test against local development servers
- Use staging/production environments
- Maintain full control over your data infrastructure

## Features

✅ **Runtime Configuration**: No need to rebuild the app - just change the URL in settings
✅ **Persistent Storage**: Your custom URL is saved across app restarts
✅ **Full Coverage**: Applies to both HTTP API calls and WebSocket connections
✅ **Validation**: Built-in URL validation to prevent configuration errors
✅ **Easy Reset**: Clear the field to return to default Omi infrastructure

## How to Configure

### Step 1: Open Developer Settings

1. Launch the Omi app
2. Navigate to **Settings** → **Developer Settings**

### Step 2: Set Custom API Base URL

1. Scroll to the **Infrastructure** section
2. Enter your custom backend URL in the **Custom API Base URL** field
   - Example: `https://api.yourserver.com`
   - Example: `http://localhost:8000` (for local development)
3. Tap **Save** in the top-right corner

### Step 3: Restart the App

**Important**: You must restart the app for the changes to take effect.

1. Close the app completely
2. Reopen the app
3. The app will now use your custom backend

## URL Format Requirements

Your custom URL should:

- Include the protocol (`https://` or `http://`)
- **Not** include the trailing slash (it's added automatically)
- **Not** include API version paths like `/v1` or `/v3` (these are added by the app)
- Be accessible from your device's network

### Examples

✅ **Correct Format**:
```
https://api.yourserver.com
https://staging.yourserver.com
http://192.168.1.100:8000
http://localhost:8000
```

❌ **Incorrect Format**:
```
api.yourserver.com              (missing protocol)
https://api.yourserver.com/     (unnecessary trailing slash)
https://api.yourserver.com/v1   (includes API version path)
```

## Technical Implementation

### Files Modified

1. **`lib/backend/preferences.dart`**
   - Added `customApiBaseUrl` getter/setter for persistent storage

2. **`lib/env/env.dart`**
   - Modified `apiBaseUrl` getter to check SharedPreferences first
   - Automatically adds trailing slash for proper URL concatenation

3. **`lib/providers/developer_mode_provider.dart`**
   - Added `TextEditingController` for custom URL input
   - Added validation and save logic

4. **`lib/pages/settings/developer.dart`**
   - Added UI section for infrastructure configuration
   - Clear instructions and hints for users

### How It Works

When the app makes an API call:

1. `Env.apiBaseUrl` is accessed
2. The system checks `SharedPreferences` for a custom URL
3. If found and not empty, the custom URL is used
4. Otherwise, it falls back to the default URL from `.env` files
5. All API endpoints (HTTP and WebSocket) automatically use this URL

### URL Construction Example

```dart
// In your API file:
var response = await makeApiCall(
  url: '${Env.apiBaseUrl}v3/memories',  // Custom URL + API version + endpoint
  method: 'GET',
);

// If custom URL is "https://api.yourserver.com"
// Actual URL becomes: "https://api.yourserver.com/v3/memories"
```

### WebSocket Support

WebSocket connections automatically convert the custom URL:

```dart
// Original URL:     https://api.yourserver.com
// WebSocket URL:    wss://api.yourserver.com/v4/listen?...
```

## Backend Requirements

Your custom backend must implement the same API contract as the official Omi backend:

### Required Endpoints

- **Authentication**: Firebase Auth integration or compatible system
- **Conversations API**: `/v3/conversations/*`
- **Memories API**: `/v3/memories/*`
- **Messages API**: `/v1/messages/*`
- **WebSocket**: `/v4/listen` for real-time transcription
- **Webhooks**: Support for developer webhook configurations

### Environment Variables

Your backend should expose:
- Same API structure as Omi backend
- Compatible authentication mechanisms
- WebSocket support for transcription

## Troubleshooting

### App Can't Connect After Setting Custom URL

1. **Check URL format**: Ensure protocol is included (`https://` or `http://`)
2. **Network accessibility**: Verify your backend is accessible from your device
3. **Firewall rules**: Check if your backend allows connections from your IP
4. **HTTPS/HTTP mismatch**: If using HTTP, ensure your backend accepts HTTP

### WebSocket Connection Fails

1. Ensure your backend supports WebSocket connections
2. Check that your server's WebSocket path matches `/v4/listen`
3. Verify your firewall allows WebSocket upgrades

### Want to Reset to Default

1. Go to Developer Settings
2. Clear the **Custom API Base URL** field completely
3. Tap **Save**
4. Restart the app

## Security Considerations

⚠️ **Important Security Notes**:

- **HTTPS Recommended**: Always use HTTPS in production
- **Local Development**: HTTP is acceptable for `localhost` testing only
- **API Keys**: Ensure your backend validates authentication tokens
- **Network Security**: Use VPN or secure networks when testing

## Example: Local Development Setup

For developers running a local backend:

```bash
# Start your local backend on port 8000
./start_backend.sh

# In the Omi app:
# 1. Go to Developer Settings
# 2. Set Custom API Base URL to: http://localhost:8000
# 3. Save and restart the app
# 4. App now connects to your local backend
```

## Example: Production Deployment

For production self-hosted deployments:

```bash
# 1. Deploy your backend to your server
# 2. Configure DNS: api.yourcompany.com → your_server_ip
# 3. Set up SSL certificate (Let's Encrypt)

# In the Omi app:
# 1. Go to Developer Settings
# 2. Set Custom API Base URL to: https://api.yourcompany.com
# 3. Save and restart the app
# 4. App now uses your production backend
```

## API Compatibility

Ensure your backend implements these core features:

### Authentication
- Firebase ID token validation
- User session management
- OAuth support (Google, Apple)

### Real-time Features
- WebSocket support for transcription
- Server-sent events for notifications
- Webhook delivery for developer integrations

### Data APIs
- Conversation CRUD operations
- Memory management
- Message handling
- Action items
- Speech profiles

## Contributing

If you build a compatible backend implementation, please share it with the community!

## Support

For issues or questions:
- Check the official Omi documentation
- Review backend API specifications
- Join the Omi community Discord
- Open an issue on GitHub

---

**Last Updated**: October 2024
**Compatibility**: Omi App v1.0.74+
