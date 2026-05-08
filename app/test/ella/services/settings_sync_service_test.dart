import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/settings_sync_service.dart';
import 'package:omi/env/env.dart';

class _TestEnv implements EnvFields {
  @override
  String? get apiBaseUrl => 'https://api.ella-ai-care.com/';

  @override
  String? get googleClientId => null;

  @override
  String? get googleClientSecret => null;

  @override
  String? get googleMapsApiKey => null;

  @override
  String? get growthbookApiKey => null;

  @override
  String? get intercomAndroidApiKey => null;

  @override
  String? get intercomAppId => null;

  @override
  String? get intercomIOSApiKey => null;

  @override
  String? get mixpanelProjectToken => null;

  @override
  String? get openAIAPIKey => null;

  @override
  bool? get useAuthCustomToken => false;

  @override
  bool? get useWebAuth => false;
}

class _FakeSettingsTransport implements EllaSettingsSyncTransport {
  final List<String> getPaths = [];
  final List<String> patchPaths = [];
  final List<Map<String, dynamic>> patchPayloads = [];
  EllaSettingsSyncResponse? getResponse;
  EllaSettingsSyncResponse? patchResponse;

  @override
  Future<EllaSettingsSyncResponse?> get(String path) async {
    getPaths.add(path);
    return getResponse;
  }

  @override
  Future<EllaSettingsSyncResponse?> patch(String path, Map<String, dynamic> payload) async {
    patchPaths.add(path);
    patchPayloads.add(payload);
    return patchResponse;
  }
}

void main() {
  group('EllaSettingsSyncService', () {
    late _FakeSettingsTransport transport;

    setUpAll(() {
      Env.init(_TestEnv());
    });

    setUp(() async {
      SharedPreferences.setMockInitialValues({});
      await SharedPreferencesUtil.init();
      SharedPreferencesUtil().uid = 'uid-123';
      SharedPreferencesUtil().ttsProvider = 'elevenlabs';
      transport = _FakeSettingsTransport();
      EllaSettingsSyncService.transport = transport;
    });

    test('builds backend-friendly voice settings payload', () {
      final payload = EllaSettingsSyncService.buildVoiceSettingsPatchPayload(
        voiceMode: 'gemini-live',
        updatedAt: DateTime.utc(2026, 5, 8, 18),
        clientVersion: '1.0.524+780',
      );
      final voice = (payload['settings'] as Map<String, dynamic>)['voice'] as Map<String, dynamic>;

      expect(payload['voice_mode'], 'gemini-native-live');
      expect(payload['tts_provider'], 'gemini-native-live');
      expect(payload['source_setting'], 'devTtsProvider');
      expect(voice['conversation_provider'], 'gemini-native-live');
      expect(voice['uses_v2v_session'], isTrue);
      expect(voice['session_voice_mode'], 'gemini-native-live-v1');
      expect(voice['client_version'], '1.0.524+780');
    });

    test('setVoiceMode stores local cache and clears dirty flag after patch succeeds', () async {
      transport.patchResponse = const EllaSettingsSyncResponse(statusCode: 200, body: {});

      final ok = await EllaSettingsSyncService.setVoiceMode('openai-realtime');

      expect(ok, isTrue);
      expect(SharedPreferencesUtil().ttsProvider, 'openai-native-realtime');
      expect(EllaSettingsSyncService.hasPendingVoiceMode, isFalse);
      expect(EllaSettingsSyncService.lastSyncedVoiceMode, 'openai-native-realtime');
      expect(transport.patchPaths, ['v1/ella/settings']);
      expect(transport.patchPayloads.single['voice_mode'], 'openai-native-realtime');
    });

    test('keeps dirty local voice mode when patch fails', () async {
      transport.patchResponse = const EllaSettingsSyncResponse(statusCode: 404, body: {});

      final ok = await EllaSettingsSyncService.setVoiceMode('grok-voice');

      expect(ok, isFalse);
      expect(SharedPreferencesUtil().ttsProvider, 'grok-voice');
      expect(EllaSettingsSyncService.hasPendingVoiceMode, isTrue);
      expect(EllaSettingsSyncService.lastSyncError, 'patch_status:404');
    });

    test('syncOnAppStart merges server voice mode when no local dirty change exists', () async {
      transport.patchResponse = const EllaSettingsSyncResponse(statusCode: 200, body: {});
      transport.getResponse = const EllaSettingsSyncResponse(
        statusCode: 200,
        body: {
          'settings': {
            'voice': {'voice_mode': 'gemini-live'},
          },
        },
      );

      await EllaSettingsSyncService.syncOnAppStart();

      expect(SharedPreferencesUtil().ttsProvider, 'gemini-native-live');
      expect(EllaSettingsSyncService.lastSyncedVoiceMode, 'gemini-native-live');
      expect(transport.getPaths, ['v1/ella/settings/effective']);
    });

    test('syncOnAppStart preserves dirty local voice mode if retry still fails', () async {
      await SharedPreferencesUtil().saveString('ellaSettingsPendingVoiceMode', 'grok-voice');
      await SharedPreferencesUtil().saveBool('ellaSettingsVoiceModeDirty', true);
      SharedPreferencesUtil().ttsProvider = 'grok-voice';
      transport.patchResponse = const EllaSettingsSyncResponse(statusCode: 500, body: {});
      transport.getResponse = const EllaSettingsSyncResponse(
        statusCode: 200,
        body: {
          'voice_mode': 'elevenlabs',
        },
      );

      await EllaSettingsSyncService.syncOnAppStart();

      expect(SharedPreferencesUtil().ttsProvider, 'grok-voice');
      expect(EllaSettingsSyncService.hasPendingVoiceMode, isTrue);
      expect(transport.getPaths, isEmpty);
    });
  });
}
