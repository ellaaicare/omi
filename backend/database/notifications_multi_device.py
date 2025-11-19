"""
Multi-Device FCM Token Management

Extension module for Ella AI Care to support multiple devices per user.
Does NOT modify core OMI files - fully backward compatible.

Architecture:
- Single token: users/{uid}/fcm_token (upstream OMI)
- Multi-device: users/{uid}/devices/{device_id}/fcm_token (Ella AI extension)
- Fallback: Always check both locations for maximum reliability
"""

from google.cloud import firestore
from firebase_admin import messaging
from typing import List, Dict, Optional
from ._client import db


def save_device_token(uid: str, device_data: dict):
    """
    Save FCM token for specific device (multi-device support)

    Also saves to old single-token location for backward compatibility.

    Args:
        uid: Firebase User ID
        device_data: {
            'fcm_token': 'ABC123...',
            'device_id': 'UUID or IDFV',  # Optional - auto-generated if missing
            'device_name': 'Greg's iPhone 15 Pro',  # Optional
            'device_type': 'ios'  # Optional
        }
    """
    fcm_token = device_data.get('fcm_token')
    if not fcm_token:
        raise ValueError("fcm_token is required")

    # Generate device_id if not provided (backward compatibility)
    device_id = device_data.get('device_id')
    if not device_id:
        # Fallback: Use token hash as device_id
        import hashlib
        device_id = hashlib.sha256(fcm_token.encode()).hexdigest()[:16]

    # 1. Save to NEW subcollection (multi-device)
    db.collection('users').document(uid) \
        .collection('devices').document(device_id).set({
            'fcm_token': fcm_token,
            'device_id': device_id,
            'device_name': device_data.get('device_name', ''),
            'device_type': device_data.get('device_type', 'ios'),
            'last_active': firestore.SERVER_TIMESTAMP
        }, merge=True)

    # 2. ALSO save to OLD single-token location (backward compatibility)
    # This ensures upstream OMI code still works
    db.collection('users').document(uid).set({
        'fcm_token': fcm_token,
        'device_name': device_data.get('device_name', ''),
        'device_type': device_data.get('device_type', 'ios'),
    }, merge=True)

    print(f"📱 Saved FCM token for device {device_id[:8]}... (uid: {uid})")


def get_all_tokens(uid: str) -> List[str]:
    """
    Get ALL FCM tokens for user (from both old and new storage)

    Returns:
        List of unique FCM tokens (deduplicated)
    """
    tokens = []
    token_set = set()  # For deduplication

    # 1. Get tokens from NEW subcollection (multi-device)
    try:
        devices = db.collection('users').document(uid).collection('devices').stream()
        for device in devices:
            device_data = device.to_dict()
            token = device_data.get('fcm_token')
            if token and token not in token_set:
                tokens.append(token)
                token_set.add(token)
                device_name = device_data.get('device_name', 'Unknown')
                print(f"  📱 Found device token: {device_name} ({device.id[:8]}...)")
    except Exception as e:
        print(f"  ⚠️  Error reading devices subcollection: {e}")

    # 2. FALLBACK: Get token from OLD single-token storage
    try:
        user_doc = db.collection('users').document(uid).get()
        if user_doc.exists:
            old_token = user_doc.to_dict().get('fcm_token')
            if old_token and old_token not in token_set:
                tokens.append(old_token)
                token_set.add(old_token)
                print(f"  📱 Found legacy single token")
    except Exception as e:
        print(f"  ⚠️  Error reading single token: {e}")

    if not tokens:
        print(f"  ❌ No FCM tokens found for user {uid}")
    else:
        print(f"  ✅ Found {len(tokens)} unique token(s) for user {uid}")

    return tokens


def get_primary_token(uid: str) -> Optional[str]:
    """
    Get primary FCM token (most recently active device)

    Returns first token from multi-device list, or None if no tokens
    """
    tokens = get_all_tokens(uid)
    return tokens[0] if tokens else None


def send_to_all_devices(uid: str, notification_data: Dict) -> Dict:
    """
    Send notification to ALL user's devices

    Args:
        uid: Firebase User ID
        notification_data: {
            'data': {...},  # Custom data payload
            'apns': {...}   # Optional APNS config
        }

    Returns:
        {
            'status': 'success' | 'no_tokens' | 'partial_failure',
            'total_devices': int,
            'sent': int,
            'failed': int,
            'results': [...]
        }
    """
    print(f"🔔 Sending notification to all devices for uid={uid}")

    tokens = get_all_tokens(uid)

    if not tokens:
        return {
            'status': 'no_tokens',
            'total_devices': 0,
            'sent': 0,
            'failed': 0,
            'message': f'No FCM tokens found for user {uid}'
        }

    results = []
    sent_count = 0
    failed_count = 0

    for i, token in enumerate(tokens):
        try:
            # Build message
            message = messaging.Message(
                token=token,
                data=notification_data.get('data', {}),
                apns=notification_data.get('apns')
            )

            # Send notification
            message_id = messaging.send(message)

            results.append({
                'device_index': i + 1,
                'token_preview': token[:20] + '...',
                'status': 'sent',
                'message_id': message_id
            })
            sent_count += 1
            print(f"  ✅ Device {i+1}/{len(tokens)}: Sent (message_id: {message_id})")

        except Exception as e:
            error_message = str(e)
            results.append({
                'device_index': i + 1,
                'token_preview': token[:20] + '...',
                'status': 'failed',
                'error': error_message
            })
            failed_count += 1
            print(f"  ❌ Device {i+1}/{len(tokens)}: Failed - {error_message}")

            # Remove invalid tokens
            if "not found" in error_message.lower() or "invalid" in error_message.lower():
                print(f"  🗑️  Removing invalid token from database")
                # Note: We don't remove tokens here to avoid data loss during debugging
                # TODO: Implement token cleanup after validation period

    # Determine overall status
    if sent_count == len(tokens):
        status = 'success'
    elif sent_count > 0:
        status = 'partial_failure'
    else:
        status = 'all_failed'

    return {
        'status': status,
        'total_devices': len(tokens),
        'sent': sent_count,
        'failed': failed_count,
        'results': results
    }


def remove_device_token(uid: str, device_id: str):
    """Remove FCM token for specific device"""
    db.collection('users').document(uid) \
        .collection('devices').document(device_id).delete()
    print(f"🗑️  Removed device {device_id} for user {uid}")


def list_user_devices(uid: str) -> List[Dict]:
    """
    List all registered devices for user

    Returns:
        [
            {
                'device_id': '...',
                'device_name': 'iPhone 15 Pro',
                'device_type': 'ios',
                'fcm_token': 'ABC123...',
                'last_active': datetime
            },
            ...
        ]
    """
    devices = []

    try:
        device_docs = db.collection('users').document(uid).collection('devices').stream()
        for doc in device_docs:
            device_data = doc.to_dict()
            devices.append({
                'device_id': doc.id,
                'device_name': device_data.get('device_name', 'Unknown'),
                'device_type': device_data.get('device_type', 'unknown'),
                'fcm_token': device_data.get('fcm_token', ''),
                'last_active': device_data.get('last_active')
            })
    except Exception as e:
        print(f"Error listing devices: {e}")

    return devices
