import Foundation
import UIKit

struct DebugEvent {
    let id: String
    let triggerType: String
    let message: String
    let metadata: [String: Any]
    let receivedAt: Date
}

class DebugEventBuffer {
    static let shared = DebugEventBuffer()
    private init() {}

    static let didUpdateNotification = Notification.Name("DebugEventBufferUpdated")
    private let maxItems = 100
    private let lock = NSLock()
    private(set) var events: [DebugEvent] = []

    func add(id: String?, triggerType: String, message: String, metadata: [String: Any]) {
        let event = DebugEvent(
            id: id ?? UUID().uuidString,
            triggerType: triggerType,
            message: message,
            metadata: metadata,
            receivedAt: Date()
        )
        lock.lock()
        events.insert(event, at: 0)
        if events.count > maxItems { events = Array(events.prefix(maxItems)) }
        lock.unlock()
        NotificationCenter.default.post(name: DebugEventBuffer.didUpdateNotification, object: nil)
        NSLog("DebugEvent: [\(triggerType)] \(message)")
    }

    /// Serialize for Flutter MethodChannel
    func asFlutterList() -> [[String: Any]] {
        lock.lock()
        defer { lock.unlock() }
        let formatter = ISO8601DateFormatter()
        return events.map { e in [
            "id": e.id,
            "trigger_type": e.triggerType,
            "message": e.message,
            "received_at": formatter.string(from: e.receivedAt),
            "metadata": e.metadata
        ]}
    }

    func clear() {
        lock.lock(); events.removeAll(); lock.unlock()
        NotificationCenter.default.post(name: DebugEventBuffer.didUpdateNotification, object: nil)
    }
}

/// A content-free, account-partitioned incident journal. The file is encrypted
/// by iOS Data Protection, excluded from backups, and never uploaded directly.
final class EllaDiagnosticEventStore {
    static let shared = EllaDiagnosticEventStore()

    private let maxEvents = 200
    private let maxBytes = 256 * 1024
    private let queue = DispatchQueue(label: "com.ellaaicare.ella.diagnostic-events")
    private let allowedKeys: Set<String> = [
        "schema_version", "event_id", "diagnostic_session_id", "capture_attempt_id",
        "account_binding_fingerprint", "authority_generation", "source_revision",
        "layer", "event_name", "outcome", "retry_class", "client_sequence",
        "client_monotonic_ms", "client_utc_time", "opaque_resource_id", "firmware",
        "codec", "stable_failure_code", "expected_next_event", "deadline_ms",
        "safe_counters", "projection_revision", "action_revision"
    ]

    private init() {}

    func append(_ event: [String: Any], completion: @escaping (Error?) -> Void) {
        queue.async {
            do {
                try self.validate(event)
                let fingerprint = event["account_binding_fingerprint"] as! String
                var enriched = event
                enriched["app_version"] = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown"
                enriched["app_build"] = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "unknown"
                enriched["ios_version"] = UIDevice.current.systemVersion
                enriched["device_model"] = UIDevice.current.model

                var events = try self.readEvents(fingerprint: fingerprint)
                events.append(enriched)
                if events.count > self.maxEvents {
                    events.removeFirst(events.count - self.maxEvents)
                }
                while try JSONSerialization.data(withJSONObject: events).count > self.maxBytes && events.count > 1 {
                    events.removeFirst()
                }
                let data = try JSONSerialization.data(withJSONObject: events)
                guard data.count <= self.maxBytes else {
                    throw StoreError.eventTooLarge
                }
                try self.write(data, fingerprint: fingerprint)
                DispatchQueue.main.async { completion(nil) }
            } catch {
                DispatchQueue.main.async { completion(error) }
            }
        }
    }

    func events(fingerprint: String, completion: @escaping ([[String: Any]]?, Error?) -> Void) {
        queue.async {
            do {
                try self.validateFingerprint(fingerprint)
                let events = try self.readEvents(fingerprint: fingerprint)
                DispatchQueue.main.async { completion(events, nil) }
            } catch {
                DispatchQueue.main.async { completion(nil, error) }
            }
        }
    }

    func clear(fingerprint: String, completion: @escaping (Error?) -> Void) {
        queue.async {
            do {
                try self.validateFingerprint(fingerprint)
                let url = try self.fileURL(fingerprint: fingerprint)
                if FileManager.default.fileExists(atPath: url.path) {
                    try FileManager.default.removeItem(at: url)
                }
                DispatchQueue.main.async { completion(nil) }
            } catch {
                DispatchQueue.main.async { completion(error) }
            }
        }
    }

    private func validate(_ event: [String: Any]) throws {
        guard Set(event.keys).isSubset(of: allowedKeys),
              event["schema_version"] as? String == "ella.diagnostic_event.v1",
              let fingerprint = event["account_binding_fingerprint"] as? String else {
            throw StoreError.invalidEvent
        }
        try validateFingerprint(fingerprint)
        guard JSONSerialization.isValidJSONObject(event) else {
            throw StoreError.invalidEvent
        }
        try validateValue(event)
    }

    private func validateValue(_ value: Any) throws {
        if let string = value as? String {
            let lower = string.lowercased()
            if string.count > 256 || lower.contains("://") || lower.contains("bearer ") || lower.contains("?token=") {
                throw StoreError.prohibitedValue
            }
            return
        }
        if let dictionary = value as? [String: Any] {
            for (key, child) in dictionary {
                if key.lowercased().contains("uid") || key.lowercased().contains("token") || key.lowercased().contains("url") {
                    throw StoreError.prohibitedValue
                }
                try validateValue(child)
            }
            return
        }
        if let array = value as? [Any] {
            for child in array { try validateValue(child) }
        }
    }

    private func validateFingerprint(_ fingerprint: String) throws {
        let pattern = "^[a-f0-9]{64}$"
        guard fingerprint.range(of: pattern, options: .regularExpression) != nil else {
            throw StoreError.invalidFingerprint
        }
    }

    private func readEvents(fingerprint: String) throws -> [[String: Any]] {
        let url = try fileURL(fingerprint: fingerprint)
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        let data = try Data(contentsOf: url)
        guard data.count <= maxBytes,
              let events = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
            throw StoreError.invalidEvent
        }
        return Array(events.suffix(maxEvents))
    }

    private func write(_ data: Data, fingerprint: String) throws {
        let url = try fileURL(fingerprint: fingerprint)
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes(
            [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
            ofItemAtPath: url.path
        )
        var resourceValues = URLResourceValues()
        resourceValues.isExcludedFromBackup = true
        var mutableURL = url
        try mutableURL.setResourceValues(resourceValues)
    }

    private func fileURL(fingerprint: String) throws -> URL {
        try validateFingerprint(fingerprint)
        let root = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ).appendingPathComponent("EllaDiagnostics", isDirectory: true)
        try FileManager.default.createDirectory(
            at: root,
            withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]
        )
        return root.appendingPathComponent("events-v1-\(fingerprint.prefix(24)).json", isDirectory: false)
    }

    private enum StoreError: String, LocalizedError {
        case invalidEvent
        case invalidFingerprint
        case prohibitedValue
        case eventTooLarge

        var errorDescription: String? { rawValue }
    }
}
