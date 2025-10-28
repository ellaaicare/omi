# OMI Backend Documentation

This directory contains comprehensive documentation for the OMI backend infrastructure.

## 📋 Documentation Files

### 1. **SESSION_SUMMARY.md**
Complete overview of the backend setup session (October 27-28, 2025):
- All accomplishments and components built
- Test results and validation
- Configuration details
- ML models downloaded (17GB)
- Firestore setup
- Security considerations
- Future development roadmap

**When to Read**: To understand what was built, why, and the complete state of the backend infrastructure.

---

### 2. **SECURITY_HIPAA_CHECKLIST.md**
Critical security and HIPAA compliance requirements for production deployment:
- Firestore security rules (MUST change within 7 days)
- Environment variable cleanup
- API key rotation
- HIPAA compliance checklist (PHI protection)
- Business Associate Agreements (BAA)
- Encryption requirements
- Audit logging
- Incident response procedures

**When to Read**: **BEFORE deploying to production** - contains mandatory security changes.

⚠️ **CRITICAL**: Firestore security rules are currently OPEN (30-day temporary setting). Must be restricted before production deployment.

---

### 3. **README_TESTING.md**
Comprehensive testing guide:
- Complete test infrastructure overview
- Step-by-step testing instructions
- All test scenarios (synthetic audio, real audio files)
- Expected outputs and verification
- Troubleshooting common issues
- Advanced testing options

**When to Read**: When you want to thoroughly test the backend or understand all testing capabilities.

---

### 4. **QUICK_TEST.md**
Minimal quick-start testing guide:
- Fastest path to verify backend works
- 3-step process
- Essential commands only
- Basic verification steps

**When to Read**: When you just want to quickly verify the backend is working.

---

### 5. **TESTING_SETUP_NOTE.md**
Troubleshooting reference for common setup issues:
- Firestore API enablement
- Database creation steps
- Alternative testing methods
- Workarounds for common blockers
- Firebase project setup

**When to Read**: When you encounter errors during setup or testing.

---

## 🚀 Quick Navigation

**Just Starting?**
1. Read: `../CLAUDE.md` (Backend root directory)
2. Run: Quick Start commands in CLAUDE.md

**Setting Up Testing?**
1. Read: `QUICK_TEST.md`
2. Run: `python test_omi_device_simulation.py --audio-file test_audio/pyannote_sample.wav`

**Encountered Issues?**
1. Check: `TESTING_SETUP_NOTE.md`
2. Review: "Troubleshooting" section in `../CLAUDE.md`

**Preparing for Production?**
1. **MUST READ**: `SECURITY_HIPAA_CHECKLIST.md`
2. Complete: All items in pre-production checklist
3. Timeline: 4 weeks minimum from development to production

**Want Complete Context?**
1. Read: `SESSION_SUMMARY.md`
2. Review: Architecture decisions and implementation details

---

## 📁 Related Files (Outside docs/)

### In Backend Root (`../`)
- **CLAUDE.md**: Primary developer guide - START HERE
- **start_server.py**: Backend startup script with environment setup
- **test_omi_device_simulation.py**: Device simulator for testing
- **create_test_user.py**: Firestore test user setup
- **download_models.py**: PyAnnote model downloader
- **download_whisper_models.py**: WhisperX model downloader

### Test Audio (`../test_audio/`)
- **pyannote_sample.wav**: 30s, 16kHz mono (best for speaker diarization)
- **silero_test.wav**: 60s, 16kHz mono (full pipeline test)
- **librivox_sample.wav**: 38.8s, 48kHz stereo (audio conversion test)
- **conversation_sample.wav**: 9.2s, 44.1kHz stereo (quick smoke test)

---

## 🎯 Documentation Maintenance

**When Adding New Features**:
- Update relevant documentation files
- Add new test scenarios to README_TESTING.md
- Update CLAUDE.md with new commands or procedures

**When Fixing Issues**:
- Add solutions to TESTING_SETUP_NOTE.md
- Update troubleshooting sections

**Before Production Deployment**:
- Review and complete SECURITY_HIPAA_CHECKLIST.md
- Update SESSION_SUMMARY.md with deployment details
- Document any production-specific configuration

---

## 📞 Support Resources

- **Firebase Console**: https://console.firebase.google.com/project/omi-dev-ca005
- **Deepgram Dashboard**: https://console.deepgram.com/
- **Hugging Face Models**: https://huggingface.co/pyannote

---

**Last Updated**: October 28, 2025
