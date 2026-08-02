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

private final class LockedUID {
    private let lock = NSLock()
    private var value: String

    init(_ value: String) {
        self.value = value
    }

    func get() -> String {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    func set(_ value: String) {
        lock.lock()
        self.value = value
        lock.unlock()
    }
}

private final class ControlledPollTransport {
    let started = DispatchSemaphore(value: 0)
    let cancelled = DispatchSemaphore(value: 0)

    private let lock = NSLock()
    private var continuation: CheckedContinuation<(Data, URLResponse), Error>?

    func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        started.signal()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                lock.lock()
                self.continuation = continuation
                lock.unlock()
            }
        } onCancel: {
            self.cancelled.signal()
        }
    }

    func complete() throws {
        let url = URL(string: "https://api.ella-ai-care.com/v1/ella/guardian/next-audio")!
        guard let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil) else {
            throw TestFailure.failed("could not create poll response")
        }
        let data = Data(#"{"url":"https://audio.example/account-a.mp3","id":"guardian-a","priority":"normal"}"#.utf8)
        lock.lock()
        let continuation = self.continuation
        self.continuation = nil
        lock.unlock()
        guard let continuation else { throw TestFailure.failed("poll transport was not waiting") }
        continuation.resume(returning: (data, response))
    }
}

private final class PollReleaseRecorder {
    private let lock = NSLock()
    private(set) var releasedUIDs: [String] = []

    func record(uid: String) {
        lock.lock()
        releasedUIDs.append(uid)
        lock.unlock()
    }

    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        return releasedUIDs.count
    }
}

private func makePollingService(
    transport: ControlledPollTransport,
    uid: LockedUID,
    releases: PollReleaseRecorder
) -> GuardianModePollingService {
    GuardianModePollingService(
        transport: transport.send,
        uidProvider: uid.get,
        responseHandler: { _, _, releasedUID in
            releases.record(uid: releasedUID)
        }
    )
}

private func testProductionCurrentPollReleasesExactlyOnce() async throws {
    let availability = GuardianModeAvailability.shared
    availability.setEnabled(false)
    let uid = LockedUID("uid-a")
    let transport = ControlledPollTransport()
    let releases = PollReleaseRecorder()
    let service = makePollingService(transport: transport, uid: uid, releases: releases)

    availability.setEnabled(true)
    service.startPolling()
    let poll = Task { await service.executePoll() }
    try expect(transport.started.wait(timeout: .now() + 2) == .success, "current production poll did not start")
    try transport.complete()
    await poll.value

    try expect(releases.count == 1, "current production poll did not release exactly once")
    service.stopPolling()
    availability.setEnabled(false)
}

private func testProductionPollResponseCannotCrossAccountReenable() async throws {
    let availability = GuardianModeAvailability.shared
    availability.setEnabled(false)
    let uid = LockedUID("uid-a")
    let transport = ControlledPollTransport()
    let releases = PollReleaseRecorder()
    let service = makePollingService(transport: transport, uid: uid, releases: releases)

    availability.setEnabled(true)
    service.startPolling()
    let accountAPoll = Task { await service.executePoll() }
    try expect(transport.started.wait(timeout: .now() + 2) == .success, "account-A production poll did not start")

    availability.setEnabled(false)
    service.stopPolling()
    uid.set("uid-b")
    availability.setEnabled(true)
    service.startPolling()

    try transport.complete()
    await accountAPoll.value

    try expect(releases.count == 0, "STALE_RESPONSE_RELEASED_UNDER_NEW_GENERATION")
    service.stopPolling()
    availability.setEnabled(false)
}

private func testProductionPollResponseCannotReleaseAfterDisable() async throws {
    let availability = GuardianModeAvailability.shared
    availability.setEnabled(false)
    let uid = LockedUID("uid-a")
    let transport = ControlledPollTransport()
    let releases = PollReleaseRecorder()
    let service = makePollingService(transport: transport, uid: uid, releases: releases)

    availability.setEnabled(true)
    service.startPolling()
    let poll = Task { await service.executePoll() }
    try expect(transport.started.wait(timeout: .now() + 2) == .success, "disable-boundary poll did not start")

    availability.setEnabled(false)
    service.stopPolling()
    try transport.complete()
    await poll.value

    try expect(releases.count == 0, "in-flight response released after Guardian disable")
    availability.setEnabled(false)
}

