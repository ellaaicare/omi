import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import 'package:agora_rtc_engine/agora_rtc_engine.dart';

/// Service managing an Ella Agora voice call.
///
/// Flow:
///   startCall() → POST /start_agent (starts server bot) → POST /token (get RTC token)
///              → join Agora channel → bidirectional audio streams
///   endCall()  → leave channel → POST /stop_agent
class EllaVoiceCallService extends ChangeNotifier {
  static const _voiceBotBaseUrl = 'https://voice.ella-ai-care.com';
  static const _agoraAppId = '55dd93fbff4946d7bcbff6f6ebcee462';
  static const _botUid = 1; // Server bot always joins as UID 1
  static const _clientUid = 2; // iOS client uses UID 2

  RtcEngine? _engine;
  String? _channelName;

  EllaCallState _state = EllaCallState.idle;
  String? _error;

  EllaCallState get state => _state;
  String? get error => _error;
  bool get isInCall => _state == EllaCallState.connected || _state == EllaCallState.connecting;

  // ── Public API ─────────────────────────────────────────────────────

  /// Start a call with Ella. [userId] is used as the Agora channel name
  /// and as the OpenClaw session key.
  Future<void> startCall(String userId) async {
    if (_state != EllaCallState.idle) return;
    _setState(EllaCallState.connecting);
    _error = null;

    try {
      final channel = 'ella-$userId';
      _channelName = channel;

      // 1. Start the server bot
      await _startBot(channel, userId);

      // 2. Get an Agora RTC token
      final token = await _getToken(channel, _clientUid);

      // 3. Join the Agora channel
      await _joinChannel(token, channel);

      _setState(EllaCallState.connected);
    } catch (e) {
      debugPrint('[EllaVoiceCall] startCall error: $e');
      _error = e.toString();
      _setState(EllaCallState.error);
      await _cleanup();
    }
  }

  /// End the call and clean up.
  Future<void> endCall() async {
    if (_state == EllaCallState.idle) return;
    _setState(EllaCallState.disconnecting);
    final channel = _channelName;
    await _cleanup();
    if (channel != null) {
      try {
        await _stopBot(channel);
      } catch (e) {
        debugPrint('[EllaVoiceCall] stopBot error (non-fatal): $e');
      }
    }
    _setState(EllaCallState.idle);
  }

  // ── Bot Lifecycle ──────────────────────────────────────────────────

  Future<void> _startBot(String channel, String userId) async {
    final resp = await http.post(
      Uri.parse('$_voiceBotBaseUrl/start_agent'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({
        'channel_name': channel,
        'uid': _botUid,
        'session_key': 'voice-$userId',
      }),
    ).timeout(const Duration(seconds: 10));

    if (resp.statusCode != 200) {
      throw Exception('Failed to start bot: ${resp.statusCode} ${resp.body}');
    }
    debugPrint('[EllaVoiceCall] Bot started in channel $channel');
  }

  Future<void> _stopBot(String channel) async {
    await http.post(
      Uri.parse('$_voiceBotBaseUrl/stop_agent'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'channel_name': channel}),
    ).timeout(const Duration(seconds: 5));
    debugPrint('[EllaVoiceCall] Bot stopped in channel $channel');
  }

  Future<String> _getToken(String channel, int uid) async {
    final resp = await http.post(
      Uri.parse('$_voiceBotBaseUrl/token'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'channel_name': channel, 'uid': uid, 'role': 'publisher', 'ttl': 3600}),
    ).timeout(const Duration(seconds: 10));

    if (resp.statusCode != 200) {
      throw Exception('Failed to get token: ${resp.statusCode} ${resp.body}');
    }
    final data = jsonDecode(resp.body) as Map<String, dynamic>;
    return data['token'] as String;
  }

  // ── Agora Channel ──────────────────────────────────────────────────

  Future<void> _joinChannel(String token, String channel) async {
    _engine = createAgoraRtcEngine();
    await _engine!.initialize(const RtcEngineContext(appId: _agoraAppId));

    _engine!.registerEventHandler(RtcEngineEventHandler(
      onJoinChannelSuccess: (connection, elapsed) {
        debugPrint('[EllaVoiceCall] Joined channel ${connection.channelId}, uid=${connection.localUid}');
      },
      onUserJoined: (connection, remoteUid, elapsed) {
        debugPrint('[EllaVoiceCall] Remote user joined: uid=$remoteUid');
      },
      onUserOffline: (connection, remoteUid, reason) {
        debugPrint('[EllaVoiceCall] Remote user offline: uid=$remoteUid reason=$reason');
        // Bot left — end call
        if (remoteUid == _botUid && _state == EllaCallState.connected) {
          endCall();
        }
      },
      onError: (err, msg) {
        debugPrint('[EllaVoiceCall] Agora error: $err — $msg');
        _error = '$err: $msg';
        _setState(EllaCallState.error);
      },
    ));

    // Audio-only call — disable video
    await _engine!.disableVideo();
    await _engine!.enableAudio();
    await _engine!.setAudioProfile(
      profile: AudioProfileType.audioProfileMusicHighQualityStereo,
      scenario: AudioScenarioType.audioScenarioChatroom,
    );

    await _engine!.joinChannel(
      token: token,
      channelId: channel,
      uid: _clientUid,
      options: const ChannelMediaOptions(
        channelProfile: ChannelProfileType.channelProfileCommunication,
        clientRoleType: ClientRoleType.clientRoleBroadcaster,
        autoSubscribeAudio: true,
        publishMicrophoneTrack: true,
      ),
    );
  }

  // ── Helpers ────────────────────────────────────────────────────────

  Future<void> _cleanup() async {
    try {
      await _engine?.leaveChannel();
      await _engine?.release();
    } catch (_) {}
    _engine = null;
    _channelName = null;
  }

  void _setState(EllaCallState newState) {
    _state = newState;
    notifyListeners();
  }

  @override
  void dispose() {
    _cleanup();
    super.dispose();
  }
}

enum EllaCallState { idle, connecting, connected, disconnecting, error }
