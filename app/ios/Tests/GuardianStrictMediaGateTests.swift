import Foundation
@main
private enum GuardianStrictMediaGateTests {
    static func main() {
        let gate = GuardianStrictMediaGate()
        let a = GuardianStrictMediaTicket(uid: "a", epoch: "00000000-0000-4000-8000-000000000001", session: "00000000-0000-4000-8000-000000000002", generation: 2, presentation: "audio")
        precondition(gate.activate(a))
        var effects = 0
        precondition(gate.perform(a) { effects += 1 })
        precondition(!gate.close(uid: "b", epoch: a.epoch, generation: 3))
        precondition(!gate.close(uid: "a", epoch: nil, generation: 3))
        precondition(gate.close(uid: "a", epoch: a.epoch, generation: 3))
        precondition(!gate.perform(a) { effects += 1 })
        precondition(!gate.activate(a))
        precondition(gate.close(uid: "a", epoch: nil, generation: 4))
        print("PASS exact owner Stop fences pending media and rejects stale activation")
        let resumed = GuardianStrictMediaTicket(uid: "a", epoch: a.epoch, session: "00000000-0000-4000-8000-000000000003", generation: 4, presentation: "text_only")
        precondition(gate.activate(resumed))
        precondition(!gate.perform(a) { effects += 1 })
        precondition(gate.perform(resumed) { effects += 1 })
        gate.retire()
        precondition(!gate.activate(resumed))
        let fresh = GuardianStrictMediaTicket(uid: "a", epoch: "00000000-0000-4000-8000-000000000004", session: "00000000-0000-4000-8000-000000000005", generation: 1, presentation: "audio")
        precondition(gate.activate(fresh))
        precondition(!gate.close(uid: "a", epoch: a.epoch, generation: 99))
        precondition(gate.perform(fresh) { effects += 1 })
        precondition(effects == 3)
        print("PASS same-owner replacement epoch rejects retired callbacks without silencing fresh media")
    }
}
