import Foundation
import AVFoundation

class GuardianModePollingService {
    static let shared = GuardianModePollingService()

    // Strong reference — AVSpeechSynthesizer must outlive the speak call
    private let speechSynthesizer = AVSpeechSynthesizer()

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
        let traceId: String?
        let metadata: AnyCodableDict?

        enum CodingKeys: String, CodingKey {
            case url, id, priority, message, metadata
            case triggerType = "trigger_type"
            case traceId = "trace_id"
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

        let hasMessage = !(pollResponse.message?.isEmpty ?? true)
        if pollResponse.url != nil || pollResponse.priority == "debug" || hasMessage {
            recordNextAudioPayload(pollResponse, data: data, httpStatus: httpResponse.statusCode)
            return pollResponse
        }

        return nil
    }

    private func recordNextAudioPayload(_ response: PollResponse, data: Data, httpStatus: Int) {
        let rawBody = String(data: data, encoding: .utf8) ?? "<non-utf8>"
        let eventId = response.id ?? response.traceId ?? "unknown"
        let hasURL = !(response.url?.isEmpty ?? true)
        let hasMessage = !(response.message?.isEmpty ?? true)
        let message = "next-audio id=\(eventId) has_url=\(hasURL) has_message=\(hasMessage)"
        NSLog("NEXT_AUDIO_PAYLOAD id=\(eventId) has_url=\(hasURL) url=\(response.url ?? "none") priority=\(response.priority ?? "none") body=\(rawBody)")
        DebugEventBuffer.shared.add(
            id: eventId,
            triggerType: "guardian_next_audio_payload",
            message: message,
            metadata: [
                "http_status": httpStatus,
                "id": response.id ?? "",
                "trace_id": response.traceId ?? "",
                "trigger_type": response.triggerType ?? "",
                "priority": response.priority ?? "",
                "has_url": hasURL,
                "url": response.url ?? "",
                "has_message": hasMessage,
                "message_length": response.message?.count ?? 0,
                "raw_payload": rawBody
            ]
        )
    }

    // MARK: - On-Device TTS

    private func speakText(_ text: String) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        utterance.rate = 0.5
        utterance.pitchMultiplier = 1.0
        speechSynthesizer.speak(utterance)
        NSLog("TTS_SPEAK: \(text.prefix(80))")
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
                let metadata = result.metadata?.dict ?? [:]
                let traceId = result.traceId
                    ?? metadata["trace_id"] as? String
                    ?? metadata["traceId"] as? String
                let eventId = result.id ?? traceId ?? "unknown"

                if let urlString = result.url, !urlString.isEmpty,
                          let audioURL = URL(string: urlString) {
                    NSLog("POLL_RECEIVED(\(eventId)) ts=\(Date().timeIntervalSince1970)")
                    var pollMetadata = metadata
                    pollMetadata["url"] = urlString
                    pollMetadata["trace_id"] = traceId ?? ""
                    pollMetadata["priority"] = result.priority ?? ""
                    DebugEventBuffer.shared.add(
                        id: eventId,
                        triggerType: "guardian_audio_inject_request",
                        message: "inject request id=\(eventId)",
                        metadata: pollMetadata
                    )
                    // Inject via GuardianModeManager (handles pre-download + retry)
                    GuardianModeManager.shared.injectRemoteAudio(
                        audioURL: audioURL,
                        eventId: eventId,
                        traceId: traceId,
                        triggerType: result.triggerType,
                        metadata: metadata
                    )
                } else if result.priority == "debug" {
                    // Metadata-only debug items never play audio; debug items with a URL are injected above.
                    var debugMetadata = metadata
                    debugMetadata["priority"] = result.priority ?? ""
                    debugMetadata["debug_skip_reason"] = "no_audio_url"
                    DebugEventBuffer.shared.add(
                        id: result.id,
                        triggerType: result.triggerType ?? "unknown",
                        message: result.message ?? "",
                        metadata: debugMetadata
                    )
                } else if let message = result.message, !message.isEmpty {
                    // Text-only (consolidated) — use on-device TTS
                    NSLog("POLL_TTS(\(eventId)) message=\(message.prefix(50))")
                    var ttsMetadata = metadata
                    ttsMetadata["playback_source"] = "on_device_tts"
                    GuardianModeManager.shared.reportPlaybackEvent(
                        eventType: "started",
                        queueItemId: eventId,
                        traceId: traceId,
                        triggerType: result.triggerType,
                        metadata: ttsMetadata
                    )
                    speakText(message)
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
