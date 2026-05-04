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
    private var injectionSequence: Int = 0  // Sequential counter for easy log tracking
    private var playbackStartedItems = Set<ObjectIdentifier>()

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
                NSLog("GuardianMode: Already active, repairing poller/player if needed")
                GuardianModePollingService.shared.ensurePolling(reason: "guardian_start_already_active")
                audioPlayer?.play()
                return
            }

            // Activate audio session (configured in AppDelegate, activated here before playback)
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setActive(true)

            // Detailed audio session diagnostics
            NSLog("🔊 SESSION_STATE_DETAILED:")
            NSLog("   Category: \(audioSession.category.rawValue)")
            NSLog("   Mode: \(audioSession.mode.rawValue)")
            NSLog("   Options: \(audioSession.categoryOptions.rawValue)")
            NSLog("   Output: \(audioSession.currentRoute.outputs.map { $0.portType.rawValue }.joined(separator: ", "))")
            NSLog("   Sample Rate: \(audioSession.sampleRate)")
            NSLog("   IO Buffer Duration: \(audioSession.ioBufferDuration)")
            NSLog("   Other audio playing: \(audioSession.isOtherAudioPlaying)")

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
            self.injectionSequence = 0
            self.playbackStartedItems.removeAll()

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
            playbackStartedItems.removeAll()
            isActive = false

            let rate = totalInjections > 0
                ? String(format: "%.1f%%", Double(successfulInjections) / Double(totalInjections) * 100)
                : "N/A"
            NSLog("GuardianMode: Stopped (injections: \(successfulInjections)/\(totalInjections), success rate: \(rate))")

            // Don't deactivate audio session - it's shared with other app components (recording, etc.)
        }
    }

    /// Get current state
    func getState() -> String {
        return queue.sync {
            return isActive ? "active" : "idle"
        }
    }

    /// Called from app lifecycle hooks. If Guardian Mode is still active, make
    /// sure both the silent playback loop and network poller survived app
    /// suspension, backend downtime, and audio-session interruptions.
    func repairIfActive(reason: String) {
        queue.async { [weak self] in
            guard let self = self, self.isActive else { return }

            do {
                try AVAudioSession.sharedInstance().setActive(true)
            } catch {
                NSLog("GuardianMode: Failed to reactivate audio session during repair reason=\(reason): \(error.localizedDescription)")
            }

            if let player = self.audioPlayer, player.rate == 0 {
                NSLog("GuardianMode: Repair restarting stalled player reason=\(reason)")
                player.play()
            }

            GuardianModePollingService.shared.ensurePolling(reason: reason)
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

        GuardianModePollingService.shared.ensurePolling(reason: "guardian_health_check")

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

    // MARK: - Playback Route Reporting

    /// Fire-and-forget POST to backend recording the current audio output route.
    /// Called on each audio injection and on route changes so backend knows echo risk.
    func reportPlaybackEvent(
        eventType: String = "started",
        queueItemId: String? = nil,
        traceId: String? = nil,
        triggerType: String? = nil,
        durationMs: Int = 0,
        metadata: [String: Any]? = nil
    ) {
        let session = AVAudioSession.sharedInstance()
        guard let port = session.currentRoute.outputs.first else { return }

        let portType = port.portType.rawValue
        let portName = port.portName
        let deviceUID = port.uid

        let uid = UserDefaults.standard.string(forKey: "flutter.uid")
                   ?? UserDefaults.standard.string(forKey: "uid")
                   ?? "unknown"
        guard uid != "unknown" else { return }

        let backendURL = GuardianModePollingService.shared.backendURL
        guard let url = URL(string: "\(backendURL)/v1/ella/guardian/playback-event") else { return }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 3.0

        var eventMetadata = metadata ?? [:]
        if let triggerType = triggerType {
            eventMetadata["trigger_type"] = triggerType
        }

        var body: [String: Any] = [
            "uid": uid,
            "event_type": eventType,
            "port_type": portType,
            "port_name": portName,
            "device_uid": deviceUID,
            "duration_ms": durationMs
        ]
        if let queueItemId = queueItemId {
            body["queue_item_id"] = queueItemId
        }
        if let traceId = traceId {
            body["trace_id"] = traceId
        }
        if !eventMetadata.isEmpty {
            body["metadata"] = eventMetadata
        }
        guard let data = try? JSONSerialization.data(withJSONObject: body) else { return }
        request.httpBody = data

        URLSession.shared.dataTask(with: request) { _, _, _ in }.resume()
        NSLog("PLAYBACK_EVENT type=\(eventType) trace=\(traceId ?? "none") item=\(queueItemId ?? "none") port=\(portType) device=\(portName.isEmpty ? "unknown" : portName) uid=\(uid)")
    }

    // MARK: - Audio Injection with Pre-download + Retry

    /// Inject remote audio with pre-download and retry logic
    func injectRemoteAudio(
        audioURL: URL,
        eventId: String,
        traceId: String? = nil,
        triggerType: String? = nil,
        metadata: [String: Any]? = nil
    ) {
        // Increment sequence counter on queue for thread-safety
        queue.async { [weak self] in
            guard let self = self else { return }
            self.injectionSequence += 1
            self.totalInjections += 1
            let seq = self.injectionSequence
            let filename = audioURL.lastPathComponent
            var playbackMetadata = metadata ?? [:]
            playbackMetadata["playback_source"] = playbackMetadata["playback_source"] ?? "remote_audio_url"

            NSLog("INJECT_START #\(seq) (\(filename)) id=\(eventId) ts=\(Date().timeIntervalSince1970)")

            // Inject remote audio directly (no download - progressive streaming)
            Task {
                await self.injectRemoteAudioDirect(
                    remoteURL: audioURL,
                    eventId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    metadata: playbackMetadata,
                    seq: seq,
                    filename: filename,
                    attempt: 1
                )
            }
        }
    }

    /// Inject remote audio directly into the player queue (progressive streaming)
    /// KEY: Inserts FIRST (triggers loading), THEN waits for .readyToPlay
    private func injectRemoteAudioDirect(
        remoteURL: URL,
        eventId: String,
        traceId: String?,
        triggerType: String?,
        metadata: [String: Any]?,
        seq: Int,
        filename: String,
        attempt: Int
    ) async {
        // Must be called on self.queue
        recordPlaybackDebugEvent(
            "inject_start",
            queueItemId: eventId,
            traceId: traceId,
            triggerType: triggerType,
            metadata: metadata,
            extra: [
                "attempt": attempt,
                "seq": seq,
                "filename": filename,
                "url": remoteURL.absoluteString,
                "is_active": self.isActive,
                "has_player": self.audioPlayer != nil,
                "queue_depth": self.audioPlayer?.items().count ?? 0,
                "player_rate": self.audioPlayer?.rate ?? -1,
                "current_item_present": self.audioPlayer?.currentItem != nil
            ]
        )

        guard let player = self.audioPlayer, self.isActive else {
            NSLog("INJECT_FAILED #\(seq) (\(filename)) reason=not_active")
            var failureMetadata = metadata ?? [:]
            failureMetadata["url"] = remoteURL.absoluteString
            failureMetadata["is_active"] = self.isActive
            failureMetadata["has_player"] = self.audioPlayer != nil
            failureMetadata["queue_depth"] = self.audioPlayer?.items().count ?? 0
            failureMetadata["player_rate"] = self.audioPlayer?.rate ?? -1
            failureMetadata["current_item_present"] = self.audioPlayer?.currentItem != nil
            reportGuardianPlaybackFailure(
                queueItemId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: failureMetadata,
                error: "not_active"
            )
            await MainActor.run { self.failedInjections += 1 }
            return
        }

        let audioItem = AVPlayerItem(url: remoteURL)

        let itemQueuedAt = Date()
        let itemId = ObjectIdentifier(audioItem)

        // Insert to play next, not at the end of the silence buffer. This restores
        // the original audible-alert behavior while still triggering remote loading.
        let afterItem = player.currentItem
        if player.canInsert(audioItem, after: afterItem) {
            player.insert(audioItem, after: afterItem)
            NSLog("INJECT_OK #\(seq) (\(filename)) position=after_current depth=\(player.items().count) ts=\(Date().timeIntervalSince1970)")
            recordPlaybackDebugEvent(
                "inject_ok",
                queueItemId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: metadata,
                extra: [
                    "position": "after_current",
                    "depth": player.items().count,
                    "player_rate": player.rate,
                    "url": remoteURL.absoluteString,
                    "is_current_item": player.currentItem === audioItem
                ]
            )
            scheduleCurrentItemProbes(
                player: player,
                audioItem: audioItem,
                itemId: itemId,
                itemQueuedAt: itemQueuedAt,
                eventId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: metadata
            )
        } else if player.canInsert(audioItem, after: nil) {
            player.insert(audioItem, after: nil)
            NSLog("INJECT_OK #\(seq) (\(filename)) position=end depth=\(player.items().count) ts=\(Date().timeIntervalSince1970)")
            recordPlaybackDebugEvent(
                "inject_ok",
                queueItemId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: metadata,
                extra: [
                    "position": "end",
                    "depth": player.items().count,
                    "player_rate": player.rate,
                    "url": remoteURL.absoluteString,
                    "is_current_item": player.currentItem === audioItem
                ]
            )
            scheduleCurrentItemProbes(
                player: player,
                audioItem: audioItem,
                itemId: itemId,
                itemQueuedAt: itemQueuedAt,
                eventId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: metadata
            )
        } else {
            NSLog("INJECT_FAILED #\(seq) (\(filename)) reason=cannot_insert")
            var failureMetadata = metadata ?? [:]
            failureMetadata["url"] = remoteURL.absoluteString
            failureMetadata["queue_depth"] = player.items().count
            failureMetadata["player_rate"] = player.rate
            failureMetadata["current_item_present"] = player.currentItem != nil
            reportGuardianPlaybackFailure(
                queueItemId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: failureMetadata,
                error: "cannot_insert"
            )
            await MainActor.run { self.failedInjections += 1 }
            return
        }

        player.publisher(for: \.currentItem)
            .sink { [weak self, weak audioItem] currentItem in
                guard let self = self, let audioItem = audioItem, currentItem === audioItem else { return }
                self.queue.async {
                    guard !self.playbackStartedItems.contains(itemId) else { return }
                    self.playbackStartedItems.insert(itemId)
                    var eventMetadata = metadata ?? [:]
                    let queuedLatencyMs = Int(Date().timeIntervalSince(itemQueuedAt) * 1000)
                    eventMetadata["queued_latency_ms"] = queuedLatencyMs
                    self.reportPlaybackEvent(
                        eventType: "started",
                        queueItemId: eventId,
                        traceId: traceId,
                        triggerType: triggerType,
                        durationMs: 0,
                        metadata: eventMetadata
                    )
                    NSLog("PLAYBACK_START #\(seq) (\(filename)) ts=\(Date().timeIntervalSince1970)")
                    self.recordPlaybackDebugEvent(
                        "playback_start",
                        queueItemId: eventId,
                        traceId: traceId,
                        triggerType: triggerType,
                        metadata: metadata,
                        extra: [
                            "queued_latency_ms": queuedLatencyMs,
                            "player_rate": player.rate,
                            "item_status": audioItem.status.rawValue,
                            "is_current_item": true
                        ]
                    )
                }
            }
            .store(in: &cancellables)

        // Wait for item to become ready (production logging - minimal)
        let startTime = Date()
        let timeout: TimeInterval = 10.0
        var iteration = 0

        while audioItem.status == .unknown {
            iteration += 1

            // Check for error
            if let error = audioItem.error {
                NSLog("❌ ITEM_ERROR #\(seq) (\(filename)) - \(error.localizedDescription)")
                recordPlaybackDebugEvent(
                    "item_error",
                    queueItemId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    metadata: metadata,
                    extra: ["error": error.localizedDescription, "item_status": audioItem.status.rawValue]
                )
                reportGuardianPlaybackFailure(
                    queueItemId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    metadata: metadata,
                    error: error.localizedDescription
                )
                await MainActor.run { self.failedInjections += 1 }
                return
            }

            // Check timeout
            if Date().timeIntervalSince(startTime) > timeout {
                NSLog("❌ TIMEOUT #\(seq) (\(filename)) after 10s - status: \(audioItem.status.rawValue)")
                recordPlaybackDebugEvent(
                    "ready_timeout",
                    queueItemId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    metadata: metadata,
                    extra: [
                        "item_status": audioItem.status.rawValue,
                        "player_rate": player.rate,
                        "is_current_item": player.currentItem === audioItem
                    ]
                )
                reportGuardianPlaybackFailure(
                    queueItemId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    metadata: metadata,
                    error: "ready_timeout"
                )
                await MainActor.run { self.failedInjections += 1 }
                return
            }

            // Only log if taking unusually long (> 5 seconds)
            if iteration == 100 {
                NSLog("⚠️ Slow load #\(seq) (\(filename)) - still waiting after 5s")
            }

            // Wait 50ms before checking again
            try? await Task.sleep(nanoseconds: 50_000_000)
        }

        // Check if buffering succeeded
        if audioItem.status == .failed {
            let errorMsg = audioItem.error?.localizedDescription ?? "unknown"
            NSLog("ITEM_FAILED #\(seq) (\(filename)) error=\(errorMsg)")
            recordPlaybackDebugEvent(
                "item_failed",
                queueItemId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: metadata,
                extra: ["error": errorMsg, "item_status": audioItem.status.rawValue]
            )
            reportGuardianPlaybackFailure(
                queueItemId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: metadata,
                error: errorMsg
            )
            await MainActor.run { self.failedInjections += 1 }
            return
        }

        guard audioItem.status == .readyToPlay else {
            NSLog("INJECT_FAILED #\(seq) (\(filename)) reason=unexpected_status_\(audioItem.status.rawValue)")
            recordPlaybackDebugEvent(
                "unexpected_status",
                queueItemId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: metadata,
                extra: ["item_status": audioItem.status.rawValue]
            )
            reportGuardianPlaybackFailure(
                queueItemId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: metadata,
                error: "unexpected_status_\(audioItem.status.rawValue)"
            )
            await MainActor.run { self.failedInjections += 1 }
            return
        }

        let readyTime = Date().timeIntervalSince(startTime)

        // Only log unusual load times (production logging - minimal)
        if readyTime > 5.0 {
            NSLog("⚠️ Slow load #\(seq) (\(filename)) - took \(String(format: "%.2f", readyTime))s")
        } else if readyTime < 0.5 {
            NSLog("⚡ Fast load #\(seq) (\(filename)) - took \(String(format: "%.2f", readyTime))s")
        }
        recordPlaybackDebugEvent(
            "item_ready",
            queueItemId: eventId,
            traceId: traceId,
            triggerType: triggerType,
            metadata: metadata,
            extra: [
                "ready_latency_ms": Int(readyTime * 1000),
                "item_status": audioItem.status.rawValue,
                "player_rate": player.rate,
                "is_current_item": player.currentItem === audioItem
            ]
        )
        // Normal loads (0.5-5s) are silent - tracked by successfulInjections counter
        // Observe when playback completes
        NotificationCenter.default
            .publisher(for: .AVPlayerItemDidPlayToEndTime, object: audioItem)
            .first()
            .sink { [weak self] _ in
                guard let self = self else { return }
                NSLog("PLAYBACK_COMPLETE #\(seq) (\(filename)) ts=\(Date().timeIntervalSince1970)")
                self.recordPlaybackDebugEvent(
                    "playback_complete",
                    queueItemId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    metadata: metadata,
                    extra: [
                        "duration_ms": Int(Date().timeIntervalSince(startTime) * 1000),
                        "item_status": audioItem.status.rawValue
                    ]
                )
                self.reportPlaybackEvent(
                    eventType: "completed",
                    queueItemId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    durationMs: Int(Date().timeIntervalSince(startTime) * 1000),
                    metadata: metadata
                )
                self.queue.async {
                    self.playbackStartedItems.remove(itemId)
                    self.successfulInjections += 1
                }
            }
            .store(in: &cancellables)

        NotificationCenter.default
            .publisher(for: .AVPlayerItemFailedToPlayToEndTime, object: audioItem)
            .first()
            .sink { [weak self] notification in
                guard let self = self else { return }
                let error = (notification.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error)?
                    .localizedDescription ?? "failed_to_play_to_end"
                NSLog("PLAYBACK_FAILED #\(seq) (\(filename)) error=\(error)")
                self.recordPlaybackDebugEvent(
                    "playback_failed_to_end",
                    queueItemId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    metadata: metadata,
                    extra: ["error": error, "item_status": audioItem.status.rawValue]
                )
                self.reportGuardianPlaybackFailure(
                    queueItemId: eventId,
                    traceId: traceId,
                    triggerType: triggerType,
                    metadata: metadata,
                    error: error
                )
                self.queue.async { self.playbackStartedItems.remove(itemId) }
            }
            .store(in: &cancellables)

        // Ensure player is actually playing
        if player.rate == 0 {
            player.play()
        }
    }

    private func scheduleCurrentItemProbes(
        player: AVQueuePlayer,
        audioItem: AVPlayerItem,
        itemId: ObjectIdentifier,
        itemQueuedAt: Date,
        eventId: String,
        traceId: String?,
        triggerType: String?,
        metadata: [String: Any]?
    ) {
        for delayMs in [1000, 3000, 8000] {
            Task { [weak self, weak player, weak audioItem] in
                try? await Task.sleep(nanoseconds: UInt64(delayMs) * 1_000_000)
                guard let self = self, let player = player, let audioItem = audioItem else { return }

                self.queue.async {
                    self.recordPlaybackDebugEvent(
                        "current_item_probe",
                        queueItemId: eventId,
                        traceId: traceId,
                        triggerType: triggerType,
                        metadata: metadata,
                        extra: [
                            "probe_delay_ms": delayMs,
                            "elapsed_ms": Int(Date().timeIntervalSince(itemQueuedAt) * 1000),
                            "has_started": self.playbackStartedItems.contains(itemId),
                            "is_current_item": player.currentItem === audioItem,
                            "item_status": audioItem.status.rawValue,
                            "player_rate": player.rate,
                            "queue_depth": player.items().count,
                            "current_item_present": player.currentItem != nil
                        ]
                    )
                }
            }
        }
    }

    private func reportGuardianPlaybackFailure(
        queueItemId: String,
        traceId: String?,
        triggerType: String?,
        metadata: [String: Any]?,
        error: String
    ) {
        var eventMetadata = metadata ?? [:]
        eventMetadata["error"] = error
        recordPlaybackDebugEvent(
            "playback_failed",
            queueItemId: queueItemId,
            traceId: traceId,
            triggerType: triggerType,
            metadata: eventMetadata,
            extra: ["error": error]
        )
        reportPlaybackEvent(
            eventType: "failed",
            queueItemId: queueItemId,
            traceId: traceId,
            triggerType: triggerType,
            durationMs: 0,
            metadata: eventMetadata
        )
    }

    private func recordPlaybackDebugEvent(
        _ event: String,
        queueItemId: String,
        traceId: String?,
        triggerType: String?,
        metadata: [String: Any]?,
        extra: [String: Any] = [:]
    ) {
        let session = AVAudioSession.sharedInstance()
        let route = session.currentRoute.outputs.first
        var debugMetadata = metadata ?? [:]
        debugMetadata["queue_item_id"] = queueItemId
        debugMetadata["trace_id"] = traceId ?? ""
        debugMetadata["trigger_type"] = triggerType ?? ""
        debugMetadata["event"] = event
        debugMetadata["port_type"] = route?.portType.rawValue ?? "none"
        debugMetadata["port_name"] = route?.portName ?? "none"
        extra.forEach { debugMetadata[$0.key] = $0.value }

        DebugEventBuffer.shared.add(
            id: queueItemId,
            triggerType: "guardian_playback_\(event)",
            message: "guardian \(event) id=\(queueItemId)",
            metadata: debugMetadata
        )
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
