import 'dart:async';

/// The backend owner's opt-in Grok protocol 1. This is not an audio quality check.
class GrokProviderReadyResult {
  const GrokProviderReadyResult.ready()
      : errorCode = '',
        httpStatus = null;
  const GrokProviderReadyResult.failed(this.errorCode, {this.httpStatus});
  final String errorCode;
  final int? httpStatus;
  bool get ready => errorCode.isEmpty;
}

class GrokProviderReadiness {
  GrokProviderReadiness(
      {required this.sessionId, required this.isCurrent, Duration timeout = const Duration(seconds: 16)}) {
    _deadline = Timer(timeout, () => fail('provider_ready_timeout'));
    _authorityCheck = Timer.periodic(const Duration(milliseconds: 100), (_) {
      if (!isCurrent()) fail('connect_cancelled');
    });
  }
  final String sessionId;
  final bool Function() isCurrent;
  final _completion = Completer<GrokProviderReadyResult>();
  Timer? _deadline;
  Timer? _authorityCheck;
  Future<GrokProviderReadyResult> get result => _completion.future;
  bool get settled => _completion.isCompleted;
  GrokProviderReadyResult? _outcome;
  GrokProviderReadyResult? get outcome => _outcome;
  static const terminalCodes = {
    'provider_ready_timeout',
    'provider_startup_invalid_event',
    'provider_startup_input_not_allowed',
    'provider_session_configuration_rejected',
    'provider_ready_protocol_mismatch',
    'provider_ready_protocol_unsupported',
    'provider_startup_client_closed',
    'provider_handshake_rejected',
    'provider_startup_failed',
  };
  bool matches(Map<String, dynamic> event) =>
      event['ready_protocol'] is int &&
      event['ready_protocol'] == 1 &&
      event['provider'] == 'grok-voice' &&
      event['session_id'] == sessionId;
  void handle(Map<String, dynamic> event) {
    if (_outcome != null && !_outcome!.ready) return;
    if (!isCurrent()) {
      fail('connect_cancelled');
      return;
    }
    if (event['type'] == 'provider_ready') {
      if (settled) return;
      if (!matches(event) || event['configured_model'] is! String || (event['configured_model'] as String).isEmpty) {
        fail('provider_ready_protocol_mismatch');
      } else {
        _finish(const GrokProviderReadyResult.ready());
      }
    } else if (event['type'] == 'error' || event['type'] == 'session_end') {
      final code = event['code'];
      if (event['terminal'] == true && matches(event) && terminalCodes.contains(code)) {
        final status = event['http_status'];
        _finish(GrokProviderReadyResult.failed(code as String,
            httpStatus: status is int && status >= 100 && status <= 599 ? status : null));
      } else {
        fail('provider_startup_failed');
      }
    }
  }

  void fail(String code, {int? httpStatus}) {
    if (_outcome == null || _outcome!.ready) _finish(GrokProviderReadyResult.failed(code, httpStatus: httpStatus));
  }

  void _finish(GrokProviderReadyResult value) {
    _deadline?.cancel();
    _authorityCheck?.cancel();
    if (_outcome != null && !_outcome!.ready) return;
    _outcome = value;
    if (!settled) _completion.complete(value);
  }
}
