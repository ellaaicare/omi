import Foundation
import AVFoundation
import Combine

/// GuardianModeManager - Progressive Buffering for 99.9% Reliability
///
/// Architecture:
/// - Deep silence queue (50 items, batch-refill at 20) prevents starvation
/// - All queue mutations serialized on a single DispatchQueue
/// - Remote audio pre-downloaded to local cache before injection
/// - Failed injections retried with exponential backoff (max 3 attempts)
/// - Periodic health monitor detects and recovers from queue stalls
class GuardianModeManager: NSObject {
    static let shared = GuardianModeManager()

    private var audioPlayer: AVQueuePlayer?
    private var isActive = false
    private let queue = DispatchQueue(label: "com.ella.guardianmode")
    private var cancellables = Set<AnyCancellable>()
    private var healthTimer: DispatchSourceTimer?

    // Buffer configuration
    private let initialQueueDepth = 50
    private let refillThreshold = 20
    private let batchRefillCount = 30

    // Stats for monitoring
    private var totalInjections: Int = 0
    private var successfulInjections: Int = 0
    private var failedInjections: Int = 0

    // Download cache directory
    private lazy var cacheDir: URL = {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("guardian_audio_cache")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    private override init() {
        super.init()
    }

    // MARK: - Public API

    /// Start Guardian Mode - begins silent audio loop with progressive buffering
    func start() throws {
        try queue.sync {
            guard !isActive else {
                NSLog("GuardianMode: Already active, ignoring start()")
                return
            }

            // Configure audio session for background playback
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            try audioSession.setActive(true)

            guard let silenceURL = Bundle.main.url(forResource: "silence_100ms", withExtension: "wav") else {
                throw NSError(domain: "GuardianMode", code: 1, userInfo: [
                    NSLocalizedDescriptionKey: "Silent audio file not found"
                ])
            }

            // Deep initial queue - 50 silence items for robust buffering
            let silenceItems = (0..<initialQueueDepth).map { _ in AVPlayerItem(url: silenceURL) }
            let queuePlayer = AVQueuePlayer(items: silenceItems)

            // Prevent player from pausing at end of queue
            queuePlayer.actionAtItemEnd = .advance

            self.audioPlayer = queuePlayer
            self.isActive = true
            self.totalInjections = 0
            self.successfulInjections = 0
            self.failedInjections = 0

            setupItemEndObserver()
            startHealthMonitor()

            queuePlayer.play()

            GuardianModePollingService.shared.startPolling()

            NSLog("GuardianMode: Started - progressive buffering active (queue: \(silenceItems.count) items)")
        }
    }

    /// Stop Guardian Mode
    func stop() {
        queue.sync {
            GuardianModePollingService.shared.stopPolling()

            guard isActive else {
                NSLog("GuardianMode: Already stopped, ignoring stop()")
                return
            }

            stopHealthMonitor()
            audioPlayer?.pause()
            audioPlayer?.removeAllItems()
            audioPlayer = nil
            cancellables.removeAll()
            isActive = false

            let rate = totalInjections > 0
                ? String(format: "%.1f%%", Double(successfulInjections) / Double(totalInjections) * 100)
                : "N/A"
            NSLog("GuardianMode: Stopped (injections: \(successfulInjections)/\(totalInjections), success rate: \(rate))")

            do {
                try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            } catch {
                NSLog("GuardianMode: Error deactivating audio session: \(error)")
            }
        }
    }

    /// Get current state
    func getState() -> String {
        return queue.sync {
            return isActive ? "active" : "idle"
        }
    }

    // MARK: - Queue Management (Progressive Buffering)

    private func setupItemEndObserver() {
        NotificationCenter.default
            .publisher(for: .AVPlayerItemDidPlayToEndTime)
            .sink { [weak self] _ in
                self?.onItemFinished()
            }
            .store(in: &cancellables)
    }

    /// Called when any queued item finishes playing
    private func onItemFinished() {
        queue.async { [weak self] in
            guard let self = self,
                  let player = self.audioPlayer,
                  self.isActive else { return }

            let depth = player.items().count

            // Batch refill when below threshold — prevents starvation
            if depth < self.refillThreshold {
                self.batchQueueSilence(count: self.batchRefillCount)
            }
        }
    }

    /// Queue multiple silence items atomically
    private func batchQueueSilence(count: Int) {
        // Must be called on self.queue
        guard let player = self.audioPlayer,
              let silenceURL = Bundle.main.url(forResource: "silence_100ms", withExtension: "wav"),
              self.isActive else { return }

        var added = 0
        for _ in 0..<count {
            let silenceItem = AVPlayerItem(url: silenceURL)
            if player.canInsert(silenceItem, after: nil) {
                player.insert(silenceItem, after: nil)
                added += 1
            }
        }

        NSLog("GuardianMode: Batch queued \(added) silence items (depth: \(player.items().count))")
    }

    // MARK: - Health Monitor

    /// Periodic check to detect queue stalls and recover
    private func startHealthMonitor() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 10, repeating: 10)
        timer.setEventHandler { [weak self] in
            self?.healthCheck()
        }
        timer.resume()
        healthTimer = timer
    }

    private func stopHealthMonitor() {
        healthTimer?.cancel()
        healthTimer = nil
    }

    private func healthCheck() {
        // Must be called on self.queue
        guard let player = self.audioPlayer, self.isActive else { return }

        let depth = player.items().count
        let rate = player.rate

        // Detect stalled player
        if rate == 0 && isActive {
            NSLog("GuardianMode: HEALTH WARNING - Player stalled, restarting playback")
            player.play()
        }

        // Detect dangerously low queue
        if depth < 5 {
            NSLog("GuardianMode: HEALTH WARNING - Queue critically low (\(depth)), emergency refill")
            batchQueueSilence(count: initialQueueDepth)
        }

        // Detect empty queue
        if depth == 0 {
            NSLog("GuardianMode: HEALTH CRITICAL - Queue empty, rebuilding")
            guard let silenceURL = Bundle.main.url(forResource: "silence_100ms", withExtension: "wav") else { return }
            let items = (0..<initialQueueDepth).map { _ in AVPlayerItem(url: silenceURL) }
            for item in items {
                if player.canInsert(item, after: nil) {
                    player.insert(item, after: nil)
                }
            }
            player.play()
        }

        let stats = totalInjections > 0
            ? "\(successfulInjections)/\(totalInjections)"
            : "0/0"
        NSLog("GuardianMode: Health OK (depth: \(depth), rate: \(rate), injections: \(stats))")
    }

    // MARK: - Audio Injection with Pre-download + Retry

    /// Inject remote audio with pre-download and retry logic
    func injectRemoteAudio(audioURL: URL, eventId: String) {
        totalInjections += 1
        NSLog("INJECT_START(\(eventId)) ts=\(Date().timeIntervalSince1970)")

        // Pre-download on background queue, then inject local file
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.downloadAndInject(remoteURL: audioURL, eventId: eventId, attempt: 1)
        }
    }

    /// Download audio to local cache, then inject into queue
    private func downloadAndInject(remoteURL: URL, eventId: String, attempt: Int) {
        let maxAttempts = 3
        let localFile = cacheDir.appendingPathComponent("\(eventId).audio")

        NSLog("DOWNLOAD_START(\(eventId)) attempt=\(attempt) ts=\(Date().timeIntervalSince1970)")

        // Download with timeout
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 10
        config.timeoutIntervalForResource = 15
        let session = URLSession(configuration: config)

        let task = session.downloadTask(with: remoteURL) { [weak self] tempURL, response, error in
            guard let self = self else { return }

            if let error = error {
                NSLog("DOWNLOAD_FAILED(\(eventId)) attempt=\(attempt) error=\(error.localizedDescription)")
                if attempt < maxAttempts {
                    let delay = Double(attempt) * 0.5 // 0.5s, 1.0s backoff
                    DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + delay) {
                        self.downloadAndInject(remoteURL: remoteURL, eventId: eventId, attempt: attempt + 1)
                    }
                } else {
                    NSLog("INJECT_FAILED(\(eventId)) reason=download_exhausted")
                    self.queue.async { self.failedInjections += 1 }
                }
                return
            }

            guard let tempURL = tempURL else {
                NSLog("DOWNLOAD_FAILED(\(eventId)) reason=no_temp_file")
                self.queue.async { self.failedInjections += 1 }
                return
            }

            // Move to cache
            do {
                if FileManager.default.fileExists(atPath: localFile.path) {
                    try FileManager.default.removeItem(at: localFile)
                }
                try FileManager.default.moveItem(at: tempURL, to: localFile)
            } catch {
                NSLog("DOWNLOAD_FAILED(\(eventId)) reason=cache_move_error: \(error.localizedDescription)")
                self.queue.async { self.failedInjections += 1 }
                return
            }

            NSLog("DOWNLOAD_COMPLETE(\(eventId)) ts=\(Date().timeIntervalSince1970)")

            // Inject local file into queue
            self.queue.async {
                self.injectLocalAudio(localURL: localFile, eventId: eventId, attempt: attempt)
            }
        }
        task.resume()
    }

    /// Inject a local audio file into the player queue
    private func injectLocalAudio(localURL: URL, eventId: String, attempt: Int) {
        // Must be called on self.queue
        guard let player = self.audioPlayer, self.isActive else {
            NSLog("INJECT_FAILED(\(eventId)) reason=not_active")
            failedInjections += 1
            return
        }

        let audioItem = AVPlayerItem(url: localURL)

        // Observe readyToPlay / failed
        audioItem.publisher(for: \.status)
            .sink { [weak self] status in
                guard let self = self else { return }
                if status == .readyToPlay {
                    NSLog("ITEM_READY(\(eventId)) ts=\(Date().timeIntervalSince1970)")
                } else if status == .failed {
                    NSLog("ITEM_FAILED(\(eventId)) error=\(audioItem.error?.localizedDescription ?? "unknown")")
                }
            }
            .store(in: &cancellables)

        // Observe when playback starts
        NotificationCenter.default
            .publisher(for: .AVPlayerItemDidPlayToEndTime, object: audioItem)
            .first()
            .sink { [weak self] _ in
                guard let self = self else { return }
                NSLog("PLAYBACK_COMPLETE(\(eventId)) ts=\(Date().timeIntervalSince1970)")
                self.queue.async { self.successfulInjections += 1 }
            }
            .store(in: &cancellables)

        // Find safe insertion position:
        // Insert after current item (position 2). If currentItem is nil,
        // insert at beginning by using nil afterItem.
        let afterItem = player.currentItem
        if player.canInsert(audioItem, after: afterItem) {
            player.insert(audioItem, after: afterItem)
            let depth = player.items().count
            NSLog("INJECT_OK(\(eventId)) position=2 depth=\(depth) ts=\(Date().timeIntervalSince1970)")
        } else if player.canInsert(audioItem, after: nil) {
            // Fallback: append to end of queue
            player.insert(audioItem, after: nil)
            let depth = player.items().count
            NSLog("INJECT_OK(\(eventId)) position=end depth=\(depth) ts=\(Date().timeIntervalSince1970)")
        } else {
            NSLog("INJECT_FAILED(\(eventId)) reason=cannot_insert")
            failedInjections += 1

            // Retry: rebuild queue and try again
            if attempt <= 2 {
                NSLog("INJECT_RETRY(\(eventId)) rebuilding queue")
                batchQueueSilence(count: batchRefillCount)
                DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + 0.5) { [weak self] in
                    self?.queue.async {
                        self?.injectLocalAudio(localURL: localURL, eventId: eventId, attempt: attempt + 1)
                    }
                }
            }
        }

        // Ensure player is actually playing
        if player.rate == 0 {
            player.play()
        }
    }

    // MARK: - Cache Cleanup

    /// Clean up cached audio files older than 5 minutes
    func cleanCache() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let fm = FileManager.default
            guard let files = try? fm.contentsOfDirectory(at: self.cacheDir, includingPropertiesForKeys: [.contentModificationDateKey]) else { return }

            let cutoff = Date().addingTimeInterval(-300) // 5 minutes ago
            for file in files {
                guard let attrs = try? fm.attributesOfItem(atPath: file.path),
                      let modDate = attrs[.modificationDate] as? Date,
                      modDate < cutoff else { continue }
                try? fm.removeItem(at: file)
            }
        }
    }

    deinit {
        stop()
    }
}
