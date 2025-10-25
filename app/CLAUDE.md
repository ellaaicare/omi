# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with the Omi Flutter mobile application.

## Overview

The Omi Flutter app is a sophisticated cross-platform application (iOS/Android/macOS/Web/Windows) that serves as the primary interface for Omi AI wearable devices. It handles real-time audio processing, Bluetooth Low Energy communication, background services, and AI-powered conversation analysis through a complex Provider-based state management system.

## Architecture

### State Management (Provider Pattern)
The app uses a hierarchical Provider architecture with 20+ providers managing different aspects:

```dart
// Core provider hierarchy in main.dart
MultiProvider(
  providers: [
    ChangeNotifierProvider(create: (context) => AuthenticationProvider()),
    ChangeNotifierProvider(create: (context) => ConversationProvider()),
    ChangeNotifierProxyProvider<AppProvider, MessageProvider>(...),
    ChangeNotifierProxyProvider4<ConversationProvider, MessageProvider,
        PeopleProvider, UsageProvider, CaptureProvider>(...),
    // 17 providers total with complex dependencies
  ],
)
```

**Key Providers:**
- `CaptureProvider` - Audio recording and device communication
- `DeviceProvider` - Bluetooth device management
- `ConversationProvider` - Conversation history and processing
- `AuthenticationProvider` - Firebase authentication
- `MessageProvider` - Real-time messaging and chat

### Platform Architecture
```
AppShell (root)
├── DesktopApp (≥1100px width)
│   ├── Desktop pages and widgets
│   └── Window management features
└── MobileApp (<1100px width)
    ├── Mobile-optimized UI
    └── Touch-based interactions
```

## Development Commands

### Setup and Installation
```bash
# First-time setup (choose platform)
bash setup.sh ios        # iOS setup with certificates
bash setup.sh android    # Android setup
bash setup.sh macos     # macOS desktop setup

# SSH setup for certificate repositories
cd ~/.ssh && ssh-add

# Install dependencies
flutter pub get
```

### Running the App
```bash
# Development mode
flutter run --flavor dev

# Specific platform
flutter run --flavor dev -d ios
flutter run --flavor dev -d android
flutter run --flavor dev -d macos
flutter run --flavor dev -d chrome  # Web

# Release mode
flutter run --flavor prod --release
```

### Building for Distribution
```bash
# iOS release build
flutter build ios --flavor dev --release
ios-deploy --bundle build/ios/iphoneos/Runner.app --debug

# Android builds
flutter build apk --flavor dev --release
flutter build appbundle --flavor prod --release

# Desktop builds
flutter build macos --flavor dev --release
flutter build windows --flavor dev --release
```

### Testing
```bash
# Unit tests
flutter test

# Integration tests
flutter drive --target=test_driver/integration_test.dart

# Code generation (after model changes)
flutter packages pub run build_runner build

# Clean and reset
flutter clean && flutter pub get
```

## Key Components and Structure

### Core Directories
```
lib/
├── backend/            # API clients, preferences, schemas
├── core/               # AppShell, platform routing
├── desktop/            # Desktop-specific pages and widgets
├── mobile/             # Mobile-specific pages and widgets
├── providers/          # 20+ state management providers
├── services/           # Core services (auth, BLE, notifications)
├── utils/              # Audio, Bluetooth, platform utilities
└── pages/              # Feature-based page organization
```

### Provider Dependencies
```dart
// Example complex provider dependency
ChangeNotifierProxyProvider4<ConversationProvider, MessageProvider,
    PeopleProvider, UsageProvider, CaptureProvider>(
  create: (context) => CaptureProvider(),
  update: (context, conversation, message, people, usage, previous) =>
      (previous?..updateProviderInstances(conversation, message, people, usage))
      ?? CaptureProvider(),
)
```

## Bluetooth Low Energy (BLE) Integration

### Device Connection Management
```dart
// Core BLE service classes
lib/services/devices/
├── device_connection.dart        # Base connection class
├── omi_connection.dart          # Omi device connection
└── frame_connection.dart        # Frame device connection
```

### Platform-Specific BLE Handling
```dart
// Platform conditional BLE imports
import 'package:flutter_blue_plus/flutter_blue_plus.dart' as ble;
import 'package:flutter_blue_plus_windows/flutter_blue_plus_windows.dart';

// Service initialization
if (!PlatformService.isWindows) {
  ble.FlutterBluePlus.setLogLevel(ble.LogLevel.info, color: true);
}
```

### BLE Service UUIDs and Characteristics
The app connects to multiple device services:
- Battery service for power monitoring
- Audio service for real-time streaming
- Storage service for device data
- Button service for user interactions
- Acceleration service for motion detection

## Audio Processing (OPUS Integration)

### Audio Pipeline
```dart
// OPUS codec initialization
if (PlatformService.isMobile) initOpus(await opus_flutter.load());

// Audio processing utilities
lib/utils/audio/
├── wav_bytes.dart              # WAV file handling
└── opus_frame_util.dart        # OPUS frame processing
```

### Supported Audio Formats
- PCM8, PCM16 - Raw audio formats
- mulaw8, mulaw16 - Compressed formats
- OPUS variants - Real-time compression
- WAV - File format conversion

## Firebase Integration

