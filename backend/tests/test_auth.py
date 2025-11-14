"""
Unit tests for authentication utilities in routers/auth.py
"""
import os
import json
import time
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.backends import default_backend


class TestAppleClientSecretGeneration:
    """Tests for Apple client secret JWT generation."""

    @pytest.fixture
    def apple_private_key(self):
        """Generate a test private key for Apple authentication."""
        private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        return pem.decode('utf-8')

    def test_generate_apple_client_secret_success(self, apple_private_key):
        """Test successful generation of Apple client secret."""
        from routers.auth import _generate_apple_client_secret

        client_id = "com.test.app"
        team_id = "ABC123DEF4"
        key_id = "XYZ987WVU6"

        secret = _generate_apple_client_secret(
            client_id, team_id, key_id, apple_private_key
        )

        # Should return a JWT string
        assert isinstance(secret, str)
        assert len(secret) > 0
        # JWT has 3 parts separated by dots
        parts = secret.split('.')
        assert len(parts) == 3

    def test_generate_apple_client_secret_jwt_structure(self, apple_private_key):
        """Test that generated JWT has correct structure."""
        import jwt
        from routers.auth import _generate_apple_client_secret

        client_id = "com.test.app"
        team_id = "ABC123DEF4"
        key_id = "XYZ987WVU6"

        secret = _generate_apple_client_secret(
            client_id, team_id, key_id, apple_private_key
        )

        # Decode without verification to check structure
        unverified = jwt.decode(secret, options={"verify_signature": False})

        assert unverified['iss'] == team_id
        assert unverified['sub'] == client_id
        assert unverified['aud'] == 'https://appleid.apple.com'
        assert 'iat' in unverified
        assert 'exp' in unverified

        # Check that expiration is about 1 hour in future
        assert unverified['exp'] - unverified['iat'] == 3600

    def test_generate_apple_client_secret_invalid_key(self):
        """Test error handling with invalid private key."""
        from routers.auth import _generate_apple_client_secret
        from fastapi import HTTPException

        client_id = "com.test.app"
        team_id = "ABC123DEF4"
        key_id = "XYZ987WVU6"
        invalid_key = "not_a_valid_pem_key"

        with pytest.raises(HTTPException) as exc_info:
            _generate_apple_client_secret(
                client_id, team_id, key_id, invalid_key
            )

        assert exc_info.value.status_code == 500


class TestAuthRedirects:
    """Tests for OAuth redirect URL generation."""

    @pytest.mark.asyncio
    async def test_google_auth_redirect(self, test_env_vars):
        """Test Google OAuth redirect URL generation."""
        from routers.auth import _google_auth_redirect
        from urllib.parse import urlparse, parse_qs

        session_id = "test_session_123"
        result = await _google_auth_redirect(session_id)

        # Should be a RedirectResponse
        assert result.status_code == 307
        assert result.headers['location'].startswith('https://accounts.google.com/o/oauth2/v2/auth')

        # Parse URL and check parameters
        parsed = urlparse(result.headers['location'])
        params = parse_qs(parsed.query)

        assert params['client_id'][0] == 'test_google_client_id'
        assert params['response_type'][0] == 'code'
        assert params['state'][0] == session_id
        assert 'openid' in params['scope'][0]
        assert 'email' in params['scope'][0]

    @pytest.mark.asyncio
    async def test_google_auth_redirect_missing_client_id(self):
        """Test Google redirect with missing client ID."""
        from routers.auth import _google_auth_redirect
        from fastapi import HTTPException

        # Remove client ID
        old_client_id = os.environ.get('GOOGLE_CLIENT_ID')
        if 'GOOGLE_CLIENT_ID' in os.environ:
            del os.environ['GOOGLE_CLIENT_ID']

        try:
            with pytest.raises(HTTPException) as exc_info:
                await _google_auth_redirect("session_123")
            assert exc_info.value.status_code == 500
        finally:
            if old_client_id:
                os.environ['GOOGLE_CLIENT_ID'] = old_client_id

    @pytest.mark.asyncio
    async def test_apple_auth_redirect(self, test_env_vars):
        """Test Apple OAuth redirect URL generation."""
        from routers.auth import _apple_auth_redirect
        from urllib.parse import urlparse, parse_qs

        session_id = "test_session_456"
        result = await _apple_auth_redirect(session_id)

        # Should be a RedirectResponse
        assert result.status_code == 307
        assert result.headers['location'].startswith('https://appleid.apple.com/auth/authorize')

        # Parse URL and check parameters
        parsed = urlparse(result.headers['location'])
        params = parse_qs(parsed.query)

        assert params['client_id'][0] == 'test_apple_client_id'
        assert params['response_type'][0] == 'code'
        assert params['response_mode'][0] == 'form_post'
        assert params['state'][0] == session_id


