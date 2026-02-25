import 'dart:async';
import 'package:flutter/services.dart';

enum CallState {
  idle,
  ringing,
  connecting,
  connected,
  disconnected,
}

class TwilioVoicePlugin {
  static const MethodChannel _channel = MethodChannel('twilio_voice');

  final _callStateController = StreamController<CallState>.broadcast();

  Stream<CallState> get callStateStream => _callStateController.stream;

  CallState _currentState = CallState.idle;
  CallState get currentState => _currentState;

  TwilioVoicePlugin() {
    _channel.setMethodCallHandler(_handleMethodCall);
    print('TwilioVoicePlugin: Initialized');
  }

  Future<void> _handleMethodCall(MethodCall call) async {
    print('TwilioVoicePlugin: Received method call: ${call.method}');

    switch (call.method) {
      case 'call_ringing':
        _updateState(CallState.ringing);
        break;
      case 'call_connecting':
        _updateState(CallState.connecting);
        break;
      case 'call_connected':
        _updateState(CallState.connected);
        break;
      case 'call_disconnected':
        _updateState(CallState.disconnected);
        break;
      default:
        print('TwilioVoicePlugin: Unknown method: ${call.method}');
    }
  }

  void _updateState(CallState newState) {
    _currentState = newState;
    _callStateController.add(newState);
    print('TwilioVoicePlugin: State changed to $newState');
  }

  Future<String?> startCall() async {
    try {
      print('TwilioVoicePlugin: Calling startCall method');
      final result = await _channel.invokeMethod('startCall');
      return result as String?;
    } on PlatformException catch (e) {
      print('TwilioVoicePlugin: ERROR starting call: ${e.message}');
      return null;
    }
  }

  Future<void> endCall() async {
    try {
      print('TwilioVoicePlugin: Calling endCall method');
      await _channel.invokeMethod('endCall');
    } on PlatformException catch (e) {
      print('TwilioVoicePlugin: ERROR ending call: ${e.message}');
    }
  }

  Future<void> setMuted(bool muted) async {
    try {
      print('TwilioVoicePlugin: Setting muted to $muted');
      await _channel.invokeMethod('setMuted', {'muted': muted});
    } on PlatformException catch (e) {
      print('TwilioVoicePlugin: ERROR setting mute: ${e.message}');
    }
  }

  void dispose() {
    _callStateController.close();
  }
}
