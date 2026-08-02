import Foundation

private enum TestFailure: Error, CustomStringConvertible {
    case failed(String)

    var description: String {
        switch self {
        case .failed(let message): return message
        }
    }
}

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TestFailure.failed(message) }
}

private func encodedDefines(_ values: [String]) -> String {
    values.map { Data($0.utf8).base64EncodedString() }.joined(separator: ",")
}

private final class EffectRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: Int] = [:]
    private var requests: [URLRequest] = []

    func record(_ name: String) {
        lock.lock()
        values[name, default: 0] += 1
        lock.unlock()
    }

    func record(request: URLRequest) {
        lock.lock()
        requests.append(request)
        lock.unlock()
    }

    func count(_ name: String) -> Int {
        lock.lock()
        defer { lock.unlock() }
        return values[name, default: 0]
    }

    var requestCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return requests.count
    }

    var lastRequest: URLRequest? {
        lock.lock()
        defer { lock.unlock() }
        return requests.last
    }
}

private final class AsyncGate: @unchecked Sendable {
    private let lock = NSLock()
    private var isOpen = false
    private var continuations: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if isOpen {
                lock.unlock()
                continuation.resume()
            } else {
                continuations.append(continuation)
                lock.unlock()
            }
        }
    }

    func open() {
        lock.lock()
        isOpen = true
        let continuations = self.continuations
        self.continuations.removeAll()
        lock.unlock()
        continuations.forEach { $0.resume() }
    }
}

private final class ControlledPollTransport: @unchecked Sendable {
    let started = DispatchSemaphore(value: 0)
    let cancelled = DispatchSemaphore(value: 0)

    private let lock = NSLock()
    private var completion: GuardianModePollingService.PollTransportCompletion?
    private var requests: [URLRequest] = []

    func send(
        _ request: URLRequest,
        completion: @escaping GuardianModePollingService.PollTransportCompletion
    ) -> (() -> Void) {
        lock.lock()
        requests.append(request)
        self.completion = completion
        lock.unlock()
        started.signal()
        return { [cancelled] in
            cancelled.signal()
        }
    }

    func complete(json: String) throws {
        let url = URL(string: "https://api.ella-ai-care.com/v1/ella/guardian/next-audio")!
        guard let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil) else {
            throw TestFailure.failed("could not create poll response")
        }
        lock.lock()
        let completion = self.completion
        self.completion = nil
        lock.unlock()
        guard let completion else { throw TestFailure.failed("poll transport was not waiting") }
        completion(.success((Data(json.utf8), response)))
    }

    var requestCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return requests.count
    }

    var lastRequest: URLRequest? {
        lock.lock()
        defer { lock.unlock() }
        return requests.last
    }
}

private final class TaskBox: @unchecked Sendable {
    private let lock = NSLock()
    private var task: Task<Void, Never>?

    func set(_ task: Task<Void, Never>) {
        lock.lock()
        self.task = task
        lock.unlock()
    }

    func value() async {
        let task = lock.withLock { self.task }
        await task?.value
    }
}

private func configure(_ uid: String?) {
    GuardianModeAvailability.shared.configure(enabled: uid != nil, uid: uid)
}

private func tokenBridge(
    uid: String = "uid-a",
    token: String = "firebase-token-a"
) -> GuardianFirebaseTokenBridge {
    GuardianFirebaseTokenBridge { _ in
        GuardianBearerCredential(uid: uid, token: token)
    }
}

private func failingTokenBridge() -> GuardianFirebaseTokenBridge {
    GuardianFirebaseTokenBridge { _ in
        throw GuardianCredentialError.unavailable
    }
}

private func expiredTokenBridge() -> GuardianFirebaseTokenBridge {
    GuardianFirebaseTokenBridge { _ in
        throw TestFailure.failed("expired Firebase credential")
    }
}

private func makeEffects(
    recorder: EffectRecorder,
    injection: ((GuardianWorkLease) -> Void)? = nil
) -> GuardianModePollingService.Effects {
    GuardianModePollingService.Effects(
        debugMutation: { _, _ in recorder.record("debug") },
        injectionEnqueue: { _, _, _, _, _, lease in
            recorder.record("injection")
            injection?(lease)
        },
        playbackReport: { _, _, _, _, _ in recorder.record("tts_report") },
        speak: { _ in recorder.record("tts") },
        stopSpeaking: { recorder.record("tts_stop") },
        cleanCache: { recorder.record("cache") }
    )
}

