import AVFoundation
import Foundation

// DEV-ONLY timed capture token gate: integration harness through the PRODUCTION PhoneMicController.
//
// Compiled by ios/test/dev_timed_capture_controller_test.rb with -D BASE_DEV_TIMED_CAPTURE together with the
// production PhoneMic sources and the Pigeon contract stub (same extraction as the lifecycle replay harness).
// Only OS audio/permission/session I/O is faked; setup stages get hooks so a token can be revoked or expire
// in the middle of activation. Each scenario asserts the engine-start count, teardown, refusal stage and the
// controller's own session state.

final class Sink: PhoneMicEventSink, @unchecked Sendable {
    func onAudioFrame(pcm16leMono16k: Data, sessionId: Int64) {}
    func onStateChanged(state: PhoneMicCaptureState, sessionId: Int64) {}
    func onCaptureError(code: String, message: String, sessionId: Int64) {}
    func onBatchProgress(capturedSeconds: Double, sessionId: Int64) {}
}

final class Engine: PhoneMicEngineControlling {
    var beforeBuild: () -> Void = {}
    private(set) var starts = 0
    private(set) var teardowns = 0
    var isRunning: Bool { starts > 0 && teardowns == 0 }
    var audioEngineForMonitor: AVAudioEngine? { nil }
    func buildAndInstallTap(epoch: UInt64) throws { beforeBuild() }
    func startEngine() throws { starts += 1 }
    func teardown() { teardowns += 1 }
}

final class Monitor: PhoneMicInterruptionSource {
    let queue: DispatchQueue
    let handler: (PhoneMicInterruptionSignal) -> Void
    init(queue: DispatchQueue, handler: @escaping (PhoneMicInterruptionSignal) -> Void) {
        self.queue = queue
        self.handler = handler
    }
    func fire(_ signal: PhoneMicInterruptionSignal) { queue.async { self.handler(signal) } }
    func startObserving() {}
    func stopObserving() {}
    func bindEngine(_ engine: AVAudioEngine?) {}
}

final class Granted: PhoneMicPermissionChecking {
    func current() -> PhoneMicPermissionStatus { .granted }
    func request(_ completion: @escaping (Bool) -> Void) { completion(true) }
}

final class World {
    var engines: [Engine] = []
    var onConfigure: () -> Void = {}
    var onBuild: () -> Void = {}
    var monitor: Monitor?

    func controller() -> PhoneMicController {
        PhoneMicController(environment: PhoneMicControllerEnvironment(
            sink: Sink(),
            permission: Granted(),
            configureSession: { [unowned self] in self.onConfigure() },
            describeRoute: { "harness-route" },
            makeMonitor: { [unowned self] queue, onSignal in
                let monitor = Monitor(queue: queue, handler: onSignal)
                self.monitor = monitor
                return monitor
            },
            makeEngine: { [unowned self] _, _, _ in
                let engine = Engine()
                engine.beforeBuild = { self.onBuild() }
                self.engines.append(engine)
                return engine
            },
            batchDirectory: { nil },
            batchAutoMarker: { false },
            makeEncoder: { nil },
            makeWriter: { _, _ in fatalError("stream only") }
        ))
    }

    var engineStarts: Int { engines.reduce(0) { $0 + $1.starts } }
}

var failures = 0
var checks = 0
func check(_ ok: @autoclosure () -> Bool, _ name: String, line: Int = #line) {
    checks += 1
    if !ok() { failures += 1; print("FAIL line \(line): \(name)") }
}

func testId(_ token: Int64, _ seq: Int64) -> Int64 { PhoneMicDevTokenPolicy.testBit | (token << 16) | seq }

// PhoneMicController delivers start/stop/cutoff completions on DispatchQueue.main, and these scenarios run on the
// main thread. Every wait therefore pumps the main run loop with a bounded deadline (the same pattern as
// phone_mic_lifecycle_replay_test.swift's waitUntil/drainMain); a semaphore wait here would block the very queue
// the completion needs.
func drainMain(_ seconds: Double) {
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
}

func waitMain(timeout: Double = 5, until done: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while !done() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
    return done()
}

/// Start and wait for its main-queue completion: the error code, nil on success, "timeout" if none arrives.
func start(_ c: PhoneMicController, _ id: Int64) -> String? {
    var finished = false
    var code: String?
    c.start(mode: .stream, sessionId: id) { result in
        switch result {
        case .success: code = nil
        case .failure(let error): code = (error as? PhoneMicPigeonError)?.code ?? "\(error)"
        }
        finished = true
    }
    return waitMain(until: { finished }) ? code : "timeout"
}

func stop(_ c: PhoneMicController) {
    var finished = false
    c.stop { finished = true }
    _ = waitMain(until: { finished })
}

func cutoff(_ c: PhoneMicController, _ token: Int64) -> String {
    var outcome: String?
    c.devCutoff(token: token) { outcome = $0 }
    return waitMain(until: { outcome != nil }) ? outcome! : "timeout"
}

