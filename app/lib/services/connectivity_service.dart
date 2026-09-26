import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:http/http.dart' as http;

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
  }

  static bool _hasNetworkInterface(List<ConnectivityResult> results) =>
      results.isNotEmpty && !results.contains(ConnectivityResult.none);

  void _handleConnectivityChange(List<ConnectivityResult> results) {
    final available = _hasNetworkInterface(results);
    _updateConnectionState(available);
    if (available) {
      unawaited(_probeBackend());
    } else {
      _backendReachable = false;
      _lastBackendProbeAt = DateTime.now();
      _lastBackendProbeStatus = null;
      _lastBackendProbeError = 'no_network_interface';
    }
  }

  Future<void> _probeBackend() async {
    try {
      final response = await http.head(_healthUri).timeout(_probeTimeout);
      _lastBackendProbeStatus = response.statusCode;
      _backendReachable = response.statusCode < 500;
      _lastBackendProbeError = _backendReachable == true ? '' : 'http_${response.statusCode}';
    } catch (error) {
      _lastBackendProbeStatus = null;
      _backendReachable = false;
      _lastBackendProbeError = error is TimeoutException ? 'timeout' : error.runtimeType.toString();
    } finally {
      _lastBackendProbeAt = DateTime.now();
    }
  }

  void _updateConnectionState(bool newIsConnected) {
    if (_isConnected == newIsConnected) return;
    _isConnected = newIsConnected;
    _connectionChangeController.add(_isConnected);
  }
}
