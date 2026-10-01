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

private final class ImmediateSequencePollTransport: @unchecked Sendable {
    private let lock = NSLock()
    private var responses: [(statusCode: Int, json: String)]
    private var requests: [URLRequest] = []

    init(responses: [(statusCode: Int, json: String)]) {
        self.responses = responses
    }

    func send(
        _ request: URLRequest,
        completion: @escaping GuardianModePollingService.PollTransportCompletion
    ) -> (() -> Void) {
        lock.lock()
        requests.append(request)
        let next = responses.removeFirst()
        lock.unlock()
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: next.statusCode,
            httpVersion: nil,
            headerFields: nil
        )!
        completion(.success((Data(next.json.utf8), response)))
        return {}
    }

    var recordedRequests: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }
}

private final class TokenRefreshRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [Bool] = []

    func record(_ forcingRefresh: Bool) {
        lock.lock()
        values.append(forcingRefresh)
        lock.unlock()
    }

    var calls: [Bool] {
        lock.lock()
        defer { lock.unlock() }
        return values
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
    GuardianFirebaseTokenBridge { _, _ in
        GuardianBearerCredential(uid: uid, token: token)
    }
}

private func failingTokenBridge() -> GuardianFirebaseTokenBridge {
    GuardianFirebaseTokenBridge { _, _ in
        throw GuardianCredentialError.unavailable
    }
}

