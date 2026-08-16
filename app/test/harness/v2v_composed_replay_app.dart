import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/platform/platform_manager.dart';

const composedReplayAppTestName =
    'actual app terminal handler persists canonical replay and emits only the current-session ACK';

class _HarnessEnv implements EnvFields {
  @override
  String? get apiBaseUrl => 'http://127.0.0.1/';
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

class _CurrentAuthority implements ExactAccountAuthorityVerifier {
  const _CurrentAuthority(this.uid);

  @override
  final String uid;

  @override
  bool isExactCurrent() => true;
}

String _requiredEnvironment(String name) {
  final value = Platform.environment[name]?.trim() ?? '';
  if (value.isEmpty) throw StateError('missing composed replay environment: $name');
  return value;
}

void _grantCurrentConsent(String uid) {
  final preferences = SharedPreferencesUtil();
  preferences.uid = uid;
  preferences.verifiedPersonaId = 'composed-replay-persona';
  const profileBindingId = 'composed-replay-profile';
  const receiptId = '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}composed-replay';
  preferences.acceptAiConsent(
    receiptId: receiptId,
    uid: uid,
    profileBindingId: profileBindingId,
    serverDecidedAt: '2026-08-15T20:00:00Z',
  );
  preferences.markAiConsentServerVerified(
    uid: uid,
    receiptId: receiptId,
    policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    profileBindingId: profileBindingId,
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  Env.init(_HarnessEnv());
  PlatformManager.initializeForTesting();

  test(composedReplayAppTestName, () async {
    HttpOverrides.global = null;
    const uid = 'uid-composed-replay';
    final backendUrl = _requiredEnvironment('ELLA_COMPOSED_BACKEND_URL');
    final framePath = _requiredEnvironment('ELLA_COMPOSED_FRAME_PATH');
    final ackPath = _requiredEnvironment('ELLA_COMPOSED_ACK_PATH');
    final sessionId = _requiredEnvironment('ELLA_COMPOSED_SESSION_ID');
    final dropAck = _requiredEnvironment('ELLA_COMPOSED_DROP_ACK') == 'true';
    final terminalFrame = await File(framePath).readAsString();
    final terminalPayload = jsonDecode(terminalFrame) as Map<String, dynamic>;
    expect(terminalPayload['session_id'], sessionId);

    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    _grantCurrentConsent(uid);
    const authority = _CurrentAuthority(uid);

    final incoming = StreamController<dynamic>(sync: true);
    final acknowledgements = <Map<String, dynamic>>[];
    final client = V2VClient(
      onTerminalTurnDurable: (turn) async {
        final result = await persistEllaV2VTurn(
          uid: uid,
          sessionId: turn.sessionId,
          turnId: turn.turnId,
          userEventId: turn.userEventId,
          assistantEventId: turn.assistantEventId,
          userTranscript: turn.userTranscript,
          assistantTranscript: turn.assistantTranscript,
          startedAt: turn.startedAt,
          completedAt: turn.completedAt,
          turnOrdinal: null,
          exactAuthority: authority,
          transport: ({required url, required body, required expectedAuthenticatedUid, required exactAuthority}) {
            expect(expectedAuthenticatedUid, uid);
            expect(exactAuthority.isExactCurrent(), isTrue);
            return http.post(
              Uri.parse(backendUrl),
              headers: {'Content-Type': 'application/json', 'X-Composed-Authenticated-Uid': uid},
              body: body,
            );
          },
        );
        expect(result.isSuccess, isTrue, reason: result.failure?.kind.name);
        expect(result.value, hasLength(2));
        expect(result.value!.map((message) => message.id).toSet(), {turn.userEventId, turn.assistantEventId});
        return result.isSuccess;
      },
      providerRegistryValidator: (_) async => null,
      sessionCreator: (_, provider, __) async => {
        'session_token': 'opaque-composed-test-token',
        'voice_endpoint': 'wss://example.invalid/voice',
        'provider': provider,
        'voice_mode': 'grok-voice',
        'session_id': sessionId,
      },
      audioSessionConfigurator: () async {},
      webSocketConnector: (_) => V2VWebSocketTransport(
        ready: Future<void>.value(),
        stream: incoming.stream,
        send: (value) {
          if (value is! String) return;
          final payload = jsonDecode(value) as Map<String, dynamic>;
          if (payload['type'] != 'transcript_turn_ack') return;
          acknowledgements.add(payload);
          if (!dropAck) File(ackPath).writeAsStringSync(value, flush: true);
        },
        close: incoming.close,
      ),
      microphoneStarter: () async => true,
    );

    final connection = await client.connect(provider: 'grok-voice', beforeTransportActivation: (_) async => true);
    expect(connection.connected, isTrue);
    expect(connection.sessionId, sessionId);

    incoming.add(terminalFrame);
    await client.settleTerminalTurnsForTesting();

    expect(acknowledgements, hasLength(1));
    expect(acknowledgements.single, {
      'type': 'transcript_turn_ack',
      'contract_version': 1,
      'session_id': sessionId,
      'turn_id': terminalPayload['turn_id'],
    });
    expect(File(ackPath).existsSync(), isNot(dropAck));
    await client.disconnect();
  });
}
