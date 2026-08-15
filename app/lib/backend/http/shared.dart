import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:path/path.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/http_pool_manager.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/log_redaction.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class ApiClient {
  static const Duration requestTimeoutRead = Duration(seconds: 30);
  static const Duration requestTimeoutWrite = Duration(seconds: 300);

  static void dispose() {
    HttpPoolManager.instance.dispose();
  }
}

Future<String> getAuthHeader() async {
  DateTime? expiry = DateTime.fromMillisecondsSinceEpoch(SharedPreferencesUtil().tokenExpirationTime);
  bool hasAuthToken = SharedPreferencesUtil().authToken.isNotEmpty;

  bool isExpirationDateValid = !(expiry.isBefore(DateTime.now()) ||
      expiry.isAtSameMomentAs(DateTime.fromMillisecondsSinceEpoch(0)) ||
      (expiry.isBefore(DateTime.now().add(const Duration(minutes: 5))) && expiry.isAfter(DateTime.now())));

  if (!hasAuthToken || !isExpirationDateValid) {
    SharedPreferencesUtil().authToken = await AuthService.instance.getIdToken() ?? '';
  }

  if (SharedPreferencesUtil().authToken.isEmpty) {
    if (AuthService.instance.isSignedIn()) {
      // should only throw if the user is signed in but the token is not found
      // if the user is not signed in, the token will always be empty
      throw Exception('No auth token found');
    }
  }
  return 'Bearer ${SharedPreferencesUtil().authToken}';
}

