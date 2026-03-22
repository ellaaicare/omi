import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

class DebugEvent {
  final String id;
  final String triggerType;
  final String message;
  final DateTime receivedAt;
  final Map<String, dynamic> metadata;

  const DebugEvent({
    required this.id,
    required this.triggerType,
    required this.message,
    required this.receivedAt,
    required this.metadata,
  });

  factory DebugEvent.fromMap(Map<String, dynamic> map) {
    return DebugEvent(
      id: map['id'] as String? ?? '',
      triggerType: map['trigger_type'] as String? ?? 'unknown',
      message: map['message'] as String? ?? '',
      receivedAt: DateTime.tryParse(map['received_at'] as String? ?? '') ?? DateTime.now(),
      metadata: (map['metadata'] as Map?)?.cast<String, dynamic>() ?? {},
    );
  }
}

class DebugEventBuffer extends ChangeNotifier {
  static final DebugEventBuffer instance = DebugEventBuffer._();
  DebugEventBuffer._();

  static const _channel = MethodChannel('com.ellaaicare.ella/debug_events');

  List<DebugEvent> _events = [];
  List<DebugEvent> get events => List.unmodifiable(_events);

  /// Fetch latest events from native buffer and notify listeners.
  Future<void> refresh() async {
    try {
      final raw = await _channel.invokeMethod<List>('getEvents');
      if (raw == null) return;
      _events = raw
          .cast<Map>()
          .map((m) => DebugEvent.fromMap(m.cast<String, dynamic>()))
          .toList();
      notifyListeners();
    } catch (e) {
      debugPrint('DebugEventBuffer.refresh error: $e');
    }
  }

  Future<void> clear() async {
    try {
      await _channel.invokeMethod('clearEvents');
      _events = [];
      notifyListeners();
    } catch (e) {
      debugPrint('DebugEventBuffer.clear error: $e');
    }
  }
}
