# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with the Omi firmware for Nordic nRF5340-based hardware.

## Overview

The Omi firmware is a sophisticated embedded system built on Nordic nRF5340 dual-core SoC using Zephyr RTOS v2.9.0. It provides real-time audio processing with OPUS codec, Bluetooth Low Energy communication, comprehensive peripheral management, and secure over-the-air (OTA) updates through MCUboot bootloader system.

## Architecture

### Nordic nRF5340 Dual-Core Platform
```
┌─────────────────────────────────────────────────────────┐
│                nRF5340 SoC Architecture                 │
├─────────────────────────┬───────────────────────────────┤
│    Application Core     │       Network Core           │
│    (Cortex-M33)         │       (Cortex-M33)          │
├─────────────────────────┼───────────────────────────────┤
│ • Main application      │ • Bluetooth LE stack         │
│ • OPUS audio codec      │ • Radio management            │
│ • Peripheral drivers    │ • OTA protocol                │
│ • File system          │ • MCUmgr services             │
│ • 1MB Flash/512KB RAM  │ • 256KB Flash/64KB RAM        │
└─────────────────────────┴───────────────────────────────┘
```

### Zephyr RTOS Structure
- **Real-time OS**: Thread-based scheduling with priority management
- **Device Tree System**: Hardware abstraction in `.dts` files
- **Kconfig**: Build-time configuration system
- **West Workspace**: Multi-repository dependency management

### MCUboot Secure Bootloader
- **RSA-2048 signing** of all firmware components
- **Dual-image slots** with rollback protection
- **Multi-core coordination** for application and network cores
- **Secure OTA updates** with verification and recovery

## Development Environment Setup

### Prerequisites
```bash
# Install nRF Connect SDK toolchain
nrfutil toolchain-manager install --ncs-version v2.9.0

# System dependencies
# macOS:
brew install ninja ccache python3
# Ubuntu:
sudo apt install ninja-build ccache python3-pip

# Python dependencies for West environment
export WEST_PYTHON="/opt/homebrew/Cellar/west/1.4.0/libexec/bin/python"
$WEST_PYTHON -m pip install cryptography intelhex ecdsa click cbor2
```

### Workspace Initialization
```bash
# Create and initialize nRF Connect SDK workspace
mkdir -p v2.9.0 && cd v2.9.0
west init -m https://github.com/nrfconnect/sdk-nrf --mr v2.9.0
west update  # Downloads ~1.5GB of dependencies

# If workspace exists but is corrupted:
rm -rf .west
west init -m https://github.com/nrfconnect/sdk-nrf --mr v2.9.0
west update
```

### Project Configuration
```bash
# Copy configuration template
cd ../omi
cp omi.conf prj.conf  # Zephyr expects prj.conf

# Key configuration files:
# - omi.conf: Main feature configuration
# - CMakeLists.txt: Build system configuration
# - Kconfig: Custom configuration options
```

## Build Commands

### Standard Build Process
```bash
# From v2.9.0 directory:
nrfutil toolchain-manager launch --ncs-version v2.9.0 --shell

# In the SDK environment:
west build -b omi/nrf5340/cpuapp ../omi --sysbuild -- -DBOARD_ROOT=/path/to/firmware

# Clean build:
west build -b omi/nrf5340/cpuapp ../omi --sysbuild --pristine -- -DBOARD_ROOT=/path/to/firmware
```

### Build Outputs
```
build/
├── dfu_application.zip          # OTA package (440 KB) - PRIMARY FOR MOBILE APP
├── merged.hex                   # Complete firmware (869 KB) - Direct programming
├── signed_by_mcuboot_*.hex      # Signed application image
├── merged_CPUNET.hex            # Network core firmware (533 KB)
├── partitions.yml               # Memory layout
└── build_info.yml               # Build metadata
```

### Alternative Build Methods
```bash
# Using CMakePresets.json:
cmake --preset omi_nrf5340_cpuapp
cmake --build --preset omi_nrf5340_cpuapp

# Docker build (if available):
cd scripts/
./docker-build.sh
```

## Hardware Architecture

