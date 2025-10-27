# Omi DevKit2 Speaker-Enabled Firmware Build Guide

**Date**: October 26, 2025
**Hardware**: Seeed XIAO nRF52840 + MAX98357A I2S Audio Amplifier
**NCS Version**: v2.7.0 (required for DevKit)
**Status**: ✅ Builds successfully, firmware flashed and running

---

## Summary

Built speaker-enabled firmware for Omi DevKit2. The firmware compiles and flashes successfully, device boots and connects via BLE. However, **I2S audio output is not yet working** - only haptic vibration functions.

---

## Prerequisites

1. **NCS v2.7.0** installed via nrfutil toolchain-manager
2. **nrfutil** command line tool
3. **Python 3** with bleak library for BLE testing

---

## Build Process

### Step 1: Clean Any Previous Builds

```bash
cd /Users/greg/repos/omi/omi/firmware/devkit
rm -rf build/
```

### Step 2: Critical Device Tree Fix

The original overlay file was missing I2C0 pinctrl configuration, causing build failure. Fixed in:

**File**: `overlay/xiao_ble_sense_devkitv2-adafruit.overlay`

**Added** (lines 17-30):
```dts
&pinctrl {
    i2c0_default: i2c0_default {
        group1 {
            psels = <NRF_PSEL(TWIM_SDA, 0, 4)>,     // SDA pin on P0.4 (D4)
                    <NRF_PSEL(TWIM_SCL, 0, 5)>;      // SCL pin on P0.5 (D5)
        };
    };

    i2c0_sleep: i2c0_sleep {
        group1 {
            psels = <NRF_PSEL(TWIM_SDA, 0, 4)>,     // SDA pin on P0.4 (D4)
                    <NRF_PSEL(TWIM_SCL, 0, 5)>;      // SCL pin on P0.5 (D5)
            low-power-enable;
        };
    };
};

&i2c0 {
    status = "okay";
    pinctrl-0 = <&i2c0_default>;
    pinctrl-1 = <&i2c0_sleep>;
    pinctrl-names = "default", "sleep";
    // ... rest of I2C config
};
```

### Step 3: Verify Speaker is Enabled

**File**: `prj_xiao_ble_sense_devkitv2-adafruit.conf`

**Lines 205-209**:
```conf
CONFIG_OMI_ENABLE_SPEAKER=y          # ✅ ENABLED
CONFIG_OMI_ENABLE_BATTERY=y
CONFIG_OMI_ENABLE_USB=y
CONFIG_OMI_ENABLE_HAPTIC=y
```

**Note**: Accelerometer is **disabled** (`CONFIG_OMI_ENABLE_ACCELEROMETER=n`)

### Step 4: Build Command

**CRITICAL**: Must run from NCS workspace directory, not project directory.

```bash
cd /opt/nordic/ncs/v2.7.0

nrfutil toolchain-manager launch --ncs-version v2.7.0 --shell -- \
  west build \
    -b xiao_ble \
    -d build \
    /Users/greg/repos/omi/omi/firmware/devkit \
    -- \
    -DCONF_FILE=prj_xiao_ble_sense_devkitv2-adafruit.conf \
    -DDTC_OVERLAY_FILE=overlay/xiao_ble_sense_devkitv2-adafruit.overlay \
    -DCMAKE_BUILD_TYPE=Debug
```

**Build Output**:
- ✅ 394 source files compiled successfully
- ✅ Linking successful
- ✅ Generated: `build/zephyr/zephyr.uf2` (587 KB)

### Step 5: Copy Firmware to Convenient Location

```bash
cp /opt/nordic/ncs/v2.7.0/build/zephyr/zephyr.uf2 \
   /Users/greg/repos/omi/omi/firmware/devkit/firmware-speaker-enabled.uf2
```

---

## Flashing Process

### Step 1: Put Device in Bootloader Mode

1. **Double-tap reset button** on XIAO nRF52840
2. Device appears as USB mass storage device named "XIAO-SENSE"
3. Red LED should be breathing/pulsing

### Step 2: Flash Firmware

**Drag and drop** the UF2 file to the XIAO-SENSE drive:

```bash
# Method 1: Drag and drop in Finder
# Method 2: Command line
cp /Users/greg/repos/omi/omi/firmware/devkit/firmware-speaker-enabled.uf2 /Volumes/XIAO-SENSE/
```

### Step 3: Verify Flash Success

