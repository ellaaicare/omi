import Foundation

private enum TestFailure: Error {
    case failed(String)
}

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TestFailure.failed(message) }
}

private func makeGate() throws -> (AppleRemindersSyncIdempotency, UserDefaults) {
    let suite = "AppleRemindersSyncIdempotencyTests.\(UUID().uuidString)"
    guard let defaults = UserDefaults(suiteName: suite) else {
        throw TestFailure.failed("could not create isolated defaults")
    }
    defaults.removePersistentDomain(forName: suite)
    return (AppleRemindersSyncIdempotency(defaults: defaults), defaults)
}

private func testOldWorkerAfterSuccessfulSuccessorCreatesExactlyOnce() throws {
    let (gate, _) = try makeGate()
    var creations = 0

    let successor = gate.performOnce(idempotencyKey: "stable-finalization-operation") {
        creations += 1
    }
    let oldWorker = gate.performOnce(idempotencyKey: "stable-finalization-operation") {
        creations += 1
    }

    try expect(successor == .performed, "successor did not perform the reminder creation")
    try expect(oldWorker == .alreadyCompleted, "old worker did not observe the durable receipt")
    try expect(creations == 1, "replay created more than one Apple Reminder")
}

private func testFailedCreationRemainsRetryable() throws {
    let (gate, _) = try makeGate()
    var attempts = 0

    let failed = gate.performOnce(idempotencyKey: "retryable-operation") {
        attempts += 1
        throw TestFailure.failed("injected save failure")
    }
    let retried = gate.performOnce(idempotencyKey: "retryable-operation") {
        attempts += 1
    }

    try expect(failed == .failed, "failed reminder save was recorded as complete")
    try expect(retried == .performed, "failed reminder save could not be retried")
    try expect(attempts == 2, "retryable reminder operation ran an unexpected number of times")
}

@main
private enum AppleRemindersSyncIdempotencyTests {
    static func main() throws {
        try testOldWorkerAfterSuccessfulSuccessorCreatesExactlyOnce()
        try testFailedCreationRemainsRetryable()
        print("Apple Reminders sync idempotency tests passed")
    }
}
