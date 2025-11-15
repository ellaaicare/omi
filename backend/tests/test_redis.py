"""
Unit tests for Redis database operations.
"""
import json
from unittest.mock import MagicMock, patch
import pytest
from database import redis_db


class TestGenericCache:
    """Tests for generic cache operations."""

    def test_set_generic_cache(self, mock_redis):
        """Test setting generic cache."""
        path = "/test/path"
        data = {"key": "value", "count": 123}

        redis_db.set_generic_cache(path, data)

        # Verify Redis set was called
        assert mock_redis.set.called

    def test_get_generic_cache(self, mock_redis):
        """Test getting generic cache."""
        path = "/test/path"
        expected_data = {"key": "value"}

        mock_redis.get.return_value = json.dumps(expected_data).encode()

        result = redis_db.get_generic_cache(path)

        assert result == expected_data
        assert mock_redis.get.called

    def test_get_generic_cache_not_found(self, mock_redis):
        """Test getting non-existent cache."""
        mock_redis.get.return_value = None

        result = redis_db.get_generic_cache("/nonexistent/path")

        assert result is None

    def test_delete_generic_cache(self, mock_redis):
        """Test deleting generic cache."""
        path = "/test/path"

        redis_db.delete_generic_cache(path)

        assert mock_redis.delete.called

    def test_set_generic_cache_with_ttl(self, mock_redis):
        """Test setting cache with TTL."""
        path = "/test/path"
        data = {"test": "data"}
        ttl = 300

        redis_db.set_generic_cache(path, data, ttl)

        assert mock_redis.set.called
        assert mock_redis.expire.called


class TestAppCache:
    """Tests for app caching operations."""

    def test_set_app_cache_by_id(self, mock_redis):
        """Test setting app cache."""
        app_id = "app_123"
        app_data = {"name": "Test App", "version": "1.0"}

        redis_db.set_app_cache_by_id(app_id, app_data)

        assert mock_redis.set.called

    def test_get_app_cache_by_id(self, mock_redis):
        """Test getting app cache."""
        app_id = "app_123"
        app_data = {"name": "Test App"}

        mock_redis.get.return_value = json.dumps(app_data).encode()

        result = redis_db.get_app_cache_by_id(app_id)

        assert result == app_data

    def test_delete_app_cache_by_id(self, mock_redis):
        """Test deleting app cache."""
        app_id = "app_123"

        redis_db.delete_app_cache_by_id(app_id)

        assert mock_redis.delete.called

    def test_set_app_installs_count(self, mock_redis):
        """Test setting app installs count."""
        app_id = "app_123"
        count = 100

        redis_db.set_app_installs_count(app_id, count)

        assert mock_redis.set.called

    def test_increase_app_installs_count(self, mock_redis):
        """Test incrementing app installs."""
        app_id = "app_123"

        redis_db.increase_app_installs_count(app_id)

        assert mock_redis.incr.called

    def test_decrease_app_installs_count(self, mock_redis):
        """Test decrementing app installs."""
        app_id = "app_123"

        redis_db.decrease_app_installs_count(app_id)

        assert mock_redis.decr.called

    def test_get_app_installs_count(self, mock_redis):
        """Test getting app installs count."""
        app_id = "app_123"
        mock_redis.get.return_value = b"150"

        result = redis_db.get_app_installs_count(app_id)

        assert result == 150

    def test_get_app_installs_count_not_found(self, mock_redis):
        """Test getting installs count for non-existent app."""
        mock_redis.get.return_value = None

        result = redis_db.get_app_installs_count("nonexistent_app")

        assert result == 0