/// Lets queued control-queue work (retries 0.35 s apart, scheduled stops) and its main-queue completions settle.
func settle(_ seconds: Double = 1.2) { drainMain(seconds) }

var nextToken: Int64 = 100
func grant(ttl: TimeInterval = 30) -> Int64 {
    nextToken += 1
    let now = Date()
    precondition(PhoneMicController.devTokens.grant(nextToken, until: now.addingTimeInterval(ttl), now: now))
    return nextToken
}

@main
struct DevTimedCaptureControllerTest {
    static func main() {
        // A1: valid token starts once; the session carries it.
        do {
            let w = World(), c = w.controller(), t = grant()
            check(start(c, testId(t, 1)) == nil, "A1 valid token starts")
            check(w.engineStarts == 1 && c.devSessionOpen(token: t), "A1 one engine start, owned session open")
            check(cutoff(c, t) == "stopped", "A1 cutoff stops the owned session")
            settle(0.3)
            check(!c.isRecording && !c.devSessionOpen(token: t), "A1 idle after cutoff")
        }
        // A2: revocation DURING configureSession: no engine is ever started.
        do {
            let w = World(), c = w.controller(), t = grant()
            w.onConfigure = { PhoneMicController.devTokens.revoke(t) }
            check(start(c, testId(t, 1)) == "dev_token_invalid", "A2 start fails")
            settle()
            check(w.engineStarts == 0, "A2 startEngine never called")
            check(c.devRefusalStages.first == "after_configure_session", "A2 refused right after configureSession")
            check(!c.isRecording, "A2 controller idle")
        }
        // A3: expiry crossing DURING engine build/tap install: refused immediately before startEngine, torn down.
        do {
            let w = World(), c = w.controller(), t = grant(ttl: 0.2)
            w.onBuild = { Thread.sleep(forTimeInterval: 0.35) }
            check(start(c, testId(t, 1)) == "dev_token_invalid", "A3 start fails")
            settle()
            check(w.engineStarts == 0, "A3 startEngine never called")
            check(c.devRefusalStages.first == "before_start_engine", "A3 refused before startEngine")
            check(w.engines.first?.teardowns ?? 0 >= 1, "A3 prepared engine torn down")
            check(!c.isRecording, "A3 controller idle")
        }
        // A4: a late start after the deadline is refused before any setup (handleStart).
        do {
            let w = World(), c = w.controller(), t = grant(ttl: 0.1)
            settle(0.25)
            check(start(c, testId(t, 1)) == "dev_token_invalid", "A4 late start refused")
            check(w.engines.isEmpty, "A4 no engine even built")
        }
        // A5: owned stall restart (same token, new seq) joins its own session and stays owned; after revocation
        // a further restart is refused.
        do {
            let w = World(), c = w.controller(), t = grant()
            check(start(c, testId(t, 1)) == nil, "A5 first start")
            check(start(c, testId(t, 2)) == nil, "A5 same-token restart joins")
            check(c.devSessionOpen(token: t), "A5 still owned")
            PhoneMicController.devTokens.revoke(t)
            check(start(c, testId(t, 3)) == "dev_token_invalid", "A5 restart after revocation refused")
            stop(c)
        }
        // A6: a resume of a revoked token's interrupted session is refused and ends the session.
        do {
            let w = World(), c = w.controller(), t = grant()
            check(start(c, testId(t, 1)) == nil, "A6 start")
            w.monitor?.fire(.interruptionBegan)
            settle(0.3)
            PhoneMicController.devTokens.revoke(t)
            w.monitor?.fire(.interruptionEnded(shouldResume: true))
            settle()
            check(w.engineStarts == 1, "A6 no engine restart for a revoked token")
            check(!c.isRecording, "A6 dead test session ended, not probed forever")
        }
        // B1: an ordinary session is never stopped by a token cutoff, and a test start never adopts it.
        do {
            let w = World(), c = w.controller(), t = grant()
            check(start(c, 5) == nil, "B1 ordinary start (stock)")
            check(start(c, testId(t, 1)) == "dev_token_would_adopt_other_session", "B1 valid test start never adopts it")
            check(c.isRecording && !c.devSessionOpen(token: t), "B1 ordinary session untouched by the refused start")
            check(cutoff(c, t) == "not_owned", "B1 cutoff leaves the ordinary session")
            check(c.isRecording, "B1 ordinary still recording")
            stop(c)
        }
        // B2: after the owned session ends, a new ordinary session is never stopped by the old token's cutoff.
        do {
            let w = World(), c = w.controller(), t = grant()
            check(start(c, testId(t, 1)) == nil, "B2 owned start")
            stop(c)
            check(start(c, 9) == nil, "B2 new ordinary session")
            check(cutoff(c, t) == "not_owned", "B2 old token cutoff is not_owned")
            check(c.isRecording, "B2 ordinary keeps recording")
            stop(c)
        }
        print(failures == 0 ? "PASS \(checks) dev token controller checks" : "FAIL \(failures)/\(checks)")
        exit(failures == 0 ? 0 : 1)
    }
}
