#!/usr/bin/env python3
"""
Comprehensive E2E Test for n8n v5.0 Memory + Summary Agents

Tests the full flow:
1. Create real conversation in Firestore
2. Send to n8n memory agent
3. Send to n8n summary agent
4. Verify callbacks received
5. Verify iOS can poll data
6. Test edge cases (garbage filtering)
"""

import os
import sys
import time
import requests
import uuid
from datetime import datetime, timezone
from google.cloud import firestore

# Setup
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = './google-credentials.json'
db = firestore.Client()

UID = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"
N8N_BASE = "https://n8n.ella-ai-care.com/webhook"
BACKEND_BASE = "https://api.ella-ai-care.com"

def print_section(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)

def create_test_conversation(conv_id, segments, title="Test Conversation"):
    """Create a conversation in Firestore"""
    conv_ref = db.collection('users').document(UID).collection('conversations').document(conv_id)

    conversation_data = {
        'id': conv_id,
        'created_at': datetime.now(timezone.utc),
        'started_at': datetime.now(timezone.utc),
        'finished_at': datetime.now(timezone.utc),
        'status': 'in_progress',  # Will be updated to 'completed' by summary callback
        'transcript': ' '.join([s['text'] for s in segments]),
        'transcript_segments': segments,
        'discarded': False,
        'source': 'test',
        'language': 'en',
    }

    conv_ref.set(conversation_data)
    print(f"✅ Created conversation: {conv_id}")
    return conv_id

def call_memory_agent(conv_id, segments):
    """Send conversation to n8n memory agent"""
    print(f"\n📤 Calling memory agent...")

    response = requests.post(
        f"{N8N_BASE}/memory-agent",
        json={
            "uid": UID,
            "conversation_id": conv_id,
            "segments": [
                {"speaker": s['speaker'], "text": s['text'], "stt_source": "test"}
                for s in segments
            ]
        },
        timeout=30
    )

    print(f"   Status: {response.status_code}")
    if response.status_code == 200:
        result = response.json()
        memories = result.get('memories', [])
        print(f"   ✅ Returned {len(memories)} memories")
        for i, mem in enumerate(memories, 1):
            print(f"      {i}. {mem.get('content', 'N/A')[:60]}...")
        return memories
    else:
        print(f"   ❌ Error: {response.text}")
        return []

def call_summary_agent(conv_id, transcript):
    """Send conversation to n8n summary agent"""
    print(f"\n📤 Calling summary agent...")

    response = requests.post(
        f"{N8N_BASE}/summary-agent",
        json={
            "uid": UID,
            "conversation_id": conv_id,
            "transcript": transcript,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "language_code": "en"
        },
        timeout=30
    )

    print(f"   Status: {response.status_code}")
    if response.status_code == 200:
        result = response.json()
        print(f"   ✅ Title: {result.get('title', 'N/A')}")
        print(f"   ✅ Category: {result.get('category', 'N/A')}")
        print(f"   ✅ Emoji: {result.get('emoji', 'N/A')}")
        return result
    else:
        print(f"   ❌ Error: {response.text}")
        return {}

def verify_memories_in_firestore(conv_id):
    """Check if memories were stored in Firestore"""
    print(f"\n🔍 Verifying memories in Firestore...")

    memories = db.collection('users').document(UID) \
        .collection('memories') \
        .where('conversation_id', '==', conv_id) \
        .stream()

    memory_list = list(memories)
    print(f"   ✅ Found {len(memory_list)} memories")

    category_count = {}
    for mem in memory_list:
        data = mem.to_dict()
        cat = data.get('category', 'N/A')
        category_count[cat] = category_count.get(cat, 0) + 1

    print(f"   Category breakdown: {category_count}")
    return len(memory_list)

