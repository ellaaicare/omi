# PRD: Backend Testing Infrastructure

## Status
🔴 **CRITICAL** - Zero test coverage currently

## Executive Summary
Build comprehensive testing infrastructure for the backend to achieve 80% code coverage, prevent regressions, and enable confident refactoring.

## Problem Statement
- **Current Test Coverage:** 0% (no test files found)
- **Risk:** No safety net for deployments or refactoring
- **Impact:** Bugs reach production, manual testing is time-consuming

## Goals
- Achieve 80% code coverage
- Implement pytest suite with unit, integration, and e2e tests
- Add CI/CD integration
- Create testing best practices documentation

## Success Metrics
- ✅ 80% code coverage
- ✅ All critical paths covered
- ✅ Tests run in <5 minutes
- ✅ Zero flaky tests
- ✅ Integrated with CI/CD

## Technical Specification

### Test Structure
```
backend/tests/
├── conftest.py              # Shared fixtures
├── unit/
│   ├── test_auth.py
│   ├── test_encryption.py
│   ├── database/
│   │   ├── test_conversations.py
│   │   ├── test_memories.py
│   │   └── test_redis.py
│   └── utils/
│       ├── test_llm.py
│       └── test_stt.py
├── integration/
│   ├── test_api_auth.py
│   ├── test_transcribe_flow.py
│   ├── test_payment_flow.py
│   └── test_conversation_processing.py
├── e2e/
│   ├── test_full_transcription.py
│   └── test_user_journey.py
└── fixtures/
    ├── audio_samples/
    ├── mock_data.py
    └── test_users.py
```

### Implementation

**1. Setup pytest Infrastructure**

```python
# backend/requirements-dev.txt
pytest==7.4.0
pytest-asyncio==0.21.0
pytest-cov==4.1.0
pytest-mock==3.11.1
httpx==0.24.1
fakeredis==2.19.0
firebase-admin-mock==1.0.0

# backend/pyproject.toml
[tool.pytest.ini_options]
minversion = "7.0"
addopts = "-ra -q --cov=backend --cov-report=html --cov-report=term"
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.coverage.run]
source = ["backend"]
omit = [
    "*/tests/*",
    "*/migrations/*",
    "*/__pycache__/*"
]

[tool.coverage.report]
exclude_lines = [
    "pragma: no cover",
    "def __repr__",
    "raise AssertionError",
    "raise NotImplementedError",
    "if __name__ == .__main__.:",
]
```

**2. Shared Fixtures**

```python
# backend/tests/conftest.py
import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as firebase_auth
from unittest.mock import Mock, patch
import fakeredis

from backend.main import app
from backend.database._client import db
from backend.database.redis_db import r

@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)

@pytest.fixture
def mock_firebase_auth():
    """Mock Firebase authentication."""
    with patch('firebase_admin.auth.verify_id_token') as mock:
        mock.return_value = {'uid': 'test_user_123'}
        yield mock

@pytest.fixture
def mock_redis():
    """Mock Redis for testing."""
    fake_redis = fakeredis.FakeRedis()
    with patch('backend.database.redis_db.r', fake_redis):
        yield fake_redis

@pytest.fixture
def mock_firestore():
    """Mock Firestore database."""
    mock_db = Mock()
    with patch('backend.database._client.db', mock_db):
        yield mock_db

@pytest.fixture
def test_user_uid():
    """Standard test user UID."""
    return "test_user_123"

@pytest.fixture
def auth_headers(test_user_uid):
    """Authentication headers for test requests."""
    return {"Authorization": f"Bearer test_token_{test_user_uid}"}
```

**3. Unit Tests Examples**

