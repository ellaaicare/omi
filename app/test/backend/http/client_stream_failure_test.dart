import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/http_pool_manager.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class _StreamingClient extends http.BaseClient {
  _StreamingClient(this.handler);

  final Future<http.StreamedResponse> Function(http.BaseRequest request) handler;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) => handler(request);
}

class _TestEnv implements EnvFields {
  @override
  String? get apiBaseUrl => 'https://api.ella.test/';
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

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  Env.init(_TestEnv());
  PlatformManager.initializeForTesting();

  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'uid-a'});
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil()
      ..authToken = 'test-bearer'
      ..tokenExpirationTime = DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch;
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-a',
      serverDecidedAt: '2026-08-04T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
  });

  test('Hermes SSE failure is typed before it can become assistant content', () {
    expect(
      () => parseMessageChunk('data: Error: hermes_runtime_required', 'message-a'),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.workspaceRequired),
      ),
    );
  });

  test('unknown JSON SSE error is sanitized instead of becoming assistant content', () {
    expect(
      () => parseMessageChunk('data: {"error":"provider included private diagnostic text"}', 'message-a'),
      throwsA(
        isA<ClientApiFailure>()
            .having((failure) => failure.kind, 'kind', ClientApiFailureKind.unavailable)
            .having((failure) => failure.backendCode, 'raw backend code', isNull),
      ),
    );
  });

  test('streaming non-200 surfaces update-required instead of ending empty', () async {
    HttpPoolManager.instance.replaceClientForTesting(
      _StreamingClient(
        (_) async => http.StreamedResponse(
          Stream.value(utf8.encode('{"detail":"upgrade_required"}')),
          426,
        ),
      ),
    );

    await expectLater(
      makeStreamingApiCall(url: 'https://stream.test/chat').toList(),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.updateRequired),
      ),
    );
  });

  test('multipart voice stream surfaces backend failure instead of ending empty', () async {
    final file = File('${Directory.systemTemp.path}/typed-voice-stream.wav')..writeAsBytesSync([1, 2, 3]);
    addTearDown(() async {
      if (await file.exists()) await file.delete();
    });
    HttpPoolManager.instance.replaceClientForTesting(
      _StreamingClient(
        (_) async => http.StreamedResponse(
          Stream.value(utf8.encode('{"detail":"provider_unavailable"}')),
          503,
        ),
      ),
    );

    await expectLater(
      makeMultipartStreamingApiCall(url: 'https://stream.test/voice', files: [file]).toList(),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.unavailable),
      ),
    );
  });

  test('stream transport exception is typed instead of swallowed', () async {
    HttpPoolManager.instance.replaceClientForTesting(
      _StreamingClient((_) => throw const SocketException('offline')),
    );

    await expectLater(
      makeStreamingApiCall(url: 'https://stream.test/chat').toList(),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.unavailable),
      ),
    );
  });

  test('split text-stream backend error releases no assistant chunks', () async {
    _installStreamResponse(['data: Er', 'ror: hermes_runtime_required\n\n']);

    await _expectNoReleasedChunks(
      sendEllaMessageStream('private question'),
      ClientApiFailureKind.workspaceRequired,
    );
  });

  test('text data followed by EOF releases no assistant chunks', () async {
    _installStreamResponse(['data: partial assistant response\n\n']);

    await _expectNoReleasedChunks(
      sendEllaMessageStream('private question'),
      ClientApiFailureKind.incompleteStream,
    );
  });

  test('split multipart voice backend error releases no assistant chunks', () async {
    final file = _temporaryVoiceFile('split-error');
    addTearDown(() => file.delete());
    _installStreamResponse(['data: Error: hermes_', 'runtime_required\n\n']);

    await _expectNoReleasedChunks(
      sendVoiceMessageStreamServer([file]),
      ClientApiFailureKind.workspaceRequired,
    );
  });

  test('multipart voice data followed by EOF releases no assistant chunks', () async {
    final file = _temporaryVoiceFile('premature-eof');
    addTearDown(() => file.delete());
    _installStreamResponse(['data: partial voice response\n\n']);

    await _expectNoReleasedChunks(
      sendVoiceMessageStreamServer([file]),
      ClientApiFailureKind.incompleteStream,
    );
  });

  test('fragmented text data is released only with a valid terminal done frame', () async {
    final done = _doneFrame('Complete text reply');
    _installStreamResponse(['data: Complete ', 'text reply\n\ndon', done.substring(3)]);

    final chunks = await sendEllaMessageStream('private question').toList();

    expect(chunks.map((chunk) => chunk.type), [MessageChunkType.data, MessageChunkType.done]);
    expect(chunks.first.text, 'Complete text reply');
    expect(chunks.last.message?.text, 'Complete text reply');
  });

  test('fragmented multipart voice data is released only with a valid terminal done frame', () async {
    final file = _temporaryVoiceFile('complete');
    addTearDown(() => file.delete());
    final done = _doneFrame('Complete voice reply');
    _installStreamResponse(['data: Complete voice reply\r\n\r\nd', done.substring(1)]);

    final chunks = await sendVoiceMessageStreamServer([file]).toList();

    expect(chunks.map((chunk) => chunk.type), [MessageChunkType.data, MessageChunkType.done]);
    expect(chunks.first.text, 'Complete voice reply');
    expect(chunks.last.message?.text, 'Complete voice reply');
  });
}

void _installStreamResponse(List<String> chunks) {
  HttpPoolManager.instance.replaceClientForTesting(
    _StreamingClient(
      (_) async => http.StreamedResponse(
        Stream.fromIterable(chunks.map(utf8.encode)),
        200,
      ),
    ),
  );
}

Future<void> _expectNoReleasedChunks(
  Stream<ServerMessageChunk> stream,
  ClientApiFailureKind expectedKind,
) async {
  final released = <ServerMessageChunk>[];
  Object? caught;
  try {
    await for (final chunk in stream) {
      released.add(chunk);
    }
  } catch (error) {
    caught = error;
  }

  expect(released, isEmpty);
  expect(
    caught,
    isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', expectedKind),
  );
}

File _temporaryVoiceFile(String suffix) =>
    File('${Directory.systemTemp.path}/typed-voice-stream-$suffix.wav')..writeAsBytesSync([1, 2, 3]);

String _doneFrame(String text) {
  final message = jsonEncode({
    'id': 'assistant-a',
    'created_at': '2026-08-04T00:00:00Z',
    'text': text,
    'sender': 'ai',
    'type': 'text',
    'plugin_id': null,
    'from_integration': false,
    'files': <Object>[],
    'files_id': <Object>[],
    'memories': <Object>[],
    'ask_for_nps': false,
  });
  return 'done: ${base64Encode(utf8.encode(message))}\n\n';
}
