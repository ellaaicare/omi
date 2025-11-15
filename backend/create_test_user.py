#!/usr/bin/env python3
"""
Create a test user in Firestore for LOCAL_DEVELOPMENT mode.
This user will have UID '123' and minimal profile data.
"""
import os
from dotenv import load_dotenv
from google.cloud import firestore

load_dotenv()

# Ensure Google credentials are set
if not os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.join(
        os.path.dirname(__file__),
        'google-credentials.json'
    )

# Initialize Firestore
db = firestore.Client()

# Test user data
test_uid = '123'
test_user_data = {
    'id': test_uid,
    'name': 'Test User',
    'email': 'test@omi.dev',
    'created_at': firestore.SERVER_TIMESTAMP,
    'updated_at': firestore.SERVER_TIMESTAMP,
   'transcription_credits': 10000,  # Give plenty of credits for testing
    'plan': 'basic',
}

print(f"🔨 Creating test user with UID '{test_uid}'...")

try:
    # Create or update test user
    user_ref = db.collection('users').document(test_uid)
    user_ref.set(test_user_data, merge=True)

    print(f"✅ Test user created successfully!")
    print(f"   UID: {test_uid}")
    print(f"   Name: {test_user_data['name']}")
    print(f"   Email: {test_user_data['email']}")
    print(f"   Credits: {test_user_data['transcription_credits']}")
    print(f"\n📌 You can now use LOCAL_DEVELOPMENT=true in .env for testing")
    print(f"   The backend will use this user (UID '123') for all requests")

except Exception as e:
    print(f"❌ Error creating test user: {e}")
    exit(1)
