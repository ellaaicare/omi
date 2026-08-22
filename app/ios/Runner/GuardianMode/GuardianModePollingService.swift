import Foundation
#if !GUARDIAN_NATIVE_POLICY_TESTS
import AVFoundation
#endif

final class GuardianModePollingService: @unchecked Sendable {
#if !GUARDIAN_NATIVE_POLICY_TESTS
    static let shared = GuardianModePollingService()
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

    typealias PollTransportCompletion = (Result<(Data, URLResponse), Error>) -> Void
    typealias PollTransport = (URLRequest, @escaping PollTransportCompletion) -> (() -> Void)
    typealias TokenProvider = (GuardianWorkLease, Bool) async throws -> GuardianBearerCredential

    struct Effects {
        let debugMutation: (PollResponse, [String: Any]) -> Void
        let injectionEnqueue: (URL, String, String?, String?, [String: Any], GuardianWorkLease) -> Void
        let playbackReport: (String, String?, String?, [String: Any], GuardianWorkLease) -> Void
        let speak: (String) -> Void
        let stopSpeaking: () -> Void
        let cleanCache: () -> Void
    }

    private struct InFlightPoll {
        let id: UUID
        let task: Task<Void, Never>
    }

    private final class CancellationBox: @unchecked Sendable {
        private let lock = NSLock()
        private var cancellation: (() -> Void)?
        private var isCancelled = false

        func set(_ cancellation: @escaping () -> Void) {
            lock.lock()
            if isCancelled {
                lock.unlock()
                cancellation()
            } else {
                self.cancellation = cancellation
                lock.unlock()
            }
        }

        func cancel() {
            lock.lock()
            isCancelled = true
            let cancellation = self.cancellation
            self.cancellation = nil
            lock.unlock()
            cancellation?()
        }
    }

    private let pollStateQueue = DispatchQueue(label: "com.ella.guardianmode.polling")
    private let transport: PollTransport
    private let tokenProvider: TokenProvider
    private let effects: Effects
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
        transport = { request, completion in
            let task = session.dataTask(with: request) { data, response, error in
                if let error {
                    completion(.failure(error))
                } else if let data, let response {
                    completion(.success((data, response)))
                } else {
                    completion(.failure(URLError(.badServerResponse)))
                }
            }
            task.resume()
            return task.cancel
        }
        tokenProvider = { lease, forcingRefresh in
            try await GuardianFirebaseTokenBridge.shared.credential(
                for: lease,
                forcingRefresh: forcingRefresh
            )
        }
        let speechSynthesizer = AVSpeechSynthesizer()
        effects = Effects(
            debugMutation: { result, metadata in
                DebugEventBuffer.shared.add(
                    id: result.id,
                    triggerType: result.triggerType ?? "unknown",
                    message: result.message ?? "",
                    metadata: metadata
                )
            },
            injectionEnqueue: { audioURL, eventId, traceId, triggerType, metadata, lease in
                GuardianModeManager.shared.injectRemoteAudio(
                    audioURL: audioURL,
                    eventId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    metadata: metadata,
                    pollLease: lease
                )
            },
            playbackReport: { eventId, traceId, triggerType, metadata, lease in
                GuardianModeManager.shared.reportPlaybackEvent(
                    eventType: "started",
                    queueItemId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    metadata: metadata,
                    lease: lease
                )
            },
            speak: { text in
                let utterance = AVSpeechUtterance(string: text)
                utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
                utterance.rate = 0.5
                utterance.pitchMultiplier = 1.0
                speechSynthesizer.speak(utterance)
                NSLog("TTS_SPEAK: \(text.prefix(80))")
            },
            stopSpeaking: {
                speechSynthesizer.stopSpeaking(at: .immediate)
            },
            cleanCache: GuardianModeManager.shared.cleanCache
        )
        schedulesTimer = true
    }
