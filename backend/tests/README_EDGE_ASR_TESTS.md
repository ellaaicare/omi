# Edge ASR Test Suite

Comprehensive test suite for the edge ASR integration that allows iOS devices to send pre-transcribed text from on-device ASR.

## Quick Start

### Prerequisites
```bash
# Install pytest if not already installed
pip install pytest pytest-asyncio websockets
```

### Run All Tests
```bash
# From backend directory
cd /Users/greg/repos/omi/backend

# Make sure backend server is running in another terminal
python start_server.py

# Run all edge ASR tests
pytest tests/test_edge_asr.py -v
```

### Run Specific Test Classes
```bash
# Test basic functionality only
pytest tests/test_edge_asr.py::TestEdgeASRBasic -v

# Test multiple segments
pytest tests/test_edge_asr.py::TestEdgeASRMultipleSegments -v

# Test speaker handling
pytest tests/test_edge_asr.py::TestEdgeASRSpeakerHandling -v

# Test error handling
pytest tests/test_edge_asr.py::TestEdgeASRErrorHandling -v

# Full integration test
pytest tests/test_edge_asr.py::TestEdgeASRIntegration -v
```

### Run Specific Test
```bash
# Test single segment sending
pytest tests/test_edge_asr.py::TestEdgeASRBasic::test_send_valid_segment -v

# Test full conversation flow
pytest tests/test_edge_asr.py::TestEdgeASRIntegration::test_full_conversation_flow -v
```

### Manual Test
```bash
# Quick manual test for development
python tests/test_edge_asr.py
```

## Test Coverage

### Basic Functionality (`TestEdgeASRBasic`)
- ✅ WebSocket connection with edge ASR client
- ✅ Send valid segment with all fields
- ✅ Send minimal segment (only required fields)
- ✅ Empty text handling (ignored gracefully)
- ✅ Whitespace-only text handling

### Multiple Segments (`TestEdgeASRMultipleSegments`)
- ✅ Send 3 segments in sequence (simulates conversation)
- ✅ Rapid segment sending (no delay)

### Speaker Handling (`TestEdgeASRSpeakerHandling`)
- ✅ Single speaker conversation
- ✅ Missing speaker field (defaults to SPEAKER_00)

### Timestamp Handling (`TestEdgeASRTimestamps`)
- ✅ Segments with start/end timestamps
- ✅ Segments without timestamps (defaults to 0)

### Confidence Scores (`TestEdgeASRConfidence`)
- ✅ High confidence segments (0.95+)
- ✅ Low confidence segments (0.65)

### Language Support (`TestEdgeASRLanguages`)
- ✅ English (en)
- ✅ Spanish (es)

### Error Handling (`TestEdgeASRErrorHandling`)
- ✅ Invalid JSON (handled gracefully)
- ✅ Missing 'type' field (ignored)
- ✅ Wrong 'type' value (ignored)

### Integration (`TestEdgeASRIntegration`)
- ✅ Full conversation flow (4 segments + stop)

## Test Data Formats

### Valid Segment (All Fields)
```json
{
  "type": "transcript_segment",
  "text": "I went for a walk in the park this morning",
  "speaker": "SPEAKER_00",
  "start": 0.0,
  "end": 3.5,
  "is_final": true,
  "confidence": 0.95
}
```

### Minimal Segment (Required Only)
```json
{
  "type": "transcript_segment",
  "text": "Hello world"
}
```

## Expected Behavior

### Successful Processing
- Backend receives segment
- Creates `TranscriptSegment` object
- Feeds into `stream_transcript` pipeline
- Logs: `📱 Edge ASR segment: ...`
- Connection stays open

### Ignored Cases
- Empty text (`""`)
- Whitespace-only text (`"   "`)
- Missing `type` field
- Wrong `type` value (not `"transcript_segment"`)

### Default Values
- `speaker`: `"SPEAKER_00"`
- `start`: `0`
- `end`: `0`
- `is_final`: `true`

## Debugging

### Check Backend Logs
When tests run, backend should log:
```
📱 Edge ASR segment: I went for a walk in the park this mor...
```

### Common Issues

**Connection Refused**:
```bash
# Make sure backend is running
python start_server.py
```

**Authentication Failed**:
```bash
# Verify .env has LOCAL_DEVELOPMENT=true
grep LOCAL_DEVELOPMENT .env
```

**Tests Timeout**:
```bash
# Increase timeout in test if needed
await asyncio.sleep(2.0)  # Increase from 0.5
```

## CI/CD Integration

### GitHub Actions Example
```yaml
- name: Run Edge ASR Tests
  run: |
    cd backend
    source venv/bin/activate
    python start_server.py &
    sleep 5
    pytest tests/test_edge_asr.py -v
    pkill -f start_server
```

### Pre-commit Hook
```bash
#!/bin/bash
# .git/hooks/pre-commit
cd backend
pytest tests/test_edge_asr.py -q || exit 1
```

## Performance Benchmarks

Expected test execution times:
- Single test: ~0.5-1.5 seconds
- Full test suite: ~15-30 seconds
- Integration test: ~5-10 seconds

## Future Enhancements

- [ ] Add Firestore verification (check conversation created)
- [ ] Add scanner agent trigger verification
- [ ] Add memory extraction verification
- [ ] Add load testing (1000+ segments)
- [ ] Add concurrent client testing
- [ ] Add real iOS device integration test

## Documentation

- Implementation: `docs/EDGE_ASR_INTEGRATION_GUIDE.md`
- iOS Requirements: Posted to Discord #general channel
- Backend Code: `routers/transcribe.py` lines 1100-1115

## Support

If tests fail unexpectedly:
1. Check backend server is running
2. Check `.env` has `LOCAL_DEVELOPMENT=true`
3. Check test user exists in Firestore (UID: HbBdbnRkPJhpYFIIsd34krM8FKD3)
4. Check backend logs for errors
5. Run manual test: `python tests/test_edge_asr.py`

---

**Last Updated**: November 8, 2025
**Test Coverage**: 25 tests across 8 test classes
**Status**: ✅ Ready for CI/CD integration
