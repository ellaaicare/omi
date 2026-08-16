import Foundation

enum AppleRemindersSyncOutcome: Equatable {
    case performed
    case alreadyCompleted
    case ambiguous
    case failed
}

/// Owns the durable local state machine around the EventKit commit boundary.
final class AppleRemindersSyncIdempotency {
    static let shared = AppleRemindersSyncIdempotency()

    private enum ReceiptState: String {
        case outboundStarted = "outbound_started"
        case completed
    }

    private let queue = DispatchQueue(label: "com.omi.apple-reminders-sync-idempotency")
    private let defaults: UserDefaults
    private let keyPrefix: String
    private var activeReceiptKeys: Set<String> = []

    init(defaults: UserDefaults = .standard, keyPrefix: String = "omi.apple-reminders-sync.") {
        self.defaults = defaults
        self.keyPrefix = keyPrefix
    }

    static func operationMarker(for idempotencyKey: String) -> String? {
        let normalizedKey = idempotencyKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalizedKey.isEmpty, normalizedKey.count <= 200 else { return nil }
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._:")
        guard normalizedKey.unicodeScalars.allSatisfy(allowed.contains) else { return nil }
        return "omi://task-sync-operation/\(normalizedKey)"
    }

    func performOnce(
        idempotencyKey: String,
        canReconcile: Bool,
        reconcile: @escaping (String, @escaping (Result<Bool, Error>) -> Void) -> Void,
        operation: @escaping (String) throws -> Void,
        completion: @escaping (AppleRemindersSyncOutcome) -> Void
    ) {
        guard let operationMarker = Self.operationMarker(for: idempotencyKey) else {
            completion(.failed)
            return
        }

        queue.async {
            let receiptKey = self.keyPrefix + idempotencyKey
            if self.isCompleted(receiptKey: receiptKey) {
                completion(.alreadyCompleted)
                return
            }
            if self.activeReceiptKeys.contains(receiptKey) {
                completion(.ambiguous)
                return
            }

            if self.defaults.string(forKey: receiptKey) == ReceiptState.outboundStarted.rawValue {
                guard canReconcile else {
                    completion(.ambiguous)
                    return
                }
                self.activeReceiptKeys.insert(receiptKey)
                reconcile(operationMarker) { result in
                    self.queue.async {
                        switch result {
                        case .success(true):
                            guard self.persist(.completed, receiptKey: receiptKey) else {
                                self.finish(.ambiguous, receiptKey: receiptKey, completion: completion)
                                return
                            }
                            self.finish(.alreadyCompleted, receiptKey: receiptKey, completion: completion)
                        case .success(false):
                            self.runOperation(
                                operationMarker: operationMarker,
                                receiptKey: receiptKey,
                                operation: operation,
                                completion: { outcome in
                                    self.finish(outcome, receiptKey: receiptKey, completion: completion)
                                }
                            )
                        case .failure:
                            self.finish(.ambiguous, receiptKey: receiptKey, completion: completion)
                        }
                    }
                }
                return
            }

            self.activeReceiptKeys.insert(receiptKey)
            guard self.persist(.outboundStarted, receiptKey: receiptKey) else {
                self.finish(.failed, receiptKey: receiptKey, completion: completion)
                return
            }
            self.runOperation(
                operationMarker: operationMarker,
                receiptKey: receiptKey,
                operation: operation,
                completion: { outcome in
                    self.finish(outcome, receiptKey: receiptKey, completion: completion)
                }
            )
        }
    }

    private func isCompleted(receiptKey: String) -> Bool {
        defaults.string(forKey: receiptKey) == ReceiptState.completed.rawValue || defaults.bool(forKey: receiptKey)
    }

    private func persist(_ state: ReceiptState, receiptKey: String) -> Bool {
        defaults.set(state.rawValue, forKey: receiptKey)
        return defaults.synchronize()
    }

    private func finish(
        _ outcome: AppleRemindersSyncOutcome,
        receiptKey: String,
        completion: (AppleRemindersSyncOutcome) -> Void
    ) {
        activeReceiptKeys.remove(receiptKey)
        completion(outcome)
    }

    private func runOperation(
        operationMarker: String,
        receiptKey: String,
        operation: (String) throws -> Void,
        completion: (AppleRemindersSyncOutcome) -> Void
    ) {
        do {
            try operation(operationMarker)
            guard persist(.completed, receiptKey: receiptKey) else {
                completion(.ambiguous)
                return
            }
            completion(.performed)
        } catch {
            // The EventKit save may already have committed. Keep the durable
            // outbound marker so a reader-authorized relaunch can reconcile it.
            completion(.ambiguous)
        }
    }
}
