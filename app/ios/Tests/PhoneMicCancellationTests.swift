import Foundation
import AVFoundation

private final class Permission: PhoneMicPermissionChecking {
    var callbacks: [(Bool) -> Void] = []
    func current() -> PhoneMicPermissionStatus { .undetermined }
    func request(_ completion: @escaping (Bool) -> Void) { callbacks.append(completion) }
}
private final class Sink: PhoneMicEventSink {
    func onAudioFrame(pcm16leMono16k: Data, sessionId: Int64) {}
    func onStateChanged(state: PhoneMicCaptureState, sessionId: Int64) {}
    func onCaptureError(code: String, message: String, sessionId: Int64) {}
    func onBatchProgress(capturedSeconds: Double, sessionId: Int64) {}
}
private final class Engine: PhoneMicEngineControlling {
    var isRunning = false
    var audioEngineForMonitor: AVAudioEngine? { nil }
    func buildAndInstallTap(epoch: UInt64) throws {}
    func startEngine() throws { isRunning = true }
    func teardown() { isRunning = false }
}
@main
private enum PhoneMicCancellationTests {
    static func main() async {
        let permission = Permission()
        var starts = 0
        let controller = PhoneMicController(environment: PhoneMicControllerEnvironment(
            sink: Sink(), permission: permission,
            configureSession: {}, describeRoute: { "synthetic" },
            makeMonitor: { _, _ in nil },
            makeEngine: { _, _, _ in starts += 1; return Engine() },
            batchDirectory: { nil }, batchAutoMarker: { false },
            makeEncoder: { nil }, makeWriter: { _, _ in fatalError("No batch in this test") }
        ))
        let first = Task { await withCheckedContinuation { continuation in
            controller.start(mode: .stream, sessionId: 1) { continuation.resume(returning: $0) }
        }}
        try? await Task.sleep(nanoseconds: 20_000_000)
        // A synchronous state read drains the control queue without answering permission.
        precondition(!controller.isRecording && permission.callbacks.count == 1)
        await withCheckedContinuation { continuation in controller.stop { continuation.resume() } }
        switch await first.value {
        case .failure: break
        case .success: fatalError("Stopped pending start reported success")
        }
        precondition(starts == 0 && !controller.isRecording)
        print("PASS immediate Stop settles an unanswered permission start")
        let second = Task { await withCheckedContinuation { continuation in
            controller.start(mode: .stream, sessionId: 2) { continuation.resume(returning: $0) }
        }}
        try? await Task.sleep(nanoseconds: 20_000_000)
        precondition(!controller.isRecording && permission.callbacks.count == 2)
        permission.callbacks[0](true)
        precondition(!controller.isRecording && starts == 0)
        print("PASS stale permission grant cannot start replacement session")
        permission.callbacks[1](true)
        switch await second.value {
        case .success: break
        case .failure: fatalError("Fresh permission grant failed")
        }
        precondition(controller.isRecording && starts == 1)
        await withCheckedContinuation { continuation in controller.stop { continuation.resume() } }
        precondition(!controller.isRecording)
        print("PASS fresh explicit start works after cancellation")
    }
}
