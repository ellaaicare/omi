import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_voice_call_service.dart';
import 'package:omi/ella/widgets/ella_voice_orb.dart';

/// Voice call page — connects to the Ella Agora server bot.
///
/// The entire STT→LLM→TTS pipeline runs server-side.
/// This page manages the call lifecycle and shows connection state.
class EllaVoiceChatPage extends StatefulWidget {
  const EllaVoiceChatPage({super.key});

  @override
  State<EllaVoiceChatPage> createState() => _EllaVoiceChatPageState();
}

class _EllaVoiceChatPageState extends State<EllaVoiceChatPage> with AutomaticKeepAliveClientMixin {
  final _callService = EllaVoiceCallService();

  // Placeholder — replace with Firebase UID or SharedPrefs UID in production
  static const _userId = 'greg';

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
    _callService.addListener(_onCallStateChanged);
    // Auto-start when page opens
    _callService.startCall(_userId);
  }

  @override
  void dispose() {
    _callService.removeListener(_onCallStateChanged);
    _callService.endCall();
    _callService.dispose();
    super.dispose();
  }

  void _onCallStateChanged() {
    if (mounted) setState(() {});
  }

  Future<void> _onOrbTap() async {
    HapticFeedback.mediumImpact();
    switch (_callService.state) {
      case EllaCallState.idle:
      case EllaCallState.error:
        await _callService.startCall(_userId);
        break;
      case EllaCallState.connected:
        await _callService.endCall();
        break;
      default:
        break;
    }
  }

  VoiceOrbState get _orbState {
    switch (_callService.state) {
      case EllaCallState.connecting:
        return VoiceOrbState.processing;
      case EllaCallState.connected:
        return VoiceOrbState.listening;
      case EllaCallState.disconnecting:
        return VoiceOrbState.processing;
      case EllaCallState.error:
      case EllaCallState.idle:
        return VoiceOrbState.idle;
    }
  }

  String get _statusText {
    switch (_callService.state) {
      case EllaCallState.connecting:
        return 'Connecting to Ella...';
      case EllaCallState.connected:
        return 'Tap to end call';
      case EllaCallState.disconnecting:
        return 'Ending call...';
      case EllaCallState.error:
        return _callService.error != null ? 'Error — Tap to retry' : 'Connection error';
      case EllaCallState.idle:
        return 'Tap to call Ella';
    }
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        automaticallyImplyLeading: false,
        backgroundColor: EllaColors.bgPrimary,
        title: const Text(
          'Voice Chat',
          style: TextStyle(fontSize: 22, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
        ),
        elevation: 0,
        centerTitle: true,
      ),
      body: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Spacer(flex: 2),
            Center(
              child: EllaVoiceOrb(
                state: _orbState,
                audioLevel: _callService.state == EllaCallState.connected ? 0.5 : 0.0,
                onTap: _onOrbTap,
              ),
            ),
            const SizedBox(height: 24),
            Center(
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                decoration: BoxDecoration(
                  color: EllaColors.bgSecondary,
                  borderRadius: BorderRadius.circular(EllaSizes.radiusCircular),
                  border: Border.all(
                    color: _callService.state == EllaCallState.error
                        ? Colors.red.withOpacity(0.4)
                        : EllaColors.primary.withOpacity(0.22),
                    width: 1,
                  ),
                ),
                child: Text(
                  _statusText,
                  style: TextStyle(
                    fontSize: 18,
                    fontWeight: FontWeight.w600,
                    color: _callService.state == EllaCallState.error
                        ? Colors.red[300]
                        : EllaColors.textSecondary,
                  ),
                  textAlign: TextAlign.center,
                ),
              ),
            ),
            if (_callService.state == EllaCallState.error && _callService.error != null) ...[
              const SizedBox(height: 8),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 32),
                child: Text(
                  _callService.error!,
                  style: TextStyle(fontSize: 13, color: Colors.red[200]),
                  textAlign: TextAlign.center,
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ],
            const Spacer(flex: 3),
          ],
        ),
      ),
    );
  }
}
