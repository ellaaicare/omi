import 'dart:async';

import 'package:collection/collection.dart';

import 'package:omi/backend/schema/bt_device/bt_device.dart' as legacy_device;
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/services/devices.dart' as legacy_service;
import 'package:omi/services/devices/device_connection.dart' as legacy_connection;
import 'package:omi/services/devices/models.dart' as legacy_models;
import 'package:omi/services/devices/transports/device_transport.dart' as legacy_transport;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart' as upstream_device;
import 'package:omi/upstream_capture/services/devices.dart' as upstream_service;
import 'package:omi/upstream_capture/services/devices/connectors/device_connection.dart' as upstream_connection;
import 'package:omi/upstream_capture/services/devices/transports/device_transport.dart' as upstream_transport;
import 'package:omi/upstream_capture/services/services.dart' as upstream_services;
import 'package:omi/utils/logger.dart';

typedef UpstreamDeviceServiceLoader = Future<upstream_service.DeviceService> Function();
typedef UpstreamDeviceConnector = Future<bool> Function(String ownerId, upstream_device.BtDevice device);
typedef UpstreamDeviceDisconnect = Future<void> Function(String? deviceId);
typedef UpstreamConnectionOwner = String? Function();

/// Adapts legacy picker/settings call sites to the single upstream hardware
/// authority used by the flag-ON capture dock.
///
/// This class owns no scanner or reconnect policy. Discovery, GATT connection,
/// and device controls all delegate to upstream's [upstream_service.DeviceService].
/// The small operation generation only fences callbacks that complete after a
/// picker or account lifecycle cancellation.
class EllaUpstreamDeviceServiceAdapter
    implements
        legacy_service.IDeviceService,
        legacy_service.IAuthoritativeDeviceService,
        upstream_service.IDeviceServiceSubsciption {
  EllaUpstreamDeviceServiceAdapter({
    required UpstreamDeviceServiceLoader serviceLoader,
    required UpstreamDeviceConnector connect,
    required UpstreamDeviceDisconnect disconnect,
    UpstreamConnectionOwner? connectionOwner,
  })  : _serviceLoader = serviceLoader,
        _connect = connect,
        _disconnect = disconnect,
        _connectionOwner = connectionOwner ?? _noConnectionOwner;

  factory EllaUpstreamDeviceServiceAdapter.production(EllaUpstreamCaptureRuntime runtime) {
    return EllaUpstreamDeviceServiceAdapter(
      serviceLoader: () async {
        await runtime.ensureBooted();
        return upstream_services.ServiceManager.instance().device;
      },
      connect: (ownerId, device) async {
        final outcome = await runtime.connectNecklace(ownerId, device);
        if (outcome == EllaCaptureStartOutcome.consentRequired) {
          throw const legacy_service.DeviceConsentRequiredException();
        }
        return outcome == EllaCaptureStartOutcome.started;
      },
      disconnect: (deviceId) => runtime.disconnectNecklace(deviceId: deviceId),
      connectionOwner: () => runtime.boundOwnerId,
    );
  }

  static String? _noConnectionOwner() => null;

  final UpstreamDeviceServiceLoader _serviceLoader;
  final UpstreamDeviceConnector _connect;
  final UpstreamDeviceDisconnect _disconnect;
  final UpstreamConnectionOwner _connectionOwner;
  final Map<Object, legacy_service.IDeviceServiceSubsciption> _subscriptions = {};
  final Map<
      String,
      ({
        upstream_connection.DeviceConnection inner,
        int generation,
        String owner,
        _EllaLegacyDeviceConnection wrapper,
      })> _connectionAdapters = {};
  final Map<String, int> _connectionGenerations = {};
  final Map<String, String> _connectionOwners = {};

  upstream_service.DeviceService? _service;
  Future<upstream_service.DeviceService>? _loadingService;
  Future<void>? _activeDiscovery;
  List<legacy_device.BtDevice> _devices = const [];
  legacy_service.DeviceServiceStatus _status = legacy_service.DeviceServiceStatus.init;
  int _operationGeneration = 0;
  int _lifecycleGeneration = 0;
  int _connectionGeneration = 0;
  String? _activeDeviceId;
  bool _acceptingDiscoveryEvents = false;

  Future<upstream_service.DeviceService> _ensureService() {
    final loaded = _service;
    if (loaded != null) return Future.value(loaded);
    final loading = _loadingService;
    if (loading != null) return loading;

    final lifecycleGeneration = _lifecycleGeneration;
    late final Future<upstream_service.DeviceService> next;
    next = _serviceLoader().then((service) {
      if (lifecycleGeneration != _lifecycleGeneration || _status == legacy_service.DeviceServiceStatus.stop) {
        return service;
      }
      _service?.unsubscribe(this);
      _service = service;
      service.subscribe(this, this);
      service.start();
      for (final connection in service.connections) {
        if (connection.status == upstream_service.DeviceConnectionState.connected) {
          _projectConnectedConnection(connection.device.id);
        }
      }
      return service;
    }).whenComplete(() {
      if (identical(_loadingService, next)) _loadingService = null;
    });
    _loadingService = next;
    return next;
  }

  @override
  void start() {
    if (_status == legacy_service.DeviceServiceStatus.ready && (_service != null || _loadingService != null)) return;
    _lifecycleGeneration++;
    _status = legacy_service.DeviceServiceStatus.ready;
    _notifyStatus();
    unawaited(
      _ensureService().then<void>(
        (_) {},
        onError: (Object error, StackTrace _) {
          Logger.debug('[EllaUpstreamDeviceAdapter] startup failed: ${error.runtimeType}');
        },
      ),
    );
  }

  @override
  Future<void> stop() async {
    final stopGeneration = ++_lifecycleGeneration;
    _operationGeneration++;
    _acceptingDiscoveryEvents = false;
    _activeDiscovery = null;
    _status = legacy_service.DeviceServiceStatus.stop;
    _notifyStatus();

    upstream_service.DeviceService? service = _service;
    try {
      service ??= await _loadingService;
    } catch (error) {
      Logger.debug('[EllaUpstreamDeviceAdapter] pending startup failed during stop: ${error.runtimeType}');
    }
    if (stopGeneration != _lifecycleGeneration || _status != legacy_service.DeviceServiceStatus.stop) return;

    await _disconnectActiveDevice(_activeDeviceId);
    if (service != null) {
      service.unsubscribe(this);
      try {
        await service.stop();
      } catch (error) {
        Logger.debug('[EllaUpstreamDeviceAdapter] service stop failed: ${error.runtimeType}');
      }
    }
    _service = null;
    _loadingService = null;
    _connectionAdapters.clear();
    _connectionGenerations.clear();
    _connectionOwners.clear();
    _activeDeviceId = null;
    _devices = const [];
  }

  @override
  Future<void> discover({String? desirableDeviceId, int timeout = 5}) {
    final active = _activeDiscovery;
    if (active != null) return active;
    if (_status != legacy_service.DeviceServiceStatus.ready) return Future<void>.value();

    final operationGeneration = ++_operationGeneration;
    _acceptingDiscoveryEvents = true;
    _status = legacy_service.DeviceServiceStatus.scanning;
    _notifyStatus();

    late final Future<void> discovery;
    discovery = (() async {
      try {
        final service = await _ensureService();
        if (!_isCurrent(operationGeneration)) return;
        await service.discover(desirableDeviceId: desirableDeviceId, timeout: timeout);
        if (!_isCurrent(operationGeneration)) return;
        _publishDevices(service.devices);
      } catch (error) {
        Logger.debug('[EllaUpstreamDeviceAdapter] discovery failed: ${error.runtimeType}');
      } finally {
        if (_isCurrent(operationGeneration)) {
          _acceptingDiscoveryEvents = false;
          _status = legacy_service.DeviceServiceStatus.ready;
          _notifyStatus();
        }
        if (identical(_activeDiscovery, discovery)) _activeDiscovery = null;
      }
    })();
    _activeDiscovery = discovery;
    return discovery;
  }

  bool _isCurrent(int operationGeneration) =>
      operationGeneration == _operationGeneration && _status != legacy_service.DeviceServiceStatus.stop;

  @override
  Future<legacy_service.AuthoritativeDeviceConnection?> connectForCurrentUser(
    String ownerId,
    legacy_device.BtDevice device,
  ) async {
    final operationGeneration = ++_operationGeneration;
    _acceptingDiscoveryEvents = false;
    final service = await _ensureService();
    if (!_isCurrent(operationGeneration)) return null;

    final upstreamDevice =
        service.devices.where((candidate) => candidate.id == device.id).firstOrNull ?? _toUpstreamDevice(device);
    if (upstreamDevice.id.isEmpty) return null;

    var connected = false;
    try {
      connected = await _connect(ownerId, upstreamDevice);
    } on legacy_service.DeviceConsentRequiredException {
      if (_isCurrent(operationGeneration)) rethrow;
      return null;
    } catch (error) {
      Logger.debug('[EllaUpstreamDeviceAdapter] explicit connection failed: ${error.runtimeType}');
      await _disconnectStaleConnection(device.id);
      return null;
    }
    if (!_isCurrent(operationGeneration)) {
      if (connected) await _disconnectStaleConnection(device.id);
      return null;
    }
    if (!connected) return null;

    final connection = service.connectionFor(device.id) ?? await service.ensureConnection(device.id);
    final connectedDevice = connection == null ? upstreamDevice : connection.device;
    final mappedDevice = _toLegacyDevice(connectedDevice);
    if (mappedDevice.id.isEmpty) {
      await _disconnectStaleConnection(device.id);
      return null;
    }
    _activeDeviceId = device.id;
    _connectionOwners[device.id] = ownerId;
    final generation = _connectionGenerations.putIfAbsent(device.id, () => ++_connectionGeneration);
    return legacy_service.AuthoritativeDeviceConnection(
      device: mappedDevice,
      connectionGeneration: generation,
    );
  }

  Future<void> _disconnectStaleConnection([String? deviceId]) async {
    try {
      await _disconnect(deviceId);
    } catch (error) {
      Logger.debug('[EllaUpstreamDeviceAdapter] stale connection teardown failed: ${error.runtimeType}');
    }
  }

  @override
  String? ownerBindingForConnection(String deviceId) => _connectionOwners[deviceId];

  @override
  Future<legacy_connection.DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async {
    final service = await _ensureService();
    final connection = service.connectionFor(deviceId);
    // Generic legacy callers may inspect/control an existing connection, but
    // only connectForCurrentUser may create one because it binds consent + UID.
    if (connection == null || connection.status != upstream_service.DeviceConnectionState.connected) return null;
    final owner = _connectionOwners[deviceId] ?? _connectionOwner()?.trim();
    if (owner == null || owner.isEmpty) return null;
    _connectionOwners[deviceId] = owner;
    final generation = _connectionGenerations.putIfAbsent(deviceId, () => ++_connectionGeneration);
    final existing = _connectionAdapters[deviceId];
    if (existing != null &&
        identical(existing.inner, connection) &&
        existing.generation == generation &&
        existing.owner == owner) {
      return existing.wrapper;
    }
    final wrapper = _EllaLegacyDeviceConnection(
      connection,
      disconnect: () => _disconnectProjectedConnection(deviceId, connection, generation, owner),
      isCurrent: () => _isProjectedConnectionCurrent(deviceId, connection, generation, owner),
    );
    _connectionAdapters[deviceId] = (
      inner: connection,
      generation: generation,
      owner: owner,
      wrapper: wrapper,
    );
    return wrapper;
  }

  @override
  void subscribe(legacy_service.IDeviceServiceSubsciption subscription, Object context) {
    _subscriptions[context] = subscription;
    subscription.onDevices(_devices);
    subscription.onStatusChanged(_status);
    final activeDeviceId = _activeDeviceId;
    final generation = activeDeviceId == null ? null : _connectionGenerations[activeDeviceId];
    if (activeDeviceId != null && generation != null && _connectionOwners[activeDeviceId]?.isNotEmpty == true) {
      subscription.onDeviceConnectionStateChanged(
        activeDeviceId,
        legacy_service.DeviceConnectionState.connected,
        connectionGeneration: generation,
      );
    }
  }

  @override
  void unsubscribe(Object context) => _subscriptions.remove(context);

  @override
  DateTime? getFirstConnectedAt() => _service?.getFirstConnectedAt();

  @override
  void setWifiSyncInProgress(bool value) {
    // Upstream owns its transfer coordinator; legacy Wi-Fi sync must not
    // acquire a second transport policy while the flag-ON graph is active.
  }

  @override
  Future<void> cancelPendingConnection() async {
    _operationGeneration++;
    _acceptingDiscoveryEvents = false;
    _activeDiscovery = null;
    try {
      await _service?.stopDiscoverers();
    } catch (error) {
      Logger.debug('[EllaUpstreamDeviceAdapter] cancel discovery failed: ${error.runtimeType}');
    }
    if (_status == legacy_service.DeviceServiceStatus.scanning) {
      _status = legacy_service.DeviceServiceStatus.ready;
      _notifyStatus();
    }
  }

  @override
  Future<void> disconnectDevice() async {
    _operationGeneration++;
    await _disconnectActiveDevice(_activeDeviceId);
  }

  @override
  void onDevices(List<upstream_device.BtDevice> devices) {
    if (!_acceptingDiscoveryEvents || _status == legacy_service.DeviceServiceStatus.stop) return;
    _publishDevices(devices);
  }

  void _publishDevices(List<upstream_device.BtDevice> devices) {
    _devices = devices.map(_toLegacyDevice).where((device) => device.id.isNotEmpty).toList(growable: false);
    for (final subscription in _subscriptions.values.toList()) {
      subscription.onDevices(_devices);
    }
  }

  @override
  void onStatusChanged(upstream_service.DeviceServiceStatus status) {
    if (status != upstream_service.DeviceServiceStatus.stop) return;
    _status = legacy_service.DeviceServiceStatus.stop;
    _connectionAdapters.clear();
    _connectionOwners.clear();
    _activeDeviceId = null;
    _notifyStatus();
  }

  @override
  void onDeviceConnectionStateChanged(String deviceId, upstream_service.DeviceConnectionState state) {
    if (state == upstream_service.DeviceConnectionState.connecting) return;
    if (state == upstream_service.DeviceConnectionState.disconnected) {
      final current = _service?.connectionFor(deviceId);
      if (current?.status == upstream_service.DeviceConnectionState.connected) return;
    }
    final legacyState = state == upstream_service.DeviceConnectionState.connected
        ? legacy_service.DeviceConnectionState.connected
        : legacy_service.DeviceConnectionState.disconnected;
    final generation = state == upstream_service.DeviceConnectionState.connected
        ? ++_connectionGeneration
        : _connectionGenerations[deviceId];
    if (generation == null) return;
    _connectionGenerations[deviceId] = generation;
    if (legacyState == legacy_service.DeviceConnectionState.connected) {
      _activeDeviceId = deviceId;
      final owner = _connectionOwner()?.trim();
      if (owner != null && owner.isNotEmpty) _connectionOwners[deviceId] = owner;
    } else {
      _connectionAdapters.remove(deviceId);
      _connectionOwners.remove(deviceId);
      if (_activeDeviceId == deviceId) _activeDeviceId = null;
    }
    for (final subscription in _subscriptions.values.toList()) {
      subscription.onDeviceConnectionStateChanged(deviceId, legacyState, connectionGeneration: generation);
    }
  }

  void _projectConnectedConnection(String deviceId) {
    onDeviceConnectionStateChanged(deviceId, upstream_service.DeviceConnectionState.connected);
  }

  Future<void> _disconnectProjectedConnection(
    String deviceId,
    upstream_connection.DeviceConnection connection,
    int generation,
    String owner,
  ) async {
    if (_isProjectedConnectionCurrent(deviceId, connection, generation, owner)) {
      await _disconnectActiveDevice(deviceId);
      return;
    }
    try {
      await connection.disconnect();
      await connection.transport.dispose();
    } catch (error) {
      Logger.debug('[EllaUpstreamDeviceAdapter] stale wrapper teardown failed: ${error.runtimeType}');
    }
  }

  bool _isProjectedConnectionCurrent(
    String deviceId,
    upstream_connection.DeviceConnection connection,
    int generation,
    String owner,
  ) {
    return identical(_service?.connectionFor(deviceId), connection) &&
        _connectionGenerations[deviceId] == generation &&
        _connectionOwners[deviceId] == owner;
  }

  Future<void> _disconnectActiveDevice(String? requestedDeviceId) async {
    final targetDeviceId = requestedDeviceId ??
        _activeDeviceId ??
        _service?.connections
            .where((connection) => connection.status == upstream_service.DeviceConnectionState.connected)
            .firstOrNull
            ?.device
            .id;
    try {
      await _disconnect(targetDeviceId);
    } catch (error) {
      Logger.debug('[EllaUpstreamDeviceAdapter] disconnect failed: ${error.runtimeType}');
    } finally {
      if (targetDeviceId != null) {
        _connectionAdapters.remove(targetDeviceId);
        _connectionOwners.remove(targetDeviceId);
        if (_activeDeviceId == targetDeviceId) _activeDeviceId = null;
      }
    }
  }

  void _notifyStatus() {
    for (final subscription in _subscriptions.values.toList()) {
      subscription.onStatusChanged(_status);
    }
  }
}

