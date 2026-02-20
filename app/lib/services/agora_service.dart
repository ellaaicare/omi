import 'dart:async';
import 'package:agora_rtc_engine/agora_rtc_engine.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:flutter/foundation.dart';

/// Agora RTC voice service with lazy initialization
/// to avoid conflicts with WebSocket-based voice chat.
class AgoraService {
  static final AgoraService _instance = AgoraService._internal();
  factory AgoraService() => _instance;
  AgoraService._internal();

  RtcEngine? _engine;
  bool _isInitialized = false;
  bool _isInChannel = false;
  String? _currentChannel;
  int? _currentUid;

  final _onUserJoinedController = StreamController<int>.broadcast();
  final _onUserOfflineController = StreamController<int>.broadcast();
  final _onJoinChannelSuccessController = StreamController<RtcConnection>.broadcast();
  final _onLeaveChannelController = StreamController<RtcStats>.broadcast();
  final _onErrorController = StreamController<ErrorCodeType>.broadcast();

  Stream<int> get onUserJoined => _onUserJoinedController.stream;
  Stream<int> get onUserOffline => _onUserOfflineController.stream;
  Stream<RtcConnection> get onJoinChannelSuccess => _onJoinChannelSuccessController.stream;
  Stream<RtcStats> get onLeaveChannel => _onLeaveChannelController.stream;
  Stream<ErrorCodeType> get onError => _onErrorController.stream;

  bool get isInitialized => _isInitialized;
  bool get isInChannel => _isInChannel;
  String? get currentChannel => _currentChannel;

  /// Lazy initialization - only called when actually needed
  Future<void> _ensureInitialized() async {
    if (_isInitialized) return;

    debugPrint('[AgoraService] Initializing Agora RTC Engine...');

    // Request microphone permission
    final status = await Permission.microphone.request();
    if (!status.isGranted) {
      debugPrint('[AgoraService] Microphone permission denied');
      throw Exception('Microphone permission required for Agora calls');
    }

    // Create and initialize engine
    _engine = createAgoraRtcEngine();
    await _engine!.initialize(const RtcEngineContext(
      appId: '55dd93fbff4946d7bcbff6f6ebcee462', // Will be configured via environment/config
      channelProfile: ChannelProfileType.channelProfileCommunication,
    ));

    // Register event handlers
    _engine!.registerEventHandler(RtcEngineEventHandler(
      onJoinChannelSuccess: (connection, elapsed) {
        debugPrint('[AgoraService] Successfully joined channel: \${connection.channelId}');
        _isInChannel = true;
        _onJoinChannelSuccessController.add(connection);
      },
      onUserJoined: (connection, remoteUid, elapsed) {
        debugPrint('[AgoraService] Remote user joined: \$remoteUid');
        _onUserJoinedController.add(remoteUid);
      },
      onUserOffline: (connection, remoteUid, reason) {
        debugPrint('[AgoraService] Remote user offline: \$remoteUid');
        _onUserOfflineController.add(remoteUid);
      },
      onLeaveChannel: (connection, stats) {
        debugPrint('[AgoraService] Left channel');
        _isInChannel = false;
        _currentChannel = null;
        _currentUid = null;
        _onLeaveChannelController.add(stats);
      },
      onAudioVolumeIndication: (connection, speakers, speakerNumber, totalVolume) {
        if (speakers.isNotEmpty) {
          debugPrint("[AgoraService] Audio volume: ${speakers[0].volume}");
        }
      },
      onFirstRemoteAudioFrame: (connection, userId, elapsed) {
        debugPrint("[AgoraService] First remote audio frame from user: $userId");
      },
      onFirstLocalAudioFrame: (connection, elapsed) {
        debugPrint("[AgoraService] First local audio frame sent (${elapsed}ms)");
      },
      onError: (err, msg) {
        debugPrint('[AgoraService] Error: \$err - \$msg');
        _onErrorController.add(err);
      },
    ));

    // Enable audio
    await _engine!.enableAudio();
    await _engine!.setAudioProfile(
      profile: AudioProfileType.audioProfileDefault,
      scenario: AudioScenarioType.audioScenarioChatroom,
    );

    _isInitialized = true;
    debugPrint('[AgoraService] Agora RTC Engine initialized successfully');
  }

  /// Join an Agora voice channel
  Future<void> joinChannel({
    required String channelName,
    required String token,
    required int uid,
  }) async {
    debugPrint('[AgoraService] Joining channel: \$channelName with UID: \$uid');

    // Ensure engine is initialized
    await _ensureInitialized();

    if (_isInChannel) {
      debugPrint('[AgoraService] Already in a channel, leaving first');
      await leaveChannel();
    }

    _currentChannel = channelName;
    _currentUid = uid;

    final options = ChannelMediaOptions(
      channelProfile: ChannelProfileType.channelProfileCommunication,
      clientRoleType: ClientRoleType.clientRoleBroadcaster,
      autoSubscribeAudio: true,
      publishMicrophoneTrack: true,
    );

    await _engine!.joinChannel(
      token: token,
      channelId: channelName,
      uid: uid,
      options: options,
    );

    debugPrint('[AgoraService] Join channel request sent');
  }

  /// Leave the current channel
  Future<void> leaveChannel() async {
    if (!_isInitialized || !_isInChannel) {
      debugPrint('[AgoraService] Not in a channel, nothing to leave');
      return;
    }

    debugPrint('[AgoraService] Leaving channel: \$_currentChannel');
    await _engine!.leaveChannel();
  }

  /// Mute/unmute local microphone
  Future<void> muteLocalAudio(bool muted) async {
    if (!_isInitialized) return;
    await _engine!.muteLocalAudioStream(muted);
    debugPrint('[AgoraService] Local audio ${muted ? "muted" : "unmuted"}');
  }

  /// Dispose and clean up resources
  Future<void> dispose() async {
    debugPrint('[AgoraService] Disposing Agora service');

    if (_isInChannel) {
      await leaveChannel();
    }

    if (_isInitialized) {
      await _engine!.release();
      _isInitialized = false;
      _engine = null;
    }

    await _onUserJoinedController.close();
    await _onUserOfflineController.close();
    await _onJoinChannelSuccessController.close();
    await _onLeaveChannelController.close();
    await _onErrorController.close();

    debugPrint('[AgoraService] Agora service disposed');
  }
}
