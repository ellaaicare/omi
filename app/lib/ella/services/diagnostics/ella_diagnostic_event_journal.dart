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

  Future<List<Map<String, Object?>>> eventsFor(String accountBindingFingerprint) async {
    try {
      final events = await _channel.invokeListMethod<Map<Object?, Object?>>('getEvents', <String, Object?>{
        'account_binding_fingerprint': accountBindingFingerprint,
      });
      return (events ?? const <Map<Object?, Object?>>[])
          .map((event) => event.map((key, value) => MapEntry(key.toString(), value)))
          .toList(growable: false);
    } on MissingPluginException {
      return const <Map<String, Object?>>[];
    } on PlatformException {
      return const <Map<String, Object?>>[];
    }
  }

  Future<void> clearFor(String accountBindingFingerprint) async {
    try {
      await _channel.invokeMethod<void>('clearEvents', <String, Object?>{
        'account_binding_fingerprint': accountBindingFingerprint,
      });
    } on MissingPluginException {
      // Non-iOS platforms do not persist the iOS support journal.
    } on PlatformException catch (error) {
      debugPrint('Ella diagnostic events were not cleared: ${error.code}');
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
