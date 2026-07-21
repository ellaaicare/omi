import 'dart:async';

import 'package:flutter/foundation.dart';

import 'package:omi/backend/preferences.dart';
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
  bool _requestInFlight = false;

  bool get isOperational => state == EllaProvisioningState.ready && receipt?.isOperational == true;

  String get supportCode => receipt?.supportCode ?? '';

  Future<void> start({required String uid, required EllaProvisioningRequestContext requestContext}) async {
    if (uid.isEmpty) {
      _setFailure('auth_required', blocked: true);
      return;
    }
    if (_activeUid == uid && isOperational) return;

    final generation = ++_generation;
    _cancelPoll();
    _activeUid = uid;
    _requestContext = requestContext;
    _pollAttempts = 0;
    _requestInFlight = false;
    state = EllaProvisioningState.checking;
    errorCode = '';

    await _preferences.prepareEllaProvisioningAccount(uid);
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
    final generation = ++_generation;
    _cancelPoll();
    _pollAttempts = 0;
    state = EllaProvisioningState.checking;
    errorCode = '';
    notifyListeners();
    await _ensure(generation);
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
    _cancelPoll();
    _activeUid = '';
    _requestContext = null;
    _pollAttempts = 0;
    _requestInFlight = false;
    receipt = null;
    errorCode = '';
    state = EllaProvisioningState.idle;
    notifyListeners();
  }

  Future<void> _ensure(int generation) async {
    final context = _requestContext;
    if (context == null || generation != _generation || _requestInFlight) return;
    _requestInFlight = true;
    try {
      final response = await _transport.ensure(context);
      if (generation != _generation) return;
      await _applyResponse(response, generation);
    } catch (error) {
      if (generation != _generation) return;
      Logger.debug('[ProvisioningGate] Ensure failed: $error');
      _setFailure('network_unavailable');
      _schedulePoll(generation, _backoffDelay);
    } finally {
      if (generation == _generation) _requestInFlight = false;
    }
  }

  Future<void> _poll(int generation) async {
    if (generation != _generation || _requestInFlight || !isForeground) return;
    if (_pollAttempts >= maxPollAttempts) {
      _setFailure('provisioning_timeout');
      return;
    }

    _pollAttempts++;
    _requestInFlight = true;
    try {
      final response = await _transport.status();
      if (generation != _generation) return;
      await _applyResponse(response, generation);
    } catch (error) {
      if (generation != _generation) return;
      Logger.debug('[ProvisioningGate] Status failed: $error');
      _setFailure('network_unavailable');
      _schedulePoll(generation, _backoffDelay);
    } finally {
      if (generation == _generation) _requestInFlight = false;
    }
  }

  Future<void> _applyResponse(EllaProvisioningResponse response, int generation) async {
    final nextReceipt = response.receipt;
    if (!response.isAccepted || nextReceipt == null) {
      if (nextReceipt != null) {
        receipt = nextReceipt;
        await _preferences.saveEllaProvisioningReceipt(_activeUid, nextReceipt.toCacheJson());
        if (generation != _generation) return;
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
          (nextReceipt?.state == EllaProvisioningState.blocked && nextReceipt?.retryable != true);
      _setFailure(code, blocked: blocked);
      if (_shouldPoll) _schedulePoll(generation, _backoffDelay);
      return;
    }

    receipt = nextReceipt;
    errorCode = nextReceipt.errorCode;
    await _preferences.saveEllaProvisioningReceipt(_activeUid, nextReceipt.toCacheJson());
    if (generation != _generation) return;

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
    notifyListeners();

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

  @override
  void dispose() {
    _generation++;
    _cancelPoll();
    super.dispose();
  }
}
