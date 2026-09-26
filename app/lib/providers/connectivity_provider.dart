import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'package:omi/services/connectivity_service.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/widgets/dialog.dart';

class ConnectivityProvider extends ChangeNotifier {
  bool _isConnected = true;
  bool _previousConnection = true;
  bool _isInitialized = false;

  final ConnectivityService _connectivityService = ConnectivityService();
  StreamSubscription? _connectionSubscription;
  StreamSubscription<bool?>? _probeSubscription;

  bool get isConnected => _isConnected;
  bool get previousConnection => _previousConnection;
  bool get isInitialized => _isInitialized;
  bool? get backendReachable => _connectivityService.backendReachable;
  String get lastBackendProbeError => _connectivityService.lastBackendProbeError;

  ConnectivityProvider() {
    init();
  }

  void init() {
    _isConnected = _connectivityService.isConnected;
    _previousConnection = _isConnected;
    _isInitialized = true;

    _connectionSubscription = _connectivityService.onConnectionChange.listen(_updateConnectionState);
    _probeSubscription = _connectivityService.onBackendProbe.listen((_) => notifyListeners());
  }

  @override
  void dispose() {
    _connectionSubscription?.cancel();
    _probeSubscription?.cancel();
    super.dispose();
  }

  void _updateConnectionState(bool newIsConnected) {
    if (_isConnected != newIsConnected) {
      _previousConnection = _isConnected;
      _isConnected = newIsConnected;
      notifyListeners();
    }
  }

  static void showNoInternetDialog(BuildContext context) {
    showDialog(
      context: context,
      builder: (c) => getDialog(
        context,
        () => Navigator.pop(context),
        () => Navigator.pop(context),
        'No Internet Connection',
        'You need an internet connection to execute this action. Please check your connection and try again.',
        singleButton: true,
        okButtonText: 'Ok',
      ),
    );
  }
}

/// Shows the health-probe result without blocking taps or API calls.
class PassiveBackendProbeBanner extends StatelessWidget {
  const PassiveBackendProbeBanner({super.key});

  @override
  Widget build(BuildContext context) {
    final connectivity = context.watch<ConnectivityProvider>();
    if (connectivity.backendReachable != false) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.fromLTRB(18, 8, 18, 0),
      child: Text(
        context.l10n.ellaServerUnreachableBanner,
        key: const Key('passive-backend-probe-banner'),
      ),
    );
  }
}
