#!/usr/bin/env python3
"""
Debug n8n Status Response - Verify Processing Status
Test what n8n is actually returning and where status might be getting lost
"""

import requests
import json
import time

N8N_BASE = "https://n8n.ella-ai-care.com/webhook"
UID = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"

def test_memory_agent_status():
    """Test what Memory Agent v5.0 actually returns"""
    print("=" * 70)
    print("TEST: Memory Agent v5.0 - Status Response Debug")
    print("=" * 70)

    payload = {
        "uid": UID,
        "conversation_id": f"status-test-mem-{int(time.time())}",
        "segments": [
            {
                "speaker": "User",
                "text": "I had lunch with Sarah today.",
                "stt_source": "test"
            }
        ]
    }

    print(f"\n📤 Sending to: {N8N_BASE}/memory-agent")
    print(f"   Payload: {json.dumps(payload, indent=2)}")

    start = time.time()
    response = requests.post(
        f"{N8N_BASE}/memory-agent",
        json=payload,
        timeout=30
    )
    elapsed = time.time() - start

    print(f"\n📥 Response received in {elapsed:.2f}s")
    print(f"   HTTP Status: {response.status_code}")
    print(f"   Headers: {dict(response.headers)}")
    print(f"   Content-Type: {response.headers.get('content-type', 'N/A')}")
    print(f"   Content-Length: {len(response.content)} bytes")

    print(f"\n📄 Raw Response Body:")
    print(f"   Text: {repr(response.text)}")
    print(f"   Bytes: {response.content}")

    if response.text:
        try:
            parsed = response.json()
            print(f"\n✅ Parsed JSON:")
            print(json.dumps(parsed, indent=2))

            # Check for status field
            if 'status' in parsed:
                print(f"\n✅ FOUND STATUS: {parsed['status']}")
                print(f"   uid: {parsed.get('uid', 'N/A')}")
                print(f"   conversation_id: {parsed.get('conversation_id', 'N/A')}")
                print(f"   message: {parsed.get('message', 'N/A')}")
            else:
                print(f"\n❌ NO STATUS FIELD IN RESPONSE")
                print(f"   Available keys: {list(parsed.keys())}")
        except json.JSONDecodeError as e:
            print(f"\n❌ JSON Parse Error: {e}")
    else:
        print(f"\n❌ EMPTY RESPONSE BODY")

    return response

def test_summary_agent_status():
    """Test what Summary Agent v5.0 actually returns"""
    print("\n\n" + "=" * 70)
    print("TEST: Summary Agent v5.0 - Status Response Debug")
    print("=" * 70)

    payload = {
        "uid": UID,
        "conversation_id": f"status-test-sum-{int(time.time())}",
        "transcript": "I had a great lunch with Sarah today. We discussed the project timeline.",
        "started_at": "2025-11-21T00:00:00Z",
        "language_code": "en"
    }

    print(f"\n📤 Sending to: {N8N_BASE}/summary-agent")
    print(f"   Payload: {json.dumps(payload, indent=2)}")

    start = time.time()
    response = requests.post(
        f"{N8N_BASE}/summary-agent",
        json=payload,
        timeout=30
    )
    elapsed = time.time() - start

    print(f"\n📥 Response received in {elapsed:.2f}s")
    print(f"   HTTP Status: {response.status_code}")
    print(f"   Headers: {dict(response.headers)}")
    print(f"   Content-Type: {response.headers.get('content-type', 'N/A')}")
    print(f"   Content-Length: {len(response.content)} bytes")

    print(f"\n📄 Raw Response Body:")
    print(f"   Text: {repr(response.text)}")
    print(f"   Bytes: {response.content}")

    if response.text:
        try:
            parsed = response.json()
            print(f"\n✅ Parsed JSON:")
            print(json.dumps(parsed, indent=2))

            # Check for status field
            if 'status' in parsed:
                print(f"\n✅ FOUND STATUS: {parsed['status']}")
                print(f"   uid: {parsed.get('uid', 'N/A')}")
                print(f"   conversation_id: {parsed.get('conversation_id', 'N/A')}")
                print(f"   message: {parsed.get('message', 'N/A')}")
            else:
                print(f"\n❌ NO STATUS FIELD IN RESPONSE")
                print(f"   Available keys: {list(parsed.keys())}")
        except json.JSONDecodeError as e:
            print(f"\n❌ JSON Parse Error: {e}")
    else:
        print(f"\n❌ EMPTY RESPONSE BODY")

    return response