- Device automatically reboots after flashing
- USB reconnects as `/dev/tty.usbmodem1101`
- BLE device "Omi DevKit 2" appears in Bluetooth scan

---

## Testing

### BLE Connection Test

**Python script**: `/Users/greg/repos/omi/test_speaker.py`

```bash
python3 /Users/greg/repos/omi/test_speaker.py
```

**Expected Output**:
- ✅ Finds "Omi DevKit 2" device
- ✅ Connects successfully
- ✅ Sends 32KB of 440Hz audio data in 80 chunks

### BLE UUIDs

**Service**: `19B10000-E8F2-537E-4F6C-D104768A1214`
**Speaker Audio Characteristic**: `19B10003-E8F2-537E-4F6C-D104768A1214` (write, notify)

**Audio Protocol** (from firmware `speaker.c` lines 14-22):
1. Send 4-byte packet with total audio size (uint32, little-endian)
2. Send audio data in 400-byte chunks (16-bit signed samples)
3. Firmware plays audio when all data received

---

## Current Issues

### ❌ I2S Audio Not Working

**Symptoms**:
- BLE communication works perfectly
- Audio data transfer succeeds (no errors)
- No actual audio output from speaker
- Only haptic vibration works

### Suspected Issues

**1. Pin Conflict (CRITICAL)**

**Problem**: P0.4 is used for BOTH I2C SDA and speaker enable pin

- **Overlay file** (line 19): `psels = <NRF_PSEL(TWIM_SDA, 0, 4)>`
- **speaker.c** (line 46): `speaker_gpio_pin.pin = 4;`

Even though accelerometer is disabled, the I2C peripheral might still be initialized and claiming P0.4.

**2. Missing I2S Configuration**

The overlay enables I2S:
```dts
&i2s0 {
    status = "okay";
    pinctrl-0 = <&i2s0_default>;
    pinctrl-names = "default";
    label = "I2S_0";
};
```

But the pins might not be correctly configured for the MAX98357A amplifier.

**I2S Pin Assignment** (overlay lines 9-15):
- **SCK** (bit clock): P0.29 (A3)
- **LRCK** (word select): P0.28 (A2)
- **SDOUT** (data): P0.3 (A1)

**Speaker Enable Pin** (speaker.c line 46): P0.4

---

## I2S Hardware Configuration

**From speaker.c `speaker_init()` (lines 142-154)**:

```c
struct i2s_config config = {
    .word_size = 16,                          // 16-bit samples
    .channels = 2,                            // Stereo (2 channels)
    .format = I2S_FMT_DATA_FORMAT_LEFT_JUSTIFIED,
    .options = I2S_OPT_FRAME_CLK_MASTER | I2S_OPT_BIT_CLK_MASTER | I2S_OPT_BIT_CLK_GATED,
    .frame_clk_freq = 8000,                   // 8kHz sample rate
    .mem_slab = &mem_slab,
    .block_size = MAX_BLOCK_SIZE,             // 10,000 bytes
    .timeout = -1,
};
```

---

## Firmware Code Structure

### Main Initialization (main.c)

**Lines 186-193**:
```c
#ifdef CONFIG_OMI_ENABLE_SPEAKER
    err = speaker_init();
    if (err) {
        LOG_ERR("Speaker failed to start");
        return err;  // CRITICAL: Exits if speaker init fails
    }
    LOG_INF("Speaker initialized");
#endif
```

**Lines 264-266** (boot sound):
```c
#ifdef CONFIG_OMI_ENABLE_SPEAKER
    play_boot_sound();  // Plays C5, E5, G5, C6 chime
#endif
```

### Speaker Implementation (speaker.c)

**Key Functions**:
- `speaker_init()` (line 120): Initializes I2S, allocates buffers
- `speak()` (line 178): Receives audio via BLE, writes to I2S
- `play_boot_sound()` (line 252): Generates and plays test chime

**Boot Sound** (lines 236-248):
```c
const float frequencies[] = {523.25, 659.25, 783.99, 1046.50};  // C5, E5, G5, C6
```

### BLE Service (transport.c)

**Line 86-87** (UUID definition):
```c
static struct bt_uuid_128 audio_characteristic_speaker_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x19B10003, 0xE8F2, 0x537E, 0x4F6C, 0xD104768A1214));
```

**Lines 314-327** (BLE write handler):
```c
static ssize_t audio_data_write_handler(...) {
    amount = speak(len, buf);  // Calls speaker.c speak() function
    return len;
}
```