legacy_device.BtDevice _toLegacyDevice(upstream_device.BtDevice device) {
  try {
    return legacy_device.BtDevice.fromJson(Map<String, dynamic>.from(device.toJson()));
  } catch (_) {
    return legacy_device.BtDevice.empty();
  }
}

upstream_device.BtDevice _toUpstreamDevice(legacy_device.BtDevice device) {
  try {
    return upstream_device.BtDevice.fromJson(Map<String, dynamic>.from(device.toJson())) as upstream_device.BtDevice;
  } catch (_) {
    return upstream_device.BtDevice.empty();
  }
}

legacy_service.DeviceConnectionState _legacyConnectionState(upstream_service.DeviceConnectionState state) =>
    state == upstream_service.DeviceConnectionState.connected
        ? legacy_service.DeviceConnectionState.connected
        : legacy_service.DeviceConnectionState.disconnected;

class _EllaLegacyDeviceConnection extends legacy_connection.DeviceConnection {
  _EllaLegacyDeviceConnection(
    this._inner, {
    required Future<void> Function() disconnect,
    required bool Function() isCurrent,
  })  : _disconnect = disconnect,
        _isCurrent = isCurrent,
        super(_toLegacyDevice(_inner.device), _EllaLegacyDeviceTransport(_inner.transport));

