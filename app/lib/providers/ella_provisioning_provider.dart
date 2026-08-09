import 'dart:async';

import 'package:flutter/foundation.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_account_isolation_service.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/utils/logger.dart';

abstract class EllaProvisioningPollHandle {
  void cancel();
}

class _TimerPollHandle implements EllaProvisioningPollHandle {
  _TimerPollHandle(Duration delay, VoidCallback callback) : _timer = Timer(delay, callback);

  final Timer _timer;

  @override
  void cancel() => _timer.cancel();
}

typedef EllaProvisioningScheduler = EllaProvisioningPollHandle Function(Duration delay, VoidCallback callback);

class EllaProvisioningProvider extends ChangeNotifier {
  EllaProvisioningProvider({
    EllaProvisioningTransport? transport,
    SharedPreferencesUtil? preferences,
    EllaProvisioningScheduler? scheduler,
    this.maxPollAttempts = 30,
  })  : _transport = transport ?? const EllaProvisioningHttpTransport(),
        _preferences = preferences ?? SharedPreferencesUtil(),
        _scheduler = scheduler ?? ((delay, callback) => _TimerPollHandle(delay, callback));

  final EllaProvisioningTransport _transport;
  final SharedPreferencesUtil _preferences;
  final EllaProvisioningScheduler _scheduler;
  final int maxPollAttempts;

  EllaProvisioningState state = EllaProvisioningState.idle;
  EllaProvisioningReceipt? receipt;
  String errorCode = '';
  bool isForeground = true;

  String _activeUid = '';
  EllaProvisioningRequestContext? _requestContext;
  EllaProvisioningPollHandle? _pollHandle;
  int _pollAttempts = 0;
  int _generation = 0;

  // Changes independently of [_generation] so an obsolete request is denied
  // while its finally block can still release the shared in-flight slot.
  int _requestContextEpoch = 0;
  bool _requestInFlight = false;
  bool _retryEnsureAfterCurrentRequest = false;

  bool get isOperational => state == EllaProvisioningState.ready && receipt?.isOperational == true;

  String get supportCode => receipt?.supportCode ?? '';

  Future<void> start({required String uid, required EllaProvisioningRequestContext requestContext}) async {
    if (uid.isEmpty) {
      _setFailure('auth_required', blocked: true);
      return;
    }
    if (_activeUid == uid) {
      final consentReceiptId = requestContext.consentReceiptId;
      if (consentReceiptId.isNotEmpty && consentReceiptId != _requestContext?.consentReceiptId) {
        setConsentReceiptId(consentReceiptId);
      }
      if (isOperational ||
          _requestInFlight ||
          _pollHandle != null ||
          _shouldPoll ||
          state == EllaProvisioningState.blocked) {
        return;
      }
      await retry();
      return;
    }

    final generation = ++_generation;
    _cancelPoll();
    _activeUid = uid;
    _requestContext = requestContext;
    _requestContextEpoch++;
    _pollAttempts = 0;
    _requestInFlight = false;
    _retryEnsureAfterCurrentRequest = false;
    state = EllaProvisioningState.checking;
    errorCode = '';

    await const EllaAccountIsolationService().prepareProvisioningAccount(uid, preferences: _preferences);
    if (generation != _generation) return;

    final cached = _preferences.getEllaProvisioningReceipt(uid);
    if (cached != null) {
      receipt = EllaProvisioningReceipt.fromJson(cached);
    } else {
      receipt = null;
    }
    notifyListeners();
    await _ensure(generation);
  }

  Future<void> retry() async {
    if (_activeUid.isEmpty || _requestContext == null) return;
    if (_requestInFlight) {
      _retryEnsureAfterCurrentRequest = true;
      return;
    }
    final generation = ++_generation;
    _cancelPoll();
    _pollAttempts = 0;
    state = EllaProvisioningState.checking;
    errorCode = '';
    notifyListeners();
    await _ensure(generation);
  }

