import 'package:flutter/material.dart';
import 'package:omi/services/agora_service.dart';
import 'package:http/http.dart' as http;
import 'dart:convert';

/// Simple test button for Agora voice calls
class AgoraTestButton extends StatefulWidget {
  const AgoraTestButton({super.key});

  @override
  State<AgoraTestButton> createState() => _AgoraTestButtonState();
}

class _AgoraTestButtonState extends State<AgoraTestButton> {
  final _agoraService = AgoraService();
  bool _isInCall = false;
  String _statusMessage = 'Test Agora';

  Future<void> _startAgoraTest() async {
    try {
      setState(() {
        _statusMessage = 'Starting...';
      });

      // Call the /v2/call/start endpoint
      final response = await http.post(
        Uri.parse('https://voice.ella-ai-care.com/v2/call/start'),
        headers: {'Content-Type': 'application/json'},
        body: json.encode({
          'user_id': 'test_user_${DateTime.now().millisecondsSinceEpoch}',
          'direction': 'outbound',
        }),
      );

      if (response.statusCode != 200) {
        throw Exception('Failed to start call: ${response.statusCode}');
      }

      final data = json.decode(response.body);
      final channelName = data['channel_name'] as String;
      final token = data['token'] as String;
      final userUid = data['user_uid'] as int;

      debugPrint('[AgoraTest] Joining channel: $channelName');

      // Join the Agora channel
      await _agoraService.joinChannel(
        channelName: channelName,
        token: token,
        uid: userUid,
      );

      setState(() {
        _isInCall = true;
        _statusMessage = 'In Call';
      });

      debugPrint('[AgoraTest] Successfully joined channel');
    } catch (e) {
      debugPrint('[AgoraTest] Error starting call: $e');
      setState(() {
        _statusMessage = 'Error';
        _isInCall = false;
      });
      
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Agora test failed: $e')),
        );
      }
    }
  }

  Future<void> _endAgoraTest() async {
    try {
      setState(() {
        _statusMessage = 'Ending...';
      });

      await _agoraService.leaveChannel();

      setState(() {
        _isInCall = false;
        _statusMessage = 'Test Agora';
      });

      debugPrint('[AgoraTest] Successfully left channel');
    } catch (e) {
      debugPrint('[AgoraTest] Error ending call: $e');
      setState(() {
        _statusMessage = 'Error';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: _isInCall ? Colors.red.shade700 : Colors.blue.shade700,
        borderRadius: BorderRadius.circular(30),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withOpacity(0.3),
            blurRadius: 8,
            offset: const Offset(0, 4),
          ),
        ],
      ),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: _isInCall ? _endAgoraTest : _startAgoraTest,
          borderRadius: BorderRadius.circular(30),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(
                  _isInCall ? Icons.call_end : Icons.phone,
                  color: Colors.white,
                  size: 20,
                ),
                const SizedBox(width: 8),
                Text(
                  _statusMessage,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 14,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  @override
  void dispose() {
    if (_isInCall) {
      _agoraService.leaveChannel();
    }
    super.dispose();
  }
}
