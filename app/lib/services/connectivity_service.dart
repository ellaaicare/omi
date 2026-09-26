import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

/// A `/v1/health` status below 500 means this backend answered. Reachability
/// is that response alone. Build 865 also required a HEAD to 1.1.1.1, so a
/// 200 from `/v1/health` left the app offline whenever the second host failed,
/// and every feature then refused to call the API.
bool healthStatusIsReachable(int statusCode) => statusCode > 0 && statusCode < 500;

class ConnectivityService {
  static final ConnectivityService _instance = ConnectivityService._internal();
  factory ConnectivityService() => _instance;

  ConnectivityService._internal();

  static const Duration _probeInterval = Duration(seconds: 10);
  static const Duration _probeTimeout = Duration(seconds: 3);
  static final Uri _healthUri = Uri.parse('https://api.ella-ai-care.com/v1/health');

  final Connectivity _connectivity = Connectivity();
  StreamSubscription? _connectivitySubscription;
  Timer? _backendProbeTimer;

  final _connectionChangeController = StreamController<bool>.broadcast();
  Stream<bool> get onConnectionChange => _connectionChangeController.stream;
  final _probeController = StreamController<bool?>.broadcast();

  /// Passive probe updates. Listeners may show a banner. They must not
  /// refuse API calls based on this stream.
  Stream<bool?> get onBackendProbe => _probeController.stream;

  bool _isConnected = true;
  bool get isConnected => _isConnected;

  bool? _backendReachable;
  bool? get backendReachable => _backendReachable;
  DateTime? _lastBackendProbeAt;
  DateTime? get lastBackendProbeAt => _lastBackendProbeAt;
  int? _lastBackendProbeStatus;
  int? get lastBackendProbeStatus => _lastBackendProbeStatus;
  String _lastBackendProbeError = '';
  String get lastBackendProbeError => _lastBackendProbeError;

  bool _isInitialized = false;

  Future<void> init() async {
    if (_isInitialized) return;

    _updateConnectionState(_hasNetworkInterface(await _connectivity.checkConnectivity()));
    _connectivitySubscription = _connectivity.onConnectivityChanged.listen(_handleConnectivityChange);
    _isInitialized = true;
    if (_isConnected) unawaited(_probeBackend());
    _backendProbeTimer = Timer.periodic(_probeInterval, (_) {
      if (_isConnected) unawaited(_probeBackend());
    });
  }

  void dispose() {
    _connectivitySubscription?.cancel();
    _backendProbeTimer?.cancel();
    _connectionChangeController.close();
    _probeController.close();
  }

  /// Records a health probe without changing [isConnected]. A 200 therefore
  /// cannot be held offline by any other host, and a failed probe cannot
  /// block chat, consent, or any other request.
  @visibleForTesting
  void applyHealthProbeForTest({int? statusCode, Object? error}) {
    final interfaceWasUp = _isConnected;
    if (error != null || statusCode == null) {
      _recordProbe(
        reachable: false,
        statusCode: null,
        error: error is TimeoutException ? 'timeout' : error.runtimeType.toString(),
      );
    } else {
      final reachable = healthStatusIsReachable(statusCode);
      _recordProbe(
        reachable: reachable,
        statusCode: statusCode,
        error: reachable ? '' : 'http_$statusCode',
      );
    }
    _isConnected = interfaceWasUp;
  }

  static bool _hasNetworkInterface(List<ConnectivityResult> results) =>
      results.isNotEmpty && !results.contains(ConnectivityResult.none);

  void _handleConnectivityChange(List<ConnectivityResult> results) {
    final available = _hasNetworkInterface(results);
    _updateConnectionState(available);
    if (available) {
      unawaited(_probeBackend());
    } else {
      _recordProbe(reachable: false, statusCode: null, error: 'no_network_interface');
    }
  }

  Future<void> _probeBackend() async {
    try {
      final response = await http.head(_healthUri).timeout(_probeTimeout);
      final reachable = healthStatusIsReachable(response.statusCode);
      _recordProbe(
        reachable: reachable,
        statusCode: response.statusCode,
        error: reachable ? '' : 'http_${response.statusCode}',
      );
    } catch (error) {
      _recordProbe(
        reachable: false,
        statusCode: null,
        error: error is TimeoutException ? 'timeout' : error.runtimeType.toString(),
      );
    }
  }

  void _recordProbe({required bool reachable, required int? statusCode, required String error}) {
    _backendReachable = reachable;
    _lastBackendProbeStatus = statusCode;
    _lastBackendProbeError = error;
    _lastBackendProbeAt = DateTime.now();
    if (!_probeController.isClosed) _probeController.add(reachable);
  }

  void _updateConnectionState(bool newIsConnected) {
    if (_isConnected == newIsConnected) return;
    _isConnected = newIsConnected;
    _connectionChangeController.add(_isConnected);
  }
}
