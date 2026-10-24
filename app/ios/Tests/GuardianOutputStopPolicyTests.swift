import Foundation
@main
private enum GuardianOutputStopPolicyTests {
    static func main() {
        let gate = GuardianWorkLeaseGate()
        gate.configure(enabled: true, uid: "synthetic-a")
        let old = gate.captureLease()!
        let fresh = gate.retireOutput(old)!
        precondition(gate.isEnabled && gate.currentUID == "synthetic-a")
        precondition(!gate.isCurrent(old) && !gate.isCurrent(fresh) && gate.isExactOwnerLease(fresh))
        precondition(gate.retireOutput(old) == nil)
        precondition(gate.captureLease() == nil)
        gate.configure(enabled: true, uid: "synthetic-a")
        precondition(gate.captureLease() == nil)
        print("PASS Stop preserves configured mode and invalidates stale output/repeated receipt")
        precondition(gate.invalidateIfUIDChanged("synthetic-b"))
        gate.configure(enabled: true, uid: "synthetic-b")
        precondition(gate.captureLease() != nil)
        precondition(gate.retireOutput(fresh) == nil)
        gate.configure(enabled: true, uid: "synthetic-a")
        precondition(gate.retireOutput(fresh) == nil)
        precondition(gate.captureLease() != old)
        print("PASS account ABA rejects retired output without disabling replacement owner")
        let name = "ella.guardian.stop.policy.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let barrier = GuardianOutputStopBarrier(defaults: defaults)
        barrier.record(uid: "synthetic-a", operation: "stop-a")
        barrier.record(uid: "synthetic-b", operation: "stop-b")
        let reopened = GuardianOutputStopBarrier(defaults: defaults)
        precondition(reopened.operation(for: "synthetic-a") == "stop-a")
        precondition(reopened.operation(for: "synthetic-b") == "stop-b")
        print("PASS backlog barrier persists across relaunch and owner ABA")
    }
}
