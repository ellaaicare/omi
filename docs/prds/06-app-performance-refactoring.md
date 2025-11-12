# PRD: App Performance & Provider Refactoring

## Status
🟡 **HIGH PRIORITY** - Performance and maintainability improvements

## Executive Summary
Refactor large providers, reduce excessive widget rebuilds, move heavy operations to isolates, and implement proper resource management to improve app performance and battery life.

## Problem Statement
- **CaptureProvider:** 1,203 lines, handles too many responsibilities
- **Excessive notifyListeners():** 36+ calls causing full widget tree rebuilds
- **Heavy operations on main thread:** Filtering, sorting blocking UI
- **No lazy loading:** Loading all messages/apps at once
- **Memory leaks:** Stream subscriptions not properly managed

## Goals
- Split CaptureProvider into 3 smaller providers
- Reduce widget rebuilds by 70%
- Move heavy operations to isolates
- Implement lazy loading for lists
- Fix all memory leaks

## Success Metrics
- ✅ No provider >400 lines
- ✅ 70% reduction in notifyListeners() calls
- ✅ UI stays responsive during heavy operations
- ✅ 50% reduction in memory usage
- ✅ Zero memory leaks (DevTools confirmed)

## Technical Specification

### 1. Split CaptureProvider

**Current (1,203 lines):**
```dart
class CaptureProvider extends ChangeNotifier {
  // Audio recording
  // Transcription
  // Photos
  // Voice commands
  // System audio
  // WAL sync
  // 36 notifyListeners() calls
}
```

**Target Structure:**
```dart
// lib/providers/capture/audio_capture_provider.dart (300 lines)
class AudioCaptureProvider extends ChangeNotifier {
  bool _isRecording = false;
  List<int> _audioBuffer = [];
  StreamSubscription? _audioSubscription;

  Future<void> startCapture() async {
    if (_isRecording) return;
    _isRecording = true;
    notifyListeners(); // Only notify on state change

    _audioSubscription = deviceService.audioStream.listen(_handleAudioChunk);
  }

  void _handleAudioChunk(List<int> chunk) {
    _audioBuffer.addAll(chunk);
    // Process without notifying every time
    if (_audioBuffer.length >= _chunkSize) {
      _processBuffer();
    }
  }

  Future<void> stopCapture() async {
    await _audioSubscription?.cancel();
    _isRecording = false;
    notifyListeners();
  }
}

// lib/providers/capture/photo_capture_provider.dart (200 lines)
class PhotoCaptureProvider extends ChangeNotifier {
  List<CapturedPhoto> _photos = [];
  StreamSubscription? _photoSubscription;

  // Photo-specific logic only
}

// lib/providers/capture/transcription_provider.dart (300 lines)
class TranscriptionProvider extends ChangeNotifier {
  List<TranscriptSegment> _segments = [];
  SocketService? _socketService;

  // Transcription-specific logic only

  // Use ChangeNotifierProxyProvider to depend on AudioCaptureProvider
  void updateAudioProvider(AudioCaptureProvider audio) {
    if (audio.hasNewChunks) {
      _sendToBackend(audio.popChunks());
    }
  }
}
```

**main.dart Provider Setup:**
```dart
MultiProvider(
  providers: [
    // Independent providers
    ChangeNotifierProvider(create: (_) => AudioCaptureProvider()),
    ChangeNotifierProvider(create: (_) => PhotoCaptureProvider()),

    // Dependent provider
    ChangeNotifierProxyProvider<AudioCaptureProvider, TranscriptionProvider>(
      create: (_) => TranscriptionProvider(),
      update: (_, audio, previous) =>
        (previous?..updateAudioProvider(audio)) ?? TranscriptionProvider(),
    ),
  ],
  child: MyApp(),
)
```

### 2. Reduce Widget Rebuilds

**Use Selector Instead of Consumer:**

**Before (rebuilds entire widget):**
```dart
Consumer<CaptureProvider>(
  builder: (context, capture, child) {
    return Column(
      children: [
        Text('Recording: ${capture.isRecording}'),
        Text('Duration: ${capture.duration}'),
        Text('Segments: ${capture.segments.length}'),
        // Entire widget rebuilds on any change
      ],
    );
  },
)
```

**After (rebuilds only what changed):**
```dart
Column(
  children: [
    Selector<AudioCaptureProvider, bool>(
      selector: (_, provider) => provider.isRecording,
      builder: (_, isRecording, __) => Text('Recording: $isRecording'),
    ),
    Selector<AudioCaptureProvider, Duration>(
      selector: (_, provider) => provider.duration,
      builder: (_, duration, __) => Text('Duration: $duration'),
    ),
    Selector<TranscriptionProvider, int>(
      selector: (_, provider) => provider.segments.length,
      builder: (_, count, __) => Text('Segments: $count'),
    ),
  ],
)
```

**Implement Equatable for Smart Rebuilds:**
```dart
import 'package:equatable/equatable.dart';

class TranscriptSegment extends Equatable {
  final String text;
  final DateTime timestamp;
  final String? speakerId;

  @override
  List<Object?> get props => [text, timestamp, speakerId];
}

// Now Selector can compare efficiently
Selector<TranscriptionProvider, List<TranscriptSegment>>(
  selector: (_, provider) => provider.segments,
  shouldRebuild: (previous, next) => previous != next,  // Uses Equatable
  builder: (_, segments, __) => TranscriptList(segments: segments),
)
```

