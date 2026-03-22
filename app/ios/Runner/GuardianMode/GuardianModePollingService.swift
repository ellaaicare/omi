import Foundation

class GuardianModePollingService {
    static let shared = GuardianModePollingService()

    private init() {}

    private var pollTimer: DispatchSourceTimer?

    private let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 10.0
        config.timeoutIntervalForResource = 10.0
        return URLSession(configuration: config)
    }()

    var pollInterval: TimeInterval = 3.0
    var backendURL: String = "https://api.ella-ai-care.com"

    struct PollResponse: Codable {
        let url: String?
        let id: String?
        let priority: String?
        let triggerType: String?
        let message: String?
        let metadata: AnyCodableDict?

        enum CodingKeys: String, CodingKey {
            case url, id, priority, message, metadata
            case triggerType = "trigger_type"
        }
    }

    // Minimal JSON dictionary decoder for metadata field
    struct AnyCodableDict: Codable {
        var dict: [String: Any] = [:]

        init(from decoder: Decoder) throws {
            let container = try decoder.singleValueContainer()
            if let raw = try? container.decode([String: AnyCodableValue].self) {
                dict = raw.mapValues { $0.value }
            }
        }

        func encode(to encoder: Encoder) throws {
            var container = encoder.singleValueContainer()
            try container.encode(dict.mapValues { AnyCodableValue($0) })
        }
    }

    struct AnyCodableValue: Codable {
        let value: Any

        init(_ value: Any) { self.value = value }

        init(from decoder: Decoder) throws {
            let c = try decoder.singleValueContainer()
            if let v = try? c.decode(Bool.self)   { value = v; return }
            if let v = try? c.decode(Int.self)    { value = v; return }
            if let v = try? c.decode(Double.self) { value = v; return }
            if let v = try? c.decode(String.self) { value = v; return }
            value = ""
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.singleValueContainer()
            switch value {
            case let v as Bool:   try c.encode(v)
            case let v as Int:    try c.encode(v)
            case let v as Double: try c.encode(v)
            case let v as String: try c.encode(v)
            default: try c.encode("")
            }
        }
    }

    private var isPolling = false
    private var consecutiveErrors: Int = 0

    // MARK: - Public Methods

    func startPolling() {
        guard !isPolling else {
            NSLog("GuardianPolling: Already polling")
            return
        }

        isPolling = true
        consecutiveErrors = 0
        NSLog("GuardianPolling: Starting poll timer (interval: \(pollInterval)s)")

        createPollTimer()
    }

    func stopPolling() {
        guard isPolling else { return }

        NSLog("GuardianPolling: Stopping poll timer")

        pollTimer?.cancel()
        pollTimer = nil
        isPolling = false
    }

    func pollForNewAudio() async throws -> PollResponse? {
        // Flutter shared_preferences stores with "flutter." prefix on iOS
        let uid = UserDefaults.standard.string(forKey: "flutter.uid") ?? UserDefaults.standard.string(forKey: "uid") ?? "unknown"
        let endpoint = "\(backendURL)/v1/ella/guardian/next-audio?uid=\(uid)"

        guard let url = URL(string: endpoint) else {
            throw NSError(domain: "GuardianPolling", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Invalid backend URL"
            ])
        }

        let (data, response) = try await session.data(from: url)

        guard let httpResponse = response as? HTTPURLResponse else {
            throw NSError(domain: "GuardianPolling", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "Invalid response type"
            ])
        }

        guard httpResponse.statusCode == 200 else {
            throw NSError(domain: "GuardianPolling", code: httpResponse.statusCode, userInfo: [
                NSLocalizedDescriptionKey: "HTTP \(httpResponse.statusCode)"
            ])
        }

        let pollResponse = try JSONDecoder().decode(PollResponse.self, from: data)

        if pollResponse.url != nil || pollResponse.priority == "debug" {
            return pollResponse
        }

        return nil
    }

    // MARK: - Private Methods

    private func createPollTimer() {
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .background))

        timer.schedule(deadline: .now(), repeating: pollInterval)

        timer.setEventHandler { [weak self] in
            Task {
                await self?.executePoll()
            }
        }

        timer.resume()
        pollTimer = timer
    }

    private func executePoll() async {
        guard isPolling else { return }

        do {
            if let result = try await pollForNewAudio() {
                consecutiveErrors = 0
                let eventId = result.id ?? "unknown"

                if result.priority == "debug" {
                    // Route to debug buffer — never plays audio
                    DebugEventBuffer.shared.add(
                        id: result.id,
                        triggerType: result.triggerType ?? "unknown",
                        message: result.message ?? "",
                        metadata: result.metadata?.dict ?? [:]
                    )
                } else if let urlString = result.url,
                          let audioURL = URL(string: urlString) {
                    NSLog("POLL_RECEIVED(\(eventId)) ts=\(Date().timeIntervalSince1970)")
                    // Inject via GuardianModeManager (handles pre-download + retry)
                    GuardianModeManager.shared.injectRemoteAudio(audioURL: audioURL, eventId: eventId)
                }
            }
        } catch {
            consecutiveErrors += 1
            if consecutiveErrors <= 3 || consecutiveErrors % 10 == 0 {
                NSLog("GuardianPolling: Poll error (\(consecutiveErrors)x): \(error.localizedDescription)")
            }
        }

        // Periodic cache cleanup every ~60 polls (5 minutes at 5s interval)
        if Int.random(in: 0..<60) == 0 {
            GuardianModeManager.shared.cleanCache()
        }
    }
}