private func testProductionPollResponseCannotReleaseAfterSameUIDReenable() async throws {
    let availability = GuardianModeAvailability.shared
    availability.setEnabled(false)
    let uid = LockedUID("uid-a")
    let transport = ControlledPollTransport()
    let releases = PollReleaseRecorder()
    let service = makePollingService(transport: transport, uid: uid, releases: releases)

    availability.setEnabled(true)
    service.startPolling()
    let oldGenerationPoll = Task { await service.executePoll() }
    try expect(transport.started.wait(timeout: .now() + 2) == .success, "re-enable-boundary poll did not start")

    availability.setEnabled(false)
    service.stopPolling()
    availability.setEnabled(true)
    service.startPolling()
    try transport.complete()
    await oldGenerationPoll.value

    try expect(releases.count == 0, "old generation released after same-UID re-enable")
    service.stopPolling()
    availability.setEnabled(false)
}

private func testProductionPollResponseCannotCrossUIDWithoutGenerationChange() async throws {
    let availability = GuardianModeAvailability.shared
    availability.setEnabled(false)
    let uid = LockedUID("uid-a")
    let transport = ControlledPollTransport()
    let releases = PollReleaseRecorder()
    let service = makePollingService(transport: transport, uid: uid, releases: releases)

    availability.setEnabled(true)
    service.startPolling()
    let accountAPoll = Task { await service.executePoll() }
    try expect(transport.started.wait(timeout: .now() + 2) == .success, "UID-drift poll did not start")

    uid.set("uid-b")
    try transport.complete()
    await accountAPoll.value

    try expect(releases.count == 0, "account-A response released after UID-only drift")
    service.stopPolling()
    availability.setEnabled(false)
}

private func testStopCancelsRetainedProductionPollTask() async throws {
    let availability = GuardianModeAvailability.shared
    availability.setEnabled(false)
    let uid = LockedUID("uid-a")
    let transport = ControlledPollTransport()
    let releases = PollReleaseRecorder()
    let service = makePollingService(transport: transport, uid: uid, releases: releases)

    availability.setEnabled(true)
    service.startPolling()
    service.schedulePollNow()
    try expect(transport.started.wait(timeout: .now() + 2) == .success, "tracked production poll did not start")

    availability.setEnabled(false)
    service.stopPolling()
    try expect(transport.cancelled.wait(timeout: .now() + 2) == .success, "stop did not cancel the retained poll task")
    uid.set("uid-b")
    availability.setEnabled(true)
    service.startPolling()
    try transport.complete()
    try await Task.sleep(nanoseconds: 50_000_000)

    try expect(releases.count == 0, "cancelled account-A poll released under account B")
    service.stopPolling()
    availability.setEnabled(false)
}

private func testDisableDuringLoadCannotReplay() throws {
    let gate = GuardianWorkLeaseGate()
    gate.setEnabled(true)
    guard let lease = gate.captureLease() else { throw TestFailure.failed("missing initial lease") }

    let loadStarted = DispatchSemaphore(value: 0)
    let finishLoad = DispatchSemaphore(value: 0)
    let finished = DispatchSemaphore(value: 0)
    let counterLock = NSLock()
    var inserts = 0
    var plays = 0

    Thread.detachNewThread {
        _ = gate.performIfCurrent(lease) {
            counterLock.lock()
            inserts += 1
            counterLock.unlock()
            return true
        }
        loadStarted.signal()
        finishLoad.wait()
        _ = gate.performIfCurrent(lease) {
            counterLock.lock()
            plays += 1
            counterLock.unlock()
            return true
        }
        finished.signal()
    }

    try expect(loadStarted.wait(timeout: .now() + 2) == .success, "native load did not start")
    gate.setEnabled(false)
    finishLoad.signal()
    try expect(finished.wait(timeout: .now() + 2) == .success, "native load task did not finish")
    try expect(inserts == 1, "pre-disable insert should execute exactly once")
    try expect(plays == 0, "old lease replayed after disable during load")
}

