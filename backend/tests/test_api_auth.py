"""
Integration tests for API authentication endpoints.
Tests the complete flow of OAuth authentication.
"""
import json
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from fastapi.testclient import TestClient


class TestGoogleAuthFlow:
    """Integration tests for Google OAuth flow."""

    def test_complete_google_auth_flow(self, test_client, mock_redis, mock_requests):
        """Test complete Google authentication flow."""
        # Step 1: Initiate OAuth flow
        with patch('database.redis_db.set_auth_session') as mock_set_session:
            response = test_client.get(
                "/v1/auth/authorize",
                params={
                    "provider": "google",
                    "redirect_uri": "http://localhost:3000/callback",
                    "state": "user_state_123"
                }
            )

            assert response.status_code == 307
            assert 'accounts.google.com' in response.headers['location']

            # Verify session was stored
            assert mock_set_session.called
            call_args = mock_set_session.call_args
            session_id = call_args[0][0]
            session_data = call_args[0][1]
            assert session_data['provider'] == 'google'
            assert session_data['redirect_uri'] == 'http://localhost:3000/callback'

        # Step 2: Simulate callback from Google
        with patch('database.redis_db.get_auth_session') as mock_get_session, \
             patch('database.redis_db.set_auth_code') as mock_set_code, \
             patch('requests.post') as mock_post:

            # Mock session retrieval
            mock_get_session.return_value = {
                'provider': 'google',
                'redirect_uri': 'http://localhost:3000/callback',
                'state': 'user_state_123',
                'flow_type': 'user_auth'
            }

            # Mock Google token exchange
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                'id_token': 'google_id_token_abc123',
                'access_token': 'google_access_token_xyz789'
            }

            response = test_client.get(
                "/v1/auth/callback/google",
                params={
                    "code": "google_auth_code_123",
                    "state": session_id
                }
            )

            # Should redirect back to app
            assert response.status_code in [307, 302]
            redirect_url = response.headers['location']
            assert 'localhost:3000' in redirect_url
            assert 'code=' in redirect_url

            # Verify auth code was stored
            assert mock_set_code.called

        # Step 3: Exchange auth code for tokens
        with patch('database.redis_db.get_auth_code') as mock_get_code, \
             patch('database.redis_db.delete_auth_code') as mock_delete_code:

            oauth_credentials = {
                'provider': 'google',
                'id_token': 'google_id_token_abc123',
                'access_token': 'google_access_token_xyz789',
                'provider_id': 'google.com'
            }
            mock_get_code.return_value = json.dumps(oauth_credentials)

            response = test_client.post(
                "/v1/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "temp_auth_code",
                    "redirect_uri": "http://localhost:3000/callback"
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert data['provider'] == 'google'
            assert data['id_token'] == 'google_id_token_abc123'
            assert data['access_token'] == 'google_access_token_xyz789'
            assert data['token_type'] == 'Bearer'

            # Verify code was deleted after use
            assert mock_delete_code.called

    def test_google_auth_callback_error_handling(self, test_client):
        """Test Google callback error handling."""
        response = test_client.get(
            "/v1/auth/callback/google",
            params={
                "error": "access_denied",
                "state": "session_123"
            }
        )

        assert response.status_code == 400
        assert "access_denied" in response.json()['detail']

    def test_google_auth_invalid_session(self, test_client, mock_redis):
        """Test Google callback with invalid session."""
        with patch('database.redis_db.get_auth_session') as mock_get_session:
            mock_get_session.return_value = None

            response = test_client.get(
                "/v1/auth/callback/google",
                params={
                    "code": "valid_code",
                    "state": "invalid_session"
                }
            )

            assert response.status_code == 400
            assert "Invalid auth session" in response.json()['detail']


class TestAppleAuthFlow:
    """Integration tests for Apple OAuth flow."""

    def test_complete_apple_auth_flow(self, test_client, mock_redis, mock_requests):
        """Test complete Apple authentication flow."""
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend

        # Generate test private key
        private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        test_private_key = pem.decode('utf-8')

        # Step 1: Initiate OAuth flow
        with patch('database.redis_db.set_auth_session') as mock_set_session:
            response = test_client.get(
                "/v1/auth/authorize",
                params={
                    "provider": "apple",
                    "redirect_uri": "http://localhost:3000/callback",
                    "state": "user_state_456"
                }
            )

            assert response.status_code == 307
            assert 'appleid.apple.com' in response.headers['location']

            # Verify session was stored
            assert mock_set_session.called
            call_args = mock_set_session.call_args
            session_id = call_args[0][0]
            session_data = call_args[0][1]
            assert session_data['provider'] == 'apple'

        # Step 2: Simulate callback from Apple (POST form)
        with patch('database.redis_db.get_auth_session') as mock_get_session, \
             patch('database.redis_db.set_auth_code') as mock_set_code, \
             patch('requests.post') as mock_post, \
             patch.dict('os.environ', {'APPLE_PRIVATE_KEY': test_private_key}):

            # Mock session retrieval
            mock_get_session.return_value = {
                'provider': 'apple',
                'redirect_uri': 'http://localhost:3000/callback',
                'state': 'user_state_456',
                'flow_type': 'user_auth'
            }

            # Mock Apple token exchange
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                'id_token': 'apple_id_token_def456',
                'access_token': 'apple_access_token_uvw123'
            }

            response = test_client.post(
                "/v1/auth/callback/apple",
                data={
                    "code": "apple_auth_code_456",
                    "state": session_id
                }
            )

            # Should redirect back to app
            assert response.status_code in [307, 302]
            redirect_url = response.headers['location']
            assert 'localhost:3000' in redirect_url
            assert 'code=' in redirect_url

            # Verify auth code was stored
            assert mock_set_code.called

        # Step 3: Exchange auth code for tokens
        with patch('database.redis_db.get_auth_code') as mock_get_code, \
             patch('database.redis_db.delete_auth_code') as mock_delete_code:

            oauth_credentials = {
                'provider': 'apple',
                'id_token': 'apple_id_token_def456',
                'access_token': 'apple_access_token_uvw123',
                'provider_id': 'apple.com'
            }
            mock_get_code.return_value = json.dumps(oauth_credentials)

            response = test_client.post(
                "/v1/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "temp_apple_code",
                    "redirect_uri": "http://localhost:3000/callback"
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert data['provider'] == 'apple'
            assert data['id_token'] == 'apple_id_token_def456'
            assert data['token_type'] == 'Bearer'

            # Verify code was deleted after use
            assert mock_delete_code.called

    def test_apple_auth_callback_error_handling(self, test_client):
        """Test Apple callback error handling."""
        response = test_client.post(
            "/v1/auth/callback/apple",
            data={
                "error": "user_cancelled_authorize",
                "state": "session_456"
            }
        )

        assert response.status_code == 400
        assert "user_cancelled_authorize" in response.json()['detail']

    def test_apple_auth_invalid_session(self, test_client, mock_redis):
        """Test Apple callback with invalid session."""
        with patch('database.redis_db.get_auth_session') as mock_get_session:
            mock_get_session.return_value = None

            response = test_client.post(
                "/v1/auth/callback/apple",
                data={
                    "code": "valid_code",
                    "state": "invalid_session"
                }
            )

            assert response.status_code == 400
            assert "Invalid auth session" in response.json()['detail']


class TestTokenEndpoint:
    """Integration tests for token exchange endpoint."""

    def test_token_exchange_success(self, test_client, mock_redis):
        """Test successful token exchange."""
        oauth_credentials = {
            'provider': 'google',
            'id_token': 'test_id_token',
            'access_token': 'test_access_token',
            'provider_id': 'google.com'
        }

        with patch('database.redis_db.get_auth_code') as mock_get_code, \
             patch('database.redis_db.delete_auth_code') as mock_delete_code:

            mock_get_code.return_value = json.dumps(oauth_credentials)

            response = test_client.post(
                "/v1/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "valid_auth_code",
                    "redirect_uri": "http://localhost:3000/callback"
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert data['provider'] == 'google'
            assert data['id_token'] == 'test_id_token'
            assert data['access_token'] == 'test_access_token'
            assert data['provider_id'] == 'google.com'
            assert data['token_type'] == 'Bearer'
            assert data['expires_in'] == 3600

            # Verify code was consumed
            mock_delete_code.assert_called_once_with("valid_auth_code")

    def test_token_exchange_expired_code(self, test_client, mock_redis):
        """Test token exchange with expired code."""
        with patch('database.redis_db.get_auth_code') as mock_get_code:
            mock_get_code.return_value = None

            response = test_client.post(
                "/v1/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "expired_code",
                    "redirect_uri": "http://localhost:3000/callback"
                }
            )

            assert response.status_code == 400
            assert "Invalid or expired code" in response.json()['detail']

    def test_token_exchange_invalid_json(self, test_client, mock_redis):
        """Test token exchange with corrupted credentials."""
        with patch('database.redis_db.get_auth_code') as mock_get_code, \
             patch('database.redis_db.delete_auth_code') as mock_delete_code:

            mock_get_code.return_value = "invalid_json_data"

            response = test_client.post(
                "/v1/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "valid_code",
                    "redirect_uri": "http://localhost:3000/callback"
                }
            )

            assert response.status_code == 400
            assert "Invalid OAuth credentials" in response.json()['detail']

    def test_token_exchange_wrong_grant_type(self, test_client):
        """Test token exchange with unsupported grant type."""
        response = test_client.post(
            "/v1/auth/token",
            data={
                "grant_type": "client_credentials",
                "code": "valid_code",
                "redirect_uri": "http://localhost:3000/callback"
            }
        )

        assert response.status_code == 400
        assert "Unsupported grant type" in response.json()['detail']