class TestOAuthCodeExchange:
    """Tests for OAuth code exchange functionality."""

    @pytest.mark.asyncio
    async def test_exchange_google_code_success(self, test_env_vars, mock_requests, sample_auth_session):
        """Test successful Google code exchange."""
        from routers.auth import _exchange_google_code_for_oauth_credentials

        code = "test_authorization_code"
        result = await _exchange_google_code_for_oauth_credentials(code, sample_auth_session)

        # Should return JSON string
        credentials = json.loads(result)
        assert credentials['provider'] == 'google'
        assert credentials['id_token'] == 'test_id_token'
        assert credentials['access_token'] == 'test_access_token'
        assert credentials['provider_id'] == 'google.com'

    @pytest.mark.asyncio
    async def test_exchange_google_code_failure(self, test_env_vars, sample_auth_session):
        """Test Google code exchange failure."""
        from routers.auth import _exchange_google_code_for_oauth_credentials
        from fastapi import HTTPException

        with patch('requests.post') as mock_post:
            mock_post.return_value.status_code = 400
            mock_post.return_value.text = "Invalid code"

            with pytest.raises(HTTPException) as exc_info:
                await _exchange_google_code_for_oauth_credentials("invalid_code", sample_auth_session)

            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_exchange_apple_code_success(self, test_env_vars, mock_requests, sample_auth_session, apple_private_key):
        """Test successful Apple code exchange."""
        from routers.auth import _exchange_apple_code_for_oauth_credentials

        # Set up Apple environment
        os.environ['APPLE_PRIVATE_KEY'] = apple_private_key

        code = "test_apple_authorization_code"
        session_data = {**sample_auth_session, 'provider': 'apple'}

        result = await _exchange_apple_code_for_oauth_credentials(code, session_data)

        # Should return JSON string
        credentials = json.loads(result)
        assert credentials['provider'] == 'apple'
        assert credentials['id_token'] == 'test_id_token'
        assert credentials['provider_id'] == 'apple.com'

    @pytest.mark.asyncio
    async def test_exchange_apple_code_missing_config(self, sample_auth_session):
        """Test Apple code exchange with missing configuration."""
        from routers.auth import _exchange_apple_code_for_oauth_credentials
        from fastapi import HTTPException

        # Remove required env vars
        old_team_id = os.environ.get('APPLE_TEAM_ID')
        if 'APPLE_TEAM_ID' in os.environ:
            del os.environ['APPLE_TEAM_ID']

        try:
            with pytest.raises(HTTPException) as exc_info:
                await _exchange_apple_code_for_oauth_credentials("code", sample_auth_session)
            assert exc_info.value.status_code == 500
        finally:
            if old_team_id:
                os.environ['APPLE_TEAM_ID'] = old_team_id


class TestAuthEndpoints:
    """Tests for authentication API endpoints (integration tests)."""

    @pytest.mark.asyncio
    async def test_auth_authorize_google(self, test_client, mock_redis):
        """Test /v1/auth/authorize endpoint for Google."""
        with patch('database.redis_db.set_auth_session') as mock_set_session:
            response = test_client.get(
                "/v1/auth/authorize",
                params={
                    "provider": "google",
                    "redirect_uri": "http://localhost:3000/callback",
                    "state": "test_state"
                }
            )

            # Should redirect to Google
            assert response.status_code == 307
            assert 'accounts.google.com' in response.headers['location']

            # Should have stored session
            mock_set_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_auth_authorize_apple(self, test_client, mock_redis):
        """Test /v1/auth/authorize endpoint for Apple."""
        with patch('database.redis_db.set_auth_session') as mock_set_session:
            response = test_client.get(
                "/v1/auth/authorize",
                params={
                    "provider": "apple",
                    "redirect_uri": "http://localhost:3000/callback",
                    "state": "test_state"
                }
            )

            # Should redirect to Apple
            assert response.status_code == 307
            assert 'appleid.apple.com' in response.headers['location']

            # Should have stored session
            mock_set_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_auth_authorize_unsupported_provider(self, test_client):
        """Test /v1/auth/authorize with unsupported provider."""
        response = test_client.get(
            "/v1/auth/authorize",
            params={
                "provider": "facebook",
                "redirect_uri": "http://localhost:3000/callback"
            }
        )

        assert response.status_code == 400
        assert "Unsupported provider" in response.json()['detail']

    @pytest.mark.asyncio
    async def test_auth_token_exchange(self, test_client, mock_redis, sample_oauth_credentials):
        """Test /v1/auth/token endpoint."""
        with patch('database.redis_db.get_auth_code') as mock_get_code, \
             patch('database.redis_db.delete_auth_code') as mock_delete_code:

            # Mock Redis returning OAuth credentials
            mock_get_code.return_value = json.dumps(sample_oauth_credentials)

            response = test_client.post(
                "/v1/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "test_auth_code",
                    "redirect_uri": "http://localhost:3000/callback"
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert data['provider'] == 'google'
            assert data['id_token'] == 'test_id_token'
            assert data['access_token'] == 'test_access_token'
            assert data['token_type'] == 'Bearer'

            # Should have deleted the code
            mock_delete_code.assert_called_once()

    @pytest.mark.asyncio
    async def test_auth_token_invalid_code(self, test_client, mock_redis):
        """Test /v1/auth/token with invalid code."""
        with patch('database.redis_db.get_auth_code') as mock_get_code:
            mock_get_code.return_value = None

            response = test_client.post(
                "/v1/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "invalid_code",
                    "redirect_uri": "http://localhost:3000/callback"
                }
            )

            assert response.status_code == 400
            assert "Invalid or expired code" in response.json()['detail']

    @pytest.mark.asyncio
    async def test_auth_token_unsupported_grant_type(self, test_client):
        """Test /v1/auth/token with unsupported grant type."""
        response = test_client.post(
            "/v1/auth/token",
            data={
                "grant_type": "refresh_token",
                "code": "test_code",
                "redirect_uri": "http://localhost:3000/callback"
            }
        )

        assert response.status_code == 400
        assert "Unsupported grant type" in response.json()['detail']


class TestAppleIDTokenVerification:
    """Tests for Apple ID token verification."""

    @pytest.mark.asyncio
    async def test_verify_apple_id_token_structure(self):
        """Test Apple ID token verification with mock data."""
        from routers.auth import _verify_apple_id_token
        import jwt

        # This test would require mocking the Apple keys endpoint
        # and creating a valid test JWT. For now, we test the error path.

        with patch('requests.get') as mock_get:
            mock_get.return_value.status_code = 500

            with pytest.raises(Exception):
                _verify_apple_id_token("fake_token", "com.test.app")