class TestUsernameOperations:
    """Tests for username management."""

    def test_is_username_taken_false(self, mock_redis):
        """Test checking if username is not taken."""
        mock_redis.exists.return_value = 0

        result = redis_db.is_username_taken("available_username")

        assert result is False

    def test_is_username_taken_true(self, mock_redis):
        """Test checking if username is taken."""
        mock_redis.exists.return_value = 1

        result = redis_db.is_username_taken("taken_username")

        assert result is True

    def test_save_username(self, mock_redis):
        """Test saving a username."""
        username = "testuser"
        uid = "user_123"

        redis_db.save_username(username, uid)

        # Should set username:uid mapping
        assert mock_redis.set.called
        # Should add to user's username set
        assert mock_redis.sadd.called

    def test_get_uid_by_username(self, mock_redis):
        """Test getting UID by username."""
        username = "testuser"
        uid = "user_123"

        mock_redis.get.return_value = uid.encode()

        result = redis_db.get_uid_by_username(username)

        assert result == uid

    def test_get_uid_by_username_not_found(self, mock_redis):
        """Test getting UID for non-existent username."""
        mock_redis.get.return_value = None

        result = redis_db.get_uid_by_username("nonexistent")

        assert result is None

    def test_get_usernames_by_uid(self, mock_redis):
        """Test getting all usernames for a user."""
        uid = "user_123"
        usernames = {b"username1", b"username2"}

        mock_redis.smembers.return_value = usernames

        result = redis_db.get_usernames_by_uid(uid)

        assert len(result) == 2
        assert "username1" in result
        assert "username2" in result

    def test_delete_username(self, mock_redis):
        """Test deleting a username."""
        username = "testuser"
        uid = "user_123"

        mock_redis.get.return_value = uid.encode()

        redis_db.delete_username(username)

        # Should remove from user's set
        assert mock_redis.srem.called
        # Should delete username:uid mapping
        assert mock_redis.delete.called


class TestAuthSession:
    """Tests for authentication session management."""

    def test_set_auth_session(self, mock_redis):
        """Test setting auth session."""
        session_id = "session_123"
        session_data = {
            "provider": "google",
            "redirect_uri": "http://localhost/callback"
        }

        redis_db.set_auth_session(session_id, session_data, 300)

        assert mock_redis.set.called

    def test_get_auth_session(self, mock_redis):
        """Test getting auth session."""
        session_id = "session_123"
        session_data = {"provider": "google"}

        mock_redis.get.return_value = json.dumps(session_data).encode()

        result = redis_db.get_auth_session(session_id)

        assert result == session_data

    def test_get_auth_session_not_found(self, mock_redis):
        """Test getting non-existent session."""
        mock_redis.get.return_value = None

        result = redis_db.get_auth_session("nonexistent")

        assert result is None

    def test_set_auth_code(self, mock_redis):
        """Test setting auth code."""
        auth_code = "code_123"
        token = "firebase_token"

        redis_db.set_auth_code(auth_code, token, 300)

        assert mock_redis.set.called

    def test_get_auth_code(self, mock_redis):
        """Test getting auth code."""
        auth_code = "code_123"
        token = "firebase_token"

        mock_redis.get.return_value = token.encode()

        result = redis_db.get_auth_code(auth_code)

        assert result == token

    def test_delete_auth_code(self, mock_redis):
        """Test deleting auth code."""
        auth_code = "code_123"

        redis_db.delete_auth_code(auth_code)

        assert mock_redis.delete.called


