import 'package:mockito/mockito.dart';
import 'package:omi/services/services.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/sockets/transcription_connection.dart';
import 'package:omi/services/wals.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';

// Mock for ServiceManager
class MockServiceManager extends Mock implements ServiceManager {}

// Mock for IDeviceService
class MockDeviceService extends Mock implements IDeviceService {}

// Mock for IDeviceServiceConnection
class MockDeviceServiceConnection extends Mock implements IDeviceServiceConnection {}

// Mock for TranscriptSegmentSocketService
class MockTranscriptSegmentSocketService extends Mock implements TranscriptSegmentSocketService {}

// Mock for IWalService
class MockWalService extends Mock implements IWalService {}

// Mock for IWalSync
class MockWalSync extends Mock implements IWalSync {}

// Mock for IWalSyncs
class MockWalSyncs extends Mock implements IWalSyncs {}

// Mock for BtDevice
class MockBtDevice extends Mock implements BtDevice {}
