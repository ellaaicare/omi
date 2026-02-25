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

    func pollForNewAudio() async throws -> (url: URL, id: String)? {
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

        if let urlString = pollResponse.url,
           let id = pollResponse.id,
           let audioURL = URL(string: urlString) {
            return (url: audioURL, id: id)
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
                let audioURL = result.url
                let eventId = result.id

                consecutiveErrors = 0
                NSLog("POLL_RECEIVED(\(eventId)) ts=\(Date().timeIntervalSince1970)")

                // Inject via GuardianModeManager (handles pre-download + retry)
                GuardianModeManager.shared.injectRemoteAudio(audioURL: audioURL, eventId: eventId)
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
