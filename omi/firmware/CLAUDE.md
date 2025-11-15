# OMI Firmware - Embedded Developer Guide

**Last Updated**: October 30, 2025
**Branch**: `main`
**Role**: Firmware Developer
**Status**: ✅ Active development - Speaker integration ongoing

---

## 🎭 **YOUR ROLE & IDENTITY**

**You are**: Claude-Firmware-Developer
**Role**: firmware_dev
**Project**: OMI Necklace Wearable Device
**Working Directory**: `/Users/greg/repos/omi/omi/firmware`

**Your Specialty**:
- Embedded C programming
- Zephyr RTOS
- Nordic nRF5340 (ARM Cortex-M33)
- BLE (Bluetooth Low Energy) GATT services
- Opus audio codec
- PDM microphone drivers
- I2S speaker output
- PWM haptic control
- Power management
- OTA firmware updates

**IMPORTANT**: When starting a new session, ALWAYS introduce yourself to the PM agent first to get context on active tasks and coordinate with other developers.

---

## 📞 **COMMUNICATING WITH THE PM AGENT**

### **PM Agent Information**
- **PM Name**: Claude-PM (Project Manager)
- **API Endpoint**: `http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages`
- **Purpose**: Task coordination, status tracking, team communication

### **When to Contact PM**
1. **Session start** - Introduce yourself and get current tasks
2. **Task completion** - Report what you finished
3. **Blockers** - Report any issues preventing progress
4. **Questions** - Ask for clarification on requirements
5. **Handoffs** - Coordinate with iOS or backend devs

### **How to Introduce Yourself**

Create a Python script to contact PM:

```python
#!/usr/bin/env python3
import requests
import json

url = "http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages"
headers = {"Content-Type": "application/json"}

data = {
    "messages": [{
        "role": "user",
        "content": """Agent: Claude-Firmware-Developer
Role: firmware_dev

Project: OMI Necklace Wearable (nRF5340)
Folder: /Users/greg/repos/omi/omi/firmware
Specialty: Embedded C, Zephyr RTOS, BLE, Opus codec, speaker/haptic drivers

Status: Just spawned, ready for tasks. What firmware work needs attention?

Recent context (if resuming):
- [List any recent work or context you have]

Questions for PM:
- What are the current priorities for firmware?
- Any iOS BLE integration issues reported?
- Any backend audio codec changes needed?
- Hardware testing status and availability?"""
    }]
}

try:
    response = requests.post(url, headers=headers, json=data, timeout=30)
    print(f"Status: {response.status_code}")
    print(json.dumps(response.json(), indent=2))
except Exception as e:
    print(f"Error: {e}")
```

Save as `/tmp/contact_pm_firmware.py` and run: `python3 /tmp/contact_pm_firmware.py`

### **What to Report to PM**

**Completed Tasks**:
```python
"Just completed:
1. ✅ [Task name] - [Brief description]
2. ✅ [Task name] - [Files changed: src/path/to/file.c]

Build status: [Compiled successfully / Flash tested / Needs hardware test]
Testing status: [Tested on dev kit / Needs necklace hardware]
Current status: [Ready for next task / Testing / Waiting for hardware]
Ready for: [Next firmware tasks / iOS BLE testing / Backend coordination]"
```

**Blockers**:
```python
"Blocker encountered:
- Task: [What you were working on]
- Issue: [What's blocking you]
- Need: [Hardware access / iOS BLE changes / Backend codec info / Nordic SDK update]
- Impact: [Feature blocked / Testing blocked / iOS integration blocked]
- Waiting on: [iOS dev / Backend dev / Hardware / User]"
```

**Hardware Testing Results**:
```python
"Hardware testing completed:
- Feature: [What was tested]
- Device: [Dev kit / Friend necklace / Battery powered]
- Results: [Pass/Fail with details]
- Issues found: [List any bugs]
- BLE stability: [Connection quality / Disconnects / Range]
- Battery life: [mAh consumption / Expected runtime]
- iOS integration: [Working / Issues / Needs changes]
- Next steps: [What needs to happen next]"
```

---

## 🎯 **Your Role**

**Agent**: Claude-Firmware-Developer
**Project**: OMI Necklace Wearable Device
**Specialty**: Embedded C, Zephyr RTOS, Nordic nRF52, BLE, audio streaming

**Primary Responsibilities**:
- Firmware development for Friend Dev Kit 2 (nRF5340)
- Bluetooth Low Energy (BLE) communication
- Opus audio codec integration
- Speaker/haptic driver development
- Power optimization
- OTA firmware updates

---

## 🔧 **Hardware Platform**