  final upstream_connection.DeviceConnection _inner;
  final Future<void> Function() _disconnect;
  final bool Function() _isCurrent;

  @override
  legacy_service.DeviceConnectionState get status => _legacyConnectionState(_inner.status);

  @override
  legacy_service.DeviceConnectionState get connectionState => _legacyConnectionState(_inner.connectionState);

  @override
  DateTime? get pongAt => _inner.pongAt;

  @override
  Future<void> connect({
    void Function(String deviceId, legacy_service.DeviceConnectionState state)? onConnectionStateChanged,
  }) async {
    if (!_isCurrent()) return;
    if (await _inner.isConnected()) return;
    await _inner.connect(
      onConnectionStateChanged: (deviceId, state) =>
          onConnectionStateChanged?.call(deviceId, _legacyConnectionState(state)),
    );
  }

  @override
  Future<void> disconnect() => _disconnect();

  @override
  Future<void> unpair() async {
    if (_isCurrent()) await _inner.unpair();
    await _disconnect();
  }

  @override
  Future<bool> ping() => _isCurrent() ? _inner.ping() : Future.value(false);

  @override
  Future<bool> isConnected() => _isCurrent() ? _inner.isConnected() : Future.value(false);

  @override
  Future<int> performRetrieveBatteryLevel() => _isCurrent() ? _inner.performRetrieveBatteryLevel() : Future.value(-1);

