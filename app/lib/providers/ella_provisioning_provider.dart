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
typedef EllaProvisioningReceiptSaver = Future<void> Function(String uid, Map<String, dynamic> receipt);

class EllaProvisioningProvider extends ChangeNotifier {
  EllaProvisioningProvider({
    EllaProvisioningTransport? transport,
    SharedPreferencesUtil? preferences,
    EllaProvisioningScheduler? scheduler,
    EllaProvisioningReceiptSaver? receiptSaver,
    this.maxPollAttempts = 30,
  })  : _transport = transport ?? const EllaProvisioningHttpTransport(),
        _preferences = preferences ?? SharedPreferencesUtil(),
        _scheduler = scheduler ?? ((delay, callback) => _TimerPollHandle(delay, callback)),
        _receiptSaver = receiptSaver;

  final EllaProvisioningTransport _transport;
  final SharedPreferencesUtil _preferences;
  final EllaProvisioningScheduler _scheduler;
  final EllaProvisioningReceiptSaver? _receiptSaver;
  final int maxPollAttempts;

  EllaProvisioningState state = EllaProvisioningState.idle;
  EllaProvisioningReceipt? receipt;
  String errorCode = '';
  int? _responseStatusCode;

  /// Presentation-only HTTP classification; never overrides receipt authority.
  /// Keep the exact server error in errorCode for support/contract semantics.
  String get failurePresentationCode {
    if (errorCode == 'unsafe_response_contract') return errorCode;
    if (_responseStatusCode == 401) return 'auth_required';
    if (_responseStatusCode == 429) return 'quota_exceeded';
    if (_responseStatusCode != null && _responseStatusCode! >= 500) return 'provider_unavailable';
    return errorCode;
  }

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
  bool _revalidatingOperationalReceipt = false;
  Object? _authorityCoordinatorOwner;

  bool get isOperational => state == EllaProvisioningState.ready && receipt?.isOperational == true;

  /// No automatic status attempts remain, and the final request has settled.
  bool get automaticPollingExhausted => _pollAttempts >= maxPollAttempts && !_requestInFlight;

  bool get isRevalidatingOperational =>
      _revalidatingOperationalReceipt &&
      (state == EllaProvisioningState.checking || state == EllaProvisioningState.queued ||
          state == EllaProvisioningState.provisioning || state == EllaProvisioningState.degraded);

  String get supportCode => receipt?.supportCode ?? '';