#endif

    init(
        backendURL: String = "https://api.ella-ai-care.com",
        transport: @escaping PollTransport,
        tokenProvider: @escaping TokenProvider,
        effects: Effects,
        schedulesTimer: Bool = false,
        pollInterval: TimeInterval = 3.0
    ) {
        self.backendURL = backendURL
        self.transport = transport
        self.tokenProvider = tokenProvider
        self.effects = effects
        self.schedulesTimer = schedulesTimer
        self.pollInterval = pollInterval
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
        effects.stopSpeaking()
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

    private func pollForNewAudio(lease: GuardianWorkLease) async throws -> PollResponse? {
        var components = URLComponents(string: "\(backendURL)/v1/ella/guardian/next-audio")
        components?.queryItems = [URLQueryItem(name: "uid", value: lease.uid)]

        guard let url = components?.url else {
            throw NSError(domain: "GuardianPolling", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Invalid backend URL"
            ])
        }

        var credential = try await tokenProvider(lease, false)
        try validate(credential: credential, lease: lease)
        var request = authorizedRequest(url: url, credential: credential)
        var (data, response) = try await performAuthorizedTransport(request, lease: lease)
        if (response as? HTTPURLResponse)?.statusCode == 401 {
            guard GuardianModeAvailability.shared.isCurrent(lease) else {
                throw GuardianCredentialError.ownerChanged
            }
            credential = try await tokenProvider(lease, true)
            try validate(credential: credential, lease: lease)
            request = authorizedRequest(url: url, credential: credential)
            (data, response) = try await performAuthorizedTransport(request, lease: lease)
        }
        guard GuardianModeAvailability.shared.isCurrent(lease) else {
            throw GuardianCredentialError.ownerChanged
        }

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

    private func validate(
        credential: GuardianBearerCredential,
        lease: GuardianWorkLease
    ) throws {
        guard credential.uid == lease.uid,
              !credential.token.isEmpty,
              GuardianModeAvailability.shared.isCurrent(lease) else {
            throw GuardianCredentialError.ownerChanged
        }
    }

    private func authorizedRequest(
        url: URL,
        credential: GuardianBearerCredential
    ) -> URLRequest {
        var request = URLRequest(url: url)
        request.setValue("Bearer \(credential.token)", forHTTPHeaderField: "Authorization")
        return request
    }

    private func performAuthorizedTransport(
        _ request: URLRequest,
        lease: GuardianWorkLease
    ) async throws -> (Data, URLResponse) {
        let cancellationBox = CancellationBox()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                let started = GuardianModeAvailability.shared.performIfCurrent(lease) {
                    let cancellation = transport(request) { result in
                        continuation.resume(with: result)
                    }
                    cancellationBox.set(cancellation)
                    return true
                }
                if !started {
                    continuation.resume(throwing: GuardianCredentialError.ownerChanged)
                }
            }
        } onCancel: {
            cancellationBox.cancel()
        }
    }

    // MARK: - Private Methods

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
              let lease = GuardianModeAvailability.shared.captureLease() else { return }

        do {
            guard let result = try await pollForNewAudio(lease: lease), !Task.isCancelled else { return }

            let released = GuardianModeAvailability.shared.performIfCurrent(lease) {
                guard pollingIsActive() else { return false }
                releaseCurrentResponse(result, lease: lease)
                pollStateQueue.sync {
                    consecutiveErrors = 0
                }
                return true
            }
            guard released else { return }
        } catch {
            guard !Task.isCancelled else { return }
            _ = GuardianModeAvailability.shared.performIfCurrent(lease) {
                pollStateQueue.sync {
                    guard isPolling else { return }
                    consecutiveErrors += 1
                    if consecutiveErrors <= 3 || consecutiveErrors % 10 == 0 {
                        let category = error is GuardianCredentialError ? "authentication" : "request"
                        NSLog("GuardianPolling: \(category) failure (\(consecutiveErrors)x)")
                    }
                }
                return true
            }
        }

        // Periodic cache cleanup every ~60 polls (5 minutes at 5s interval)
        if Int.random(in: 0..<60) == 0 {
            _ = GuardianModeAvailability.shared.performIfCurrent(lease) {
                guard pollingIsActive() else { return false }
                effects.cleanCache()
                return true
            }
        }
    }

    private func pollingIsActive() -> Bool {
        pollStateQueue.sync { isPolling }
    }

    private func releaseCurrentResponse(_ result: PollResponse, lease: GuardianWorkLease) {
        let metadata = result.metadata?.dict ?? [:]
        let traceId = result.traceId
            ?? metadata["trace_id"] as? String
            ?? metadata["traceId"] as? String
        let eventId = result.id ?? traceId ?? "unknown"

        if result.priority == "debug" {
            // Route to debug buffer — never plays audio.
            effects.debugMutation(result, metadata)
        } else if let urlString = result.url, !urlString.isEmpty,
                  let audioURL = URL(string: urlString) {
            NSLog("POLL_RECEIVED(\(eventId)) ts=\(Date().timeIntervalSince1970)")
            effects.injectionEnqueue(audioURL, eventId, traceId, result.triggerType, metadata, lease)
        } else if let message = result.message, !message.isEmpty {
            NSLog("POLL_TTS(\(eventId)) message=\(message.prefix(50))")
            var ttsMetadata = metadata
            ttsMetadata["playback_source"] = "on_device_tts"
            effects.playbackReport(eventId, traceId, result.triggerType, ttsMetadata, lease)
            effects.speak(message)
        }
    }
}