### Audio Processing Pipeline
```
┌─────────────┐    ┌──────────────┐    ┌─────────────┐    ┌──────────────┐
│  PDM Mic    │───▶│ Audio Buffer │───▶│ OPUS Codec  │───▶│ BLE TX Queue │
│ (16 kHz)    │    │  (100ms)     │    │ (32 kbps)   │    │  (High MTU)  │
└─────────────┘    └──────────────┘    └─────────────┘    └──────────────┘
```

**Audio Configuration**:
- **Sampling Rate**: 16 kHz, 16-bit PCM
- **Buffer Size**: 100ms (1600 samples)
- **OPUS Settings**: RESTRICTED_LOWDELAY, 32 kbps bitrate
- **BLE Throughput**: 498-byte L2CAP MTU, 2M PHY

### Peripheral Integration
```c
// Key peripherals and their pins (from device tree)
PDM_MIC:     P0.16 (CLK), P1.0 (DIN)
I2C_SENSOR:  P1.2 (SDA), P1.3 (SCL)
SPI_SDCARD:  P0.8 (CS), P0.6 (MOSI), P0.7 (MISO), P0.5 (CLK)
LED_PWM:     P0.30 (RED), P0.29 (GREEN), P0.31 (BLUE)
BUTTON:      P1.6 (GPIO input with pull-up)
BATTERY_ADC: P0.4 (analog input)
```

### Power Management
- **Runtime PM**: Dynamic peripheral power control
- **Sleep States**: Deep sleep between audio frames
- **Battery Monitor**: Real-time voltage and charging status
- **Haptic Control**: Motor enable/disable for notifications

## Bluetooth Low Energy

### BLE Configuration
```c
// High-performance BLE settings
L2CAP_MTU:           498 bytes
CONNECTION_INTERVAL: 7.5ms - 15ms
DATA_LENGTH:         Extended (251 bytes)
PHY:                 2M for higher throughput
GATT_MTU:            247 bytes
```

### MCUmgr Integration
```c
// OTA update protocol
CONFIG_MCUMGR=y
CONFIG_MCUMGR_CMD_IMG_MGMT=y
CONFIG_MCUMGR_TRANSPORT_BT=y
CONFIG_MCUMGR_GRP_IMG_ALLOW_ERASE_PENDING=y
```

### Custom GATT Services
- **Audio Service**: Real-time OPUS stream transmission
- **Battery Service**: Power level and charging status
- **Device Information**: Firmware version, hardware revision
- **MCUmgr Service**: Secure firmware update management

## File Systems and Storage

### SD Card Integration
```c
// EXT2 filesystem with MKFS capability
CONFIG_FILE_SYSTEM=y
CONFIG_FILE_SYSTEM_EXT2=y
CONFIG_EXT2_FS_MKFS=y

// SPI-based SD card interface
CONFIG_DISK=y
CONFIG_DISK_DRIVER_SDMMC=y
```

**Storage Strategy**:
- **Offline Recording**: When BLE disconnected
- **Automatic File Management**: Creation, streaming, cleanup
- **Power Optimization**: SD card enable/disable control

## Testing and Debugging

### Development Testing
```bash
# Unit tests (from test directory)
west build -b omi/nrf5340/cpuapp test/bluetooth
west build -b omi/nrf5340/cpuapp test/audio_test
west build -b omi/nrf5340/cpuapp test/button

# BLE throughput testing
west build -b omi/nrf5340/cpuapp test/ble_throughput
```

### Debug Configuration
```c
// Enable comprehensive logging
CONFIG_LOG=y
CONFIG_LOG_DEFAULT_LEVEL=3
CONFIG_LOG_MAX_LEVEL=4

// Module-specific logging
CONFIG_BT_DEBUG_LOG=y
CONFIG_AUDIO_LOG_LEVEL_DBG=y
CONFIG_PM_LOG_LEVEL_DBG=y
```

### Hardware Debugging
- **RTT Logging**: Real-time terminal via J-Link
- **UART Debug**: Serial console at 115200 baud
- **nRF Connect for VS Code**: Integrated debugging environment
- **Memory Analysis**: Built-in heap and stack monitoring

## OTA Update Process

### Mobile App Integration
```bash
# Generate OTA package
west build  # Creates dfu_application.zip

# Deploy via nRF Connect for Mobile:
1. Connect to Omi device
2. Navigate to DFU tab
3. Select dfu_application.zip
4. Start update (2-5 minutes)
```