class TestSessionManagement:
    """Tests for session management in auth flow."""

    def test_session_expiration(self, test_client, mock_redis):
        """Test that sessions expire correctly."""
        with patch('database.redis_db.set_auth_session') as mock_set_session:
            test_client.get(
                "/v1/auth/authorize",
                params={
                    "provider": "google",
                    "redirect_uri": "http://localhost:3000/callback"
                }
            )

            # Verify TTL was set (default 5 minutes = 300 seconds)
            call_args = mock_set_session.call_args
            ttl = call_args[0][2]
            assert ttl == 300

    def test_auth_code_expiration(self, test_client, mock_redis, mock_requests):
        """Test that auth codes expire correctly."""
        with patch('database.redis_db.get_auth_session') as mock_get_session, \
             patch('database.redis_db.set_auth_code') as mock_set_code, \
             patch('requests.post') as mock_post:

            mock_get_session.return_value = {
                'provider': 'google',
                'redirect_uri': 'http://localhost:3000/callback',
                'state': 'test_state',
                'flow_type': 'user_auth'
            }

            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                'id_token': 'test_id_token',
                'access_token': 'test_access_token'
            }

            test_client.get(
                "/v1/auth/callback/google",
                params={
                    "code": "google_code",
                    "state": "session_123"
                }
            )

            # Verify TTL was set (default 5 minutes = 300 seconds)
            call_args = mock_set_code.call_args
            ttl = call_args[0][2]
            assert ttl == 300


