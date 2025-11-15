# Backend Testing Infrastructure

This directory contains the comprehensive test suite for the OMI backend API.

## Overview

The test suite is organized into multiple test files covering:
- **Unit Tests**: Isolated tests for individual components
- **Integration Tests**: Tests for component interactions
- **End-to-End Tests**: Full user journey tests

## Test Files

### `test_encryption.py`
Unit tests for encryption utilities (`utils/encryption.py`):
- Key derivation (HKDF)
- String encryption/decryption
- Audio chunk encryption/decryption
- Edge cases and error handling

**Coverage Target**: Complete coverage of encryption module

### `test_auth.py`
Unit tests for authentication logic (`routers/auth.py`):
- Apple client secret JWT generation
- Google/Apple OAuth redirect URL generation
- OAuth code exchange
- Apple ID token verification
- Error handling

**Coverage Target**: Authentication flow logic

### `test_redis.py`
Unit tests for Redis database operations (`database/redis_db.py`):
- Generic cache operations
- App caching
- Username management
- Auth session management
- Conversation operations
- User data caching
- API key caching
- Migration status tracking
- Notification tracking
- Filter operations

**Coverage Target**: All Redis database functions

### `test_api_auth.py`
Integration tests for authentication API endpoints:
- Complete Google OAuth flow
- Complete Apple OAuth flow
- Token exchange endpoint
- Session management
- Error scenarios
- Concurrent requests

**Coverage Target**: Full auth API integration

### `test_transcribe_flow.py`
End-to-end tests for transcription user journey:
- WebSocket connection establishment
- Audio processing flow
- Conversation creation
- Transcript segments
- Speaker identification
- Usage tracking
- Translation flow
- Error handling
- Image processing
- Multi-language support

**Coverage Target**: Critical transcription paths

## Running Tests

### Prerequisites
```bash
cd backend
pip install -r requirements.txt
```

### Run All Tests
```bash
pytest tests/
```

### Run Specific Test Categories
```bash
# Unit tests only
pytest tests/ -m unit

# Integration tests only
pytest tests/ -m integration

# End-to-end tests only
pytest tests/ -m e2e
```

### Run Specific Test Files
```bash
# Encryption tests
pytest tests/test_encryption.py

# Auth tests
pytest tests/test_auth.py

# Redis tests
pytest tests/test_redis.py

# API auth tests
pytest tests/test_api_auth.py

# Transcribe flow tests
pytest tests/test_transcribe_flow.py
```

### Run with Coverage
```bash
# Generate coverage report
pytest tests/ --cov=. --cov-report=html --cov-report=term

# View HTML coverage report
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

### Run Specific Tests
```bash
# Run a specific test class
pytest tests/test_encryption.py::TestKeyDerivation