### Update Process Flow
```
┌──────────────┐    ┌─────────────┐    ┌──────────────┐    ┌───────────────┐
│ Upload Stage │───▶│ Verification│───▶│ Image Swap   │───▶│ Boot New FW   │
│ (~2-3 min)   │    │ & Signing   │    │ (Automatic)  │    │ & Confirm     │
└──────────────┘    └─────────────┘    └──────────────┘    └───────────────┘
```

### Rollback Protection
- **Image Verification**: RSA-2048 signature validation
- **Dual Slots**: Primary/secondary partition scheme
- **Automatic Rollback**: On boot failure or image corruption
- **Confirmation Required**: Manual confirmation of successful update

## Common Development Tasks

### Adding New Peripherals
1. **Update Device Tree**: Add peripheral definition to `.dts` file
2. **Configure Driver**: Enable in `omi.conf` or `Kconfig`
3. **Initialize in Code**: Add setup in `main.c` or dedicated module
4. **Test Integration**: Create test application

### Modifying Audio Pipeline
```c
// Key files for audio modifications:
src/lib/dk2/audio.c          // Audio capture and processing
src/lib/dk2/opus_encode.c    // OPUS codec integration
src/lib/dk2/config.h         // Audio parameters
```

### BLE Service Development
1. **Define GATT Service**: Create service UUID and characteristics
2. **Implement Callbacks**: Handle read/write/notify operations
3. **Register Service**: Add to BLE initialization sequence
4. **Test with Mobile App**: Verify service discovery and operation

### Power Optimization
```c
// Enable runtime power management
CONFIG_PM=y
CONFIG_PM_DEVICE=y
CONFIG_PM_DEVICE_RUNTIME=y

// Peripheral-specific power controls
pm_device_runtime_get(device);  // Enable peripheral
pm_device_runtime_put(device);  // Disable when unused
```

## Memory Management

### Partition Layout
```yaml
# pm_static.yml - Memory partition scheme
mcuboot_primary:     0x10000 - 0xf0000    # 896 KB
mcuboot_secondary:   0xf0000 - 0x1d0000   # 896 KB (external flash)
settings_storage:    0x1d0000 - 0x1e0000  # 64 KB
```

### Memory Optimization
- **Stack Size Tuning**: Per-thread stack optimization
- **Heap Management**: Dynamic allocation monitoring
- **Buffer Sizing**: Audio buffer optimization for latency vs. memory
- **Code Size**: Feature-based configuration to minimize flash usage

## Troubleshooting

### Build Issues
```bash
# Clean workspace and rebuild
west build -t clean
rm -rf build/
west build -b omi/nrf5340/cpuapp ../omi --sysbuild --pristine

# Fix board not found
west build -b omi/nrf5340/cpuapp ../omi --sysbuild -- -DBOARD_ROOT=/full/path/to/firmware

# Missing dependencies
$WEST_PYTHON -m pip install --upgrade cryptography intelhex
```

### Runtime Issues
- **Audio Problems**: Check PDM configuration and OPUS encoder settings
- **BLE Connection**: Verify connection parameters and MTU negotiation
- **Power Issues**: Monitor battery ADC and charging circuit behavior
- **SD Card**: Check SPI timing and filesystem mount status

### OTA Update Issues
- **Update Fails**: Verify `dfu_application.zip` integrity and device battery level
- **Connection Problems**: Ensure BLE is stable and no other apps connected
- **Rollback**: Check MCUboot logs for signature verification errors

## Performance Considerations

### Real-time Constraints
- **Audio Latency**: <100ms end-to-end processing
- **BLE Throughput**: >150 kbps sustained for OPUS stream
- **Power Consumption**: <10mA average during recording
- **Battery Life**: >24 hours continuous operation

### Memory Usage
- **Flash**: ~350KB application code + OPUS library
- **RAM**: ~180KB runtime usage with audio buffers
- **Stack**: Thread-specific sizing for optimal usage
- **Heap**: Minimal dynamic allocation for deterministic behavior

This firmware represents a production-ready embedded system optimized for real-time audio processing, wireless communication, and secure over-the-air updates in a battery-powered wearable device.