### 3. Move Heavy Operations to Isolates

**Before (blocks UI):**
```dart
class AppProvider extends ChangeNotifier {
  void filterApps() {
    // 70 lines of synchronous operations
    filteredApps = apps.where((app) => _matchesFilter(app)).toList();
    filteredApps.sort(_compareApps);
    notifyListeners();  // UI freezes during this
  }
}
```

**After (non-blocking):**
```dart
import 'package:flutter/foundation.dart';

class AppProvider extends ChangeNotifier {
  bool _isFiltering = false;

  Future<void> filterApps() async {
    if (_isFiltering) return;

    _isFiltering = true;
    notifyListeners();

    // Run in isolate
    filteredApps = await compute(_filterAppsIsolate, FilterParams(
      apps: apps,
      filterText: filterText,
      categories: selectedCategories,
    ));

    _isFiltering = false;
    notifyListeners();
  }

  // Top-level function for isolate
  static List<App> _filterAppsIsolate(FilterParams params) {
    var filtered = params.apps.where((app) => _matchesFilter(app, params)).toList();
    filtered.sort(_compareApps);
    return filtered;
  }
}

class FilterParams {
  final List<App> apps;
  final String filterText;
  final List<String> categories;

  FilterParams({required this.apps, required this.filterText, required this.categories});
}
```

### 4. Implement Lazy Loading

**Before (loads everything):**
```dart
class MessageProvider extends ChangeNotifier {
  Future<void> loadMessages() async {
    messages = await getMessagesServer();  // Loads ALL messages
    notifyListeners();
  }
}
```

**After (paginated):**
```dart
class MessageProvider extends ChangeNotifier {
  List<ServerMessage> messages = [];
  bool hasMore = true;
  bool isLoading = false;
  int _page = 0;
  static const int _pageSize = 20;

  Future<void> loadMessages({bool loadMore = false}) async {
    if (isLoading || (!loadMore && messages.isNotEmpty)) return;

    isLoading = true;
    notifyListeners();

    final newMessages = await getMessagesServer(
      offset: _page * _pageSize,
      limit: _pageSize,
    );

    if (newMessages.length < _pageSize) {
      hasMore = false;
    }

    messages.addAll(newMessages);
    _page++;

    isLoading = false;
    notifyListeners();
  }

  void refresh() {
    messages.clear();
    _page = 0;
    hasMore = true;
    loadMessages();
  }
}

// UI with infinite scroll
class MessageListView extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return Consumer<MessageProvider>(
      builder: (context, provider, _) {
        return ListView.builder(
          itemCount: provider.messages.length + (provider.hasMore ? 1 : 0),
          itemBuilder: (context, index) {
            if (index == provider.messages.length) {
              // Load more when reaching end
              provider.loadMessages(loadMore: true);
              return CircularProgressIndicator();
            }
            return MessageTile(message: provider.messages[index]);
          },
        );
      },
    );
  }
}
```

### 5. Fix Memory Leaks

**Comprehensive Subscription Management:**
```dart
class AudioCaptureProvider extends ChangeNotifier {
  final List<StreamSubscription> _subscriptions = [];
  Timer? _heartbeatTimer;

  void _addSubscription(StreamSubscription sub) {
    _subscriptions.add(sub);
  }

  Future<void> startCapture() async {
    // Track all subscriptions
    final audioSub = deviceService.audioStream.listen(_handleAudio);
    _addSubscription(audioSub);

    final statusSub = deviceService.statusStream.listen(_handleStatus);
    _addSubscription(statusSub);

    // Track timers
    _heartbeatTimer = Timer.periodic(Duration(seconds: 30), _sendHeartbeat);
  }

  @override
  Future<void> dispose() async {
    // Cancel all subscriptions
    for (final sub in _subscriptions) {
      await sub.cancel();
    }
    _subscriptions.clear();

    // Cancel timers
    _heartbeatTimer?.cancel();
    _heartbeatTimer = null;

    super.dispose();
  }
}
```

## Implementation Plan

### Week 1-2: Split CaptureProvider
- [ ] Create AudioCaptureProvider
- [ ] Create PhotoCaptureProvider
- [ ] Create TranscriptionProvider
- [ ] Migrate logic from CaptureProvider
- [ ] Update main.dart provider setup
- [ ] Update all UI references

### Week 3: Reduce Rebuilds & Isolates
- [ ] Replace Consumer with Selector (top 10 widgets)
- [ ] Implement Equatable for models
- [ ] Move filtering to isolates
- [ ] Move sorting to isolates

### Week 4: Lazy Loading & Memory Fixes
- [ ] Implement pagination for messages
- [ ] Implement pagination for apps
- [ ] Fix all subscription leaks
- [ ] DevTools memory profiling

## Success Criteria

- [ ] CaptureProvider split into 3 providers
- [ ] 70% reduction in notifyListeners()
- [ ] UI stays responsive (60fps) during operations
- [ ] Memory usage reduced by 50%
- [ ] Zero leaks in DevTools

---

**Estimated Effort:** 4 weeks
**Priority:** 🟡 HIGH
**Target Completion:** 4-5 weeks
