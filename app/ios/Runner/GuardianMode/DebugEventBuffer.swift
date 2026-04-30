import Foundation
import AVFoundation

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
        NSLog("DebugEvent: [\(triggerType)] \(message) metadata=\(Self.metadataDescription(metadata))")
        GuardianPlaybackDebugReporter.shared.post(event: event)
    }

    private static func metadataDescription(_ metadata: [String: Any]) -> String {
        guard JSONSerialization.isValidJSONObject(metadata),
              let data = try? JSONSerialization.data(withJSONObject: metadata, options: [.sortedKeys]),
              let json = String(data: data, encoding: .utf8) else {
            return "\(metadata)"
        }

        return json
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

class GuardianPlaybackDebugReporter {
    static let shared = GuardianPlaybackDebugReporter()

    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3.0
        config.timeoutIntervalForResource = 3.0
        return URLSession(configuration: config)
    }()

    private init() {}

    func post(event: DebugEvent) {
        guard event.triggerType.hasPrefix("guardian_") else { return }

        let uid = UserDefaults.standard.string(forKey: "flutter.uid")
            ?? UserDefaults.standard.string(forKey: "uid")
            ?? ""
        guard !uid.isEmpty else {
            NSLog("GuardianPlaybackDebugReporter: skip \(event.triggerType), missing uid")
            return
        }

        let backendURL = GuardianModePollingService.shared.backendURL
        guard let url = URL(string: "\(backendURL)/v1/ella/guardian/playback-debug") else { return }

        var metadata = Self.sanitizedDictionary(event.metadata)
        metadata["event_name"] = canonicalEventName(event.triggerType)
        metadata["queue_item_id"] = metadata["queue_item_id"] ?? event.id

        let route = AVAudioSession.sharedInstance().currentRoute.outputs.first
        let portType = (metadata["port_type"] as? String) ?? route?.portType.rawValue ?? "none"
        let portName = (metadata["port_name"] as? String) ?? route?.portName ?? "none"
        let deviceUID = (metadata["device_uid"] as? String) ?? route?.uid ?? ""
        let queueItemId = (metadata["queue_item_id"] as? String) ?? event.id
        let traceId = (metadata["trace_id"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? queueItemId

        var body: [String: Any] = [
            "uid": uid,
            "trace_id": traceId,
            "queue_item_id": queueItemId,
            "event_name": canonicalEventName(event.triggerType),
            "status": status(for: event.triggerType, metadata: metadata),
            "port_type": portType,
            "port_name": portName,
            "device_uid": deviceUID,
            "metadata": metadata
        ]

        if let latencyMs = latencyMs(from: metadata) {
            body["latency_ms"] = latencyMs
        }

        guard JSONSerialization.isValidJSONObject(body),
              let data = try? JSONSerialization.data(withJSONObject: body) else {
            NSLog("GuardianPlaybackDebugReporter: invalid payload event=\(event.triggerType)")
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 3.0
        request.httpBody = data

        session.dataTask(with: request) { _, response, error in
            if let error = error {
                NSLog("GuardianPlaybackDebugReporter: post failed event=\(event.triggerType) error=\(error.localizedDescription)")
                return
            }

            let statusCode = (response as? HTTPURLResponse)?.statusCode ?? -1
            if statusCode < 200 || statusCode >= 300 {
                NSLog("GuardianPlaybackDebugReporter: post failed event=\(event.triggerType) http=\(statusCode)")
            }
        }.resume()
    }

    private func canonicalEventName(_ triggerType: String) -> String {
        switch triggerType {
        case "guardian_playback_playback_complete":
            return "guardian_playback_complete"
        case "guardian_playback_playback_failed":
            return "guardian_playback_failed"
        default:
            return triggerType
        }
    }

    private func status(for eventName: String, metadata: [String: Any]) -> String {
        if metadata["error"] != nil ||
            eventName.contains("failed") ||
            eventName.contains("error") ||
            eventName.contains("timeout") {
            return "failed"
        }

        return "success"
    }

    private func latencyMs(from metadata: [String: Any]) -> Int? {
        for key in ["latency_ms", "queued_latency_ms", "ready_latency_ms", "duration_ms", "elapsed_ms"] {
            if let value = metadata[key] as? Int {
                return value
            }
            if let value = metadata[key] as? Double {
                return Int(value)
            }
            if let value = metadata[key] as? String, let parsed = Int(value) {
                return parsed
            }
        }

        return nil
    }

    private static func sanitizedDictionary(_ dictionary: [String: Any]) -> [String: Any] {
        dictionary.reduce(into: [:]) { result, pair in
            result[pair.key] = sanitizedValue(pair.value)
        }
    }

    private static func sanitizedValue(_ value: Any) -> Any {
        switch value {
        case let value as String:
            return value
        case let value as Int:
            return value
        case let value as Double:
            return value
        case let value as Bool:
            return value
        case let value as [String: Any]:
            return sanitizedDictionary(value)
        case let value as [Any]:
            return value.map { sanitizedValue($0) }
        default:
            return "\(value)"
        }
    }
}