def verify_conversation_in_firestore(conv_id):
    """Check if conversation was updated with summary"""
    print(f"\n🔍 Verifying conversation in Firestore...")

    conv_ref = db.collection('users').document(UID) \
        .collection('conversations').document(conv_id)
    conv_doc = conv_ref.get()

    if conv_doc.exists:
        data = conv_doc.to_dict()
        structured = data.get('structured', {})
        status = data.get('status', 'N/A')

        print(f"   ✅ Conversation found")
        print(f"   Status: {status}")
        print(f"   Title: {structured.get('title', 'N/A')}")
        print(f"   Category: {structured.get('category', 'N/A')}")

        if status == 'completed' and structured:
            return True
        else:
            print(f"   ⚠️  Status not 'completed' or missing structured data")
            return False
    else:
        print(f"   ❌ Conversation not found")
        return False

def test_ios_polling():
    """Simulate iOS app polling for conversations and memories"""
    print(f"\n📱 Simulating iOS app polling...")

    # Get conversations
    response = requests.get(
        f"{BACKEND_BASE}/v1/conversations",
        params={"limit": 5, "statuses": "processing,completed"},
        headers={"uid": UID}  # In production, uses Firebase Auth
    )

    if response.status_code == 200:
        conversations = response.json()
        print(f"   ✅ iOS would see {len(conversations)} recent conversations")
    else:
        print(f"   ❌ Failed to get conversations: {response.status_code}")

    # Get memories
    response = requests.get(
        f"{BACKEND_BASE}/v3/memories",
        params={"limit": 10},
        headers={"uid": UID}
    )

    if response.status_code == 200:
        memories = response.json()
        print(f"   ✅ iOS would see {len(memories)} recent memories")
    else:
        print(f"   ❌ Failed to get memories: {response.status_code}")

# ============================================================================
# TEST CASES
# ============================================================================

def test_case_1_normal_conversation():
    """Test 1: Normal health conversation (should create memories + summary)"""
    print_section("TEST 1: Normal Health Conversation")

    conv_id = f"test-e2e-normal-{int(time.time())}"
    segments = [
        {"speaker": "User", "text": "I've been taking vitamin D supplements, 2000 IU daily."},
        {"speaker": "Friend", "text": "That's good! Have you had your vitamin D levels checked?"},
        {"speaker": "User", "text": "Yes, my doctor recommended it after my last checkup."},
        {"speaker": "User", "text": "I also need to schedule my annual physical for next month."},
    ]

    # Create conversation
    create_test_conversation(conv_id, segments)

    # Call n8n agents
    time.sleep(2)
    transcript = ' '.join([s['text'] for s in segments])

    call_memory_agent(conv_id, segments)
    time.sleep(5)  # Wait for Letta processing

    call_summary_agent(conv_id, transcript)
    time.sleep(5)  # Wait for Letta processing

    # Verify results
    mem_count = verify_memories_in_firestore(conv_id)
    conv_updated = verify_conversation_in_firestore(conv_id)

    print(f"\n✅ Test 1 Result:")
    print(f"   Memories created: {mem_count} (expected: 2-4)")
    print(f"   Conversation updated: {conv_updated}")

    return mem_count > 0 and conv_updated

def test_case_2_background_noise():
    """Test 2: Background noise / TV dialogue (should create ZERO or minimal memories)"""
    print_section("TEST 2: Background Noise (Garbage Filtering)")

    conv_id = f"test-e2e-noise-{int(time.time())}"
    segments = [
        {"speaker": "TV", "text": "Coming up next on the news..."},
        {"speaker": "TV", "text": "The weather today will be sunny."},
        {"speaker": "Music", "text": "I love this song!"},
    ]

    create_test_conversation(conv_id, segments)
    time.sleep(2)

    call_memory_agent(conv_id, segments)
    time.sleep(5)

    mem_count = verify_memories_in_firestore(conv_id)

    print(f"\n✅ Test 2 Result:")
    print(f"   Memories created: {mem_count} (expected: 0)")
    print(f"   PASS: {mem_count == 0}")

    return mem_count == 0

