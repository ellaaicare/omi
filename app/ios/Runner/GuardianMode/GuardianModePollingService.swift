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
        config.timeoutIntervalForRequest = 8.0
        config.timeoutIntervalForResource = 8.0
        return URLSession(configuration: config)
    }()

    var basePollInterval: TimeInterval = 3.0
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

    private(set) var isPolling = false
    private var consecutiveErrors: Int = 0
    private var isPollInFlight = false  // Guard against overlapping poll requests
    private var pollGeneration: Int = 0 // Incremented on each startPolling to invalidate stale timers

    // MARK: - Public Methods

    func startPolling() {
        // Deterministic cancel-then-start: always kill old timer first
        pollTimer?.cancel()
        pollTimer = nil

        isPolling = true
        consecutiveErrors = 0
        isPollInFlight = false
        pollGeneration += 1
        let gen = pollGeneration

        let jitter = TimeInterval.random(in: 0...1.0) // 0-1s jitter on start
        NSLog("GuardianPolling: Starting poll timer (base: \(basePollInterval)s, jitter: \(String(format: "%.2f", jitter))s, gen: \(gen))")

        createPollTimer(initialDelay: jitter, generation: gen)
    }

    func stopPolling() {
        guard isPolling else { return }

        NSLog("GuardianPolling: Stopping poll timer (gen: \(pollGeneration))")

        pollGeneration += 1 // Invalidate any in-flight timer callbacks
        pollTimer?.cancel()
        pollTimer = nil
        isPolling = false
        isPollInFlight = false
    }

    /// Restart polling if Guardian is active (safe to call from foreground transitions)
    func restartPollingIfActive() {
        guard GuardianModeManager.shared.getState() == "active" else {
            NSLog("GuardianPolling: Skip restart — Guardian not active")
            return
        }
        if isPolling {
            NSLog("GuardianPolling: Already polling, forcing cancel/restart for foreground transition")
            startPolling()
        } else {
            NSLog("GuardianPolling: Was suspended, restarting for foreground transition")
            startPolling()
        }
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
            return pollResponse
        }

        return nil
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

    // MARK: - Backoff

    /// Current effective poll interval with exponential backoff on errors.
    private var effectivePollInterval: TimeInterval {
        if consecutiveErrors == 0 {
            return basePollInterval
        }
        // Exponential backoff: 3s, 6s, 12s, 24s, 30s cap
        let backoff = min(basePollInterval * Double(1 << min(consecutiveErrors, 4)), 30.0)
        return backoff
    }

    // MARK: - Private Methods

    private func createPollTimer(initialDelay: TimeInterval = 0, generation: Int) {
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .background))

        // First poll after initialDelay, then repeat at effective interval
        timer.schedule(deadline: .now() + initialDelay, repeating: basePollInterval)

        timer.setEventHandler { [weak self] in
            guard let self = self, self.pollGeneration == generation else { return }
            Task {
                await self.executePoll(generation: generation)
            }
        }

        timer.resume()
        pollTimer = timer
    }

    private func executePoll(generation: Int) async {
        // Guard: skip if stopped, generation mismatch, or previous poll still in flight
        guard isPolling, pollGeneration == generation else { return }
        guard !isPollInFlight else {
            NSLog("GuardianPolling: Skipping — previous poll still in flight")
            return
        }

        isPollInFlight = true
        defer { isPollInFlight = false }

        do {
            if let result = try await pollForNewAudio() {
                // Re-check generation after async work
                guard pollGeneration == generation else { return }

                consecutiveErrors = 0
                let metadata = result.metadata?.dict ?? [:]
                let traceId = result.traceId
                    ?? metadata["trace_id"] as? String
                    ?? metadata["traceId"] as? String
                let eventId = result.id ?? traceId ?? "unknown"

                if result.priority == "debug" {
                    // Route to debug buffer — never plays audio
                    DebugEventBuffer.shared.add(
                        id: result.id,
                        triggerType: result.triggerType ?? "unknown",
                        message: result.message ?? "",
                        metadata: metadata
                    )
                } else if let urlString = result.url, !urlString.isEmpty,
                          let audioURL = URL(string: urlString) {
                    NSLog("POLL_RECEIVED(\(eventId)) ts=\(Date().timeIntervalSince1970)")

                    // Check if Guardian player is actually active before injecting.
                    // If the player is dead (Guardian stopped, audio session failed,
                    // or app came back from background with invalid state), fall back
                    // to on-device TTS so the user still gets the response.
                    let guardianActive = GuardianModeManager.shared.getState() == "active"
                    if guardianActive {
                        GuardianModeManager.shared.injectRemoteAudio(
                            audioURL: audioURL,
                            eventId: eventId,
                            traceId: traceId,
                            triggerType: result.triggerType,
                            metadata: metadata
                        )
                    } else {
                        // Player is dead — fall back to TTS if message is available
                        NSLog("POLL_FALLBACK_TTS(\(eventId)) reason=player_not_active")
                        var fallbackMetadata = metadata
                        fallbackMetadata["playback_source"] = "on_device_tts_fallback"
                        fallbackMetadata["fallback_reason"] = "player_not_active"
                        GuardianModeManager.shared.reportPlaybackEvent(
                            eventType: "started",
                            queueItemId: eventId,
                            traceId: traceId,
                            triggerType: result.triggerType,
                            metadata: fallbackMetadata
                        )
                        if let message = result.message, !message.isEmpty {
                            speakText(message)
                        } else {
                            // No message field — report failure, can't play audio or TTS
                            NSLog("POLL_NO_FALLBACK(\(eventId)) reason=no_message_and_no_player")
                            GuardianModeManager.shared.reportPlaybackEvent(
                                eventType: "failed",
                                queueItemId: eventId,
                                traceId: traceId,
                                triggerType: result.triggerType,
                                metadata: ["error": "no_player_no_message", "fallback_attempted": true]
                            )
                        }
                    }
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
            let interval = effectivePollInterval
            if consecutiveErrors <= 3 || consecutiveErrors % 10 == 0 {
                NSLog("GuardianPolling: Poll error (\(consecutiveErrors)x, backing off to \(String(format: "%.1f", interval))s): \(error.localizedDescription)")
            }
        }

        // Periodic cache cleanup every ~60 polls
        if Int.random(in: 0..<60) == 0 {
            GuardianModeManager.shared.cleanCache()
        }
    }
}