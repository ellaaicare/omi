// Standalone tests for the DEV-ONLY timed capture card policy (SiriIntents.swift, between
// "BEGIN DEV CARD POLICY" / "END DEV CARD POLICY"). run_dev_card_policy_tests.sh compiles that exact block
// with this file; no Xcode project, simulator or device. UNRUN until a coordinated SDK slot.
import Foundation

var failures = 0
var checks = 0
func check(_ ok: @autoclosure () -> Bool, _ name: String, line: Int = #line) {
    checks += 1
    if !ok() { failures += 1; print("FAIL line \(line): \(name)") }
}

typealias P = BaseTimedCaptureCardPolicy

// v4.6 token 461300147: Dart answered recording:20, the card never started.
check(P.afterStart(outcome: "recording:20", card: "missing") == .cutoff("live_activity_missing"), "missing card cuts")
check(P.afterStart(outcome: "recording:20", card: "wait_cancelled") == .cutoff("live_activity_wait_cancelled"),
      "cancelled wait cuts")
check(P.afterStart(outcome: "recording:20", card: "started") == .keep, "started card keeps recording")
// Nothing started: never stop anything.
for refusal in ["not_armed", "dart_no_answer", "timeout", "start_timeout", "error x", ""] {
    for card in ["missing", "started", "wait_cancelled"] {
        check(P.afterStart(outcome: refusal, card: card) == .keep, "refusal \(refusal)/\(card) keeps")
    }
}
// Unknown card states are treated as missing (fail closed).
check(P.afterStart(outcome: "recording:5", card: "weird") == .cutoff("live_activity_missing"), "unknown card cuts")

print("\(checks - failures)/\(checks) passed")
exit(failures == 0 ? 0 : 1)