  void setConsentReceiptId(String receiptId) {
    final context = _requestContext;
    if (receiptId.isEmpty || context == null || context.consentReceiptId == receiptId) return;
    _requestContext = context.copyWithConsentReceiptId(receiptId);
    _requestContextEpoch++;
    unawaited(retry());
  }

  void setForeground(bool value) {
    if (isForeground == value) return;
    isForeground = value;
    if (!value) {
      _cancelPoll();
      return;
    }
    if (_shouldPoll && !_requestInFlight) {
      final generation = ++_generation;
      _schedulePoll(generation, const Duration(milliseconds: 250));
    }
  }

  void reset() {
    _generation++;
    _requestContextEpoch++;
    _cancelPoll();
    _activeUid = '';
    _requestContext = null;
    _pollAttempts = 0;
    _requestInFlight = false;
    _retryEnsureAfterCurrentRequest = false;
    receipt = null;
    errorCode = '';
    state = EllaProvisioningState.idle;
    notifyListeners();
  }

  Future<void> _ensure(int generation) async {
    final context = _requestContext;
    if (context == null || generation != _generation || _requestInFlight) return;
    final requestContextEpoch = _requestContextEpoch;
    _requestInFlight = true;
    try {
      final response = await _transport.ensure(context);
      if (!_isCurrentRequest(generation, requestContextEpoch)) return;
      await _applyResponse(response, generation, requestContextEpoch);
    } catch (error) {
      if (!_isCurrentRequest(generation, requestContextEpoch)) return;
      Logger.debug('[ProvisioningGate] Ensure failed: $error');
      _setFailure('network_unavailable');
      _schedulePoll(generation, _backoffDelay);
    } finally {
      _finishRequest(generation);
    }
  }

  Future<void> _poll(int generation) async {
    if (generation != _generation || _requestInFlight || !isForeground) return;
    if (_pollAttempts >= maxPollAttempts) {
      _setFailure('provisioning_timeout');
      return;
    }

    _pollAttempts++;
    final requestContextEpoch = _requestContextEpoch;
    _requestInFlight = true;
    try {
      final response = await _transport.status();
      if (!_isCurrentRequest(generation, requestContextEpoch)) return;
      await _applyResponse(response, generation, requestContextEpoch);
    } catch (error) {
      if (!_isCurrentRequest(generation, requestContextEpoch)) return;
      Logger.debug('[ProvisioningGate] Status failed: $error');
      _setFailure('network_unavailable');
      _schedulePoll(generation, _backoffDelay);
    } finally {
      _finishRequest(generation);
    }
  }