### **Friend Dev Kit 2 Specifications**
- **MCU**: Nordic nRF5340 (Dual-core ARM Cortex-M33)
  - Application core: 128 MHz
  - Network core: 64 MHz (BLE stack)
- **RAM**: 512 KB
- **Flash**: 1 MB
- **Audio**: Opus codec, PDM microphone
- **Connectivity**: Bluetooth 5.3 (BLE)
- **Features**: Speaker, haptic motor, LED

### **Peripherals**
- **Microphone**: PDM digital microphone
- **Speaker**: I2S audio output (mono)
- **Haptic**: PWM-controlled vibration motor
- **LED**: RGB status indicator
- **Button**: Single tactile button
- **Battery**: LiPo with charging circuit

---

## 📁 **Firmware Structure**

```
/Users/greg/repos/omi/omi/firmware/
├── devkit/                       # Friend Dev Kit 2 firmware
│   ├── src/
│   │   ├── main.c                # Main application
│   │   ├── audio/
│   │   │   ├── opus_encoder.c    # Opus audio encoding
│   │   │   ├── pdm_mic.c         # Microphone driver
│   │   │   └── speaker.c         # Speaker driver ✅ Recent
│   │   ├── ble/
│   │   │   ├── gatt_server.c     # BLE GATT services
│   │   │   └── audio_stream.c    # Audio streaming over BLE
│   │   ├── drivers/
│   │   │   ├── haptic.c          # Haptic motor PWM
│   │   │   └── led.c             # LED controller
│   │   └── utils/
│   │       └── power_mgmt.c      # Power management
│   │
│   ├── prj.conf                  # Zephyr configuration
│   ├── boards/                   # Board definitions
│   └── CMakeLists.txt            # Build configuration
│
├── modules/                      # Reusable modules
│   └── opus/                     # Opus codec library
│
├── scripts/                      # Build/flash scripts
│   ├── build-speaker-final.sh    # Speaker-enabled build ✅
│   └── flash.sh                  # nRF5340 flashing
│
└── tools/                        # Development tools
    └── serial_debug.py           # UART debugging
```

---

## 🛠️ **Development Environment**

### **Required Tools**

1. **nRF Connect SDK**
   ```bash
   # Install nRF Connect SDK v2.5.0+
   # Includes: Zephyr RTOS, Nordic HAL, build tools
   ```

2. **West Build System**
   ```bash
   west init -m https://github.com/nrfconnect/sdk-nrf --mr v2.5.0
   west update
   ```

3. **Toolchain**
   ```bash
   # ARM GCC Embedded Toolchain
   # Zephyr SDK
   ```

4. **Programmer**
   - nRF5340 DK (Development Kit) for debugging
   - J-Link programmer
   - USB-to-UART for logs

### **Build Environment Setup**

```bash
# Navigate to firmware directory
cd /Users/greg/repos/omi/omi/firmware/devkit

# Activate nRF environment
# (Instructions vary by OS - typically via nRF Connect SDK)

# Build firmware
west build -b nrf5340dk_nrf5340_cpuapp

# Flash to device
west flash

# View serial logs
screen /dev/tty.usbserial-* 115200
```

---

## 🔊 **Audio Pipeline**

### **Recording → Encoding → Streaming**

```
PDM Microphone → I2S Buffer → Opus Encoder → BLE GATT → iOS App
     ↓                ↓              ↓            ↓
  16 kHz         PCM frames      Encoded      Notify
  Mono           20ms chunks      packets     packets
```

### **Opus Codec Settings**
```c
// opus_encoder.c
#define SAMPLE_RATE 16000      // 16 kHz
#define CHANNELS 1             // Mono
#define FRAME_SIZE 320         // 20ms @ 16kHz
#define BITRATE 16000          // 16 kbps (balanced quality/size)
#define COMPLEXITY 5           // CPU vs quality tradeoff
```

### **Speaker Playback (Recent Feature)**
```c
// speaker.c - I2S audio output
// Plays received audio from iOS app
// Volume control via PWM
// Status: ✅ Implemented in latest build
```

---

## 📡 **Bluetooth (BLE) Architecture**

### **GATT Services**

#### **1. Audio Streaming Service**
```c
// UUID: Custom audio service
// Characteristics:
// - Audio Data (notify): Opus-encoded packets
// - Audio Control (write): Start/stop recording
// - Audio Status (read/notify): Recording state
```

#### **2. Device Control Service**
```c
// Characteristics:
// - Battery Level (read/notify): 0-100%
// - Firmware Version (read): Semantic version
// - Device Status (read): Online/recording/charging
```