class TestConversationOperations:
    """Tests for conversation-related Redis operations."""

    def test_store_conversation_to_uid(self, mock_redis):
        """Test storing conversation UID mapping."""
        conversation_id = "conv_123"
        uid = "user_123"

        redis_db.store_conversation_to_uid(conversation_id, uid)

        assert mock_redis.set.called

    def test_remove_conversation_to_uid(self, mock_redis):
        """Test removing conversation UID mapping."""
        conversation_id = "conv_123"

        redis_db.remove_conversation_to_uid(conversation_id)

        assert mock_redis.delete.called

    def test_get_conversation_uid(self, mock_redis):
        """Test getting conversation UID."""
        conversation_id = "conv_123"
        uid = "user_123"

        mock_redis.get.return_value = uid.encode()

        result = redis_db.get_conversation_uid(conversation_id)

        assert result == uid

    def test_set_in_progress_conversation_id(self, mock_redis):
        """Test setting in-progress conversation."""
        uid = "user_123"
        conversation_id = "conv_123"

        redis_db.set_in_progress_conversation_id(uid, conversation_id, 150)

        assert mock_redis.set.called
        assert mock_redis.expire.called

    def test_get_in_progress_conversation_id(self, mock_redis):
        """Test getting in-progress conversation."""
        uid = "user_123"
        conversation_id = "conv_123"

        mock_redis.get.return_value = conversation_id.encode()

        result = redis_db.get_in_progress_conversation_id(uid)

        assert result == conversation_id

    def test_remove_in_progress_conversation_id(self, mock_redis):
        """Test removing in-progress conversation."""
        uid = "user_123"

        redis_db.remove_in_progress_conversation_id(uid)

        assert mock_redis.delete.called

    def test_add_public_conversation(self, mock_redis):
        """Test adding public conversation."""
        conversation_id = "conv_123"

        redis_db.add_public_conversation(conversation_id)

        assert mock_redis.sadd.called

    def test_remove_public_conversation(self, mock_redis):
        """Test removing public conversation."""
        conversation_id = "conv_123"

        redis_db.remove_public_conversation(conversation_id)

        assert mock_redis.srem.called

    def test_get_public_conversations(self, mock_redis):
        """Test getting public conversations."""
        conversations = {b"conv_1", b"conv_2"}
        mock_redis.smembers.return_value = conversations

        result = redis_db.get_public_conversations()

        assert len(result) == 2


class TestUserDataOperations:
    """Tests for user data caching."""

    def test_cache_user_name(self, mock_redis):
        """Test caching user name."""
        uid = "user_123"
        name = "John Doe"

        redis_db.cache_user_name(uid, name)

        assert mock_redis.set.called
        assert mock_redis.expire.called

    def test_get_cached_user_name(self, mock_redis):
        """Test getting cached user name."""
        uid = "user_123"
        name = "John Doe"

        mock_redis.get.return_value = name.encode()

        result = redis_db.get_cached_user_name(uid)

        assert result == name

    def test_get_cached_user_name_default(self, mock_redis):
        """Test getting cached name with default."""
        mock_redis.get.return_value = None

        result = redis_db.get_cached_user_name("user_123")

        assert result == "User"

    def test_set_user_has_soniox_speech_profile(self, mock_redis):
        """Test setting speech profile flag."""
        uid = "user_123"

        redis_db.set_user_has_soniox_speech_profile(uid)

        assert mock_redis.set.called

    def test_get_user_has_soniox_speech_profile(self, mock_redis):
        """Test checking speech profile existence."""
        uid = "user_123"
        mock_redis.exists.return_value = 1

        result = redis_db.get_user_has_soniox_speech_profile(uid)

        assert result == 1

    def test_remove_user_soniox_speech_profile(self, mock_redis):
        """Test removing speech profile."""
        uid = "user_123"

        redis_db.remove_user_soniox_speech_profile(uid)

        assert mock_redis.delete.called

    def test_cache_user_geolocation(self, mock_redis):
        """Test caching user geolocation."""
        uid = "user_123"
        geolocation = {"latitude": 37.7749, "longitude": -122.4194}

        redis_db.cache_user_geolocation(uid, geolocation)

        assert mock_redis.set.called
        assert mock_redis.expire.called


class TestAPIKeyOperations:
    """Tests for API key caching."""

    def test_cache_mcp_api_key(self, mock_redis):
        """Test caching MCP API key."""
        hashed_key = "hashed_key_123"
        user_id = "user_123"

        redis_db.cache_mcp_api_key(hashed_key, user_id, 3600)

        assert mock_redis.set.called

    def test_get_cached_mcp_api_key_user_id(self, mock_redis):
        """Test getting cached MCP API key user ID."""
        hashed_key = "hashed_key_123"
        user_id = "user_123"

        mock_redis.get.return_value = user_id.encode()

        result = redis_db.get_cached_mcp_api_key_user_id(hashed_key)

        assert result == user_id

    def test_delete_cached_mcp_api_key(self, mock_redis):
        """Test deleting cached MCP API key."""
        hashed_key = "hashed_key_123"

        redis_db.delete_cached_mcp_api_key(hashed_key)

        assert mock_redis.delete.called

    def test_cache_dev_api_key(self, mock_redis):
        """Test caching developer API key."""
        hashed_key = "dev_key_123"
        user_id = "user_123"

        redis_db.cache_dev_api_key(hashed_key, user_id, 3600)

        assert mock_redis.set.called

    def test_get_cached_dev_api_key_user_id(self, mock_redis):
        """Test getting cached dev API key user ID."""
        hashed_key = "dev_key_123"
        user_id = "user_123"

        mock_redis.get.return_value = user_id.encode()

        result = redis_db.get_cached_dev_api_key_user_id(hashed_key)

        assert result == user_id


