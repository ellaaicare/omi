#!/usr/bin/env python3
"""
Test script for Inbound Calls feature.

Sends a test push notification with action: "incoming_call" to trigger
the iOS incoming call UI.

Usage:
    python3 test_incoming_call.py --uid USER_ID [--reason medication_reminder]

Requirements:
    - pip install requests firebase-admin
    - Firebase service account JSON (or use ADMIN_KEY)

Examples:
    # Using ADMIN_KEY (simpler)
    python3 test_incoming_call.py --uid abc123 --admin-key YOUR_ADMIN_KEY

    # Test medication reminder
    python3 test_incoming_call.py --uid abc123 --reason medication_reminder

    # Test urgent call (auto-answer)
    python3 test_incoming_call.py --uid abc123 --reason urgent --priority urgent --auto-answer
"""

import argparse
import json
import requests
import uuid
from datetime import datetime

# Backend API
API_BASE_URL = "https://api.ella-ai-care.com"

# Predefined call reasons with display text and voicemail
CALL_REASONS = {
    "medication_reminder": {
        "display": "Medication Reminder",
        "voicemail": "Hi! I wanted to remind you about your medication. Please take it when you get a chance, and let me know if you have any questions!"
    },
    "check_in": {
        "display": "Daily Check-in",
        "voicemail": "Hi! Just checking in to see how you're doing today. Call me back when you have a moment!"
    },
    "urgent": {
        "display": "Urgent Alert",
        "voicemail": "Hi, this is an urgent message. Please call me back as soon as possible."
    },
    "follow_up": {
        "display": "Follow-up Call",
        "voicemail": "Hi! I wanted to follow up on our earlier conversation. Call me back when you're free!"
    },
    "test": {
        "display": "Test Call",
        "voicemail": "This is a test call from Ella. If you're seeing this, the incoming call feature is working!"
    }
}


def send_push_notification(uid: str, payload: dict, admin_key: str = None) -> dict:
    """
    Send push notification via backend API.

    Uses existing push infrastructure - just need to add the right payload.
    """
    url = f"{API_BASE_URL}/v1/users/{uid}/push"

    headers = {
        "Content-Type": "application/json",
    }

    if admin_key:
        headers["Authorization"] = f"Bearer {admin_key}"

    print(f"\n📤 Sending push to {url}")
    print(f"   Payload: {json.dumps(payload, indent=2)}")

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"\n📥 Response: {response.status_code}")
        if response.text:
            print(f"   Body: {response.text[:500]}")
        return {"status": response.status_code, "body": response.text}
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return {"status": 0, "error": str(e)}


def build_incoming_call_payload(
    reason: str = "test",
    priority: str = "normal",
    auto_answer: bool = False,
    timeout_seconds: int = 30,
    custom_voicemail: str = None
) -> dict:
    """Build the incoming call push notification payload."""

    reason_config = CALL_REASONS.get(reason, CALL_REASONS["test"])
    call_id = f"call-{uuid.uuid4().hex[:8]}-{int(datetime.now().timestamp())}"

    return {
        "notification": {
            "title": "Ella is calling",
            "body": reason_config["display"]
        },
        "data": {
            "action": "incoming_call",
            "call_id": call_id,
            "reason": reason,
            "reason_display": reason_config["display"],
            "priority": priority,
            "auto_answer": auto_answer,
            "timeout_seconds": timeout_seconds,
            "voicemail_text": custom_voicemail or reason_config["voicemail"],
            # Optional: pre-generated voicemail audio URL
            # "voicemail_audio_url": "https://storage.googleapis.com/.../voicemail.mp3"
        }
    }


def main():
    parser = argparse.ArgumentParser(
        description="Test incoming call push notifications",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic test
  python3 test_incoming_call.py --uid USER_ID --admin-key KEY

  # Medication reminder
  python3 test_incoming_call.py --uid USER_ID --reason medication_reminder

  # Urgent auto-answer call
  python3 test_incoming_call.py --uid USER_ID --reason urgent --priority urgent --auto-answer

Available reasons: medication_reminder, check_in, urgent, follow_up, test
        """
    )

    parser.add_argument("--uid", required=True, help="User ID to send push to")
    parser.add_argument("--admin-key", help="Admin API key for authentication")
    parser.add_argument("--reason", default="test", choices=CALL_REASONS.keys(),
                       help="Call reason (default: test)")
    parser.add_argument("--priority", default="normal", choices=["normal", "high", "urgent"],
                       help="Call priority (default: normal)")
    parser.add_argument("--auto-answer", action="store_true",
                       help="Auto-answer the call (for urgent)")
    parser.add_argument("--timeout", type=int, default=30,
                       help="Timeout in seconds (default: 30)")
    parser.add_argument("--voicemail", help="Custom voicemail text")
    parser.add_argument("--dry-run", action="store_true",
                       help="Print payload without sending")

    args = parser.parse_args()

    # Build payload
    payload = build_incoming_call_payload(
        reason=args.reason,
        priority=args.priority,
        auto_answer=args.auto_answer,
        timeout_seconds=args.timeout,
        custom_voicemail=args.voicemail
    )

    print("\n" + "=" * 60)
    print("📞 INCOMING CALL TEST")
    print("=" * 60)
    print(f"\nTarget User: {args.uid}")
    print(f"Reason: {args.reason} ({CALL_REASONS[args.reason]['display']})")
    print(f"Priority: {args.priority}")
    print(f"Auto-answer: {args.auto_answer}")
    print(f"Timeout: {args.timeout}s")

    if args.dry_run:
        print("\n🔍 DRY RUN - Payload:")
        print(json.dumps(payload, indent=2))
        print("\nTo send for real, remove --dry-run flag")
        return

    # Send push
    result = send_push_notification(args.uid, payload, args.admin_key)

    if result.get("status") == 200:
        print("\n✅ Push sent successfully!")
        print(f"   Call ID: {payload['data']['call_id']}")
        print("\n📱 Check your iOS device for the incoming call UI")
        print("   - Say 'Answer' or 'Yes' to accept")
        print("   - Say 'Decline' or 'No' to play voicemail")
        print("   - Or tap the buttons")
    else:
        print(f"\n❌ Failed to send push: {result}")


if __name__ == "__main__":
    main()