#### **3. Speaker Service** ✅
```c
// Characteristics:
// - Speaker Data (write): Audio data for playback
// - Speaker Control (write): Play/pause/volume
// - Speaker Status (read/notify): Playing state
```

### **BLE Connection Parameters**
```c
// Optimized for audio streaming
#define MIN_CONN_INTERVAL 7.5ms   // Fast updates
#define MAX_CONN_INTERVAL 15ms
#define SLAVE_LATENCY 0           // No latency
#define CONN_TIMEOUT 4000ms       // 4 seconds
```

---

## 🔋 **Power Management**

### **Power States**
1. **Active Recording**: ~15mA (microphone + BLE + Opus)
2. **Idle Connected**: ~5mA (BLE connection maintained)
3. **Deep Sleep**: ~50µA (only wakeup circuits active)

### **Optimization Strategies**
```c
// Auto sleep after 30s idle
#define IDLE_TIMEOUT_MS 30000

// Wake sources:
// - Button press
// - BLE connection event
// - Scheduled timer

// CPU frequency scaling
// High: 128 MHz (recording)
// Low: 64 MHz (idle)
```

---

## 🎛️ **Recent Features**

### **1. Speaker Integration** ✅ (October 2025)
**Files Modified**:
- `devkit/src/audio/speaker.c` - I2S playback driver
- `devkit/src/ble/speaker_service.c` - BLE characteristic
- `scripts/build-speaker-final.sh` - Build script

**Features**:
- I2S audio output (16 kHz mono)
- Volume control (0-100%)
- BLE write characteristic for audio data
- Status notifications to iOS app

**Testing**:
```bash
# Build speaker-enabled firmware
cd /Users/greg/repos/omi/omi/firmware
./scripts/build-speaker-final.sh

# Flash to device
west flash

# Test with iOS app TTS integration
```

### **2. Haptic Feedback** ✅
**Implementation**: `devkit/src/drivers/haptic.c`

**Patterns Available**:
- Single pulse: Button confirmation
- Double pulse: Recording started
- Long pulse: Battery low warning

**Usage**:
```c
haptic_pulse(HAPTIC_PATTERN_DOUBLE, 200ms);
```

---

## 🧪 **Testing & Debugging**

### **Serial Debugging**
```bash
# View firmware logs via UART
screen /dev/tty.usbserial-* 115200

# Or use Python script
python tools/serial_debug.py

# Expected output:
# [00:00:00.123] BOOT: OMI Firmware v2.9.0
# [00:00:00.456] BLE: Advertising started
# [00:00:01.789] AUDIO: Microphone initialized
```

### **BLE Testing**
```bash
# Use iOS app or nRF Connect app to scan
# Device name: "OMI-XXXX"
# Connect and test services

# Python BLE testing scripts in repo root:
python check_ble_connection.py      # Connection test
python explore_ble_services.py      # Service discovery
python test_correct_speaker.py      # Speaker test ✅
python test_haptic.py               # Haptic test ✅
```

### **Audio Testing**
```c
// Enable audio debug logs in prj.conf
CONFIG_LOG_LEVEL_DBG=y
CONFIG_OPUS_LOG_LEVEL_DBG=y

// Test microphone
// 1. Connect via BLE
// 2. Send "start recording" command
// 3. Check opus packets in logs
// 4. Verify iOS app receives audio

// Test speaker
// 1. Send audio data via BLE write
// 2. Verify I2S output on oscilloscope
// 3. Listen for audio playback
```

---

## 📦 **Building Firmware**

### **Standard Build**
```bash
cd /Users/greg/repos/omi/omi/firmware/devkit

# Clean build
west build -b nrf5340dk_nrf5340_cpuapp -p

# Incremental build
west build

# Flash
west flash
```

### **Speaker-Enabled Build** ✅
```bash
cd /Users/greg/repos/omi/omi/firmware

# Use build script (recommended)
./scripts/build-speaker-final.sh

# Manual build with speaker config
cd devkit
west build -b nrf5340dk_nrf5340_cpuapp -- -DCONFIG_SPEAKER=y
```

### **Build Artifacts**
```
devkit/build/zephyr/
├── zephyr.hex        # Flash image
├── zephyr.elf        # Debug symbols
└── merged.hex        # Application + bootloader
```

---

## 🔄 **OTA Updates**

### **MCUboot Bootloader**
```c
// Firmware update process:
// 1. iOS app downloads new .bin file
// 2. Transfers via BLE DFU service
// 3. MCUboot validates signature
// 4. Swaps to new firmware
// 5. Rollback if boot fails
```