def test_case_3_pointless_small_talk():
    """Test 3: Pointless small talk (should create minimal/zero memories)"""
    print_section("TEST 3: Pointless Small Talk")

    conv_id = f"test-e2e-smalltalk-{int(time.time())}"
    segments = [
        {"speaker": "User", "text": "Nice weather today."},
        {"speaker": "Friend", "text": "Yeah, it's sunny."},
        {"speaker": "User", "text": "Yep."},
    ]

    create_test_conversation(conv_id, segments)
    time.sleep(2)

    call_memory_agent(conv_id, segments)
    time.sleep(5)

    mem_count = verify_memories_in_firestore(conv_id)

    print(f"\n✅ Test 3 Result:")
    print(f"   Memories created: {mem_count} (expected: 0-1)")
    print(f"   PASS: {mem_count <= 1}")

    return mem_count <= 1

def test_case_4_interesting_vs_system():
    """Test 4: Mix of interesting facts and system details"""
    print_section("TEST 4: Interesting vs System Categories")

    conv_id = f"test-e2e-categories-{int(time.time())}"
    segments = [
        {"speaker": "Friend", "text": "Did you know that octopuses have three hearts?"},  # Interesting
        {"speaker": "User", "text": "No way! That's fascinating!"},
        {"speaker": "User", "text": "By the way, I need to buy milk tomorrow."},  # System
        {"speaker": "Friend", "text": "Cool. I'm going to the gym at 5pm."},  # System
    ]

    create_test_conversation(conv_id, segments)
    time.sleep(2)

    call_memory_agent(conv_id, segments)
    time.sleep(5)

    mem_count = verify_memories_in_firestore(conv_id)

    # Check category distribution
    memories = db.collection('users').document(UID) \
        .collection('memories') \
        .where('conversation_id', '==', conv_id) \
        .stream()

    categories = [m.to_dict().get('category') for m in memories]
    interesting_count = categories.count('interesting')
    system_count = categories.count('system')

    print(f"\n✅ Test 4 Result:")
    print(f"   Total memories: {mem_count}")
    print(f"   Interesting: {interesting_count}")
    print(f"   System: {system_count}")
    print(f"   Expected: At least 1 interesting (octopus fact)")

    return interesting_count >= 1

def test_case_5_long_conversation():
    """Test 5: Very long conversation (should have reasonable memory limit)"""
    print_section("TEST 5: Long Conversation (Memory Limit)")

    conv_id = f"test-e2e-long-{int(time.time())}"

    # Generate 50 segments of varied content
    segments = []
    for i in range(50):
        segments.append({
            "speaker": "User" if i % 2 == 0 else "Friend",
            "text": f"This is segment {i+1} with some random content about topic {i}."
        })

    create_test_conversation(conv_id, segments)
    time.sleep(2)

    call_memory_agent(conv_id, segments)
    time.sleep(5)

    mem_count = verify_memories_in_firestore(conv_id)

    print(f"\n✅ Test 5 Result:")
    print(f"   Memories created: {mem_count}")
    print(f"   Expected: < 20 (reasonable limit)")
    print(f"   PASS: {mem_count < 20}")

    return mem_count < 20

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    print_section("N8N V5.0 COMPREHENSIVE E2E TEST SUITE")
    print(f"UID: {UID}")
    print(f"n8n: {N8N_BASE}")
    print(f"Backend: {BACKEND_BASE}")

    results = {}

    # Run all tests
    try:
        results['test_1_normal'] = test_case_1_normal_conversation()
        time.sleep(3)

        results['test_2_noise'] = test_case_2_background_noise()
        time.sleep(3)

        results['test_3_smalltalk'] = test_case_3_pointless_small_talk()
        time.sleep(3)

        results['test_4_categories'] = test_case_4_interesting_vs_system()
        time.sleep(3)

        results['test_5_long'] = test_case_5_long_conversation()
        time.sleep(3)

        # Test iOS polling
        test_ios_polling()

    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Summary
    print_section("TEST SUMMARY")

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {test_name}")

    print(f"\nOverall: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 ALL TESTS PASSED! n8n v5.0 is production-ready.")
        sys.exit(0)
    else:
        print(f"\n⚠️  {total - passed} tests failed. Review Letta agent configuration.")
        sys.exit(1)