---

## Next Steps to Fix Audio

### Option 1: Fix Pin Conflict

Change speaker enable pin from P0.4 to an unused pin (e.g., P0.31 or P1.10).

**Edit**: `src/speaker.c` line 46:
```c
// OLD:
struct gpio_dt_spec speaker_gpio_pin = {.port = DEVICE_DT_GET(DT_NODELABEL(gpio0)),
                                        .pin = 4,  // ❌ CONFLICTS with I2C

// NEW:
struct gpio_dt_spec speaker_gpio_pin = {.port = DEVICE_DT_GET(DT_NODELABEL(gpio0)),
                                        .pin = 31,  // ✅ Use P0.31 instead
```

### Option 2: Disable I2C Completely

Since accelerometer is disabled, completely remove I2C from the build.

**Edit**: `overlay/xiao_ble_sense_devkitv2-adafruit.overlay`

Remove or disable I2C0 configuration.

### Option 3: Debug Serial Console

Check firmware logs to see if `speaker_init()` is actually succeeding.

```bash
screen /dev/tty.usbmodem1101 115200
```

Reset device and look for:
- `"Speaker initialized"` - speaker_init() succeeded
- `"Speaker device is not supported"` - I2S device not found
- `"Error setting up speaker Pin"` - GPIO configuration failed

---

## Build Scripts Created

### Build Script (for future use)

```bash
#!/bin/bash
# build-speaker-firmware.sh

cd /opt/nordic/ncs/v2.7.0

nrfutil toolchain-manager launch --ncs-version v2.7.0 --shell -- \
  west build \
    -b xiao_ble \
    -d build \
    /Users/greg/repos/omi/omi/firmware/devkit \
    -- \
    -DCONF_FILE=prj_xiao_ble_sense_devkitv2-adafruit.conf \
    -DDTC_OVERLAY_FILE=overlay/xiao_ble_sense_devkitv2-adafruit.overlay \
    -DCMAKE_BUILD_TYPE=Debug

# Copy firmware to convenient location
cp build/zephyr/zephyr.uf2 \
   /Users/greg/repos/omi/omi/firmware/devkit/firmware-speaker-enabled.uf2

echo "✅ Firmware built: firmware-speaker-enabled.uf2"
```

---

## Files Modified

1. ✅ `/Users/greg/repos/omi/omi/firmware/devkit/overlay/xiao_ble_sense_devkitv2-adafruit.overlay`
   - Added I2C0 pinctrl configuration (lines 17-30)
   - Added pinctrl references to I2C0 node (lines 92-94)

2. ✅ `/Users/greg/repos/omi/omi/firmware/devkit/prj_xiao_ble_sense_devkitv2-adafruit.conf`
   - No changes (speaker already enabled)

---

## Test Script

**File**: `/Users/greg/repos/omi/test_speaker.py`

Sends 440Hz test tone to speaker via BLE. Confirms BLE communication works but audio output doesn't.

---

## Key Learnings

1. **Build must run from NCS workspace**, not project directory
2. **NCS v2.7.0 is required** for DevKit (not v2.9.0)
3. **Board identifier is `xiao_ble`** (not `xiao_ble_sense` or `nrf52840dk`)
4. **I2C pinctrl configuration is mandatory** even if I2C is disabled
5. **Pin conflicts cause silent failures** - GPIO configuration succeeds but hardware doesn't work
6. **UF2 bootloader is safe** - double-tap reset always allows recovery

---

## Hardware Verification Needed

- [ ] Confirm MAX98357A amplifier is actually connected to I2S pins
- [ ] Verify speaker enable pin (SD pin on MAX98357A) is connected to P0.4
- [ ] Check for shorts or incorrect wiring
- [ ] Measure I2S signals with logic analyzer/oscilloscope

---

## References

- **Official Build Docs**: https://docs.omi.me/doc/developer/firmware/Compile_firmware
- **XIAO nRF52840 Pinout**: https://wiki.seeedstudio.com/XIAO_BLE/
- **MAX98357A Datasheet**: I2S Class D Audio Amplifier
- **NCS Documentation**: https://developer.nordicsemi.com/nRF_Connect_SDK/doc/2.7.0/

---

**Last Updated**: October 26, 2025
**Build Status**: ✅ Compiles and flashes successfully
**Audio Status**: ❌ I2S output not working (debugging in progress)