```python
# backend/tests/unit/test_encryption.py
import pytest
from backend.utils.encryption import encrypt, decrypt, derive_key

def test_encrypt_decrypt_roundtrip():
    """Test encryption and decryption work correctly."""
    original_data = "sensitive information"
    uid = "user123"

    encrypted = encrypt(uid, original_data)
    decrypted = decrypt(uid, encrypted)

    assert decrypted == original_data
    assert encrypted != original_data

def test_encryption_different_users():
    """Test different users get different encryptions."""
    data = "same data"

    encrypted1 = encrypt("user1", data)
    encrypted2 = encrypt("user2", data)

    assert encrypted1 != encrypted2

def test_derive_key_consistency():
    """Test key derivation is consistent."""
    uid = "user123"

    key1 = derive_key(uid)
    key2 = derive_key(uid)

    assert key1 == key2

# backend/tests/unit/database/test_redis.py
def test_app_usage_count_cache(mock_redis):
    """Test app usage count caching."""
    from backend.database.redis_db import set_app_usage_count_cache, get_app_usage_count_cache

    app_id = "test_app_123"
    count = 42

    set_app_usage_count_cache(app_id, count)
    result = get_app_usage_count_cache(app_id)

    assert result == count

def test_cache_expiration(mock_redis):
    """Test cache values expire correctly."""
    from backend.database.redis_db import r

    r.setex("test_key", 1, "test_value")
    assert r.get("test_key") == b"test_value"

    import time
    time.sleep(2)

    assert r.get("test_key") is None

# backend/tests/unit/test_auth.py
def test_get_current_user_uid_valid_token(mock_firebase_auth):
    """Test user UID extraction from valid token."""
    from backend.utils.other.endpoints import get_current_user_uid

    uid = get_current_user_uid("valid_token")

    assert uid == "test_user_123"
    mock_firebase_auth.assert_called_once_with("valid_token")

def test_get_current_user_uid_invalid_token(mock_firebase_auth):
    """Test error handling for invalid token."""
    from backend.utils.other.endpoints import get_current_user_uid
    from fastapi import HTTPException

    mock_firebase_auth.side_effect = Exception("Invalid token")

    with pytest.raises(HTTPException) as exc_info:
        get_current_user_uid("invalid_token")

    assert exc_info.value.status_code == 401
```

**4. Integration Tests**

```python
# backend/tests/integration/test_api_auth.py
import pytest
from fastapi import status

def test_protected_endpoint_without_auth(client):
    """Test protected endpoints require authentication."""
    response = client.get("/v1/users/me")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED

def test_protected_endpoint_with_auth(client, mock_firebase_auth, auth_headers):
    """Test protected endpoints work with valid auth."""
    response = client.get("/v1/users/me", headers=auth_headers)

    assert response.status_code == status.HTTP_200_OK

# backend/tests/integration/test_transcribe_flow.py
@pytest.mark.asyncio
async def test_websocket_transcription_flow(mock_firebase_auth, mock_redis):
    """Test full WebSocket transcription flow."""
    from fastapi.testclient import TestClient
    from backend.main import app

    client = TestClient(app)

    with client.websocket_connect("/v4/listen?uid=test_user_123") as websocket:
        # Send audio chunk
        audio_data = b"fake_audio_data"
        websocket.send_bytes(audio_data)

        # Receive transcription
        response = websocket.receive_json()

        assert "transcript" in response or "segments" in response

# backend/tests/integration/test_payment_flow.py
def test_create_checkout_session(client, mock_firebase_auth, auth_headers, monkeypatch):
    """Test Stripe checkout session creation."""
    # Mock Stripe
    mock_stripe_session = Mock()
    mock_stripe_session.id = "sess_123"
    mock_stripe_session.url = "https://checkout.stripe.com/sess_123"

    with patch('stripe.checkout.Session.create', return_value=mock_stripe_session):
        response = client.post(
            "/v1/payments/checkout-session",
            headers=auth_headers,
            json={"price_id": "price_123"}
        )

    assert response.status_code == 200
    assert response.json()["url"] == "https://checkout.stripe.com/sess_123"
```

**5. E2E Tests**

