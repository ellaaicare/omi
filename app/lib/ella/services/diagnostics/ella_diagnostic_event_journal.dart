import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'package:omi/ella/services/diagnostics/ella_diagnostic_event.dart';

class EllaDiagnosticEventJournal implements EllaDiagnosticEventSink {
  EllaDiagnosticEventJournal({MethodChannel? channel})
      : _channel = channel ?? const MethodChannel('com.ellaaicare.ella/diagnostic_events');

  static final EllaDiagnosticEventJournal instance = EllaDiagnosticEventJournal();

  final MethodChannel _channel;

  @override
  Future<void> append(EllaDiagnosticEvent event) async {
    try {
      await _channel.invokeMethod<void>('appendEvent', event.toJson());
    } on MissingPluginException {
      // Non-iOS platforms do not persist the iOS support journal.
    } on PlatformException catch (error) {
      debugPrint('Ella diagnostic event was not persisted: ${error.code}');
    }
  }

  /// Purge the complete local journal before Firebase identity changes. There
  /// is intentionally no caller-selected read API until a support-grant
  /// contract can bind access to the current authenticated account.
  Future<void> clearAll() async {
    try {
      await _channel.invokeMethod<void>('clearAllEvents');
    } on MissingPluginException {
      // Non-iOS platforms do not persist the iOS support journal.
    }
  }
}

@visibleForTesting
class InMemoryEllaDiagnosticEventSink implements EllaDiagnosticEventSink {
  InMemoryEllaDiagnosticEventSink({this.maxEvents = 200});

  final int maxEvents;
  final List<EllaDiagnosticEvent> events = <EllaDiagnosticEvent>[];

  @override
  Future<void> append(EllaDiagnosticEvent event) async {
    event.toJson();
    events.add(event);
    if (events.length > maxEvents) events.removeRange(0, events.length - maxEvents);
  }
}