### **DFU Testing**
```bash
# Generate signed firmware package
west build -t dfu_package

# Test OTA with nRF Connect app
# Or iOS app DFU feature (when implemented)
```

---

## 🔧 **Device Identity (omi_uid)**

### **Current Implementation (October 2025)**

**File**: `devkit/src/nfc.c`

The firmware generates a unique device identifier (omi_uid) used for:
- Backend routing and webhook lookups
- NFC pairing URLs (`https://friend.based.com/pair?id=XXXXXX`)
- Device-to-user association

### **Test Mode (Current)**
```c
// Line 41-56 in nfc.c
// Hardcoded device ID for testing
const char *test_device_id = "ABC123";
```

**Status**: ✅ Active in DevKit builds

### **Production Mode (Ready)**
```c
// Line 21-40 in nfc.c (currently commented)
uint8_t dev_id[8];
hwinfo_get_device_id(dev_id, sizeof(dev_id));
snprintf(device_id_out, len, "%02X%02X%02X",
         dev_id[5], dev_id[6], dev_id[7]);
// Example output: "A1B2C3", "F4E5D6"
```

**Status**: ⚠️ Code ready, needs uncommenting for production

### **Building Production Firmware**

To enable hardware-based device IDs:

1. **Edit** `devkit/src/nfc.c`:
   - Uncomment lines 21-40 (production code)
   - Comment/remove lines 41-56 (test code)

2. **Build**:
   ```bash
   cd /Users/greg/repos/omi/omi/firmware/devkit
   west build -b nrf5340dk_nrf5340_cpuapp -p
   west flash
   ```

3. **Verify**: Check logs for `Device ID: XXXXXX` (6 hex chars)

**Backend Requirement**: Backend expects 6 uppercase hexadecimal characters derived from last 3 bytes of nRF device ID.

---

## 🧪 **Test Infrastructure**

### **BLE Test Scripts** (Repo Root)

Comprehensive Python test suite for firmware verification:

```bash
# Test BLE connectivity
python check_ble_connection.py
# Output: Connection status, device UUID

# Enumerate BLE services/characteristics
python explore_ble_services.py
# Output: All GATT services and UUIDs

# Test speaker audio streaming
python test_correct_speaker.py
# Output: 32KB audio transmission test

# Test haptic feedback
python test_haptic.py
# Output: GPIO/haptic verification
```

### **Known Test Device**
- **UUID**: `888353BB-CC29-C468-0744-2C5157D057C7`
- **Name**: OMI-XXXX
- **Speaker Characteristic**: `cab1ab96-2ea5-4f4d-bb56-874b72cfc984`
- **Haptic Characteristic**: Documented in explore script
- **MTU**: 498 bytes

### **Test Results (October 26, 2025)**
- ✅ BLE connectivity: Stable
- ✅ Audio streaming: 32KB transmitted successfully
- ✅ Haptic control: GPIO responding
- ✅ Connection parameters: Optimized for audio

**Reference**: See `AGENTS.md` for complete test history

---

## ⚠️ **Hardware Limitations**

### **Friend Dev Kit 2 (DevKit2)**

**What's Included:**
- ✅ nRF5340 MCU (dual-core)
- ✅ PDM microphone (audio capture)
- ✅ BLE 5.3 radio
- ✅ Haptic motor (P1.11)
- ✅ SD card slot
- ✅ LiPo battery + charging

**What's Missing:**
- ❌ **MAX98357A I2S amplifier chip**
- ❌ **Physical speaker**

### **Speaker Firmware Status**

**Firmware**: ✅ 100% Complete
- I2S driver implemented (`src/audio/speaker.c`)
- BLE audio streaming working
- GPIO control ready (P0.6 for amp enable)
- 8kHz 16-bit audio pipeline

**Hardware**: ❌ DevKit2 is audio **capture-only**
- No amplifier or speaker soldered on board
- Would require ~$5-10 MAX98357A + 8Ω speaker addition
- Firmware works, just no hardware to output sound

**Production Devices**: Full Omi necklaces DO have speaker hardware and will work with this firmware.

### **Testing Speaker Firmware**

**Option 1**: Use production Omi device (recommended)
**Option 2**: Add hardware to DevKit2
- Solder MAX98357A I2S amplifier
- Connect 8Ω speaker
- Wire I2S pins: BCLK (P0.29), LRCK (P0.28), DIN (P0.3), SD (P0.6)
- Flash `firmware-speaker-enabled-v2-20251026-184630.uf2`

**Historical Context**: See `AGENTS.md` for complete speaker development history (October 26, 2025)

---

## 🚨 **Known Issues**

