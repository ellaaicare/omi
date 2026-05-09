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
    private let stateLock = NSLock()
    private var lastPollAttemptAt: Date?
    private var lastPollSuccessAt: Date?
    private var lastPollErrorAt: Date?
    private var isPollInFlight = false
    private var pollGeneration: UInt = 0
    private var lastScheduledDelay: TimeInterval = 0
    private var lastImmediatePollRequestedAt: Date?
    private let immediatePollThrottle: TimeInterval = 0.5

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
        stateLock.lock()
        defer { stateLock.unlock() }

        guard !isPolling else {
            NSLog("GuardianPolling: Already polling")
            return
        }

        pollTimer?.cancel()
        pollTimer = nil
        pollGeneration += 1
        isPolling = true
        isPollInFlight = false
        consecutiveErrors = 0
        NSLog("GuardianPolling: Starting poll timer generation=\(pollGeneration) interval=\(pollInterval)s")

        scheduleNextPollLocked(after: 0, reason: "start")
    }

    func stopPolling() {
        stateLock.lock()
        defer { stateLock.unlock() }

        guard isPolling else { return }

        pollGeneration += 1
        NSLog("GuardianPolling: Stopping poll timer generation=\(pollGeneration)")

        pollTimer?.cancel()
        pollTimer = nil
        isPolling = false
        isPollInFlight = false
    }

    /// Keep the poller alive while Guardian Mode is active. Dispatch timers can
    /// stop firing across app suspension or service downtime without the user
    /// explicitly disabling Guardian Mode; foreground and health checks call
    /// this to repair that state.
    func ensurePolling(reason: String) {
        stateLock.lock()
        defer { stateLock.unlock() }

        let now = Date()
        let staleAfter = max(pollInterval * 5, 20.0)
        let isStale = lastPollAttemptAt.map { now.timeIntervalSince($0) > staleAfter } ?? true

        guard !isPolling || pollTimer == nil || (isStale && !isPollInFlight) else {
            return
        }

        NSLog(
            "GuardianPolling: Restarting poll timer reason=\(reason) " +
            "isPolling=\(isPolling) hasTimer=\(pollTimer != nil) stale=\(isStale)"
        )

        pollTimer?.cancel()
        pollTimer = nil
        pollGeneration += 1
        isPolling = true
        isPollInFlight = false
        consecutiveErrors = 0
        scheduleNextPollLocked(after: 0, reason: reason)
    }

    /// Wake acknowledgements are created immediately after the backend sees a
    /// wake phrase, but the normal idle poll can still be several seconds away.
    /// This interrupts the timer only while Guardian polling is active.
    func requestWakeAckPollBurst(reason: String) {
        let burstDelays: [TimeInterval] = [0.0, 1.0, 2.5]

        for (index, delay) in burstDelays.enumerated() {
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + delay) { [weak self] in
                self?.requestImmediatePoll(reason: "\(reason)_burst_\(index)")
            }
        }
    }

    private func requestImmediatePoll(reason: String) {
        stateLock.lock()
        defer { stateLock.unlock() }

        guard isPolling else {
            NSLog("GuardianPolling: Ignoring immediate poll reason=\(reason) because polling is inactive")
            return
        }

        let now = Date()
        if let lastRequested = lastImmediatePollRequestedAt,
           now.timeIntervalSince(lastRequested) < immediatePollThrottle {
            NSLog("GuardianPolling: Throttled immediate poll reason=\(reason)")
            return
        }
        lastImmediatePollRequestedAt = now

        let delay = isPollInFlight ? 0.35 : 0.0
        DebugEventBuffer.shared.add(
            id: "next-audio-poll-request-\(Int(now.timeIntervalSince1970 * 1000))",
            triggerType: "guardian_next_audio_poll_requested",
            message: "Immediate Guardian next-audio poll requested",
            metadata: [
                "reason": reason,
                "scheduled_delay_ms": Int(delay * 1000),
                "is_poll_in_flight": isPollInFlight,
                "client_native_requested_at_ms": Int(now.timeIntervalSince1970 * 1000)
            ]
        )
        scheduleNextPollLocked(after: delay, reason: reason)
    }

    func statusSnapshot() -> [String: Any] {
        stateLock.lock()
        defer { stateLock.unlock() }

        return [
            "is_polling": isPolling,
            "has_timer": pollTimer != nil,
            "is_poll_in_flight": isPollInFlight,
            "poll_generation": pollGeneration,
            "last_scheduled_delay": lastScheduledDelay,
            "consecutive_errors": consecutiveErrors,
            "last_poll_attempt_at": lastPollAttemptAt?.timeIntervalSince1970 ?? 0,
            "last_poll_success_at": lastPollSuccessAt?.timeIntervalSince1970 ?? 0,
            "last_poll_error_at": lastPollErrorAt?.timeIntervalSince1970 ?? 0
        ]
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

    private func scheduleNextPollLocked(after delay: TimeInterval, reason: String) {
        pollTimer?.cancel()
        pollTimer = nil

        guard isPolling else { return }

        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .background))
        let generation = pollGeneration
        lastScheduledDelay = delay

        timer.schedule(deadline: .now() + delay)

        timer.setEventHandler { [weak self] in
            Task {
                await self?.executePoll(generation: generation)
            }
        }

        timer.resume()
        pollTimer = timer
        NSLog("GuardianPolling: Scheduled next poll generation=\(generation) delay=\(String(format: "%.2f", delay)) reason=\(reason)")
    }

    private func nextPollDelayLocked() -> TimeInterval {
        let cappedErrors = min(consecutiveErrors, 5)
        let backoff = cappedErrors == 0 ? 0 : min(30.0, pow(2.0, Double(cappedErrors)))
        let jitter = Double.random(in: 0...0.75)
        return pollInterval + backoff + jitter
    }

    private func executePoll(generation: UInt) async {
        stateLock.lock()
        guard isPolling, generation == pollGeneration else {
            stateLock.unlock()
            return
        }

        guard !isPollInFlight else {
            let delay = nextPollDelayLocked()
            scheduleNextPollLocked(after: delay, reason: "in_flight_skip")
            stateLock.unlock()
            return
        }
        isPollInFlight = true
        lastPollAttemptAt = Date()
        stateLock.unlock()

        do {
            if let result = try await pollForNewAudio() {
                stateLock.lock()
                let isCurrentGeneration = isPolling && generation == pollGeneration
                guard isCurrentGeneration else {
                    stateLock.unlock()
                    return
                }
                consecutiveErrors = 0
                lastPollSuccessAt = Date()
                stateLock.unlock()

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
                        metadata: metadata,
                        fallbackText: result.message
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
            } else {
                stateLock.lock()
                if isPolling && generation == pollGeneration {
                    consecutiveErrors = 0
                    lastPollSuccessAt = Date()
                }
                stateLock.unlock()
            }
        } catch {
            stateLock.lock()
            if isPolling && generation == pollGeneration {
                consecutiveErrors += 1
                lastPollErrorAt = Date()
            }
            let errors = consecutiveErrors
            stateLock.unlock()

            if errors <= 3 || errors % 10 == 0 {
                NSLog("GuardianPolling: Poll error (\(errors)x): \(error.localizedDescription)")
            }
        }

        // Periodic cache cleanup every ~60 polls (5 minutes at 5s interval)
        if Int.random(in: 0..<60) == 0 {
            GuardianModeManager.shared.cleanCache()
        }

        stateLock.lock()
        if isPolling && generation == pollGeneration {
            isPollInFlight = false
            let delay = nextPollDelayLocked()
            scheduleNextPollLocked(after: delay, reason: consecutiveErrors == 0 ? "poll_complete" : "poll_error")
        }
        stateLock.unlock()
    }
}