  @override
  Future<List<int>> performGetButtonState() =>
      _isCurrent() ? _inner.performGetButtonState() : Future.value(const <int>[]);

  // Legacy capture is intentionally unable to subscribe to audio. The same
  // upstream connection is consumed only by the consent-gated capture graph.
  @override
  Future<StreamSubscription?> getBleAudioBytesListener(
          {required void Function(List<int>) onAudioBytesReceived}) async =>
      null;

  @override
  Future<StreamSubscription?> performGetBleAudioBytesListener({
    required void Function(List<int>) onAudioBytesReceived,
  }) async =>
      null;

  @override
  Future<StreamSubscription?> performGetBleStorageBytesListener({
    required void Function(List<int>) onStorageBytesReceived,
  }) {
    if (!_isCurrent()) return Future.value(null);
    return _inner.performGetBleStorageBytesListener(onStorageBytesReceived: onStorageBytesReceived);
  }

  @override
  Future performCameraStartPhotoController() =>
      _isCurrent() ? _inner.performCameraStartPhotoController() : Future<void>.value();

  @override
  Future performCameraStopPhotoController() =>
      _isCurrent() ? _inner.performCameraStopPhotoController() : Future<void>.value();

  @override
  Future<bool> performHasPhotoStreamingCharacteristic() =>
      _isCurrent() ? _inner.performHasPhotoStreamingCharacteristic() : Future.value(false);

