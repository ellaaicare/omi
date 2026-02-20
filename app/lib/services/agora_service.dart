import 'package:agora_rtc_engine/agora_rtc_engine.dart';
import 'package:permission_handler/permission_handler.dart';

class AgoraService {
  static const String appId = '55dd93fbff4946d7bcbff6f6ebcee462';

  RtcEngine? _engine;
  bool _isInCall = false;

  Future<void> initialize() async {
    await [Permission.microphone].request();

    _engine = createAgoraRtcEngine();
    await _engine!.initialize(RtcEngineContext(
      appId: appId,
      channelProfile: ChannelProfileType.channelProfileCommunication,
    ));

    await _engine!.enableAudio();

    _engine!.registerEventHandler(
      RtcEngineEventHandler(
        onJoinChannelSuccess: (RtcConnection connection, int elapsed) {
          print('[Agora] Joined channel: ${connection.channelId}');
          _isInCall = true;
        },
        onUserJoined: (RtcConnection connection, int remoteUid, int elapsed) {
          print('[Agora] Remote user joined: $remoteUid');
        },
        onLeaveChannel: (RtcConnection connection, RtcStats stats) {
          print('[Agora] Left channel');
          _isInCall = false;
        },
        onError: (ErrorCodeType err, String msg) {
          print('[Agora] Error: $err - $msg');
        },
      ),
    );
  }

  Future<void> joinChannel(String channelName, String token, int uid) async {
    if (_engine == null) {
      throw Exception('Agora engine not initialized');
    }

    await _engine!.joinChannel(
      token: token,
      channelId: channelName,
      uid: uid,
      options: const ChannelMediaOptions(
        clientRoleType: ClientRoleType.clientRoleBroadcaster,
        channelProfile: ChannelProfileType.channelProfileCommunication,
      ),
    );
  }

  Future<void> leaveChannel() async {
    await _engine?.leaveChannel();
  }

  Future<void> dispose() async {
    await _engine?.leaveChannel();
    await _engine?.release();
    _engine = null;
  }

  bool get isInCall => _isInCall;
}