/// Builds common headers for API and WebSocket requests
/// Centralizes header logic for easy maintenance and consistency
/// Automatically adds Authorization header if required
Future<Map<String, String>> buildHeaders({
  required bool requireAuthCheck,
  Map<String, String> fromHeaders = const {},
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async {
  _verifyRequestAuthority(
    exactAuthority: exactAuthority,
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    boundary: 'before request header construction',
  );
  final headers = <String, String>{
    'X-Request-Start-Time': (DateTime.now().millisecondsSinceEpoch / 1000).toString(),
    'X-App-Platform': PlatformManager.instance.platform,
    'X-Device-Id-Hash': PlatformManager.instance.deviceIdHash,
    'X-App-Version': PlatformManager.instance.appVersion,
    ...fromHeaders,
  };

  if (requireAuthCheck) {
    headers['Authorization'] = await getAuthHeader();
  }

  _verifyRequestAuthority(
    exactAuthority: exactAuthority,
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    boundary: 'during request header construction',
  );

  return headers;
}

void _verifyRequestAuthority({
  required ExactAccountAuthorityVerifier? exactAuthority,
  required String? expectedAuthenticatedUid,
  required String boundary,
}) {
  if (exactAuthority != null && !exactAuthority.isExactCurrent()) {
    throw ExactAccountAuthorityChangedException('Exact account authority changed $boundary');
  }
  if (expectedAuthenticatedUid != null && AuthService.instance.getFirebaseUser()?.uid != expectedAuthenticatedUid) {
    throw StateError('Authenticated account changed $boundary');
  }
}

bool _isRequiredAuthCheck(String url) {
  if (url.contains(Env.apiBaseUrl!)) {
    return true;
  }
  return false;
}

Future<http.StreamedResponse> makeRawApiCall({
  required String url,
  required String method,
  Map<String, String> headers = const {},
}) async {
  final builtHeaders = await buildHeaders(requireAuthCheck: _isRequiredAuthCheck(url), fromHeaders: headers);
  var request = http.Request(method, Uri.parse(url));
  request.headers.addAll(builtHeaders);
  return HttpPoolManager.instance.sendStreaming(request);
}

Future<http.Response?> makeApiCall({
  required String url,
  required Map<String, String> headers,
  required String body,
  required String method,
  Duration? timeout,
  int? retries,
  bool retryOnUnauthorized = true,
  bool enforceAbsoluteTimeout = false,
  bool? requireAuthCheck,
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async {
  try {
    final effectiveTimeout =
        timeout ?? (method == 'GET' ? ApiClient.requestTimeoutRead : ApiClient.requestTimeoutWrite);
    final effectiveRetries = retries ?? 1;
    final absoluteDeadline = enforceAbsoluteTimeout
        ? MonotonicRequestDeadline.forRequest(timeout: effectiveTimeout, retries: effectiveRetries)
        : null;
    final shouldCheckAuth = requireAuthCheck ?? _isRequiredAuthCheck(url);
    Map<String, String> builtHeaders = await buildHeaders(
      requireAuthCheck: shouldCheckAuth,
      fromHeaders: headers,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

    http.Response response = await HttpPoolManager.instance.send(
      () => _buildRequest(url, builtHeaders, body, method),
      timeout: effectiveTimeout,
      retries: effectiveRetries,
      exactAuthority: exactAuthority,
      absoluteDeadline: absoluteDeadline,
    );

    if (retryOnUnauthorized && shouldCheckAuth && response.statusCode == 401) {
      Logger.log('Token expired on 1st attempt');
      SharedPreferencesUtil().authToken = await AuthService.instance.getIdToken() ?? '';
      if (SharedPreferencesUtil().authToken.isNotEmpty) {
        builtHeaders = await buildHeaders(
          requireAuthCheck: shouldCheckAuth,
          fromHeaders: headers,
          expectedAuthenticatedUid: expectedAuthenticatedUid,
          exactAuthority: exactAuthority,
        );
        response = await HttpPoolManager.instance.send(
          () => _buildRequest(url, builtHeaders, body, method),
          timeout: effectiveTimeout,
          retries: 0,
          exactAuthority: exactAuthority,
          absoluteDeadline: absoluteDeadline,
        );
        Logger.log('Token refreshed and request retried');
        if (response.statusCode == 401) {
          await AuthService.instance.signOut();
          Logger.handle(
            Exception('Authentication failed. Please sign in again.'),
            StackTrace.current,
            message: 'Authentication failed. Please sign in again.',
          );
        }
      } else {
        await AuthService.instance.signOut();
        Logger.handle(
          Exception('Authentication failed. Please sign in again.'),
          StackTrace.current,
          message: 'Authentication failed. Please sign in again.',
        );
      }
    }

    return response;
  } catch (e, stackTrace) {
    if (e is ExactAccountAuthorityChangedException) rethrow;
    Logger.debug('HTTP request failed: $e, $stackTrace');
    await _reportTransportFailure(e, stackTrace, url: url, method: method);
    return null;
  }
}

http.Request _buildRequest(String url, Map<String, String> headers, String body, String method) {
  final request = http.Request(method, Uri.parse(url));
  request.headers.addAll(headers);
  if (method != 'GET' && body.isNotEmpty) {
    request.headers['Content-Type'] = 'application/json';
    request.body = body;
  }
  return request;
}

Future<http.Response> makeMultipartApiCall({
  required String url,
  required List<File> files,
  Map<String, String> headers = const {},
  Map<String, String> fields = const {},
  String fileFieldName = 'files',
  String method = 'POST',
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async {
  try {
    final builtHeaders = await buildHeaders(
      requireAuthCheck: _isRequiredAuthCheck(url),
      fromHeaders: headers,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

    _verifyRequestAuthority(
      exactAuthority: exactAuthority,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      boundary: 'before multipart assembly',
    );

    var request = http.MultipartRequest(method, Uri.parse(url));
    request.headers.addAll(builtHeaders);
    request.fields.addAll(fields);

    for (var file in files) {
      var stream = http.ByteStream(file.openRead());
      var length = await file.length();
      var multipartFile = http.MultipartFile(fileFieldName, stream, length, filename: basename(file.path));
      request.files.add(multipartFile);
    }

    var streamedResponse = await HttpPoolManager.instance.sendStreaming(request, exactAuthority: exactAuthority);
    final response = await http.Response.fromStream(streamedResponse);
    _verifyRequestAuthority(
      exactAuthority: exactAuthority,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      boundary: 'after multipart response',
    );
    return response;
  } catch (e, stackTrace) {
    if (e is ExactAccountAuthorityChangedException) rethrow;
    Logger.debug('Multipart HTTP request failed: $e, $stackTrace');
    await _reportTransportFailure(e, stackTrace, url: url, method: method);
    rethrow;
  }
}

Stream<String> makeStreamingApiCall({
  required String url,
  Map<String, String> headers = const {},
  String body = '',
  String method = 'POST',
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async* {
  try {
    final builtHeaders = await buildHeaders(
      requireAuthCheck: _isRequiredAuthCheck(url),
      fromHeaders: headers,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

    var request = http.Request(method, Uri.parse(url));
    request.headers.addAll(builtHeaders);

    if (body.isNotEmpty) {
      request.headers['Content-Type'] = 'application/json';
      request.body = body;
    }

    var streamedResponse = await HttpPoolManager.instance.sendStreaming(request, exactAuthority: exactAuthority);

    if (streamedResponse.statusCode != 200) {
      Logger.error('Streaming request failed: ${streamedResponse.statusCode}');
      final body = await streamedResponse.stream.transform(utf8.decoder).join();
      throw ClientApiFailure.fromHttp(statusCode: streamedResponse.statusCode, body: body);
    }

    yield* _decodeDelimitedStreamingResponse(
      streamedResponse.stream,
      exactAuthority: exactAuthority,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      responseKind: 'streaming',
    );
  } catch (e, stackTrace) {
    if (e is ExactAccountAuthorityChangedException || e is ClientApiFailure) rethrow;
    Logger.error('Streaming request error: $e');
    await _reportTransportFailure(e, stackTrace, url: url, method: method);
    throw const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true);
  }
}

Stream<String> makeMultipartStreamingApiCall({
  required String url,
  required List<File> files,
  Map<String, String> headers = const {},
  Map<String, String> fields = const {},
  String fileFieldName = 'files',
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
}) async* {
  try {
    final builtHeaders = await buildHeaders(
      requireAuthCheck: _isRequiredAuthCheck(url),
      fromHeaders: headers,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

    _verifyRequestAuthority(
      exactAuthority: exactAuthority,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      boundary: 'before multipart stream assembly',
    );

    var request = http.MultipartRequest('POST', Uri.parse(url));
    request.headers.addAll(builtHeaders);
    request.fields.addAll(fields);

    for (var file in files) {
      request.files.add(await http.MultipartFile.fromPath(fileFieldName, file.path, filename: basename(file.path)));
    }

    var response = await HttpPoolManager.instance.sendStreaming(request, exactAuthority: exactAuthority);

    if (response.statusCode != 200) {
      Logger.error('Multipart streaming request failed: ${response.statusCode}');
      final body = await response.stream.transform(utf8.decoder).join();
      throw ClientApiFailure.fromHttp(statusCode: response.statusCode, body: body);
    }

    yield* _decodeDelimitedStreamingResponse(
      response.stream,
      exactAuthority: exactAuthority,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      responseKind: 'multipart streaming',
    );
  } catch (e, stackTrace) {
    if (e is ExactAccountAuthorityChangedException || e is ClientApiFailure) rethrow;
    Logger.error('Multipart streaming request error: $e');
    await _reportTransportFailure(e, stackTrace, url: url, method: 'POST');
    throw const ClientApiFailure(ClientApiFailureKind.unavailable, retryable: true);
  }
}

Stream<String> _decodeDelimitedStreamingResponse(
  Stream<List<int>> responseStream, {
  required ExactAccountAuthorityVerifier? exactAuthority,
  required String? expectedAuthenticatedUid,
  required String responseKind,
}) async* {
  final framer = _DelimitedEventFramer();
  await for (final data in responseStream.transform(utf8.decoder)) {
    _verifyRequestAuthority(
      exactAuthority: exactAuthority,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      boundary: 'during $responseKind response',
    );
    for (final frame in framer.add(data)) {
      _verifyRequestAuthority(
        exactAuthority: exactAuthority,
        expectedAuthenticatedUid: expectedAuthenticatedUid,
        boundary: 'before $responseKind response delivery',
      );
      yield frame;
    }
  }

  _verifyRequestAuthority(
    exactAuthority: exactAuthority,
    expectedAuthenticatedUid: expectedAuthenticatedUid,
    boundary: 'after $responseKind response completion',
  );
  if (framer.hasIncompleteFrame) {
    throw const ClientApiFailure(ClientApiFailureKind.incompleteStream, retryable: true);
  }
}

class _DelimitedEventFramer {
  String _pending = '';

  bool get hasIncompleteFrame => _pending.trim().isNotEmpty;

  List<String> add(String chunk) {
    _pending += chunk;
    final frames = <String>[];
    while (true) {
      final lfIndex = _pending.indexOf('\n\n');
      final crlfIndex = _pending.indexOf('\r\n\r\n');
      if (lfIndex < 0 && crlfIndex < 0) break;

      final useCrlf = crlfIndex >= 0 && (lfIndex < 0 || crlfIndex <= lfIndex);
      final delimiterIndex = useCrlf ? crlfIndex : lfIndex;
      final delimiterLength = useCrlf ? 4 : 2;
      final frame = _pending.substring(0, delimiterIndex).replaceAll('\r\n', '\n');
      _pending = _pending.substring(delimiterIndex + delimiterLength);
      if (frame.trim().isNotEmpty) frames.add(frame);
    }
    return frames;
  }
}

Future<void> _reportTransportFailure(
  Object error,
  StackTrace stackTrace, {
  required String url,
  required String method,
}) async {
  try {
    await PlatformManager.instance.crashReporter.reportCrash(
      error,
      stackTrace,
      userAttributes: {'url': redactUrlForLogs(url), 'method': method},
    );
  } catch (_) {
    // Telemetry must never replace the typed transport failure.
  }
}

// Function to extract content from the API response.
dynamic extractContentFromResponse(
  http.Response? response, {
  bool isEmbedding = false,
  bool isFunctionCalling = false,
}) {
  if (response != null && response.statusCode == 200) {
    var data = jsonDecode(response.body);
    if (isEmbedding) {
      var embedding = data['data'][0]['embedding'];
      return embedding;
    }
    var message = data['choices'][0]['message'];
    if (isFunctionCalling && message['tool_calls'] != null) {
      Logger.debug('message $message');
      Logger.debug('message ${message['tool_calls'].runtimeType}');
      return message['tool_calls'];
    }
    return data['choices'][0]['message']['content'];
  } else {
    Logger.debug('Error fetching data: ${response?.statusCode}');
    // TODO: handle error, better specially for script migration
    PlatformManager.instance.crashReporter.reportCrash(
      Exception('Error fetching data: ${response?.statusCode}'),
      StackTrace.current,
      userAttributes: {
        'response_null': (response == null).toString(),
        'response_status_code': response?.statusCode.toString() ?? '',
        'is_embedding': isEmbedding.toString(),
        'is_function_calling': isFunctionCalling.toString(),
      },
    );
    return null;
  }
}