class TestAuthErrorScenarios:
    """Tests for various error scenarios in authentication."""

    def test_missing_required_parameters(self, test_client):
        """Test auth endpoints with missing parameters."""
        # Missing provider
        response = test_client.get("/v1/auth/authorize")
        assert response.status_code == 422

        # Missing redirect_uri
        response = test_client.get(
            "/v1/auth/authorize",
            params={"provider": "google"}
        )
        assert response.status_code == 422

    def test_network_error_during_token_exchange(self, test_client, mock_redis):
        """Test handling of network errors during token exchange."""
        with patch('database.redis_db.get_auth_session') as mock_get_session, \
             patch('requests.post') as mock_post:

            mock_get_session.return_value = {
                'provider': 'google',
                'redirect_uri': 'http://localhost:3000/callback',
                'state': 'test_state',
                'flow_type': 'user_auth'
            }

            # Simulate network error
            mock_post.side_effect = Exception("Network error")

            with pytest.raises(Exception):
                test_client.get(
                    "/v1/auth/callback/google",
                    params={
                        "code": "valid_code",
                        "state": "session_123"
                    }
                )

    def test_malformed_oauth_response(self, test_client, mock_redis):
        """Test handling of malformed OAuth provider response."""
        with patch('database.redis_db.get_auth_session') as mock_get_session, \
             patch('requests.post') as mock_post:

            mock_get_session.return_value = {
                'provider': 'google',
                'redirect_uri': 'http://localhost:3000/callback',
                'state': 'test_state',
                'flow_type': 'user_auth'
            }

            # Return response missing required fields
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                # Missing id_token and access_token
                'expires_in': 3600
            }

            response = test_client.get(
                "/v1/auth/callback/google",
                params={
                    "code": "valid_code",
                    "state": "session_123"
                }
            )

            # Should handle gracefully
            assert response.status_code in [400, 500]


class TestConcurrentAuthRequests:
    """Tests for handling concurrent authentication requests."""

    def test_multiple_simultaneous_auth_sessions(self, test_client, mock_redis):
        """Test handling multiple auth sessions simultaneously."""
        with patch('database.redis_db.set_auth_session') as mock_set_session:
            # Create multiple sessions
            sessions = []
            for i in range(5):
                response = test_client.get(
                    "/v1/auth/authorize",
                    params={
                        "provider": "google",
                        "redirect_uri": f"http://localhost:3000/callback/{i}",
                        "state": f"state_{i}"
                    }
                )
                assert response.status_code == 307
                sessions.append(response)

            # Verify all sessions were created
            assert mock_set_session.call_count == 5
