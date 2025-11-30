import 'package:flutter/foundation.dart';
import 'package:flutter/scheduler.dart';

/// Mixin to coalesce multiple notifyListeners() calls into a single notification per frame
///
/// This optimization reduces UI rebuilds when multiple state changes occur within
/// the same frame. Instead of triggering multiple rebuilds, changes are batched
/// and listeners are notified once at the end of the frame.
///
/// Usage:
/// ```dart
/// class MyProvider extends ChangeNotifier with BatchedNotifier {
///   void updateMultipleValues() {
///     _value1 = newValue1;
///     scheduleNotify(); // Instead of notifyListeners()
///     _value2 = newValue2;
///     scheduleNotify(); // Will be coalesced with the first call
///   }
/// }
/// ```
mixin BatchedNotifier on ChangeNotifier {
  bool _needsNotify = false;
  bool _isDisposed = false;

  /// Schedule a notification for the end of the current frame
  /// Multiple calls within the same frame will be coalesced into one
  void scheduleNotify() {
    if (_isDisposed) return;

    if (!_needsNotify) {
      _needsNotify = true;
      SchedulerBinding.instance.addPostFrameCallback((_) {
        if (_needsNotify && !_isDisposed) {
          _needsNotify = false;
          notifyListeners();
        }
      });
    }
  }

  /// Force immediate notification (bypasses batching)
  /// Use sparingly when immediate update is critical
  void notifyNow() {
    if (_isDisposed) return;
    _needsNotify = false;
    notifyListeners();
  }

  @override
  void dispose() {
    _isDisposed = true;
    _needsNotify = false;
    super.dispose();
  }
}

/// Extension to add batch notification support to any ChangeNotifier
extension BatchedNotification on ChangeNotifier {
  static final Map<ChangeNotifier, bool> _pendingNotifications = {};

  /// Schedule a batched notification for this notifier
  void scheduleBatchedNotify() {
    if (_pendingNotifications[this] == true) return;

    _pendingNotifications[this] = true;
    SchedulerBinding.instance.addPostFrameCallback((_) {
      if (_pendingNotifications[this] == true) {
        _pendingNotifications.remove(this);
        notifyListeners();
      }
    });
  }

  /// Cancel any pending batched notification
  void cancelBatchedNotify() {
    _pendingNotifications.remove(this);
  }
}
