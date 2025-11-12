"""
Pytest configuration and fixtures for backend tests.
"""
import os
import sys
from unittest.mock import MagicMock, patch
from typing import Generator

import pytest
from fastapi.testclient import TestClient

# Add backend to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(scope="session")
def test_env_vars():
    """Set up test environment variables."""
    os.environ['ENCRYPTION_SECRET'] = 'test_secret_key_for_encryption_1234567890abcdef'
    os.environ['REDIS_DB_HOST'] = 'localhost'
    os.environ['REDIS_DB_PORT'] = '6379'
    os.environ['REDIS_DB_PASSWORD'] = 'test_password'
    os.environ['GOOGLE_CLIENT_ID'] = 'test_google_client_id'
    os.environ['GOOGLE_CLIENT_SECRET'] = 'test_google_client_secret'
    os.environ['APPLE_CLIENT_ID'] = 'test_apple_client_id'
    os.environ['APPLE_TEAM_ID'] = 'test_apple_team_id'
    os.environ['APPLE_KEY_ID'] = 'test_apple_key_id'
    os.environ['BASE_API_URL'] = 'http://localhost:8000'
    yield


@pytest.fixture
def mock_redis(test_env_vars):
    """Mock Redis client for testing."""
    with patch('database.redis_db.r') as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance

        # Mock common Redis methods
        mock_instance.get.return_value = None
        mock_instance.set.return_value = True
        mock_instance.delete.return_value = 1
        mock_instance.exists.return_value = 0
        mock_instance.expire.return_value = True
        mock_instance.sadd.return_value = 1
        mock_instance.srem.return_value = 1
        mock_instance.smembers.return_value = set()
        mock_instance.incr.return_value = 1
        mock_instance.decr.return_value = 0
        mock_instance.mget.return_value = []
        mock_instance.srandmember.return_value = []
        mock_instance.ttl.return_value = -1

        yield mock_instance


@pytest.fixture
def mock_firebase_auth():
    """Mock Firebase authentication."""
    with patch('firebase_admin.auth') as mock_auth:
        mock_auth.verify_id_token.return_value = {'uid': 'test_user_123'}
        mock_auth.get_user.return_value = MagicMock(uid='test_user_123')
        yield mock_auth


@pytest.fixture
def test_user_id():
    """Test user ID fixture."""
    return "test_user_123"


@pytest.fixture
def test_uid():
    """Alias for test_user_id."""
    return "test_user_123"


@pytest.fixture
def test_client(test_env_vars, mock_firebase_auth):
    """Create a test client for FastAPI application."""
    # Mock firebase initialization
    with patch('firebase_admin.initialize_app'):
        from main import app
        client = TestClient(app)
        yield client


@pytest.fixture
def mock_firestore():
    """Mock Firestore client."""
    with patch('google.cloud.firestore.Client') as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def sample_auth_session():
    """Sample auth session data."""
    return {
        'provider': 'google',
        'redirect_uri': 'http://localhost:3000/auth/callback',
        'state': 'test_state',
        'flow_type': 'user_auth',
    }


@pytest.fixture
def sample_oauth_credentials():
    """Sample OAuth credentials."""
    return {
        'provider': 'google',
        'id_token': 'test_id_token',
        'access_token': 'test_access_token',
        'provider_id': 'google.com',
    }


@pytest.fixture
def mock_requests():
    """Mock requests library."""
    with patch('requests.post') as mock_post, patch('requests.get') as mock_get:
        # Mock successful token exchange
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            'id_token': 'test_id_token',
            'access_token': 'test_access_token',
        }

        # Mock Apple keys endpoint
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            'keys': []
        }

        yield {'post': mock_post, 'get': mock_get}


@pytest.fixture
def mock_user_db():
    """Mock user database operations."""
    with patch('database.users.is_exists_user') as mock_exists, \
         patch('database.users.get_user_valid_subscription') as mock_subscription, \
         patch('database.users.get_user_language_preference') as mock_language, \
         patch('database.users.get_user_private_cloud_sync_enabled') as mock_cloud, \
         patch('database.users.get_person_by_name') as mock_person:

        mock_exists.return_value = True
        mock_subscription.return_value = None
        mock_language.return_value = 'en'
        mock_cloud.return_value = False
        mock_person.return_value = None

        yield {
            'is_exists_user': mock_exists,
            'get_user_valid_subscription': mock_subscription,
            'get_user_language_preference': mock_language,
            'get_user_private_cloud_sync_enabled': mock_cloud,
            'get_person_by_name': mock_person,
        }


@pytest.fixture
def mock_conversations_db():
    """Mock conversations database operations."""
    with patch('database.conversations.get_conversation') as mock_get, \
         patch('database.conversations.upsert_conversation') as mock_upsert, \
         patch('database.conversations.update_conversation') as mock_update, \
         patch('database.conversations.delete_conversation') as mock_delete, \
         patch('database.conversations.get_processing_conversations') as mock_processing, \
         patch('database.conversations.get_last_completed_conversation') as mock_last:

        mock_get.return_value = None
        mock_upsert.return_value = True
        mock_update.return_value = True
        mock_delete.return_value = True
        mock_processing.return_value = []
        mock_last.return_value = None

        yield {
            'get_conversation': mock_get,
            'upsert_conversation': mock_upsert,
            'update_conversation': mock_update,
            'delete_conversation': mock_delete,
            'get_processing_conversations': mock_processing,
            'get_last_completed_conversation': mock_last,
        }


@pytest.fixture
def encryption_test_key():
    """Test encryption key."""
    return 'test_secret_key_for_encryption_1234567890abcdef'


@pytest.fixture(autouse=True)
def cleanup_temp_dirs():
    """Clean up temporary directories after tests."""
    yield
    # Cleanup logic if needed
    temp_dirs = ['_temp', '_samples', '_segments', '_speech_profiles']
    for temp_dir in temp_dirs:
        path = os.path.join(os.path.dirname(__file__), '..', temp_dir)
        if os.path.exists(path):
            # Don't actually delete during tests
            pass