private func expiredTokenBridge() -> GuardianFirebaseTokenBridge {
    GuardianFirebaseTokenBridge { _, _ in
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
        tokenProvider: { lease, forcingRefresh in
            try await bridge.credential(for: lease, forcingRefresh: forcingRefresh)
        },
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

private func testUnauthorizedPollRefreshesTokenOnceUnderSameLease() async throws {
    configure(nil)
    configure("uid-a")
    let transport = ImmediateSequencePollTransport(responses: [
        (401, #"{"detail":"expired"}"#),
        (200, #"{"url":"https://audio.example/a.mp3","id":"guardian-a"}"#),
    ])
    let tokenCalls = TokenRefreshRecorder()
    let bridge = GuardianFirebaseTokenBridge { uid, forcingRefresh in
        tokenCalls.record(forcingRefresh)
        return GuardianBearerCredential(
            uid: uid,
            token: forcingRefresh ? "fresh-token" : "stale-token"
        )
    }
    let recorder = EffectRecorder()
    let service = GuardianModePollingService(
        transport: transport.send,
        tokenProvider: { lease, forcingRefresh in
            try await bridge.credential(for: lease, forcingRefresh: forcingRefresh)
        },
        effects: makeEffects(recorder: recorder)
    )
    service.startPolling()
    await service.executePoll()

    let requests = transport.recordedRequests
    try expect(requests.count == 2, "401 did not trigger exactly one authenticated retry")
    try expect(tokenCalls.calls == [false, true], "poll did not force-refresh exactly once")
    try expect(
        requests[0].value(forHTTPHeaderField: "Authorization") == "Bearer stale-token",
        "initial poll did not use the cached credential"
    )
    try expect(
        requests[1].value(forHTTPHeaderField: "Authorization") == "Bearer fresh-token",
        "retry did not use the refreshed credential"
    )
    try expect(recorder.count("injection") == 1, "refreshed response did not reach injection")
    service.stopPolling()
}

private func testUnauthorizedPollCannotRefreshAfterOwnerDrift() async throws {
    configure(nil)
    configure("uid-a")
    let recorder = EffectRecorder()
    let tokenCalls = TokenRefreshRecorder()
    let bridge = GuardianFirebaseTokenBridge { uid, forcingRefresh in
        tokenCalls.record(forcingRefresh)
        return GuardianBearerCredential(uid: uid, token: "token-a")
    }
    let transportRecorder = EffectRecorder()
    let service = GuardianModePollingService(
        transport: { request, completion in
            transportRecorder.record(request: request)
            DispatchQueue.global().async {
                _ = GuardianModeAvailability.shared.invalidateIfUIDChanged("uid-b")
                let response = HTTPURLResponse(
                    url: request.url!,
                    statusCode: 401,
                    httpVersion: nil,
                    headerFields: nil
                )!
                completion(.success((Data(), response)))
            }
            return {}
        },
        tokenProvider: { lease, forcingRefresh in
            try await bridge.credential(for: lease, forcingRefresh: forcingRefresh)
        },
        effects: makeEffects(recorder: recorder)
    )
    service.startPolling()
    await service.executePoll()

    try expect(transportRecorder.requestCount == 1, "owner-drifted 401 retried transport")
    try expect(tokenCalls.calls == [false], "owner-drifted 401 refreshed a credential")
    try expect(recorder.count("injection") == 0, "owner-drifted 401 released audio")
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
            transport: { request, completion in
                recorder.record(request: request)
                completion(.success(playbackHTTPResponse(request, statusCode: 200)))
                return {}
            }
        )
        let outcome = await reporter.report(playbackEvent(), lease: lease)
        try expect(outcome != .accepted, "\(name) native credential reported playback")
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
        transport: { request, completion in
            recorder.record(request: request)
            completion(.success(playbackHTTPResponse(request, statusCode: 200)))
            return {}
        }
    )
    let outcome = await reporter.report(playbackEvent(), lease: lease)
    try expect(outcome == .accepted, "current authenticated playback report was denied")
    let request = try require(recorder.lastRequest, "playback POST was not recorded")
    try expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer firebase-token-a", "POST bearer missing")
    let body = try JSONSerialization.jsonObject(with: request.httpBody ?? Data()) as? [String: Any]
    try expect(body?["uid"] as? String == "uid-a", "POST did not carry lease UID")
}

private func playbackHTTPResponse(_ request: URLRequest, statusCode: Int) -> HTTPURLResponse {
    HTTPURLResponse(url: request.url!, statusCode: statusCode, httpVersion: nil, headerFields: nil)!
}

private func testPlaybackReporterRequiresHTTPAcknowledgement() async throws {
    for statusCode in [200, 201, 204, 401, 403, 404, 500] {
        configure(nil)
        configure("uid-a")
        let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing ACK lease")
        let recorder = EffectRecorder()
        let reporter = GuardianPlaybackReporter(
            backendURL: { "https://api.ella-ai-care.com" },
            tokenProvider: tokenBridge().credential,
            transport: { request, completion in
                recorder.record(request: request)
                completion(.success(playbackHTTPResponse(request, statusCode: statusCode)))
                return {}
            }
        )
        let outcome = await reporter.report(playbackEvent(), lease: lease)
        let expected: GuardianPlaybackReportOutcome =
            (200..<300).contains(statusCode)
            ? .accepted : .rejected(statusCode: statusCode)
        try expect(outcome == expected, "HTTP \(statusCode) was not classified truthfully")
        try expect(recorder.requestCount == 1, "HTTP \(statusCode) caused an automatic retry")
    }
}

private func testPlaybackReporterRejectsMissingHTTPAndTransportFailure() async throws {
    for failsTransport in [false, true] {
        configure(nil)
        configure("uid-a")
        let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing failure lease")
        let recorder = EffectRecorder()
        let reporter = GuardianPlaybackReporter(
            backendURL: { "https://api.ella-ai-care.com" },
            tokenProvider: tokenBridge().credential,
            transport: { request, completion in
                recorder.record(request: request)
                if failsTransport {
                    completion(.failure(URLError(.timedOut)))
                } else {
                    completion(
                        .success(
                            URLResponse(
                                url: request.url!, mimeType: nil, expectedContentLength: 0,
                                textEncodingName: nil)))
                }
                return {}
            }
        )
        let outcome = await reporter.report(playbackEvent(), lease: lease)
        try expect(outcome == (failsTransport ? .transportFailed : .invalidResponse), "invalid ACK was accepted")
        try expect(recorder.requestCount == 1, "invalid ACK caused an automatic retry")
    }
}

private func testPlaybackReporterWaitsForACKAndRejectsRetiredLease() async throws {
    for transition in ["current", "account-aba", "disabled", "same-uid-reenabled"] {
        configure(nil)
        configure("uid-a")
        let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing held ACK lease")
        let recorder = EffectRecorder()
        let started = DispatchSemaphore(value: 0)
        let release = AsyncGate()
        let reporter = GuardianPlaybackReporter(
            backendURL: { "https://api.ella-ai-care.com" },
            tokenProvider: tokenBridge().credential,
            transport: { request, completion in
                recorder.record(request: request)
                Task {
                    await release.wait()
                    completion(.success(playbackHTTPResponse(request, statusCode: 200)))
                }
                started.signal()
                return {}
            }
        )
        let task = Task {
            let outcome = await reporter.report(playbackEvent(), lease: lease)
            recorder.record("finished")
            return outcome
        }
        try expect(started.wait(timeout: .now() + 2) == .success, "held POST did not start")
        try await Task.sleep(nanoseconds: 20_000_000)
        try expect(recorder.count("finished") == 0, "scheduled POST was treated as ACK")
        if transition == "account-aba" {
            configure("uid-b")
            configure("uid-a")
        } else if transition == "disabled" || transition == "same-uid-reenabled" {
            GuardianModeAvailability.shared.disable()
            if transition == "same-uid-reenabled" { configure("uid-a") }
        }
        release.open()
        let outcome = await task.value
        try expect(
            outcome == (transition == "current" ? .accepted : .authorityChanged),
            "late ACK bypassed exact lease after \(transition)")
        try expect(recorder.requestCount == 1, "held POST was resubmitted")
    }
}

private func testPlaybackReporterTokenAwaitRejectsRetiredLease() async throws {
    configure(nil)
    configure("uid-a")
    let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing token-await lease")
    let recorder = EffectRecorder()
    let started = DispatchSemaphore(value: 0)
    let release = AsyncGate()
    let reporter = GuardianPlaybackReporter(
        backendURL: { "https://api.ella-ai-care.com" },
        tokenProvider: { lease in
            started.signal()
            await release.wait()
            return GuardianBearerCredential(uid: lease.uid, token: "synthetic-token")
        },
        transport: { request, completion in
            recorder.record(request: request)
            completion(.success(playbackHTTPResponse(request, statusCode: 200)))
            return {}
        }
    )
    let task = Task { await reporter.report(playbackEvent(), lease: lease) }
    try expect(started.wait(timeout: .now() + 2) == .success, "token lookup did not start")
    configure("uid-b")
    configure("uid-a")
    release.open()
    let outcome = await task.value
    try expect(outcome == .authorityChanged, "token completion adopted replacement same-UID lease")
    try expect(recorder.requestCount == 0, "retired token completion started a POST")
}

private func testFailedPlaybackReceiptACKIsNotAudibleSuccess() async throws {
    configure(nil)
    configure("uid-a")
    let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing failure receipt lease")
    let recorder = EffectRecorder()
    let reporter = GuardianPlaybackReporter(
        backendURL: { "https://api.ella-ai-care.com" },
        tokenProvider: tokenBridge().credential,
        transport: { request, completion in
            recorder.record(request: request)
            completion(.success(playbackHTTPResponse(request, statusCode: 200)))
            return {}
        }
    )
    let outcome = await reporter.report(playbackEvent(eventType: "failed"), lease: lease)
    let body = try JSONSerialization.jsonObject(with: recorder.lastRequest?.httpBody ?? Data()) as? [String: Any]
    try expect(outcome == .accepted, "server acceptance of failure receipt was lost")
    try expect(body?["event_type"] as? String == "failed", "failure receipt was converted to playback success")
}

private func testPlaybackReporterCancellationRetiresAwaitAndLateCallback() async throws {
    configure(nil)
    configure("uid-a")
    let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing cancelled ACK lease")
    let recorder = EffectRecorder()
    let started = DispatchSemaphore(value: 0)
    let lateCallback = DispatchSemaphore(value: 0)
    let release = AsyncGate()
    let reporter = GuardianPlaybackReporter(
        backendURL: { "https://api.ella-ai-care.com" },
        tokenProvider: tokenBridge().credential,
        transport: { request, completion in
            recorder.record(request: request)
            Task {
                await release.wait()
                completion(.success(playbackHTTPResponse(request, statusCode: 200)))
                lateCallback.signal()
            }
            started.signal()
            return { recorder.record("cancelled") }
        }
    )
    let task = Task { await reporter.report(playbackEvent(), lease: lease) }
    try expect(started.wait(timeout: .now() + 2) == .success, "cancelled POST did not start")
    task.cancel()
    let outcome = await task.value
    try expect(outcome == .cancelled, "cancelled POST claimed ACK or retained its await")
    try expect(recorder.count("cancelled") == 1, "cancellation did not cancel transport exactly once")
    release.open()
    try expect(lateCallback.wait(timeout: .now() + 2) == .success, "late callback did not execute")
    try expect(recorder.requestCount == 1, "cancelled callback retried POST")
}

private func testPlaybackReporterDuplicateReceiptKeepsOriginalIdentity() async throws {
    configure(nil)
    configure("uid-a")
    let lease = try require(GuardianModeAvailability.shared.captureLease(), "missing duplicate ACK lease")
    let recorder = EffectRecorder()
    let reporter = GuardianPlaybackReporter(
        backendURL: { "https://api.ella-ai-care.com" },
        tokenProvider: tokenBridge().credential,
        transport: { request, completion in
            recorder.record(request: request)
            completion(.success(playbackHTTPResponse(request, statusCode: 200)))
            return {}
        }
    )
    let first = await reporter.report(playbackEvent(), lease: lease)
    let originalBody = recorder.lastRequest?.httpBody
    let duplicate = await reporter.report(playbackEvent(), lease: lease)
    try expect(first == .accepted && duplicate == .accepted, "idempotent server ACK was not accepted")
    let original = try JSONSerialization.jsonObject(with: originalBody ?? Data()) as? NSDictionary
    let repeated = try JSONSerialization.jsonObject(with: recorder.lastRequest?.httpBody ?? Data()) as? NSDictionary
    try expect(original == repeated, "duplicate receipt manufactured a new playback identity")
    try expect(recorder.requestCount == 2, "duplicate receipt caused hidden transport retries")
}

private func playbackEvent(eventType: String = "started") -> GuardianPlaybackEvent {
    GuardianPlaybackEvent(
        eventType: eventType,
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
        try await testUnauthorizedPollRefreshesTokenOnceUnderSameLease()
        try await testUnauthorizedPollCannotRefreshAfterOwnerDrift()
        try await testAccountADisabledThenAccountBCannotReleaseOldResponse()
        try await testUIDDriftBeforeReleaseProducesZeroDebugOrTTSEffects()
        try await testUIDOnlyDriftAfterResponseReleaseFencesQueuedManagerEffects()
        try await testUIDDriftAfterReadinessAwaitFencesReportAndPlay()
        try await testManagerCancellationFencesPostAwaitEffects()
        try await testDuplicateScheduleSuppressionAndRetainedCancellation()
        try await testNativeAuthDenialsDoNotStartGETOrPOST()
        try await testAuthenticatedPlaybackReporterUsesExactLeaseOwner()
        try await testPlaybackReporterRequiresHTTPAcknowledgement()
        try await testPlaybackReporterRejectsMissingHTTPAndTransportFailure()
        try await testPlaybackReporterWaitsForACKAndRejectsRetiredLease()
        try await testPlaybackReporterTokenAwaitRejectsRetiredLease()
        try await testFailedPlaybackReceiptACKIsNotAudibleSuccess()
        try await testPlaybackReporterCancellationRetiresAwaitAndLateCallback()
        try await testPlaybackReporterDuplicateReceiptKeepsOriginalIdentity()
        try testProductionNotificationBoundary()
        print("Guardian native production-boundary tests passed")
    }
}
