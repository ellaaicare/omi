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
    private let maxTotalBytes = 512 * 1024
    private let maxPartitionFiles = 4
    private let maxFileAge: TimeInterval = 7 * 24 * 60 * 60
    private let queue = DispatchQueue(label: "com.ellaaicare.ella.diagnostic-events")
    private let allowedKeys: Set<String> = [
        "schema_version", "event_id", "diagnostic_session_id", "capture_attempt_id",
        "account_binding_fingerprint", "authority_generation", "source_revision",
        "layer", "event_name", "outcome", "retry_class", "client_sequence",
        "client_monotonic_ms", "client_utc_time", "opaque_resource_id", "firmware",
        "codec", "stable_failure_code", "expected_next_event", "deadline_ms",
        "safe_counters", "projection_revision", "action_revision"
    ]
    private let requiredStringKeys: Set<String> = [
        "schema_version", "event_id", "diagnostic_session_id", "capture_attempt_id",
        "account_binding_fingerprint", "source_revision", "layer", "event_name",
        "outcome", "retry_class", "client_utc_time"
    ]
    private let requiredIntegerKeys: Set<String> = [
        "authority_generation", "client_sequence", "client_monotonic_ms"
    ]
    private let optionalStringKeys: Set<String> = [
        "opaque_resource_id", "firmware", "codec", "stable_failure_code", "expected_next_event"
    ]
    private let optionalIntegerKeys: Set<String> = [
        "deadline_ms", "projection_revision", "action_revision"
    ]
    private let allowedLayers: Set<String> = [
        "account_binding", "ble_transport", "physical_audio", "server_capture", "publication", "presentation"
    ]
    private let allowedOutcomes: Set<String> = ["started", "succeeded", "failed", "cancelled", "unknown"]
    private let allowedRetryClasses: Set<String> = ["never", "user_action", "bounded_automatic", "operator_only"]
    private let allowedCounterKeys: Set<String> = ["frames", "bytes", "retry_number", "rssi_bucket", "queue_age_seconds"]
    private let clientTimestampFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    private let fallbackTimestampFormatter = ISO8601DateFormatter()

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
                try self.enforceGlobalRetention(preservingFingerprint: fingerprint)
                DispatchQueue.main.async { completion(nil) }
            } catch {
                DispatchQueue.main.async { completion(error) }
            }
        }
    }

    func clearAll(completion: @escaping (Error?) -> Void) {
        queue.async {
            do {
                let directory = try self.diagnosticsDirectory(create: false)
                if FileManager.default.fileExists(atPath: directory.path) {
                    try FileManager.default.removeItem(at: directory)
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
        for key in requiredStringKeys {
            guard let value = event[key] as? String, !value.isEmpty else { throw StoreError.invalidEvent }
        }
        for key in requiredIntegerKeys {
            guard let value = event[key] as? Int, value >= 0 else { throw StoreError.invalidEvent }
        }
        for key in optionalStringKeys where event[key] != nil {
            guard event[key] is String else { throw StoreError.invalidEvent }
        }
        for key in optionalIntegerKeys where event[key] != nil {
            guard let value = event[key] as? Int, value >= 0 else { throw StoreError.invalidEvent }
        }
        guard let layer = event["layer"] as? String, allowedLayers.contains(layer),
              let outcome = event["outcome"] as? String, allowedOutcomes.contains(outcome),
              let retryClass = event["retry_class"] as? String, allowedRetryClasses.contains(retryClass),
              let eventName = event["event_name"] as? String,
              isSafeName(eventName),
              isOpaqueIdentifier(event["event_id"] as! String),
              isOpaqueIdentifier(event["diagnostic_session_id"] as! String),
              isOpaqueIdentifier(event["capture_attempt_id"] as! String),
              isValidClientTimestamp(event["client_utc_time"] as! String) else {
            throw StoreError.invalidEvent
        }
        if let expectedNextEvent = event["expected_next_event"] as? String, !isSafeName(expectedNextEvent) {
            throw StoreError.invalidEvent
        }
        if let counters = event["safe_counters"] {
            guard let dictionary = counters as? [String: Any] else { throw StoreError.invalidEvent }
            for (key, rawValue) in dictionary {
                guard allowedCounterKeys.contains(key), let value = rawValue as? Int, value >= 0 else {
                    throw StoreError.invalidEvent
                }
            }
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

    private func isOpaqueIdentifier(_ value: String) -> Bool {
        value.range(of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", options: .regularExpression) != nil
    }

    private func isSafeName(_ value: String) -> Bool {
        value.range(of: "^[a-z][a-z0-9_]{0,63}$", options: .regularExpression) != nil
    }

    private func isValidClientTimestamp(_ value: String) -> Bool {
        clientTimestampFormatter.date(from: value) != nil || fallbackTimestampFormatter.date(from: value) != nil
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
        return try diagnosticsDirectory(create: true)
            .appendingPathComponent("events-v1-\(fingerprint).json", isDirectory: false)
    }

    private func enforceGlobalRetention(preservingFingerprint: String) throws {
        let directory = try diagnosticsDirectory(create: true)
        let preservingURL = try fileURL(fingerprint: preservingFingerprint)
        let resourceKeys: Set<URLResourceKey> = [
            .isRegularFileKey,
            .contentModificationDateKey,
            .fileSizeKey
        ]
        let fileManager = FileManager.default
        var files = try fileManager.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: Array(resourceKeys),
            options: [.skipsHiddenFiles]
        ).filter {
            $0.lastPathComponent.hasPrefix("events-v1-") && $0.pathExtension == "json"
        }
        let expiry = Date().addingTimeInterval(-maxFileAge)

        for file in files where file != preservingURL {
            let values = try file.resourceValues(forKeys: resourceKeys)
            if values.isRegularFile != true || (values.contentModificationDate ?? .distantPast) < expiry {
                try fileManager.removeItem(at: file)
            }
        }

        files = try fileManager.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: Array(resourceKeys),
            options: [.skipsHiddenFiles]
        ).filter {
            $0.lastPathComponent.hasPrefix("events-v1-") && $0.pathExtension == "json"
        }
        files.sort { left, right in
            if left == preservingURL { return true }
            if right == preservingURL { return false }
            let leftDate = (try? left.resourceValues(forKeys: resourceKeys).contentModificationDate) ?? .distantPast
            let rightDate = (try? right.resourceValues(forKeys: resourceKeys).contentModificationDate) ?? .distantPast
            return leftDate > rightDate
        }

        var retainedFiles = 0
        var retainedBytes = 0
        for file in files {
            let values = try file.resourceValues(forKeys: resourceKeys)
            guard values.isRegularFile == true else {
                try fileManager.removeItem(at: file)
                continue
            }
            let fileBytes = max(0, values.fileSize ?? 0)
            let withinBudget = retainedFiles < maxPartitionFiles && retainedBytes + fileBytes <= maxTotalBytes
            if file == preservingURL || withinBudget {
                retainedFiles += 1
                retainedBytes += fileBytes
            } else {
                try fileManager.removeItem(at: file)
            }
        }
    }

    private func diagnosticsDirectory(create: Bool) throws -> URL {
        let root = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: create
        ).appendingPathComponent("EllaDiagnostics", isDirectory: true)
        if create {
            try FileManager.default.createDirectory(
                at: root,
                withIntermediateDirectories: true,
                attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]
            )
        }
        return root
    }

    private enum StoreError: String, LocalizedError {
        case invalidEvent
        case invalidFingerprint
        case prohibitedValue
        case eventTooLarge

        var errorDescription: String? { rawValue }
    }
}
