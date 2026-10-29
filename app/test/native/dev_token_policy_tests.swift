// Standalone tests for the DEV-ONLY timed capture token policy (PhoneMicController.swift, between
// "BEGIN DEV TOKEN POLICY" / "END DEV TOKEN POLICY"). run_dev_token_policy_tests.sh compiles that exact block
// with this file; no Xcode project, simulator or device. UNRUN until a coordinated SDK slot.
import Foundation

var failures = 0
var checks = 0
func check(_ ok: @autoclosure () -> Bool, _ name: String, line: Int = #line) {
    checks += 1
    if !ok() { failures += 1; print("FAIL line \(line): \(name)") }
}

typealias P = PhoneMicDevTokenPolicy
func testId(_ token: Int64, _ seq: Int64) -> Int64 { P.testBit | (token << 16) | (seq & 0xFFFF) }

let t0 = Date(timeIntervalSince1970: 1_793_116_800)

// Encoding parity with lib/dev/dev_mic_token.dart.
check(testId(P.tokenMask, 1) == (Int64(1) << 62) | (((Int64(1) << 30) - 1) << 16) | 1, "encoding parity")
check(testId(P.tokenMask, 1) > 0, "test ids stay positive Int64")
check(P.token(of: testId(0x2A5A5, 7)) == 0x2A5A5, "token round trip")
check(P.token(of: 42) == nil && !P.isTest(42), "ordinary id carries no token")

// Registry: one live grant, expiry, revocation is permanent, no re-grant.
do {
    let r = PhoneMicDevTokenRegistry()
    check(r.grant(5, until: t0 + 45, now: t0), "grant")
    check(!r.grant(6, until: t0 + 45, now: t0 + 1), "second live grant refused")
    check(r.isValid(5, now: t0 + 44), "valid before expiry")
    check(!r.isValid(5, now: t0 + 45), "invalid at expiry")
    check(r.grant(6, until: t0 + 100, now: t0 + 46), "new grant after the first expired")
    r.revoke(6)
    check(!r.isValid(6, now: t0 + 47), "revoked is invalid")
    check(!r.grant(6, until: t0 + 200, now: t0 + 48), "revoked token never re-granted")
    check(!r.grant(0, until: t0 + 10, now: t0) && !r.grant(P.tokenMask + 1, until: t0 + 10, now: t0), "range")
    check(!r.grant(9, until: t0, now: t0), "non-positive ttl refused")
}

// Regression 1: an unrelated ordinary session (inside any time window) is never adopted or stopped.
do {
    let ordinary: Int64 = 3
    check(P.cutoffDecision(isIdle: false, current: ordinary, token: 0x2A5A5) == .notOwned, "cutoff skips ordinary")
    check(P.startDecision(isIdle: false, current: testId(0x2A5A5, 1), incoming: ordinary, tokenValid: { _ in false }) == .allow,
          "ordinary start is stock behavior even while a test session runs")
    check(P.activationAllowed(current: ordinary, tokenValid: { _ in false }), "ordinary activation never gated")
}

// Regression 2: a late start after the deadline is refused before activation.
do {
    let r = PhoneMicDevTokenRegistry()
    _ = r.grant(11, until: t0 + 45, now: t0)
    let late = t0 + 46
    check(P.startDecision(isIdle: true, current: 0, incoming: testId(11, 1), tokenValid: { r.isValid($0, now: late) }) == .refuse("invalid"),
          "late start refused at handleStart")
    check(!P.activationAllowed(current: testId(11, 1), tokenValid: { r.isValid($0, now: late) }),
          "late activation refused at attemptBringUp (retry/resume/rebuild)")
}

// Regression 3: cancelled / account-changed (revoked) token refuses native activation.
do {
    let r = PhoneMicDevTokenRegistry()
    _ = r.grant(12, until: t0 + 45, now: t0)
    r.revoke(12)
    check(P.startDecision(isIdle: true, current: 0, incoming: testId(12, 1), tokenValid: { r.isValid($0, now: t0 + 1) }) == .refuse("invalid"),
          "revoked start refused")
    check(!P.activationAllowed(current: testId(12, 1), tokenValid: { r.isValid($0, now: t0 + 1) }), "revoked activation refused")
}

// Regression 4: an owned stall restart stays the same owned token (piggyback only onto its own token).
do {
    let r = PhoneMicDevTokenRegistry()
    _ = r.grant(13, until: t0 + 45, now: t0)
    let valid: (Int64) -> Bool = { r.isValid($0, now: t0 + 5) }
    check(P.startDecision(isIdle: false, current: testId(13, 1), incoming: testId(13, 2), tokenValid: valid) == .allow,
          "same-token restart joins its own session")
    check(P.cutoffDecision(isIdle: false, current: testId(13, 2), token: 13) == .stop, "restarted session is still owned")
    check(P.startDecision(isIdle: false, current: testId(14, 1), incoming: testId(13, 2), tokenValid: valid) == .refuse("would_adopt_other_session"),
          "never joins another token")
    r.revoke(13)
    check(P.startDecision(isIdle: false, current: testId(13, 2), incoming: testId(13, 3), tokenValid: { r.isValid($0, now: t0 + 6) }) == .refuse("invalid"),
          "restart after revocation refused")
}

// Regression 5: a cutoff can never stop a new ordinary session, nor adopt one.
do {
    check(P.cutoffDecision(isIdle: false, current: 7, token: 13) == .notOwned, "cutoff after ordinary restart is not_owned")
    check(P.cutoffDecision(isIdle: true, current: testId(13, 1), token: 13) == .nothingToStop, "idle: nothing to stop")
    check(P.cutoffDecision(isIdle: false, current: testId(15, 1), token: 13) == .notOwned, "other token not owned")
    check(P.startDecision(isIdle: false, current: 7, incoming: testId(13, 1), tokenValid: { _ in true }) == .refuse("would_adopt_other_session"),
          "a valid test start never adopts a running ordinary engine")
}

print(failures == 0 ? "PASS \(checks) checks" : "FAIL \(failures)/\(checks)")
exit(failures == 0 ? 0 : 1)
