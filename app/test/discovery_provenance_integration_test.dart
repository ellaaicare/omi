import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart' as current_preferences;
import 'package:omi/backend/schema/bt_device/bt_device.dart' as legacy;
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_device_service_adapter.dart';
import 'package:omi/services/devices.dart' as legacy_service;
import 'package:omi/services/devices/device_connection.dart' as legacy_connection;
import 'package:omi/upstream_capture/backend/preferences.dart' as upstream_preferences;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart' as upstream;
import 'package:omi/upstream_capture/services/devices.dart' as native_service;
import 'package:omi/utils/debug_log_manager.dart';

class RetainedService extends native_service.DeviceService {
  RetainedService({super.connectionBuilder});
  int discovers = 0;
  @override Future<void> discover({String? desirableDeviceId,int timeout=5}) async { discovers++; }
  @override Future<void> stopDiscoverers() async {} // Fixture never reaches native APIs.
}
class Rows implements native_service.IDeviceServiceSubsciption {
  List<upstream.BtDevice> latest = [];
  @override void onDevices(List<upstream.BtDevice> rows) {
    latest = rows;
    final l = DebugLogManager.discoveryProvenance;
    l.publish(l.current, rows.map((d) => (id:d.id, friend:d.name == 'Friend')));
  }
  @override void onStatusChanged(native_service.DeviceServiceStatus status) {}
  @override void onDeviceConnectionStateChanged(String id, native_service.DeviceConnectionState state) {}
}
class DelayedRows implements legacy_service.IDeviceService {
  final gate = Completer<void>();
  final listeners = <Object,legacy_service.IDeviceServiceSubsciption>{};
  int discovers = 0, connects = 0;
  @override void start() {}
  @override Future<void> stop() async {}
  @override Future<void> discover({String? desirableDeviceId,int timeout=5}) async {
    discovers++; final recipients = listeners.values.toList(); await gate.future;
    for (final s in recipients) { s.onDevices([legacy.BtDevice(
      name:'Friend',id:'fictional-retired',type:legacy.DeviceType.omi,rssi:-30)]); }
  }
  @override void subscribe(legacy_service.IDeviceServiceSubsciption s,Object owner) {
    listeners[owner]=s; s.onStatusChanged(legacy_service.DeviceServiceStatus.ready);
  }
  @override void unsubscribe(Object owner) { listeners.remove(owner); }
  @override Future<void> cancelPendingConnection() async {}
  @override Future<legacy_connection.DeviceConnection?> ensureConnection(String id,{bool force=false}) async {
    connects++; throw StateError('Fixture prohibits connection');
  }
  @override DateTime? getFirstConnectedAt()=>null;
  @override void setWifiSyncInProgress(bool value) {}
  @override Future<void> disconnectDevice() async {}
}
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid':'owner-test','btDeviceOwnerBinding':'owner-test'});
    await current_preferences.SharedPreferencesUtil.init();
    await upstream_preferences.SharedPreferencesUtil.init();
    DebugLogManager.discoveryProvenance.retire();
  });
  test('actual Main DeviceService retained subscription is marked without any scan', () async {
    const id='11111111-1111-4111-8111-111111111111';
    await upstream_preferences.SharedPreferencesUtil().btDeviceSet(upstream.BtDevice(
      name:'Friend',id:id,type:upstream.DeviceType.omi,rssi:0));
    var factoryCalls=0;
    // Exact production stored-device path; null builder cannot create a transport.
    final service=RetainedService(connectionBuilder:(_){factoryCalls++;return null;});
    expect(await service.ensureConnection(id,force:true),isNull);
    expect(factoryCalls,1);
    final l=DebugLogManager.discoveryProvenance; final p=l.begin(); final rows=Rows();
    service.subscribe(rows,rows);
    expect(rows.latest.single.name,'Friend');
    expect(l.snapshot['friend_row_retained'],1);
    expect(l.snapshot['candidate_scan_callback_advertised_name'],0);
    expect(l.snapshot['known_id_input_count'],isNull);
    service.unsubscribe(rows); l.finish(p);
    final adapter=EllaUpstreamDeviceServiceAdapter(serviceLoader:()async=>service,
      connect:(_,__)async=>throw StateError('Fixture forbids connection'),disconnect:(_)async{});
    adapter.start();
    final provider=OnboardingProvider(deviceService:adapter)..hasBluetoothPermission=true;
    await provider.scanDevices(onShowDialog:()=>fail('Unexpected permission prompt'),owner:Object());
    expect(provider.deviceList.single.name,'Friend');
    expect(l.snapshot['friend_row_retained'],1);
    expect(l.snapshot['candidate_scan_callback_advertised_name'],0);
    expect(service.discovers,1);
    await provider.cancelDeviceDiscovery(); provider.dispose(); await adapter.stop();
  });
  test('actual Main picker rejects late rows after in-flight authority retirement', () async {
    final service=DelayedRows();
    final provider=OnboardingProvider(deviceService:service)..hasBluetoothPermission=true;
    final work=provider.scanDevices(onShowDialog:()=>fail('No permission prompt expected'),owner:Object());
    await Future<void>.delayed(Duration.zero);
    expect(service.discovers,1);
    current_preferences.SharedPreferencesUtil().invalidateAccountAuthorityForTransition();
    service.gate.complete(); await work;
    expect(provider.deviceList,isEmpty);
    expect(DebugLogManager.discoveryProvenance.snapshot['phase'],'retired');
    expect(DebugLogManager.discoveryProvenance.snapshot['friend_row_retained'],0);
    expect(service.connects,0);
    provider.dispose();
  });
}