private func makePollingService(
    transport: ControlledPollTransport,
    recorder: EffectRecorder,
    bridge: GuardianFirebaseTokenBridge? = nil,
    injection: ((GuardianWorkLease) -> Void)? = nil
) -> GuardianModePollingService {
    let bridge = bridge ?? tokenBridge()
    return GuardianModePollingService(
        transport: transport.send,
        tokenProvider: bridge.credential,
        effects: makeEffects(recorder: recorder, injection: injection)
    )
}

private func testAuthenticatedCurrentPollExecutesProductionInjectionBranch() async throws {
    configure(nil)
    configure("uid-a")
    let transport = ControlledPollTransport()
    let recorder = EffectRecorder()
    let service = makePollingService(transport: transport, recorder: recorder)
    service.startPolling()

    let poll = Task { await service.executePoll() }
    try expect(transport.started.wait(timeout: .now() + 2) == .success, "authenticated poll did not start")
    try transport.complete(json: #"{"url":"https://audio.example/a.mp3","id":"guardian-a"}"#)
    await poll.value

    try expect(recorder.count("injection") == 1, "real production injection branch did not execute")
    let request = try require(transport.lastRequest, "poll request was not recorded")
    try expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer firebase-token-a", "GET bearer missing")
    try expect(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first?.value == "uid-a", "GET UID mismatch")
    service.stopPolling()
}

private func testAccountADisabledThenAccountBCannotReleaseOldResponse() async throws {
    configure(nil)
    configure("uid-a")
    let transport = ControlledPollTransport()
    let recorder = EffectRecorder()
    let service = makePollingService(transport: transport, recorder: recorder)
    service.startPolling()
    let poll = Task { await service.executePoll() }
    try expect(transport.started.wait(timeout: .now() + 2) == .success, "account-A poll did not start")

    service.stopPolling()
    GuardianModeAvailability.shared.disable()
    configure("uid-b")
    service.startPolling()
    try transport.complete(json: #"{"url":"https://audio.example/a.mp3","id":"guardian-a"}"#)
    await poll.value

    try expect(recorder.count("injection") == 0, "account-A response enqueued under account B")
    try expect(recorder.count("debug") == 0, "stale debug mutation occurred")
    try expect(recorder.count("tts") == 0, "stale TTS occurred")
    try expect(recorder.count("tts_report") == 0, "stale TTS report occurred")
    service.stopPolling()
}

private func testUIDDriftBeforeReleaseProducesZeroDebugOrTTSEffects() async throws {
    let responses = [
        #"{"priority":"debug","id":"debug-a","message":"debug"}"#,
        #"{"id":"tts-a","message":"speak account A"}"#,
    ]
    for response in responses {
        configure(nil)
        configure("uid-a")
        let transport = ControlledPollTransport()
        let recorder = EffectRecorder()
        let service = makePollingService(transport: transport, recorder: recorder)
        service.startPolling()
        let poll = Task { await service.executePoll() }
        try expect(transport.started.wait(timeout: .now() + 2) == .success, "stale-effect poll did not start")
        _ = GuardianModeAvailability.shared.invalidateIfUIDChanged("uid-b")
        try transport.complete(json: response)
        await poll.value

        try expect(recorder.count("debug") == 0, "UID-drifted response mutated debug state")
        try expect(recorder.count("injection") == 0, "UID-drifted response enqueued injection")
        try expect(recorder.count("tts_report") == 0, "UID-drifted response reported TTS")
        try expect(recorder.count("tts") == 0, "UID-drifted response spoke TTS")
        service.stopPolling()
    }
}

private func testUIDOnlyDriftAfterResponseReleaseFencesQueuedManagerEffects() async throws {
    configure(nil)
    configure("uid-a")
    let transport = ControlledPollTransport()
    let recorder = EffectRecorder()
    let managerRelease = AsyncGate()
    let managerTask = TaskBox()
    let effectPath = GuardianModeManagerEffectPath()
    let service = makePollingService(transport: transport, recorder: recorder) { lease in
        managerTask.set(Task {
            await managerRelease.wait()
            let operations = GuardianModeManagerEffectPath.Operations(
                perform: { lease, effect in
                    GuardianModeAvailability.shared.performIfCurrent(lease, effect)
                },
                insert: { recorder.record("insert"); return true },
                awaitReadiness: { .ready(durationMs: 1) },
                reportStarted: { _ in recorder.record("report") },
                reportFailed: { _ in recorder.record("report") },
                registerCompletion: { true },
                play: { recorder.record("play"); return true }
            )
            _ = await effectPath.execute(lease: lease, operations: operations)
        })
    }
    service.startPolling()
    let poll = Task { await service.executePoll() }
    try expect(transport.started.wait(timeout: .now() + 2) == .success, "poll did not start")
    try transport.complete(json: #"{"url":"https://audio.example/a.mp3","id":"guardian-a"}"#)
    await poll.value
    try expect(recorder.count("injection") == 1, "current response was not handed to manager")

    _ = GuardianModeAvailability.shared.invalidateIfUIDChanged("uid-b")
    managerRelease.open()
    await managerTask.value()

    try expect(recorder.count("insert") == 0, "UID-drifted manager inserted audio")
    try expect(recorder.count("report") == 0, "UID-drifted manager reported playback")
    try expect(recorder.count("play") == 0, "UID-drifted manager played audio")
    service.stopPolling()
}

private func testUIDDriftAfterReadinessAwaitFencesReportAndPlay() async throws {
    configure(nil)
    configure("uid-a")
    let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing manager lease")
    let recorder = EffectRecorder()
    let effectPath = GuardianModeManagerEffectPath()
    let operations = GuardianModeManagerEffectPath.Operations(
        perform: { lease, effect in
            GuardianModeAvailability.shared.performIfCurrent(lease, effect)
        },
        insert: { recorder.record("insert"); return true },
        awaitReadiness: {
            _ = GuardianModeAvailability.shared.invalidateIfUIDChanged("uid-b")
            return .ready(durationMs: 1)
        },
        reportStarted: { _ in recorder.record("report") },
        reportFailed: { _ in recorder.record("report") },
        registerCompletion: { true },
        play: { recorder.record("play"); return true }
    )
    _ = await effectPath.execute(lease: lease, operations: operations)

    try expect(recorder.count("insert") == 1, "current owner insert should execute before drift")
    try expect(recorder.count("report") == 0, "post-readiness UID drift reported playback")
    try expect(recorder.count("play") == 0, "post-readiness UID drift played audio")
}

private func testManagerCancellationFencesPostAwaitEffects() async throws {
    configure(nil)
    configure("uid-a")
    let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing cancellation lease")
    let recorder = EffectRecorder()
    let readinessStarted = AsyncGate()
    let readinessRelease = AsyncGate()
    let effectPath = GuardianModeManagerEffectPath()
    let task = Task {
        let operations = GuardianModeManagerEffectPath.Operations(
            perform: { lease, effect in
                GuardianModeAvailability.shared.performIfCurrent(lease, effect)
            },
            insert: { recorder.record("insert"); return true },
            awaitReadiness: {
                readinessStarted.open()
                await readinessRelease.wait()
                return .ready(durationMs: 1)
            },
            reportStarted: { _ in recorder.record("report") },
            reportFailed: { _ in recorder.record("report") },
            registerCompletion: { true },
            play: { recorder.record("play"); return true }
        )
        _ = await effectPath.execute(lease: lease, operations: operations)
    }
    await readinessStarted.wait()
    task.cancel()
    readinessRelease.open()
    await task.value

    try expect(recorder.count("insert") == 1, "pre-cancellation insert did not execute")
    try expect(recorder.count("report") == 0, "cancelled manager reported")
    try expect(recorder.count("play") == 0, "cancelled manager played")
}

private func testDuplicateScheduleSuppressionAndRetainedCancellation() async throws {
    configure(nil)
    configure("uid-a")
    let transport = ControlledPollTransport()
    let recorder = EffectRecorder()
    let service = makePollingService(transport: transport, recorder: recorder)
    service.startPolling()
    service.schedulePollNow()
    service.schedulePollNow()
    try expect(transport.started.wait(timeout: .now() + 2) == .success, "scheduled poll did not start")
    try await Task.sleep(nanoseconds: 50_000_000)
    try expect(transport.requestCount == 1, "duplicate schedule started a second transport")

    service.stopPolling()
    try expect(transport.cancelled.wait(timeout: .now() + 2) == .success, "stop did not cancel retained poll")
    try transport.complete(json: #"{"priority":"debug","id":"debug-a"}"#)
    try await Task.sleep(nanoseconds: 50_000_000)
    try expect(recorder.count("debug") == 0, "cancelled poll mutated debug state")
}

private func testNativeAuthDenialsDoNotStartGETOrPOST() async throws {
    let cases = [
        ("missing", failingTokenBridge()),
        ("expired", expiredTokenBridge()),
        ("cross-account", tokenBridge(uid: "uid-b", token: "token-b")),
    ]
    for (name, bridge) in cases {
        configure(nil)
        configure("uid-a")
        let transport = ControlledPollTransport()
        let recorder = EffectRecorder()
        let service = makePollingService(transport: transport, recorder: recorder, bridge: bridge)
        service.startPolling()
        await service.executePoll()
        try expect(transport.requestCount == 0, "\(name) native credential started GET")
        service.stopPolling()

        configure("uid-a")
        let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing reporter lease")
        let reporter = GuardianPlaybackReporter(
            backendURL: { "https://api.ella-ai-care.com" },
            tokenProvider: bridge.credential,
            transport: recorder.record
        )
        let sent = await reporter.report(playbackEvent(), lease: lease)
        try expect(!sent, "\(name) native credential reported playback")
        try expect(recorder.requestCount == 0, "\(name) native credential started POST")
    }
}

private func testAuthenticatedPlaybackReporterUsesExactLeaseOwner() async throws {
    configure(nil)
    configure("uid-a")
    let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing reporter lease")
    let recorder = EffectRecorder()
    let reporter = GuardianPlaybackReporter(
        backendURL: { "https://api.ella-ai-care.com" },
        tokenProvider: tokenBridge().credential,
        transport: recorder.record
    )
    let sent = await reporter.report(playbackEvent(), lease: lease)
    try expect(sent, "current authenticated playback report was denied")
    let request = try require(recorder.lastRequest, "playback POST was not recorded")
    try expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer firebase-token-a", "POST bearer missing")
    let body = try JSONSerialization.jsonObject(with: request.httpBody ?? Data()) as? [String: Any]
    try expect(body?["uid"] as? String == "uid-a", "POST did not carry lease UID")
}

private func playbackEvent() -> GuardianPlaybackEvent {
    GuardianPlaybackEvent(
        eventType: "started",
        queueItemId: "item-a",
        traceId: "trace-a",
        triggerType: "guardian",
        portType: "Speaker",
        portName: "Speaker",
        deviceUID: "device-a",
        durationMs: 1,
        metadata: nil
    )
}

private func require<T>(_ value: T?, _ message: String) throws -> T {
    guard let value else { throw TestFailure.failed(message) }
    return value
}

private func testProductionNotificationBoundary() throws {
    let publicDefines = encodedDefines(["ELLA_GUARDIAN_ENABLED=true", "ELLA_PUBLIC_BUILD=true"])
    let invitationDefines = encodedDefines(["ELLA_GUARDIAN_ENABLED=true", "ELLA_ENTITLEMENT_GATE=true"])
    let internalDefines = encodedDefines(["ELLA_GUARDIAN_ENABLED=true"])
    let guardian: [AnyHashable: Any] = ["type": "ella_notification"]
    let ordinary: [AnyHashable: Any] = ["type": "merge_completed"]

    for lifecycle in GuardianNotificationLifecycle.allCases {
        try expect(
            GuardianNotificationPolicy.disposition(
                for: guardian,
                lifecycle: lifecycle,
                encodedDartDefines: publicDefines
            ) == .suppress,
            "public Guardian delivery did not fail closed"
        )
        try expect(
            GuardianNotificationPolicy.disposition(
                for: guardian,
                lifecycle: lifecycle,
                encodedDartDefines: invitationDefines
            ) == .suppress,
            "invitation Guardian delivery did not fail closed"
        )
        try expect(
            GuardianNotificationPolicy.disposition(
                for: guardian,
                lifecycle: lifecycle,
                encodedDartDefines: internalDefines
            ) == .forward,
            "internal Guardian delivery was suppressed"
        )
        try expect(
            GuardianNotificationPolicy.disposition(
                for: ordinary,
                lifecycle: lifecycle,
                encodedDartDefines: publicDefines
            ) == .forward,
            "ordinary notification was broadened"
        )
    }
}

@main
private enum GuardianNativePolicyTests {
    static func main() async throws {
        try await testAuthenticatedCurrentPollExecutesProductionInjectionBranch()
        try await testAccountADisabledThenAccountBCannotReleaseOldResponse()
        try await testUIDDriftBeforeReleaseProducesZeroDebugOrTTSEffects()
        try await testUIDOnlyDriftAfterResponseReleaseFencesQueuedManagerEffects()
        try await testUIDDriftAfterReadinessAwaitFencesReportAndPlay()
        try await testManagerCancellationFencesPostAwaitEffects()
        try await testDuplicateScheduleSuppressionAndRetainedCancellation()
        try await testNativeAuthDenialsDoNotStartGETOrPOST()
        try await testAuthenticatedPlaybackReporterUsesExactLeaseOwner()
        try testProductionNotificationBoundary()
        print("Guardian native production-boundary tests passed")
    }
}
