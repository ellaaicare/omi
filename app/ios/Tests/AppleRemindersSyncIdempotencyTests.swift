import Foundation

private enum TestFailure: Error {
    case failed(String)
}

private struct StoredReminder {
    let request: AppleReminderSyncRequest
}

private final class FakeAppleRemindersSink: AppleRemindersSyncSink {
    var reminders: [StoredReminder] = []
    var createAttempts = 0
    var failBeforeCommitOnce = false
    var failAfterCommitOnce = false

    func reminderExists(
        operationMarker: String,
        completion: @escaping (Result<Bool, Error>) -> Void
    ) {
        completion(
            .success(
                reminders.contains {
                    $0.request.operationMarker == operationMarker
                }
            )
        )
    }

    func createReminder(_ request: AppleReminderSyncRequest) throws {
        createAttempts += 1
        if failBeforeCommitOnce {
            failBeforeCommitOnce = false
            throw TestFailure.failed("injected failure before EventKit commit")
        }
        reminders.append(StoredReminder(request: request))
        if failAfterCommitOnce {
            failAfterCommitOnce = false
            throw TestFailure.failed("injected process death after EventKit commit")
        }
    }
}

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TestFailure.failed(message) }
}

private func makeDefaults() throws -> (UserDefaults, String) {
    let suite = "AppleRemindersSyncIdempotencyTests.\(UUID().uuidString)"
    guard let defaults = UserDefaults(suiteName: suite) else {
        throw TestFailure.failed("could not create isolated defaults")
    }
    defaults.removePersistentDomain(forName: suite)
    return (defaults, suite)
}

private func syncAndWait(
    service: AppleRemindersSyncService,
    idempotencyKey: String,
    canReconcile: Bool = true
) throws -> AppleRemindersSyncOutcome {
    let completed = DispatchSemaphore(value: 0)
    var received: AppleRemindersSyncOutcome?
    service.sync(
        idempotencyKey: idempotencyKey,
        title: "Create exactly once",
        dueDate: nil,
        canReconcile: canReconcile
    ) { outcome in
        received = outcome
        completed.signal()
    }
    guard completed.wait(timeout: .now() + 2) == .success, let received else {
        throw TestFailure.failed("production sync service did not complete")
    }
    return received
}

private func makeService(defaults: UserDefaults, sink: FakeAppleRemindersSink) -> AppleRemindersSyncService {
    AppleRemindersSyncService(
        idempotency: AppleRemindersSyncIdempotency(defaults: defaults),
        sink: sink
    )
}

private func testProductionServiceRepairsCrashWindowAcrossRelaunch() throws {
    let (defaults, suite) = try makeDefaults()
    defer { defaults.removePersistentDomain(forName: suite) }
    let sink = FakeAppleRemindersSink()
    sink.failAfterCommitOnce = true
    let key = "stable-finalization-operation"

    let crashed = try syncAndWait(service: makeService(defaults: defaults, sink: sink), idempotencyKey: key)
    try expect(crashed == .ambiguous, "commit-window process death was not fail-closed")
    try expect(sink.reminders.count == 1, "crash-window fake did not commit one reminder")
    guard let marker = AppleRemindersSyncIdempotency.operationMarker(for: key) else {
        throw TestFailure.failed("stable operation marker was rejected")
    }
    try expect(
        sink.reminders[0].request.operationMarker == marker,
        "EventKit reminder record did not contain the marker before commit"
    )

    let relaunched = try syncAndWait(
        service: makeService(defaults: defaults, sink: sink),
        idempotencyKey: key
    )
    try expect(relaunched == .alreadyCompleted, "relaunch did not reconcile the committed reminder")
    try expect(sink.reminders.count == 1, "relaunch created a duplicate Apple Reminder")

    let repairedReceipt = try syncAndWait(
        service: makeService(defaults: defaults, sink: sink),
        idempotencyKey: key
    )
    try expect(repairedReceipt == .alreadyCompleted, "relaunch did not repair the local receipt")
    try expect(sink.createAttempts == 1, "durable receipt replay reached EventKit create")
}

private func testWriteOnlyAuthorityLeavesCrashWindowAmbiguous() throws {
    let (defaults, suite) = try makeDefaults()
    defer { defaults.removePersistentDomain(forName: suite) }
    let sink = FakeAppleRemindersSink()
    sink.failAfterCommitOnce = true
    let key = "write-only-operation"

    let crashed = try syncAndWait(
        service: makeService(defaults: defaults, sink: sink),
        idempotencyKey: key,
        canReconcile: false
    )
    let relaunched = try syncAndWait(
        service: makeService(defaults: defaults, sink: sink),
        idempotencyKey: key,
        canReconcile: false
    )

    try expect(crashed == .ambiguous, "write-only crash window was not ambiguous")
    try expect(relaunched == .ambiguous, "write-only relaunch exceeded the user's read authority")
    try expect(sink.reminders.count == 1, "write-only relaunch created a duplicate reminder")
    try expect(sink.createAttempts == 1, "write-only relaunch retried an ambiguous EventKit commit")
}

private func testPreCommitFailureReconcilesThenRetries() throws {
    let (defaults, suite) = try makeDefaults()
    defer { defaults.removePersistentDomain(forName: suite) }
    let sink = FakeAppleRemindersSink()
    sink.failBeforeCommitOnce = true
    let key = "retryable-operation"

    let failed = try syncAndWait(service: makeService(defaults: defaults, sink: sink), idempotencyKey: key)
    let retried = try syncAndWait(service: makeService(defaults: defaults, sink: sink), idempotencyKey: key)

    try expect(failed == .ambiguous, "pre-commit failure discarded the durable outbound state")
    try expect(retried == .performed, "reconciled pre-commit failure was not retryable")
    try expect(sink.reminders.count == 1, "pre-commit retry created an unexpected reminder count")
    try expect(sink.createAttempts == 2, "pre-commit retry ran an unexpected number of attempts")
}

@main
private enum AppleRemindersSyncIdempotencyTests {
    static func main() throws {
        try testProductionServiceRepairsCrashWindowAcrossRelaunch()
        try testWriteOnlyAuthorityLeavesCrashWindowAmbiguous()
        try testPreCommitFailureReconcilesThenRetries()
        print("Apple Reminders production sync crash-window tests passed")
    }
}