```python
# backend/tests/e2e/test_user_journey.py
@pytest.mark.e2e
def test_complete_user_flow(client):
    """Test complete user journey from signup to conversation."""
    # 1. Signup/Auth
    auth_response = client.post("/v1/auth/signup", json={
        "email": "test@example.com",
        "password": "secure_password"
    })
    assert auth_response.status_code == 200
    token = auth_response.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. Create conversation
    conv_response = client.post(
        "/v1/conversations",
        headers=headers,
        json={"title": "Test Meeting"}
    )
    assert conv_response.status_code == 200
    conversation_id = conv_response.json()["id"]

    # 3. Add memories
    memory_response = client.post(
        "/v1/memories",
        headers=headers,
        json={
            "conversation_id": conversation_id,
            "text": "Important point discussed"
        }
    )
    assert memory_response.status_code == 200

    # 4. Retrieve conversation
    get_response = client.get(
        f"/v1/conversations/{conversation_id}",
        headers=headers
    )
    assert get_response.status_code == 200
    assert get_response.json()["title"] == "Test Meeting"
```

**6. CI/CD Integration**

```yaml
# .github/workflows/backend-tests.yml
name: Backend Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest

    services:
      redis:
        image: redis:7
        ports:
          - 6379:6379

    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          cd backend
          pip install -r requirements.txt
          pip install -r requirements-dev.txt

      - name: Run tests
        run: |
          cd backend
          pytest --cov=backend --cov-report=xml --cov-report=term
        env:
          REDIS_DB_HOST: localhost
          REDIS_DB_PORT: 6379

      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          file: ./backend/coverage.xml

      - name: Check coverage threshold
        run: |
          cd backend
          pytest --cov=backend --cov-fail-under=80
```

## Implementation Plan

### Week 1: Infrastructure Setup
- [ ] Add pytest dependencies
- [ ] Create test directory structure
- [ ] Set up fixtures and mocks
- [ ] Configure CI/CD pipeline

### Week 2: Unit Tests (50% coverage)
- [ ] Test auth and encryption
- [ ] Test database operations
- [ ] Test utility functions
- [ ] Test LLM/STT integrations

### Week 3: Integration Tests (70% coverage)
- [ ] Test API endpoints
- [ ] Test WebSocket flows
- [ ] Test payment flows
- [ ] Test conversation processing

### Week 4: E2E Tests & Optimization (80% coverage)
- [ ] Complete user journey tests
- [ ] Optimize test performance
- [ ] Fix flaky tests
- [ ] Documentation

## Testing Best Practices

### 1. Test Naming Convention
```python
def test_<function>_<scenario>_<expected_result>():
    """Clear description of what is being tested."""
    pass
```

### 2. AAA Pattern
```python
def test_example():
    # Arrange
    user = create_test_user()

    # Act
    result = perform_action(user)

    # Assert
    assert result == expected_value
```

### 3. Mocking External Services
Always mock:
- Firebase calls
- Stripe API
- OpenAI/LLM calls
- External HTTP requests
- File system operations (where possible)

### 4. Test Data Management
```python
@pytest.fixture
def sample_conversation():
    return {
        "id": "conv_123",
        "user_id": "user_123",
        "title": "Test Conversation",
        "created_at": datetime.now(),
        "status": "in_progress"
    }
```

## Rollout Plan

1. **Week 1:** Infrastructure + 20% coverage
2. **Week 2:** 50% coverage (unit tests)
3. **Week 3:** 70% coverage (integration tests)
4. **Week 4:** 80% coverage (e2e tests + optimization)

## Success Criteria

- [ ] 80% code coverage
- [ ] All tests pass in CI/CD
- [ ] Test suite runs in <5 minutes
- [ ] Zero flaky tests
- [ ] Documentation complete

---

**Estimated Effort:** 4 weeks
**Priority:** 🔴 CRITICAL
**Dependencies:** None
**Target Completion:** 4 weeks