  @override
  Future<StreamSubscription?> performGetImageListener({
    required void Function(legacy_models.OrientedImage orientedImage) onImageReceived,
  }) {
    if (!_isCurrent()) return Future.value(null);
    return _inner.performGetImageListener(
      onImageReceived: (image) => onImageReceived(
        legacy_models.OrientedImage(
          imageBytes: image.imageBytes,
          orientation: legacy_device.ImageOrientation.values.byName(image.orientation.name),
        ),
      ),
    );
  }

  @override
  Future<StreamSubscription<List<int>>?> performGetAccelListener({void Function(int)? onAccelChange}) {
    if (!_isCurrent()) return Future.value(null);
    return _inner.performGetAccelListener(onAccelChange: onAccelChange);
  }

  @override
  Future<int> performGetFeatures() => _isCurrent() ? _inner.performGetFeatures() : Future.value(0);

  @override
  Future<void> performSetLedDimRatio(int ratio) => _isCurrent() ? _inner.performSetLedDimRatio(ratio) : Future.value();

  @override
  Future<int?> performGetLedDimRatio() => _isCurrent() ? _inner.performGetLedDimRatio() : Future.value(null);

  @override
  Future<void> performSetMicGain(int gain) => _isCurrent() ? _inner.performSetMicGain(gain) : Future.value();

