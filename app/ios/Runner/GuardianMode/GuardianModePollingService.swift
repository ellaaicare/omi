import Foundation

class GuardianModePollingService {
    // Singleton instance
    static let shared = GuardianModePollingService()

    private init() {}

    // Timer for polling
    private var pollTimer: DispatchSourceTimer?

    // URLSession for HTTP requests
    private let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 10.0
        config.timeoutIntervalForResource = 10.0
        return URLSession(configuration: config)
    }()

    // Configuration
    var pollInterval: TimeInterval = 5.0
    var backendURL: String = "http://localhost:3000"  // Test server on same machine

    // Response model
    struct PollResponse: Codable {
        let url: String?
        let id: String?
    }

    // State
    private var isPolling = false

    // MARK: - Public Methods

    func startPolling() {
        guard !isPolling else {
            NSLog("GuardianPolling: Already polling")
            return
        }

        isPolling = true
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
        let endpoint = "\(backendURL)/api/guardian/next-audio?uid=test-uid"

        guard let url = URL(string: endpoint) else {
            throw NSError(domain: "GuardianPolling", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Invalid backend URL"
            ])
        }

        NSLog("GuardianPolling: GET \(endpoint)")

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

                // Log poll received with event ID and timestamp
                NSLog("POLL_RECEIVED(\(eventId)) ts=\(Date().timeIntervalSince1970)")

                NSLog("GuardianPolling: Found new audio: \(audioURL.absoluteString)")

                // Inject into Guardian Mode queue
                DispatchQueue.main.async {
                    GuardianModeManager.shared.injectRemoteAudio(audioURL: audioURL, eventId: eventId)
                }
            } else {
                // No new audio - silence continues
                NSLog("GuardianPolling: No new audio")
            }
        } catch {
            NSLog("GuardianPolling: Poll error: \(error.localizedDescription)")
            // Continue polling despite error
        }
    }
}
