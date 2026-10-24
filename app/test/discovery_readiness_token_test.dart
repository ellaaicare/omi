import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/upstream_capture/gen/pigeon_communicator.g.dart';
import 'package:omi/upstream_capture/services/bridges/ble_bridge.dart';
import 'package:omi/upstream_capture/services/devices/bluetooth_readiness.dart';
import 'package:omi/upstream_capture/services/devices/discovery/native_bluetooth_discoverer.dart';
class NoConnectHost extends BleHostApi {
  int connects=0;
  @override Future<String> getBluetoothState()async=>'on';
  @override Future<void> startScan(int timeout,List<String> services)async {
    BleBridge.instance.onPeripheralDiscovered(BlePeripheral(uuid:'fictional-old',name:'Friend',
      rssi:-40,serviceUuids:[],source:'scan',hasAdvertisedLocalName:true));
  }
  @override Future<void> stopScan()async{}
  @override Future<void> connect(String id)async{connects++;throw StateError('No native connection');}
  @override Future<List<BlePeripheral>> retrieveConnectedAndKnownPeripherals(List<String> services,List<String> ids)async=>[];
}
void main(){
  TestWidgetsFlutterBinding.ensureInitialized();
  test('actual discoverer cannot adopt replacement diagnostic token across readiness await',()async{
    final entered=Completer<void>(),release=Completer<String>();final host=NoConnectHost();
    final readiness=BluetoothReadiness(readState:(){entered.complete();return release.future;},
      permissionState:(_)async=>null,observeBridge:false);
    final discoverer=NativeBluetoothDiscoverer(hostApi:host,bluetoothReadiness:readiness);
    final l=DebugLogManager.discoveryProvenance;l.retire();final old=l.begin();
    final work=discoverer.discover(timeout:0);await entered.future;
    l.retirePass(old);final replacement=l.begin();l.knownInput(replacement,7);
    release.complete('on');await work;
    expect(l.current,replacement);expect(l.snapshot['phase'],'scanning');
    expect(l.snapshot['known_id_input_count'],7);
    expect(l.snapshot['candidate_scan_callback_advertised_name'],0);
    expect(l.snapshot['friend_row_scan_callback_advertised_name'],0);
    expect(host.connects,0);readiness.dispose();l.retire();
  });
}
