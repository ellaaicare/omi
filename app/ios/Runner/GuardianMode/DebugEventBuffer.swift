import Foundation

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
