import Foundation
#if !GUARDIAN_NATIVE_POLICY_TESTS
import AVFoundation
#endif

final class GuardianModePollingService: @unchecked Sendable {
#if !GUARDIAN_NATIVE_POLICY_TESTS
    static let shared = GuardianModePollingService()

    // Strong reference — AVSpeechSynthesizer must outlive the speak call
    private let speechSynthesizer = AVSpeechSynthesizer()
#endif

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

    typealias PollTransport = (URLRequest) async throws -> (Data, URLResponse)
    typealias UIDProvider = () -> String?
    typealias ResponseHandler = (PollResponse, GuardianWorkLease, String) -> Void

    private struct InFlightPoll {
        let id: UUID
        let task: Task<Void, Never>
    }

    private let pollStateQueue = DispatchQueue(label: "com.ella.guardianmode.polling")
    private let transport: PollTransport
    private let uidProvider: UIDProvider
    private let responseHandler: ResponseHandler?
    private let schedulesTimer: Bool
    private var pollTimer: DispatchSourceTimer?
    private var inFlightPoll: InFlightPoll?
    private var isPolling = false
    private var consecutiveErrors: Int = 0

#if !GUARDIAN_NATIVE_POLICY_TESTS
    private init() {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 10.0
        config.timeoutIntervalForResource = 10.0
        let session = URLSession(configuration: config)
        transport = { request in
            try await session.data(for: request)
        }
        uidProvider = Self.currentUID
        responseHandler = nil
        schedulesTimer = true
    }
#endif

    init(
        backendURL: String = "https://api.ella-ai-care.com",
        transport: @escaping PollTransport,
        uidProvider: @escaping UIDProvider,
        schedulesTimer: Bool = false,
        responseHandler: @escaping ResponseHandler
    ) {
        self.backendURL = backendURL
        self.transport = transport
        self.uidProvider = uidProvider
        self.schedulesTimer = schedulesTimer
        self.responseHandler = responseHandler
    }

    // MARK: - Public Methods

    func startPolling() {
        guard GuardianModeAvailability.shared.isEnabled else {
            stopPolling()
            return
        }

        let started = pollStateQueue.sync {
            guard !isPolling else { return false }
            isPolling = true
            consecutiveErrors = 0
            if schedulesTimer {
                createPollTimerLocked()
            }
            return true
        }
        guard started else {
            NSLog("GuardianPolling: Already polling")
            return
        }

        NSLog("GuardianPolling: Starting poll timer (interval: \(pollInterval)s)")
    }

    func stopPolling() {
#if !GUARDIAN_NATIVE_POLICY_TESTS
        speechSynthesizer.stopSpeaking(at: .immediate)
#endif

        let stopped = pollStateQueue.sync {
            let wasPolling = isPolling || pollTimer != nil || inFlightPoll != nil
            isPolling = false
            pollTimer?.cancel()
            pollTimer = nil
            inFlightPoll?.task.cancel()
            inFlightPoll = nil
            return wasPolling
        }

        if stopped {
            NSLog("GuardianPolling: Stopping poll timer")
        }
    }

    private func pollForNewAudio(uid: String) async throws -> PollResponse? {
        let endpoint = "\(backendURL)/v1/ella/guardian/next-audio?uid=\(uid)"

        guard let url = URL(string: endpoint) else {
            throw NSError(domain: "GuardianPolling", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Invalid backend URL"
            ])
        }

        let (data, response) = try await transport(URLRequest(url: url))

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

    // MARK: - Private Methods

#if !GUARDIAN_NATIVE_POLICY_TESTS
    private static func currentUID() -> String? {
        // Flutter shared_preferences stores with "flutter." prefix on iOS.
        UserDefaults.standard.string(forKey: "flutter.uid") ?? UserDefaults.standard.string(forKey: "uid")
    }

    private func speakText(_ text: String) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        utterance.rate = 0.5
        utterance.pitchMultiplier = 1.0
        speechSynthesizer.speak(utterance)
        NSLog("TTS_SPEAK: \(text.prefix(80))")
    }