class TestMigrationStatus:
    """Tests for data migration status tracking."""

    def test_set_migration_status(self, mock_redis):
        """Test setting migration status."""
        uid = "user_123"

        redis_db.set_migration_status(uid, "in_progress", processed=50, total=100)

        assert mock_redis.set.called

    def test_get_migration_status(self, mock_redis):
        """Test getting migration status."""
        uid = "user_123"
        status_data = {"status": "in_progress", "processed": 50, "total": 100}

        mock_redis.get.return_value = json.dumps(status_data).encode()

        result = redis_db.get_migration_status(uid)

        assert result["status"] == "in_progress"
        assert result["processed"] == 50

    def test_get_migration_status_not_found(self, mock_redis):
        """Test getting migration status when not set."""
        mock_redis.get.return_value = None

        result = redis_db.get_migration_status("user_123")

        assert result["status"] == "idle"

    def test_clear_migration_status(self, mock_redis):
        """Test clearing migration status."""
        uid = "user_123"

        redis_db.clear_migration_status(uid)

        assert mock_redis.delete.called


class TestNotificationTracking:
    """Tests for notification tracking."""

    def test_set_credit_limit_notification_sent(self, mock_redis):
        """Test setting credit limit notification flag."""
        uid = "user_123"

        redis_db.set_credit_limit_notification_sent(uid)

        assert mock_redis.set.called

    def test_has_credit_limit_notification_been_sent(self, mock_redis):
        """Test checking if credit limit notification was sent."""
        uid = "user_123"
        mock_redis.exists.return_value = 1

        result = redis_db.has_credit_limit_notification_been_sent(uid)

        assert result == 1

    def test_set_silent_user_notification_sent(self, mock_redis):
        """Test setting silent user notification flag."""
        uid = "user_123"

        redis_db.set_silent_user_notification_sent(uid)

        assert mock_redis.set.called

    def test_has_silent_user_notification_been_sent(self, mock_redis):
        """Test checking if silent user notification was sent."""
        uid = "user_123"
        mock_redis.exists.return_value = 0

        result = redis_db.has_silent_user_notification_been_sent(uid)

        assert result == 0


class TestFilterOperations:
    """Tests for filter category operations."""

    def test_add_filter_category_item(self, mock_redis):
        """Test adding filter category item."""
        uid = "user_123"
        category = "blocked_words"
        item = "spam"

        redis_db.add_filter_category_item(uid, category, item)

        assert mock_redis.sadd.called

    def test_add_filter_category_items(self, mock_redis):
        """Test adding multiple filter items."""
        uid = "user_123"
        category = "blocked_words"
        items = ["spam", "junk", "unwanted"]

        redis_db.add_filter_category_items(uid, category, items)

        assert mock_redis.sadd.called

    def test_remove_filter_category_item(self, mock_redis):
        """Test removing filter category item."""
        uid = "user_123"
        category = "blocked_words"
        item = "spam"

        redis_db.remove_filter_category_item(uid, category, item)

        assert mock_redis.srem.called

    def test_remove_all_filter_category_items(self, mock_redis):
        """Test removing all filter items."""
        uid = "user_123"
        category = "blocked_words"

        redis_db.remove_all_filter_category_items(uid, category)

        assert mock_redis.delete.called

    def test_get_filter_category_items(self, mock_redis):
        """Test getting filter category items."""
        uid = "user_123"
        category = "blocked_words"
        items = {b"spam", b"junk"}

        mock_redis.smembers.return_value = items

        result = redis_db.get_filter_category_items(uid, category)

        assert len(result) == 2