  @override
  Future<int?> performGetMicGain() => _isCurrent() ? _inner.performGetMicGain() : Future.value(null);
}

class _EllaLegacyDeviceTransport implements legacy_transport.DeviceTransport {
  _EllaLegacyDeviceTransport(this._inner);

  final upstream_transport.DeviceTransport _inner;

  @override
  String get deviceId => _inner.deviceId;

  @override
  Future<void> connect() => _inner.connect();

  @override
  Future<void> disconnect() => _inner.disconnect();

  @override
  Future<bool> isConnected() => _inner.isConnected();

  @override
  Future<bool> ping() => _inner.ping();

  @override
  Stream<List<int>> getCharacteristicStream(String serviceUuid, String characteristicUuid) =>
      _inner.getCharacteristicStream(serviceUuid, characteristicUuid);

  @override
  Future<Stream<List<int>>?> getReadyCharacteristicStream(String serviceUuid, String characteristicUuid) async =>
      _inner.getCharacteristicStream(serviceUuid, characteristicUuid);

  @override
  Future<List<int>> readCharacteristic(String serviceUuid, String characteristicUuid) =>
      _inner.readCharacteristic(serviceUuid, characteristicUuid);

  @override
  Future<void> writeCharacteristic(String serviceUuid, String characteristicUuid, List<int> data) =>
      _inner.writeCharacteristic(serviceUuid, characteristicUuid, data);

  @override
  Stream<legacy_transport.DeviceTransportState> get connectionStateStream => _inner.connectionStateStream.map(
        (state) => legacy_transport.DeviceTransportState.values.byName(state.name),
      );

  @override
  Future<void> dispose() => _inner.dispose();
}
