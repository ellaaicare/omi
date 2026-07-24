import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'package:omi/backend/preferences.dart';

@immutable
class AudioRouteState {
  const AudioRouteState({
    required this.outputName,
    required this.outputType,
    required this.hasHeadset,
    required this.usesPhoneSpeaker,
  });

  const AudioRouteState.phoneSpeaker()
    : outputName = 'iPhone speaker',
      outputType = 'builtInSpeaker',
      hasHeadset = false,
      usesPhoneSpeaker = true;

  factory AudioRouteState.fromMap(Map<Object?, Object?> map) {
    return AudioRouteState(
      outputName: map['outputName'] as String? ?? 'iPhone speaker',
      outputType: map['outputType'] as String? ?? 'unknown',
      hasHeadset: map['hasHeadset'] as bool? ?? false,
      usesPhoneSpeaker: map['usesPhoneSpeaker'] as bool? ?? false,
    );
  }

  final String outputName;
  final String outputType;
  final bool hasHeadset;
  final bool usesPhoneSpeaker;
}

class AudioRouteProvider extends ChangeNotifier {
  AudioRouteProvider({MethodChannel? channel})
    : _channel = channel ?? const MethodChannel('com.ellaaicare.ella/audio_route') {
    if (Platform.isIOS) {
      _channel.setMethodCallHandler(_handleMethodCall);
      unawaited(refresh());
    }
  }

  final MethodChannel _channel;
  AudioRouteState _route = const AudioRouteState.phoneSpeaker();

  AudioRouteState get route => _route;
  bool get presentationHasHeadset => SharedPreferencesUtil().demoMode || _route.hasHeadset;
  String get presentationOutputName => SharedPreferencesUtil().demoMode ? 'Ella headset' : _route.outputName;
  bool get presentationUsesPhoneSpeaker => !SharedPreferencesUtil().demoMode && _route.usesPhoneSpeaker;

  Future<void> refresh() async {
    if (!Platform.isIOS) return;
    try {
      final response = await _channel.invokeMapMethod<Object?, Object?>('getCurrentRoute');
      if (response != null) _updateRoute(AudioRouteState.fromMap(response));
    } on PlatformException {
      // Keep the conservative phone-speaker state when native route lookup fails.
    }
  }

  Future<void> _handleMethodCall(MethodCall call) async {
    if (call.method != 'routeChanged' || call.arguments is! Map) return;
    _updateRoute(AudioRouteState.fromMap(Map<Object?, Object?>.from(call.arguments as Map)));
  }

  void _updateRoute(AudioRouteState next) {
    if (_route.outputName == next.outputName &&
        _route.outputType == next.outputType &&
        _route.hasHeadset == next.hasHeadset &&
        _route.usesPhoneSpeaker == next.usesPhoneSpeaker) {
      return;
    }
    _route = next;
    notifyListeners();
  }

  @override
  void dispose() {
    if (Platform.isIOS) _channel.setMethodCallHandler(null);
    super.dispose();
  }
}