# Run a specific test method
pytest tests/test_encryption.py::TestKeyDerivation::test_derive_key_generates_32_bytes
```

## Test Configuration

### Environment Variables
Tests require the following environment variables (automatically set in conftest.py):
- `ENCRYPTION_SECRET`: 32+ byte encryption key
- `REDIS_DB_HOST`: Redis host
- `REDIS_DB_PORT`: Redis port
- `REDIS_DB_PASSWORD`: Redis password
- `GOOGLE_CLIENT_ID`: Google OAuth client ID
- `GOOGLE_CLIENT_SECRET`: Google OAuth client secret
- `APPLE_CLIENT_ID`: Apple OAuth client ID
- `APPLE_TEAM_ID`: Apple team ID
- `APPLE_KEY_ID`: Apple key ID
- `BASE_API_URL`: Base API URL

### Pytest Configuration
See `pytest.ini` for detailed pytest configuration including:
- Test discovery patterns
- Markers for test categorization
- Coverage options
- Logging configuration

### Coverage Configuration
See `.coveragerc` for coverage configuration including:
- Source paths
- Omitted files/directories
- Report options

## Fixtures

### `conftest.py`
Provides shared fixtures for all tests:
- `test_env_vars`: Test environment variables
- `mock_redis`: Mocked Redis client
- `mock_firebase_auth`: Mocked Firebase authentication
- `test_user_id` / `test_uid`: Test user ID
- `test_client`: FastAPI test client
- `mock_firestore`: Mocked Firestore client
- `sample_auth_session`: Sample auth session data
- `sample_oauth_credentials`: Sample OAuth credentials
- `mock_requests`: Mocked requests library
- `mock_user_db`: Mocked user database operations
- `mock_conversations_db`: Mocked conversations database
- `encryption_test_key`: Test encryption key

## Coverage Goals

### Current Status
Target: **50% minimum coverage**

### Coverage by Module
- `utils/encryption.py`: 90%+ (critical security component)
- `routers/auth.py`: 70%+ (authentication flows)
- `database/redis_db.py`: 80%+ (data layer)
- `routers/transcribe.py`: 50%+ (complex async logic)

## CI/CD Integration

Tests run automatically on:
- Push to `main`, `develop`, or `claude/**` branches
- Pull requests to `main` or `develop`
- Only when backend files change

See `.github/workflows/backend-tests.yml` for CI/CD configuration.

### CI/CD Jobs
1. **Test Job**: Runs on Python 3.11 and 3.12
   - Runs unit tests (fail on error)
   - Runs integration tests (continue on error)
   - Runs e2e tests (continue on error)
   - Generates coverage reports
   - Uploads coverage to Codecov
   - Comments coverage on PRs

2. **Lint Job**: Code quality checks
   - flake8 linting
   - black code formatting
   - isort import sorting

## Writing New Tests

### Test Structure
```python
"""
Brief description of what this test file covers.
"""
import pytest
from unittest.mock import MagicMock, patch

class TestFeatureName:
    """Tests for specific feature."""

    def test_specific_behavior(self, fixture_name):
        """Test description."""
        # Arrange
        # ...

        # Act
        # ...

        # Assert
        # ...
```

### Best Practices
1. **Use descriptive test names**: `test_derive_key_generates_32_bytes`
2. **Follow AAA pattern**: Arrange, Act, Assert
3. **Use fixtures**: Leverage conftest.py fixtures
4. **Mock external dependencies**: Database, APIs, file system
5. **Test edge cases**: Empty strings, None values, errors
6. **Add docstrings**: Brief description of what test covers
7. **Keep tests isolated**: No test should depend on another
8. **Use markers**: Tag tests as unit/integration/e2e

### Adding New Fixtures
Add shared fixtures to `conftest.py`:
```python
@pytest.fixture
def my_fixture():
    """Fixture description."""
    # Setup
    yield value
    # Teardown
```

### Async Tests
Mark async tests with `@pytest.mark.asyncio`:
```python
@pytest.mark.asyncio
async def test_async_function():
    result = await async_function()
    assert result is not None
```

## Troubleshooting

### Common Issues

**Import errors**: Ensure you're in the backend directory and have installed requirements

**Redis connection errors**: Tests mock Redis by default, no real Redis needed

**Firebase errors**: Tests mock Firebase authentication

**Coverage not showing**: Run with `--cov=.` flag from backend directory

**Async warnings**: Ensure `pytest-asyncio` is installed and tests are marked

## Maintenance

### Updating Tests
- Add tests for new features immediately
- Update tests when modifying existing code
- Maintain 50%+ coverage threshold
- Review and update mocks when dependencies change

### Test Performance
- Unit tests should be fast (<1s each)
- Integration tests can be slower (<5s each)
- E2E tests may take longer (<30s each)
- Use `@pytest.mark.slow` for slow tests

## Resources

- [Pytest Documentation](https://docs.pytest.org/)
- [pytest-cov Documentation](https://pytest-cov.readthedocs.io/)
- [pytest-asyncio Documentation](https://pytest-asyncio.readthedocs.io/)
- [Testing FastAPI Applications](https://fastapi.tiangolo.com/tutorial/testing/)