### Configuration
```dart
// Environment-based Firebase setup
if (F.env == Environment.prod) {
  await Firebase.initializeApp(options: prod.DefaultFirebaseOptions.currentPlatform);
} else {
  await Firebase.initializeApp(options: dev.DefaultFirebaseOptions.currentPlatform);
}
```

### Services Used
- **Authentication**: Google Sign-In, Apple Sign-In
- **Cloud Messaging**: Push notifications
- **Crashlytics**: Error reporting and analytics
- **Remote Config**: Feature flags and A/B testing

## Background Services

### Service Architecture
```dart
// Service manager initialization
await ServiceManager.init();
await ServiceManager.instance().start();

// Foreground task setup
FlutterForegroundTask.initCommunicationPort();
```

### Platform-Specific Background Handling
- **iOS**: Background app refresh for audio processing
- **Android**: Foreground service for continuous operation
- **Desktop**: System service integration

## Platform-Specific Features

### Desktop (≥1100px)
```dart
// Window management
await windowManager.setAsFrameless();
await windowManager.setMinimumSize(const Size(1100, 600));
await windowManager.setSize(const Size(1100, 700));
```

### Mobile Considerations
- Touch-based UI optimizations
- Bluetooth permission handling
- Audio recording permissions
- Background app refresh settings

### Cross-Platform Services
```dart
// Platform service abstraction
class PlatformService {
  static bool get isMobile => Platform.isIOS || Platform.isAndroid;
  static bool get isDesktop => Platform.isWindows || Platform.isMacOS || Platform.isLinux;
  static bool get isWindows => Platform.isWindows;
}
```

## Development Flavors

### Environment Configuration
- **Dev Flavor**: `com.friend-app-with-wearable.ios12.development`
- **Prod Flavor**: `com.friend-app-with-wearable.ios12`

### Build Variants
```bash
# Development with debug capabilities
flutter run --flavor dev

# Production optimized
flutter run --flavor prod --release
```

## Key Dependencies

### Core Packages
```yaml
# State management
provider: ^6.1.2
flutter_provider_utilities: ^1.0.6

# Audio processing
opus_flutter: ^3.0.3
opus_dart: ^3.0.1
wav: ^1.4.0

# Bluetooth
flutter_blue_plus: 1.33.6
flutter_blue_plus_windows: 1.24.21

# Background services
flutter_foreground_task: 9.1.0
flutter_background_service: 5.1.0

# Device integration
nordic_dfu: ^6.1.4+hotfix  # Nordic device firmware updates
mcumgr_flutter: ^0.4.2     # MCU management protocol
frame_sdk: ^0.0.7          # Frame device SDK
```

### Firebase & Analytics
```yaml
firebase_core: 3.13.0
firebase_auth: 5.5.3
firebase_messaging: 15.2.5
firebase_crashlytics: 4.3.2
mixpanel_flutter: ^2.4.4
intercom_flutter: 9.3.3
```

## Common Development Tasks

### Adding New Providers
1. Create provider class extending `ChangeNotifier`
2. Add to `MultiProvider` in `main.dart`
3. Set up dependencies using `ChangeNotifierProxyProvider`
4. Use `Consumer` or `context.read/watch()` in UI

### Bluetooth Device Integration
1. Extend `DeviceConnection` base class
2. Implement device-specific service discovery
3. Handle connection state changes
4. Add to `DeviceProvider` management

### Adding New Pages
1. Create page in appropriate `pages/` subdirectory
2. Follow existing provider integration patterns
3. Add routing through navigation system
4. Consider desktop vs mobile layouts

### Audio Feature Development
1. Understand OPUS frame processing pipeline
2. Test with real hardware devices
3. Handle platform-specific audio permissions
4. Consider background processing requirements

## Debugging Tips

### Common Issues
- **BLE Connection**: Check device pairing and permissions
- **Audio Processing**: Verify OPUS codec initialization
- **Provider Errors**: Check dependency injection order
- **Platform Crashes**: Verify platform-specific feature availability

### Debugging Tools
```dart
// Built-in error logging
Logger.instance.talker.log('Debug message');
DebugLogManager.logError(error, stackTrace, 'Context');

// Firebase Crashlytics
FirebaseCrashlytics.instance.recordError(error, stackTrace);
```

### Testing with Hardware
- Use real Omi devices for Bluetooth testing
- Test audio pipeline with actual microphone input
- Verify background service behavior
- Test cross-platform compatibility

## Performance Considerations

### Memory Management
- Properly dispose providers and streams
- Handle large audio buffers efficiently
- Clean up BLE connections on app backgrounding

### Battery Optimization
- Minimize background processing
- Use efficient audio compression (OPUS)
- Implement smart connection management

### Cross-Platform Optimization
- Conditional feature loading by platform
- Platform-specific UI optimizations
- Efficient resource bundling

## Important Notes

- **Provider order matters**: Dependency providers must be initialized before dependent ones
- **Platform checking**: Always check platform capabilities before accessing features
- **BLE permissions**: Handle runtime permissions properly across platforms
- **Background services**: Different requirements for iOS vs Android
- **Audio permissions**: Required for microphone access and recording
- **Firebase setup**: Requires proper configuration files for each platform
- **Code generation**: Run `build_runner` after model changes
- **Testing**: Real hardware required for full BLE and audio testing