#endif

    private func createPollTimerLocked() {
        let timer = DispatchSource.makeTimerSource(queue: pollStateQueue)

        timer.schedule(deadline: .now(), repeating: pollInterval)

        timer.setEventHandler { [weak self] in
            self?.schedulePollLocked()
        }

        pollTimer = timer
        timer.resume()
    }

    func schedulePollNow() {
        pollStateQueue.async { [weak self] in
            self?.schedulePollLocked()
        }
    }

    private func schedulePollLocked() {
        guard isPolling, inFlightPoll == nil else { return }
        let id = UUID()
        let task = Task { [weak self] in
            await self?.executePoll()
            self?.pollStateQueue.async { [weak self] in
                guard self?.inFlightPoll?.id == id else { return }
                self?.inFlightPoll = nil
            }
        }
        inFlightPoll = InFlightPoll(id: id, task: task)
    }

    func executePoll() async {
        guard pollingIsActive(),
              let lease = GuardianModeAvailability.shared.captureLease(),
              let uid = uidProvider(),
              !uid.isEmpty,
              uid != "unknown" else { return }

        do {
            guard let result = try await pollForNewAudio(uid: uid), !Task.isCancelled else { return }

            let released = GuardianModeAvailability.shared.performIfCurrent(lease) {
                guard pollingIsActive(), uidProvider() == uid else { return false }
                releaseCurrentResponse(result, lease: lease, uid: uid)
                return true
            }
            guard released else { return }
            pollStateQueue.async { [weak self] in
                self?.consecutiveErrors = 0
            }
        } catch {
            guard !Task.isCancelled else { return }
            pollStateQueue.async { [weak self] in
                guard let self, self.isPolling else { return }
                self.consecutiveErrors += 1
                if self.consecutiveErrors <= 3 || self.consecutiveErrors % 10 == 0 {
                    NSLog("GuardianPolling: Poll error (\(self.consecutiveErrors)x): \(error.localizedDescription)")
                }
            }
        }

        // Periodic cache cleanup every ~60 polls (5 minutes at 5s interval)
        if Int.random(in: 0..<60) == 0 {
            _ = GuardianModeAvailability.shared.performIfCurrent(lease) {
                guard pollingIsActive(), uidProvider() == uid else { return false }
#if !GUARDIAN_NATIVE_POLICY_TESTS
                GuardianModeManager.shared.cleanCache()
#endif
                return true
            }
        }
    }

    private func pollingIsActive() -> Bool {
        pollStateQueue.sync { isPolling }
    }

    private func releaseCurrentResponse(_ result: PollResponse, lease: GuardianWorkLease, uid: String) {
        if let responseHandler {
            responseHandler(result, lease, uid)
            return
        }

#if !GUARDIAN_NATIVE_POLICY_TESTS
        let metadata = result.metadata?.dict ?? [:]
        let traceId = result.traceId
            ?? metadata["trace_id"] as? String
            ?? metadata["traceId"] as? String
        let eventId = result.id ?? traceId ?? "unknown"

        if result.priority == "debug" {
            // Route to debug buffer — never plays audio.
            DebugEventBuffer.shared.add(
                id: result.id,
                triggerType: result.triggerType ?? "unknown",
                message: result.message ?? "",
                metadata: metadata
            )
        } else if let urlString = result.url, !urlString.isEmpty,
                  let audioURL = URL(string: urlString) {
            NSLog("POLL_RECEIVED(\(eventId)) ts=\(Date().timeIntervalSince1970)")
            GuardianModeManager.shared.injectRemoteAudio(
                audioURL: audioURL,
                eventId: eventId,
                traceId: traceId,
                triggerType: result.triggerType,
                metadata: metadata,
                pollLease: lease
            )
        } else if let message = result.message, !message.isEmpty {
            NSLog("POLL_TTS(\(eventId)) message=\(message.prefix(50))")
            var ttsMetadata = metadata
            ttsMetadata["playback_source"] = "on_device_tts"
            GuardianModeManager.shared.reportPlaybackEvent(
                eventType: "started",
                queueItemId: eventId,
                traceId: traceId,
                triggerType: result.triggerType,
                metadata: ttsMetadata,
                validatedPollLease: lease,
                expectedUID: uid
            )
            speakText(message)
        }
#endif
    }
}
