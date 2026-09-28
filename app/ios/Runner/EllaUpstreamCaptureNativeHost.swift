// Ella-owned native registration for the vendored BasedHardware/omi@f16699a
// capture hosts (ellaaicare/ella-ai#1280).
//
// Compiled into the Runner target in both configurations, but its body exists
// only when ios/Flutter/EllaUpstreamCapture.xcconfig sets
// ELLA_UPSTREAM_CAPTURE_ENABLED = YES (SWIFT_ACTIVE_COMPILATION_CONDITIONS gets
// ELLA_UPSTREAM_CAPTURE_ENABLED_YES). With the flag OFF this file is empty and the
// vendored Runner/Ble/**, Runner/PhoneMic/** sources are excluded from the build.
//
// Mirrors exactly what upstream's own AppDelegate registers for these hosts:
// BLE Pigeon (OmiBleManager + BleHostApiImpl), PhoneMic Pigeon
// (PhoneMicController + PhoneMicHostApiImpl), the native capture-policy latch
// channel ("com.omi/capture_policy") and the WAL-drain background lease channel
// ("com.friend.ios/sync_transfer"), plus the BLE lifecycle hooks.

#if ELLA_UPSTREAM_CAPTURE_ENABLED_YES
import Flutter
import UIKit

final class EllaUpstreamCaptureNativeHost {
    static let shared = EllaUpstreamCaptureNativeHost()

    private var phoneMicController: PhoneMicController?
    private var capturePolicyChannel: FlutterMethodChannel?
    private var syncTransferChannel: FlutterMethodChannel?
    private var syncTransferBackgroundTask: UIBackgroundTaskIdentifier = .invalid
    private var lifecycleObservers: [NSObjectProtocol] = []
    private var registered = false

    private lazy var syncTransferLease = SyncTransferBackgroundLease(
        begin: { [weak self] expirationHandler in
            guard let self else { return false }
            self.syncTransferBackgroundTask = UIApplication.shared.beginBackgroundTask(
                withName: "omi-live-capture-wal-drain",
                expirationHandler: expirationHandler
            )
            return self.syncTransferBackgroundTask != .invalid
        },
        end: { [weak self] in
            self?.endSyncTransferBackgroundTask()
        },
        notifyExpired: { [weak self] reason in
            self?.syncTransferChannel?.invokeMethod("expired", arguments: ["reason": reason])
        }
    )

    private init() {}

    func register(binaryMessenger messenger: FlutterBinaryMessenger) {
        guard !registered else { return }
        registered = true

        // Native BLE module — upstream's Pigeon BleHostApi / BleFlutterApi.
        let bleFlutterApi = BleFlutterApi(binaryMessenger: messenger)
        OmiBleManager.shared.setFlutterApi(bleFlutterApi)
        BleHostApiSetup.setUp(binaryMessenger: messenger, api: BleHostApiImpl(bleManager: OmiBleManager.shared))
        NSLog("[EllaUpstreamCapture] BLE Pigeon APIs registered")

        // Native phone-mic capture — upstream's Pigeon PhoneMicHostApi / PhoneMicFlutterApi.
        let phoneMicFlutterApi = PhoneMicFlutterApi(binaryMessenger: messenger)
        let micController = PhoneMicController(environment: PhoneMicLiveEnvironment.make(sink: phoneMicFlutterApi))
        phoneMicController = micController
        PhoneMicHostApiSetup.setUp(binaryMessenger: messenger, api: PhoneMicHostApiImpl(controller: micController))
        NSLog("[EllaUpstreamCapture] PhoneMic Pigeon APIs registered")

        // Native capture admission latch read by upstream's native batch writers.
        capturePolicyChannel = FlutterMethodChannel(name: "com.omi/capture_policy", binaryMessenger: messenger)
        capturePolicyChannel?.setMethodCallHandler { call, result in
            if call.method == "getRevision" {
                result(CaptureAdmissionPolicy.currentProcessRevision())
                return
            }
            guard call.method == "setMuted" else {
                result(FlutterMethodNotImplemented)
                return
            }
            guard let args = call.arguments as? [String: Any],
                  let muted = args["muted"] as? Bool,
                  let revision = CaptureAdmissionPolicy.channelRevision(args["revision"]),
                  revision >= 0 else {
                result(FlutterError(
                    code: "INVALID_CAPTURE_POLICY",
                    message: "setMuted requires {muted: bool, revision: nonnegative int}",
                    details: nil
                ))
                return
            }
            switch CaptureAdmissionPolicy.applyProcessUpdate(muted: muted, revision: revision, defaults: .standard) {
            case .applied:
                result(nil)
            case let .stale(currentRevision):
                result(FlutterError(
                    code: "STALE_CAPTURE_POLICY",
                    message: "capture policy revision is older than native state",
                    details: ["currentRevision": currentRevision]
                ))
            case .persistenceNotReady:
                result(FlutterError(
                    code: "CAPTURE_POLICY_NOT_PERSISTED",
                    message: "unmute requires the matching durable capture policy",
                    details: nil
                ))
            }
        }

        // Bounded background execution for an in-flight upstream WAL drain.
        syncTransferChannel = FlutterMethodChannel(name: "com.friend.ios/sync_transfer", binaryMessenger: messenger)
        syncTransferChannel?.setMethodCallHandler { [weak self] call, result in
            guard let self else {
                result(nil)
                return
            }
            switch call.method {
            case "start":
                self.syncTransferLease.start()
                result(nil)
            case "stop":
                self.syncTransferLease.stop()
                result(nil)
            default:
                result(FlutterMethodNotImplemented)
            }
        }

        // Upstream AppDelegate lifecycle hooks for the BLE manager.
        let center = NotificationCenter.default
        lifecycleObservers = [
            center.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { _ in
                OmiBleManager.shared.markBackgroundTelemetryStart()
            },
            center.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
                OmiBleManager.shared.markBackgroundTelemetryEnd()
            },
            center.addObserver(forName: UIApplication.willEnterForegroundNotification, object: nil, queue: .main) { _ in
                OmiBleManager.shared.reconnectStalePeripherals()
            },
        ]
    }

    func applicationWillTerminate() {
        OmiBleManager.shared.disconnectAllPeripherals()
    }

    private func endSyncTransferBackgroundTask() {
        guard syncTransferBackgroundTask != .invalid else { return }
        let task = syncTransferBackgroundTask
        syncTransferBackgroundTask = .invalid
        UIApplication.shared.endBackgroundTask(task)
    }
}
#endif
