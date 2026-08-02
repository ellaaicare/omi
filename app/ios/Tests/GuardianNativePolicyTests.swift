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
    static func main() throws {
        try testDisableDuringLoadCannotReplay()
        try testDisableBeforeInsertAndAccountGenerationChange()
        try testProductionNotificationBoundary()
        try testAccountSwitchResidueClassification()
        print("Guardian native policy tests passed")
    }
}
