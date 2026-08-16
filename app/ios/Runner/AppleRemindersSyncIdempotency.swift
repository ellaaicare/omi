import Foundation

enum AppleRemindersSyncOutcome: Equatable {
    case performed
    case alreadyCompleted
    case failed
}

/// Serializes and durably records Apple Reminder creation by the stable
/// server-side task-sync operation identity.
final class AppleRemindersSyncIdempotency {
    static let shared = AppleRemindersSyncIdempotency()

    private let lock = NSLock()
    private let defaults: UserDefaults
    private let keyPrefix: String

    init(defaults: UserDefaults = .standard, keyPrefix: String = "omi.apple-reminders-sync.") {
        self.defaults = defaults
        self.keyPrefix = keyPrefix
    }

    func performOnce(idempotencyKey: String, operation: () throws -> Void) -> AppleRemindersSyncOutcome {
        let normalizedKey = idempotencyKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalizedKey.isEmpty else { return .failed }

        lock.lock()
        defer { lock.unlock() }

        let receiptKey = keyPrefix + normalizedKey
        if defaults.bool(forKey: receiptKey) {
            return .alreadyCompleted
        }

        do {
            try operation()
            defaults.set(true, forKey: receiptKey)
            return .performed
        } catch {
            return .failed
        }
    }
}
