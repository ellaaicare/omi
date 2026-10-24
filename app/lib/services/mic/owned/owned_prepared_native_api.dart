import 'package:flutter/services.dart';
import 'owned_capture_contract.dart';
import 'owned_phone_mic.g.dart';

/// Separate prepared lifecycle; the frozen Pigeon drain/probe wire is reused.
/// No registration/activation is performed by constructing this adapter.
class PreparedOwnedPhoneMicApi implements PreparedOwnedNativeApi {
  PreparedOwnedPhoneMicApi({BinaryMessenger? messenger})
      : _prepared = MethodChannel('ella.owned_private_capture.v1', const StandardMethodCodec(), messenger),
        _drain = OwnedPhoneMicHostApi(binaryMessenger: messenger);
  final MethodChannel _prepared;
  final OwnedPhoneMicHostApi _drain;

  Future<Map<Object?, Object?>?> _invoke(String method, Map<String, Object?> arguments) async {
    try {
      return await _prepared.invokeMapMethod<Object?, Object?>(method, arguments);
    } catch (error) {
      if (method == 'startPreparedOwnedStream' &&
          error is PlatformException &&
          error.code == 'owned-start-held' &&
          error.details is Map) {
        final details = error.details as Map;
        if (details.length == 2 &&
            details['captureStartId'] is String &&
            details['nativeEpoch'] is int &&
            (details['nativeEpoch'] as int) > 0 &&
            (details['nativeEpoch'] as int) < 9223372036854775807) {
          throw PlatformException(code: 'owned-start-held', details: {
            'captureStartId': details['captureStartId'],
            'nativeEpoch': details['nativeEpoch'],
          });
        }
      }
      // Native errors may contain private paths or recognition text.
      throw StateError('owned_native_transport_unconfirmed');
    }
  }

  Map<Object?, Object?> _closed(Map<Object?, Object?>? map, Set<String> fields) {
    if (map == null ||
        map.length != fields.length ||
        !fields.every(map.containsKey) ||
        map['version'] is! int ||
        map['version'] != 1) {
      throw StateError('owned_v1_reply_invalid');
    }
    return map;
  }

  OwnedMicHandle _handle(Object? raw) {
    if (raw is! List ||
        raw.length != 2 ||
        raw[0] is! List ||
        raw[1] is! int ||
        ((raw[1] as int) <= 0 || (raw[1] as int) >= 9223372036854775807)) throw StateError('owned_handle_invalid');
    final fields = raw[0] as List;
    if (fields.length != 11 ||
        [0, 1, 3, 5, 6, 7].any((i) => fields[i] is! String) ||
        [2, 4, 8, 9, 10].any((i) => fields[i] is! int)) throw StateError('owned_binding_reply_invalid');
    if ((fields[2] as int) < 0 ||
        (fields[2] as int) >= 9223372036854775807 ||
        (fields[4] as int) <= 0 ||
        (fields[4] as int) >= 9223372036854775807) throw StateError('owned_binding_reply_invalid');
    return OwnedMicHandle(binding: OwnedMicBinding.decode(List<Object?>.from(fields)), nativeEpoch: raw[1] as int);
  }

  @override
  Future<OwnedPreparedCapabilities> getCapabilities() async {
    final map = _closed(await _invoke('getCapabilities', {'version': 1}),
        {'version', 'preparedLifecycle', 'privateStore', 'retirement', 'neverStartedCancel'});
    if (['preparedLifecycle', 'privateStore', 'retirement', 'neverStartedCancel'].any((k) => map[k] is! bool))
      throw StateError('owned_capabilities_invalid');
    return OwnedPreparedCapabilities(
        preparedLifecycle: map['preparedLifecycle'] as bool,
        privateStore: map['privateStore'] as bool,
        retirement: map['retirement'] as bool,
        neverStartedCancel: map['neverStartedCancel'] as bool);
  }

  @override
  Future<OwnedMicHandle> prepareOwnedStream(OwnedMicBinding binding) async {
    binding = copyOwnedBinding(binding);
    final value = _closed(
        await _invoke('prepareOwnedStream', {'version': 1, 'binding': binding.encode()}), {'version', 'handle'});
    final handle = _handle(value['handle']);
    if (!sameOwnedBinding(handle.binding, binding)) throw StateError('owned_prepare_binding_mismatch');
    return handle;
  }

  @override
  Future<void> startPreparedOwnedStream(OwnedMicHandle handle, String storeToken) async {
    handle = copyOwnedHandle(handle);
    if (storeToken.trim().isEmpty) throw StateError('owned_store_token_invalid');
    final value = _closed(
        await _invoke('startPreparedOwnedStream', {
          'version': 1,
          'handle': [handle.binding.encode(), handle.nativeEpoch],
          'storeToken': storeToken,
        }),
        {'version', 'storeToken', 'handle'});
    if (value['storeToken'] != storeToken || !sameOwnedHandle(_handle(value['handle']), handle))
      throw StateError('owned_start_reply_invalid');
  }

  @override
  Future<OwnedMicStopReceipt> stopAndDrain(OwnedMicStopRequest request) =>
      _drain.stopAndDrain(copyOwnedRequest(request));
  @override
  Future<OwnedMicProbe> probeOwnedCapture(OwnedMicHandle handle) => _drain.probeOwnedCapture(copyOwnedHandle(handle));

  @override
  Future<OwnedPreparedCancelReceipt> cancelPreparedOwnedStream(OwnedMicHandle handle, String storeToken) async {
    handle = copyOwnedHandle(handle);
    if (storeToken.trim().isEmpty) throw StateError('owned_store_token_invalid');
    final value = _closed(
        await _invoke('cancelPreparedOwnedStream', {
          'version': 1,
          'handle': [handle.binding.encode(), handle.nativeEpoch],
          'storeToken': storeToken,
        }),
        {'version', 'storeToken', 'handle', 'cancelledBeforeStart'});
    final captured = _handle(value['handle']);
    if (value['storeToken'] != storeToken ||
        !sameOwnedHandle(captured, handle) ||
        value['cancelledBeforeStart'] is! bool ||
        value['cancelledBeforeStart'] != true) {
      throw StateError('owned_prepared_cancel_invalid');
    }
    return OwnedPreparedCancelReceipt(handle: captured, storeToken: storeToken, cancelledBeforeStart: true);
  }
}
