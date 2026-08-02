import Foundation

final class GuardianModeAvailability {
    static let shared = GuardianModeAvailability()

    private let leaseGate = GuardianWorkLeaseGate()

    private init() {}

    var isEnabled: Bool {
        leaseGate.isEnabled
    }

    func setEnabled(_ enabled: Bool) {
        leaseGate.setEnabled(enabled)
    }

    func captureLease() -> GuardianWorkLease? {
        leaseGate.captureLease()
    }

    func isCurrent(_ lease: GuardianWorkLease) -> Bool {
        leaseGate.isCurrent(lease)
    }

    func performIfCurrent(_ lease: GuardianWorkLease, _ sideEffect: () -> Bool) -> Bool {
        leaseGate.performIfCurrent(lease, sideEffect)
    }
}

struct GuardianWorkLease: Equatable {
    fileprivate let generation: UInt64
}

/// A generation lease whose validation and side effect execute under one lock.
/// Once `setEnabled(false)` returns, no older lease can begin another side effect.
final class GuardianWorkLeaseGate {
    private let lock = NSLock()
    private var enabled = false
    private var generation: UInt64 = 0

    var isEnabled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return enabled
    }

    @discardableResult
    func setEnabled(_ enabled: Bool) -> UInt64 {
        lock.lock()
        generation &+= 1
        self.enabled = enabled
        let currentGeneration = generation
        lock.unlock()
        return currentGeneration
    }

    func captureLease() -> GuardianWorkLease? {
        lock.lock()
        defer { lock.unlock() }
        guard enabled else { return nil }
        return GuardianWorkLease(generation: generation)
    }

    func isCurrent(_ lease: GuardianWorkLease) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return enabled && generation == lease.generation
    }

    @discardableResult
    func performIfCurrent(_ lease: GuardianWorkLease, _ sideEffect: () -> Bool) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard enabled && generation == lease.generation else { return false }
        return sideEffect()
    }
}

enum GuardianNotificationLifecycle: String, CaseIterable {
    case foreground
    case background
    case terminated
}

enum GuardianNotificationDisposition: Equatable {
    case forward
    case suppress
}

/// Native policy for data-only Guardian/Ella pushes. Public and invitation
/// builds never forward these payloads to Flutter, including at cold start.
enum GuardianNotificationPolicy {
    static let notificationGroupKey = "ella.guardian.notifications"
    static let notificationCategoryIdentifier = "ELLA_GUARDIAN"

    private static let guardianTypes: Set<String> = [
        "ella_notification",
        "ella_emergency_confirmation",
        "guardian_notification",
        "guardian_alert",
        "emergency",
    ]

    static func isGuardianPayload(_ userInfo: [AnyHashable: Any]) -> Bool {
        let payload = flattenedPayload(userInfo)
        if boolValue(payload["ella_guardian_audio"]) { return true }

        let type = normalized(payload["type"])
        if guardianTypes.contains(type) { return true }
        if normalized(payload["urgency"]) == "emergency" { return true }

        let markerKeys = [
            "type",
            "subtype",
            "notification_type",
            "trigger_type",
            "guardian_mode",
            "category",
            "source",
            "navigate_to",
        ]
        let values = markerKeys.map { normalized(payload[$0]) }.joined(separator: " ")
        return ["guardian", "whisper", "wake_word", "caregiver", "emergency"]
            .contains(where: values.contains)
    }

    static func disposition(
        for userInfo: [AnyHashable: Any],
        lifecycle: GuardianNotificationLifecycle,
        encodedDartDefines: String?
    ) -> GuardianNotificationDisposition {
        _ = lifecycle // The boundary is intentionally identical in every lifecycle.
        guard isGuardianPayload(userInfo) else { return .forward }
        return allowsGuardianDelivery(encodedDartDefines: encodedDartDefines) ? .forward : .suppress
    }

    static func allowsGuardianDelivery(encodedDartDefines: String?) -> Bool {
        guard let defines = decodeDartDefines(encodedDartDefines) else { return false }
        guard defines["ELLA_GUARDIAN_ENABLED"] == "true" else { return false }
        guard defines["ELLA_PUBLIC_BUILD"] != "true" else { return false }
        guard defines["ELLA_ENTITLEMENT_GATE"] != "true" else { return false }
        return true
    }

    static func isScopedGuardianNotification(
        userInfo: [AnyHashable: Any],
        threadIdentifier: String,
        categoryIdentifier: String
    ) -> Bool {
        threadIdentifier == notificationGroupKey ||
            categoryIdentifier == notificationCategoryIdentifier ||
            isGuardianPayload(userInfo)
    }

    private static func decodeDartDefines(_ encoded: String?) -> [String: String]? {
        guard let encoded, !encoded.isEmpty, !encoded.contains("$(") else { return [:] }
        var result: [String: String] = [:]
        for value in encoded.split(separator: ",", omittingEmptySubsequences: true) {
            guard let data = Data(base64Encoded: String(value)),
                  let decoded = String(data: data, encoding: .utf8),
                  let separator = decoded.firstIndex(of: "=") else {
                return nil
            }
            let key = String(decoded[..<separator])
            let rawValue = String(decoded[decoded.index(after: separator)...]).lowercased()
            result[key] = rawValue
        }
        return result
    }

    private static func flattenedPayload(_ userInfo: [AnyHashable: Any]) -> [String: Any] {
        var flattened: [String: Any] = [:]
        for (key, value) in userInfo {
            flattened[String(describing: key)] = value
        }
        if let nested = userInfo["data"] as? [String: Any] {
            flattened.merge(nested) { current, _ in current }
        }
        return flattened
    }

    private static func normalized(_ value: Any?) -> String {
        guard let value else { return "" }
        return String(describing: value).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    private static func boolValue(_ value: Any?) -> Bool {
        value as? Bool == true || normalized(value) == "true"
    }
}