def test_scanner_agent_status():
    """Test what Scanner Agent v5.12 returns (should be sync)"""
    print("\n\n" + "=" * 70)
    print("TEST: Scanner Agent v5.12 - Status Response Debug")
    print("=" * 70)

    payload = {
        "uid": UID,
        "device_type": "omi",
        "segments": [
            {
                "speaker": "User",
                "text": "I have chest pain and shortness of breath",
                "stt_source": "test"
            }
        ]
    }

    print(f"\n📤 Sending to: {N8N_BASE}/scanner-agent")
    print(f"   Payload: {json.dumps(payload, indent=2)}")

    start = time.time()
    response = requests.post(
        f"{N8N_BASE}/scanner-agent",
        json=payload,
        timeout=30
    )
    elapsed = time.time() - start

    print(f"\n📥 Response received in {elapsed:.2f}s")
    print(f"   HTTP Status: {response.status_code}")
    print(f"   Content-Type: {response.headers.get('content-type', 'N/A')}")
    print(f"   Content-Length: {len(response.content)} bytes")

    print(f"\n📄 Raw Response Body:")
    print(f"   Text: {repr(response.text)}")

    if response.text:
        try:
            parsed = response.json()
            print(f"\n✅ Parsed JSON:")
            print(json.dumps(parsed, indent=2))

            # Scanner should have urgency_level, not status
            if 'urgency_level' in parsed:
                print(f"\n✅ SCANNER SYNC RESPONSE:")
                print(f"   urgency_level: {parsed.get('urgency_level')}")
                print(f"   urgency_reason: {parsed.get('urgency_reason', 'N/A')}")
            elif 'status' in parsed:
                print(f"\n⚠️  UNEXPECTED: Scanner returning async status")
                print(f"   status: {parsed['status']}")
        except json.JSONDecodeError as e:
            print(f"\n❌ JSON Parse Error: {e}")
    else:
        print(f"\n❌ EMPTY RESPONSE BODY")

    return response

if __name__ == "__main__":
    print("🔍 N8N STATUS RESPONSE DEBUGGER")
    print("Testing what n8n actually returns vs what backend sees\n")

    # Test all three endpoints
    mem_response = test_memory_agent_status()
    time.sleep(2)

    sum_response = test_summary_agent_status()
    time.sleep(2)

    scan_response = test_scanner_agent_status()

    # Summary
    print("\n\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    print("\n✅ Expected Behavior:")
    print("   Memory Agent: {\"status\": \"processing\", \"uid\": \"...\", \"conversation_id\": \"...\"}")
    print("   Summary Agent: {\"status\": \"processing\", \"uid\": \"...\", \"conversation_id\": \"...\"}")
    print("   Scanner Agent: {\"urgency_level\": \"...\", \"urgency_reason\": \"...\"} (sync)")

    print("\n📊 Actual Results:")
    print(f"   Memory Agent: {'✅ Has status field' if mem_response.text and 'status' in mem_response.text else '❌ No status field'}")
    print(f"   Summary Agent: {'✅ Has status field' if sum_response.text and 'status' in sum_response.text else '❌ No status field'}")
    print(f"   Scanner Agent: {'✅ Has urgency_level' if scan_response.text and 'urgency_level' in scan_response.text else '❌ No urgency_level'}")

    print("\n🔍 If status field is missing, check:")
    print("   1. n8n workflow has HTTP Response node BEFORE Letta call")
    print("   2. Response node returns {\"status\": \"processing\"}")
    print("   3. Workflow is deployed (not just saved)")
    print("   4. Using correct webhook URL (v5.0, not v4.0)")