private func testDisableBeforeInsertAndAccountGenerationChange() throws {
    let gate = GuardianWorkLeaseGate()
    gate.setEnabled(true)
    guard let oldLease = gate.captureLease() else { throw TestFailure.failed("missing old-account lease") }

    gate.setEnabled(false)
    var inserts = 0
    _ = gate.performIfCurrent(oldLease) {
        inserts += 1
        return true
    }
    try expect(inserts == 0, "disabled lease inserted audio")

    gate.setEnabled(true)
    var replays = 0
    _ = gate.performIfCurrent(oldLease) {
        replays += 1
        return true
    }
    try expect(replays == 0, "prior-account lease survived generation change")
    try expect(gate.captureLease() != oldLease, "new account reused the old generation lease")
}

private func testProductionNotificationBoundary() throws {
    let publicDefines = encodedDefines([
        "ELLA_GUARDIAN_ENABLED=true",
        "ELLA_PUBLIC_BUILD=true",
    ])
    let invitationDefines = encodedDefines([
        "ELLA_GUARDIAN_ENABLED=true",
        "ELLA_ENTITLEMENT_GATE=true",
    ])
    let internalDefines = encodedDefines(["ELLA_GUARDIAN_ENABLED=true"])
    let typeOnly: [AnyHashable: Any] = ["type": "ella_notification", "urgency": "NORMAL"]
    let emergency: [AnyHashable: Any] = ["type": "ella_emergency_confirmation"]
    let ordinary: [AnyHashable: Any] = ["type": "merge_completed"]

    try expect(GuardianNotificationPolicy.isGuardianPayload(typeOnly), "type-only Ella payload was not classified")
    try expect(GuardianNotificationPolicy.isGuardianPayload(emergency), "emergency payload was not classified")

    for lifecycle in GuardianNotificationLifecycle.allCases {
        try expect(
            GuardianNotificationPolicy.disposition(
                for: typeOnly,
                lifecycle: lifecycle,
                encodedDartDefines: publicDefines
            ) == .suppress,
            "public \(lifecycle.rawValue) delivery did not fail closed"
        )
        try expect(
            GuardianNotificationPolicy.disposition(
                for: emergency,
                lifecycle: lifecycle,
                encodedDartDefines: invitationDefines
            ) == .suppress,
            "invitation \(lifecycle.rawValue) delivery did not fail closed"
        )
        try expect(
            GuardianNotificationPolicy.disposition(
                for: typeOnly,
                lifecycle: lifecycle,
                encodedDartDefines: internalDefines
            ) == .forward,
            "explicit internal Guardian delivery was suppressed"
        )
        try expect(
            GuardianNotificationPolicy.disposition(
                for: ordinary,
                lifecycle: lifecycle,
                encodedDartDefines: publicDefines
            ) == .forward,
            "ordinary \(lifecycle.rawValue) notification was broadened into Guardian scope"
        )
    }
}

private func testAccountSwitchResidueClassification() throws {
    let delivered = [
        ("guardian-type", [AnyHashable("type"): "ella_notification"], "", ""),
        ("guardian-group", [AnyHashable("type"): "ordinary"], GuardianNotificationPolicy.notificationGroupKey, ""),
        ("guardian-category", [AnyHashable("type"): "ordinary"], "", GuardianNotificationPolicy.notificationCategoryIdentifier),
        ("ordinary", [AnyHashable("type"): "merge_completed"], "", ""),
    ]

    let removed = delivered.compactMap { identifier, payload, thread, category in
        GuardianNotificationPolicy.isScopedGuardianNotification(
            userInfo: payload,
            threadIdentifier: thread,
            categoryIdentifier: category
        ) ? identifier : nil
    }
    try expect(Set(removed) == Set(["guardian-type", "guardian-group", "guardian-category"]),
               "account-switch cleanup was not Guardian-scoped")
}

@main
private enum GuardianNativePolicyTests {
    static func main() async throws {
        try await testProductionCurrentPollReleasesExactlyOnce()
        try await testProductionPollResponseCannotCrossAccountReenable()
        try await testProductionPollResponseCannotReleaseAfterDisable()
        try await testProductionPollResponseCannotReleaseAfterSameUIDReenable()
        try await testProductionPollResponseCannotCrossUIDWithoutGenerationChange()
        try await testStopCancelsRetainedProductionPollTask()
        try testDisableDuringLoadCannotReplay()
        try testDisableBeforeInsertAndAccountGenerationChange()
        try testProductionNotificationBoundary()
        try testAccountSwitchResidueClassification()
        print("Guardian native policy tests passed")
    }
}