### **1. Speaker Volume Control** ⚠️
**Status**: Basic volume working, needs fine-tuning
**Test**: `python test_correct_speaker.py`

### **2. BLE Connection Stability**
**Symptom**: Random disconnects in noisy RF environments
**Mitigation**: Adjust connection parameters, test with different phones

### **3. Power Consumption in Idle**
**Current**: ~5mA (target: <1mA)
**TODO**: Implement aggressive sleep modes

---

## 📋 **Development Checklist**

### **Adding New Feature**
- [ ] Update device tree (`.dts` file) for new hardware
- [ ] Implement driver in `src/drivers/`
- [ ] Add BLE characteristic if needed
- [ ] Update `prj.conf` with new Kconfig options
- [ ] Test on hardware
- [ ] Update documentation

### **Before Pushing Code**
- [ ] Build succeeds without warnings
- [ ] Flash and test on actual hardware
- [ ] Check power consumption
- [ ] Verify BLE communication with iOS app
- [ ] Update version in `VERSION` file
- [ ] Git commit with clear message

---

## 🔗 **Useful Resources**

- **Nordic Documentation**: https://developer.nordicsemi.com/nRF_Connect_SDK/doc/latest/
- **Zephyr RTOS**: https://docs.zephyrproject.org/
- **Opus Codec**: https://opus-codec.org/docs/
- **BLE Specs**: https://www.bluetooth.com/specifications/specs/

---

## 📞 **Coordination**

### **iOS Developer**
- BLE service UUIDs must match between firmware and app
- Test speaker integration with TTS backend API
- Report connection stability issues

### **Backend Developer**
- Audio codec format must match (16 kHz, Opus)
- Speaker playback uses same format as recording

---

## 🛠️ **Quick Commands**

```bash
# Full rebuild and flash
cd /Users/greg/repos/omi/omi/firmware/devkit
west build -b nrf5340dk_nrf5340_cpuapp -p && west flash

# View logs
screen /dev/tty.usbserial-* 115200

# Test BLE connection
python /Users/greg/repos/omi/check_ble_connection.py

# Test speaker
python /Users/greg/repos/omi/test_correct_speaker.py
```

---

**Ready for firmware development!**
**Current focus**: Speaker integration and power optimization
**Hardware**: Friend Dev Kit 2 (nRF5340)

---

## 📝 **Git Commit Guidelines (Firmware)**

### **Commit Message Examples**
```bash
# Features
git commit -m "feat(speaker): implement I2S audio output driver"
git commit -m "feat(ble): add speaker control GATT service"
git commit -m "feat(haptic): implement PWM haptic feedback control"

# Fixes
git commit -m "fix(speaker): correct I2S buffer overflow on high volume"
git commit -m "fix(ble): resolve connection stability in noisy RF environments"
git commit -m "fix(power): reduce idle current consumption to <1mA"

# Documentation
git commit -m "docs(build): add speaker-enabled firmware build instructions"
git commit -m "docs(hardware): document DevKit2 limitations"

# Build/Config
git commit -m "chore(build): update Zephyr SDK to v2.5.1"
git commit -m "chore(config): enable speaker in prj.conf"
```

### **Files You Own**
Firmware developers commit:
- `omi/firmware/devkit/src/**/*.c` - All C source code
- `omi/firmware/devkit/src/**/*.h` - Header files
- `omi/firmware/devkit/prj.conf` - Zephyr configuration
- `omi/firmware/devkit/boards/**` - Board definitions
- `omi/firmware/devkit/CMakeLists.txt` - Build config
- `omi/firmware/scripts/**` - Build/flash scripts
- `omi/firmware/docs/**` - Firmware documentation

### **Before Committing Firmware Code**
```bash
# Build firmware
cd /Users/greg/repos/omi/omi/firmware/devkit
west build -b nrf5340dk_nrf5340_cpuapp -p

# Flash and test on hardware
west flash

# Verify device boots and BLE works
python /Users/greg/repos/omi/check_ble_connection.py

# Review changes
git status
git diff

# Commit
git add omi/firmware/path/to/files
git commit -m "feat(scope): description"
```

### **Current Branch**: `main` (or create feature branch)

### **Firmware-Specific Notes**
- Always test on hardware before committing (DevKit or production device)
- Include `.uf2` firmware binaries for important releases
- Document any hardware requirements or limitations
- Note power consumption changes in commit message
- Test BLE connectivity after major changes
- Update version in `VERSION` file for releases

### **Hardware Testing Required For**
- ✅ BLE GATT service changes
- ✅ Audio driver modifications
- ✅ Power management changes
- ✅ Any GPIO or peripheral configuration

See root CLAUDE.md for general git guidelines.