  Future<void> _applyResponse(EllaProvisioningResponse response, int generation, int requestContextEpoch) async {
    if (!_isCurrentRequest(generation, requestContextEpoch)) return;
    final nextReceipt = response.receipt;
    if (!response.isAccepted || nextReceipt == null) {
      if (nextReceipt != null) {
        await _preferences.saveEllaProvisioningReceipt(_activeUid, nextReceipt.toCacheJson());
        if (!_isCurrentRequest(generation, requestContextEpoch)) return;
        receipt = nextReceipt;
      }
      final code = nextReceipt?.errorCode.isNotEmpty == true
          ? nextReceipt!.errorCode
          : switch (response.statusCode) {
              401 => 'auth_required',
              403 || 409 => 'identity_conflict',
              426 => 'upgrade_required',
              429 => 'quota_exceeded',
              >= 500 => 'provider_unavailable',
              _ => 'invalid_provisioning_response',
            };
      final blocked = response.statusCode == 401 ||
          response.statusCode == 403 ||
          response.statusCode == 409 ||
          response.statusCode == 426 ||
          (nextReceipt?.state == EllaProvisioningState.blocked && nextReceipt?.retryable != true);
      _setFailure(code, blocked: blocked);
      if (_shouldPoll) _schedulePoll(generation, _backoffDelay);
      return;
    }

    await _preferences.saveEllaProvisioningReceipt(_activeUid, nextReceipt.toCacheJson());
    if (!_isCurrentRequest(generation, requestContextEpoch)) return;
    receipt = nextReceipt;
    errorCode = nextReceipt.errorCode;

    if (nextReceipt.state == EllaProvisioningState.ready && !nextReceipt.isOperational) {
      state = EllaProvisioningState.blocked;
      errorCode = 'incomplete_ready_receipt';
    } else if (_pollAttempts >= maxPollAttempts &&
        (nextReceipt.state == EllaProvisioningState.queued ||
            nextReceipt.state == EllaProvisioningState.provisioning ||
            nextReceipt.state == EllaProvisioningState.degraded)) {
      state = EllaProvisioningState.degraded;
      errorCode = 'provisioning_timeout';
    } else {
      state = nextReceipt.state;
    }
    if (isOperational) {
      await _preferences.markEllaProvisioningVerified(_activeUid);
      if (!_isCurrentRequest(generation, requestContextEpoch)) return;
      await const EllaAccountIsolationService().resumeAfterVerifiedProvisioning();
      if (!_isCurrentRequest(generation, requestContextEpoch)) return;
    }

    // Publish ready only after its exact account/provisioning authority has
    // been reverified. Consumers must never recapture a ready receipt between
    // the state transition and the authority commit.
    notifyListeners();
    if (!_isCurrentRequest(generation, requestContextEpoch)) return;

    if (_shouldPoll) {
      _schedulePoll(generation, _pollDelay(nextReceipt));
    } else {
      _cancelPoll();
    }
  }

  void _setFailure(String code, {bool blocked = false}) {
    errorCode = code;
    state = blocked ? EllaProvisioningState.blocked : EllaProvisioningState.degraded;
    notifyListeners();
  }

  bool get _shouldPoll =>
      isForeground &&
      _pollAttempts < maxPollAttempts &&
      (state == EllaProvisioningState.checking ||
          state == EllaProvisioningState.queued ||
          state == EllaProvisioningState.provisioning ||
          (state == EllaProvisioningState.degraded &&
              (receipt?.retryable == true ||
                  errorCode == 'network_unavailable' ||
                  errorCode == 'invalid_provisioning_response' ||
                  errorCode == 'provider_unavailable' ||
                  errorCode == 'quota_exceeded')));

  Duration get _backoffDelay {
    final exponent = _pollAttempts.clamp(0, 4);
    final milliseconds = 1000 * (1 << exponent) + ((_pollAttempts % 4) * 173);
    return Duration(milliseconds: milliseconds.clamp(1000, 15000));
  }

  Duration _pollDelay(EllaProvisioningReceipt nextReceipt) {
    final jitter = (_pollAttempts % 4) * 173;
    return Duration(milliseconds: (nextReceipt.retryAfter.inMilliseconds + jitter).clamp(500, 15000));
  }

  void _schedulePoll(int generation, Duration delay) {
    if (!_shouldPoll || generation != _generation) return;
    _cancelPoll();
    _pollHandle = _scheduler(delay, () {
      _pollHandle = null;
      _poll(generation);
    });
  }

  void _cancelPoll() {
    _pollHandle?.cancel();
    _pollHandle = null;
  }

  bool _isCurrentRequest(int generation, int requestContextEpoch) =>
      generation == _generation && requestContextEpoch == _requestContextEpoch;

  void _finishRequest(int generation) {
    if (generation != _generation) return;
    _requestInFlight = false;
    if (_retryEnsureAfterCurrentRequest) {
      _retryEnsureAfterCurrentRequest = false;
      unawaited(retry());
    }
  }

  @override
  void dispose() {
    _generation++;
    _requestContextEpoch++;
    _cancelPoll();
    super.dispose();
  }
}