  Future<void> start({
    required String uid,
    required EllaProvisioningRequestContext requestContext,
    bool forceRevalidate = false,
  }) async {
    if (uid.isEmpty) {
      _setFailure('auth_required', blocked: true);
      return;
    }
    if (_activeUid == uid) {
      _bindAuthorityCoordinator(uid);
      final consentReceiptId = requestContext.consentReceiptId;
      if (consentReceiptId.isNotEmpty && consentReceiptId != _requestContext?.consentReceiptId) {
        setConsentReceiptId(consentReceiptId);
      }
      if (forceRevalidate) {
        _requestContext = requestContext;
        _requestContextEpoch++;
        await retry();
        return;
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
    _bindAuthorityCoordinator(uid);
    _requestContext = requestContext;
    _requestContextEpoch++;
    _pollAttempts = 0;
    _requestInFlight = false;
    _retryEnsureAfterCurrentRequest = false;
    _revalidatingOperationalReceipt = false;
    state = EllaProvisioningState.checking;
    errorCode = '';
    _responseStatusCode = null;

    // A cached receipt may keep the established shell visible, but it must not
    // authorize protected work until this exact request verifies it again.
    _preferences.invalidateEllaProvisioningServerVerification();

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

  Future<void> retry({bool preserveOperationalReceipt = true}) async {
    if (_activeUid.isEmpty || _requestContext == null) return;
    _preferences.invalidateEllaProvisioningServerVerification();
    if (!preserveOperationalReceipt && _revalidatingOperationalReceipt) {
      _revalidatingOperationalReceipt = false;
      state = EllaProvisioningState.checking;
      errorCode = '';
      _responseStatusCode = null;
      notifyListeners();
    }
    if (_requestInFlight) {
      _retryEnsureAfterCurrentRequest = true;
      return;
    }
    final hadOperationalReceipt = isOperational || _revalidatingOperationalReceipt;
    final generation = ++_generation;
    _cancelPoll();
    _pollAttempts = 0;
    _revalidatingOperationalReceipt = preserveOperationalReceipt && hadOperationalReceipt;
    state = EllaProvisioningState.checking;
    errorCode = '';
    _responseStatusCode = null;
    notifyListeners();
    await _ensure(generation);
  }

  void setConsentReceiptId(String receiptId) {
    unawaited(_revalidateConsentReceipt(_activeUid, receiptId).then<void>((_) {}));
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
    _unbindAuthorityCoordinator();
    _endProvisioningOwnership();
    _activeUid = '';
    _requestContext = null;
    _pollAttempts = 0;
    _requestInFlight = false;
    _retryEnsureAfterCurrentRequest = false;
    _revalidatingOperationalReceipt = false;
    _preferences.invalidateEllaProvisioningServerVerification();
    receipt = null;
    errorCode = '';
    _responseStatusCode = null;
    state = EllaProvisioningState.idle;
    notifyListeners();
  }

  Future<void> _ensure(int generation) async {
    final context = _requestContext;
    if (context == null || generation != _generation || _requestInFlight) return;
    final requestContextEpoch = _requestContextEpoch;
    _requestInFlight = true;
    _responseStatusCode = null;
    try {
      final response = await _transport.ensure(context);
      if (!_isCurrentRequest(generation, requestContextEpoch)) return;
      await _applyResponse(response, generation, requestContextEpoch);
    } catch (error) {
      if (!_isCurrentRequest(generation, requestContextEpoch)) return;
      Logger.debug('[ProvisioningGate] Ensure failed: $error');
      _setFailure('network_unavailable', preserveOperationalReceipt: true);
      _schedulePoll(generation, _backoffDelay);
    } finally {
      _finishRequest(generation);
    }
  }

  Future<void> _poll(int generation) async {
    if (generation != _generation || _requestInFlight || !isForeground) return;
    if (_pollAttempts >= maxPollAttempts) {
      _setFailure('provisioning_timeout', preserveOperationalReceipt: true);
      return;
    }

    _pollAttempts++;
    final requestContextEpoch = _requestContextEpoch;
    _requestInFlight = true;
    _responseStatusCode = null;
    try {
      final response = await _transport.status();
      if (!_isCurrentRequest(generation, requestContextEpoch)) return;
      await _applyResponse(response, generation, requestContextEpoch);
    } catch (error) {
      if (!_isCurrentRequest(generation, requestContextEpoch)) return;
      Logger.debug('[ProvisioningGate] Status failed: $error');
      _setFailure(
        _pollAttempts >= maxPollAttempts ? 'provisioning_timeout' : 'network_unavailable',
        preserveOperationalReceipt: true,
      );
      _schedulePoll(generation, _backoffDelay);
    } finally {
      _finishRequest(generation);
    }
  }

  Future<void> _applyResponse(EllaProvisioningResponse response, int generation, int requestContextEpoch) async {
    if (!_isCurrentRequest(generation, requestContextEpoch)) return;
    _responseStatusCode = response.statusCode;
    final nextReceipt = response.receipt;
    final rejectedTerminal = !response.isAccepted &&
        (response.statusCode == 401 ||
            response.statusCode == 403 ||
            response.statusCode == 409 ||
            response.statusCode == 426 ||
            (nextReceipt?.state == EllaProvisioningState.blocked && nextReceipt?.retryable != true));
    final acceptedTerminal = response.isAccepted &&
        nextReceipt != null &&
        ((nextReceipt.state == EllaProvisioningState.ready && !nextReceipt.isOperational) ||
            (nextReceipt.state == EllaProvisioningState.blocked && nextReceipt.retryable != true));
    final terminalResponse = rejectedTerminal || acceptedTerminal;
    if (terminalResponse) {
      _preferences.invalidateEllaProvisioningTerminalAuthority();
    } else {
      _preferences.invalidateEllaProvisioningServerVerification();
    }
    if (!response.isAccepted || nextReceipt == null) {
      if (nextReceipt != null) {
        await _saveReceipt(nextReceipt);
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
              _ => response.clientFailure?.code ?? 'invalid_provisioning_response',
            };
      final blocked = response.statusCode == 401 ||
          response.statusCode == 403 ||
          response.statusCode == 409 ||
          response.statusCode == 426 ||
          (nextReceipt?.state == EllaProvisioningState.blocked && nextReceipt?.retryable != true);
      _setFailure(
        code,
        blocked: blocked,
        preserveOperationalReceipt: !blocked,
        terminalAlreadyFenced: terminalResponse,
      );
      if (_shouldPoll) _schedulePoll(generation, _backoffDelay);
      return;
    }

    await _saveReceipt(nextReceipt);
    if (!_isCurrentRequest(generation, requestContextEpoch)) return;
    receipt = nextReceipt;
    errorCode = nextReceipt.errorCode;

    if (nextReceipt.state == EllaProvisioningState.ready && !nextReceipt.isOperational) {
      state = EllaProvisioningState.blocked;
      errorCode = 'incomplete_ready_receipt';
    } else if (nextReceipt.state == EllaProvisioningState.blocked && nextReceipt.retryable != true) {
      state = EllaProvisioningState.blocked;
    } else if (_pollAttempts >= maxPollAttempts &&
        (nextReceipt.state == EllaProvisioningState.queued ||
            nextReceipt.state == EllaProvisioningState.provisioning ||
            nextReceipt.state == EllaProvisioningState.degraded)) {
      state = EllaProvisioningState.degraded;
      errorCode = 'provisioning_timeout';
    } else {
      state = nextReceipt.state;
    }
    _revalidatingOperationalReceipt = _revalidatingOperationalReceipt &&
        !terminalResponse && state != EllaProvisioningState.blocked && !isOperational;
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

  Future<void> _saveReceipt(EllaProvisioningReceipt nextReceipt) async {
    final saver = _receiptSaver ?? _preferences.saveEllaProvisioningReceipt;
    await saver(_activeUid, nextReceipt.toCacheJson());
  }

  void _setFailure(
    String code, {
    bool blocked = false,
    bool preserveOperationalReceipt = false,
    bool terminalAlreadyFenced = false,
  }) {
    // A transient foreground refresh must not replace an already-working Home
    // with setup UI. The provider still reports isOperational=false, so
    // protected operations remain fail-closed until fresh authority succeeds.
    _revalidatingOperationalReceipt =
        !blocked && preserveOperationalReceipt && _revalidatingOperationalReceipt;
    if (blocked && !terminalAlreadyFenced) {
      _preferences.invalidateEllaProvisioningTerminalAuthority();
    } else if (!blocked) {
      _preferences.invalidateEllaProvisioningServerVerification();
    }
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
                  errorCode == 'authentication_refresh_unavailable' ||
                  errorCode == 'request_timeout' ||
                  errorCode == 'transport_unavailable' ||
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
    final requested = nextReceipt.retryAfter.inMilliseconds + jitter;
    final backoff = state == EllaProvisioningState.degraded ? _backoffDelay.inMilliseconds : 0;
    return Duration(milliseconds: (requested > backoff ? requested : backoff).clamp(500, 15000));
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

  void _endProvisioningOwnership() {
    if (_activeUid.isEmpty) return;
    _preferences.invalidateEllaProvisioningTerminalAuthority();
  }

  void _bindAuthorityCoordinator(String uid) {
    _unbindAuthorityCoordinator();
    _authorityCoordinatorOwner = EllaProvisioningAuthorityCoordinator.register(
      uid: uid,
      revalidator: _revalidateConsentReceipt,
    );
  }

  void _unbindAuthorityCoordinator() {
    final owner = _authorityCoordinatorOwner;
    if (owner == null) return;
    EllaProvisioningAuthorityCoordinator.unregister(owner);
    _authorityCoordinatorOwner = null;
  }

  Future<bool> _revalidateConsentReceipt(String uid, String receiptId) async {
    final context = _requestContext;
    if (uid.isEmpty || uid != _activeUid || uid != _preferences.uid || receiptId.isEmpty || context == null) {
      return false;
    }
    final bindingRevision = receipt?.bindingRevision ?? 0;
    if (context.consentReceiptId == receiptId &&
        bindingRevision > 0 &&
        _preferences.hasCurrentEllaProvisioningAuthority(uid: uid, bindingRevision: bindingRevision)) {
      return true;
    }
    if (context.consentReceiptId == receiptId && (_requestInFlight || _pollHandle != null || _shouldPoll)) {
      return true;
    }
    _requestContext = context.copyWithConsentReceiptId(receiptId);
    _requestContextEpoch++;
    // Keep an already validated same-owner Home shell during receipt refresh.
    // retry still invalidates server authority; blocked/auth/account-change paths
    // continue to remove access and clear the retained presentation.
    await retry();
    return uid == _activeUid && uid == _preferences.uid;
  }

  bool _isCurrentRequest(int generation, int requestContextEpoch) =>
      generation == _generation && requestContextEpoch == _requestContextEpoch;

  void _finishRequest(int generation) {
    if (generation != _generation) return;
    _requestInFlight = false;
    if (_retryEnsureAfterCurrentRequest) {
      _retryEnsureAfterCurrentRequest = false;
      unawaited(retry());
    } else if (automaticPollingExhausted) {
      // Publish the settled budget for exceptions and returned HTTP failures.
      notifyListeners();
    }
  }

  @override
  void dispose() {
    _generation++;
    _requestContextEpoch++;
    _cancelPoll();
    _unbindAuthorityCoordinator();
    _endProvisioningOwnership();
    _activeUid = '';
    _preferences.invalidateEllaProvisioningServerVerification();
    super.dispose();
  }
}
