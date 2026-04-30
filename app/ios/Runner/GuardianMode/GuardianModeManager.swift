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
    static let capturePolicy = "capture_live_mark_playback_span"

    private var audioPlayer: AVQueuePlayer?
    private var isActive = false
    private let queue = DispatchQueue(label: "com.ella.guardianmode")
    private var cancellables = Set<AnyCancellable>()
    private var healthTimer: DispatchSourceTimer?
    private var activePlaybackContext: PlaybackContext?
    private var playbackStartedAt: [ObjectIdentifier: Date] = [:]
    private var playbackStartedItems = Set<ObjectIdentifier>()

    // Buffer configuration
    private let initialQueueDepth = 50
    private let refillThreshold = 20
    private let batchRefillCount = 30

    // Stats for monitoring
    private var totalInjections: Int = 0
    private var successfulInjections: Int = 0
    private var failedInjections: Int = 0
    private var injectionSequence: Int = 0  // Sequential counter for easy log tracking

    // Download cache directory
    private lazy var cacheDir: URL = {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("guardian_audio_cache")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    private override init() {
        super.init()
    }

    private struct PlaybackContext {
        let queueItemId: String
        let traceId: String
        let triggerType: String?
        let metadata: [String: Any]
    }

    // MARK: - Public API

    /// Start Guardian Mode - begins silent audio loop with progressive buffering
    func start() throws {
        try queue.sync {
            guard !isActive else {
                NSLog("GuardianMode: Already active, ignoring start()")
                return
            }

            // Re-apply route policy before playback so stale speaker overrides do not persist.
            let audioSession = AVAudioSession.sharedInstance()
            try AppDelegate.configureGuardianAudioSession(reason: "guardian_start")

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

            // Don't deactivate audio session - it's shared with other app components (recording, etc.)
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

    // MARK: - Playback Route Reporting

    /// Fire-and-forget POST to backend recording exact Guardian playback identity and current audio route.
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

        let resolvedQueueItemId = queueItemId ?? activePlaybackContext?.queueItemId
        let resolvedTraceId = traceId ?? activePlaybackContext?.traceId ?? resolvedQueueItemId
        guard let resolvedQueueItemId, let resolvedTraceId else {
            NSLog("PLAYBACK_EVENT_SKIPPED type=\(eventType) reason=missing_identity")
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 3.0

        var eventMetadata = metadata ?? [:]
        if let triggerType = triggerType {
            eventMetadata["trigger_type"] = triggerType
        }
        if eventMetadata["capture_policy"] == nil {
            eventMetadata["capture_policy"] = Self.capturePolicy
        }
        if eventMetadata["loop_suppression_hook"] == nil {
            eventMetadata["loop_suppression_hook"] = "guardian_playback_span"
        }

        var body: [String: Any] = [
            "uid": uid,
            "event_type": eventType,
            "queue_item_id": resolvedQueueItemId,
            "trace_id": resolvedTraceId,
            "capture_policy": Self.capturePolicy,
            "port_type": portType,
            "port_name": portName,
            "device_uid": deviceUID,
            "duration_ms": durationMs
        ]
        if !eventMetadata.isEmpty {
            body["metadata"] = eventMetadata
        }
        guard let data = try? JSONSerialization.data(withJSONObject: body) else { return }
        request.httpBody = data

        URLSession.shared.dataTask(with: request) { _, _, _ in }.resume()
        NSLog("PLAYBACK_EVENT type=\(eventType) trace=\(resolvedTraceId) item=\(resolvedQueueItemId) port=\(portType) device=\(portName.isEmpty ? "unknown" : portName) uid=\(uid) capture_policy=\(Self.capturePolicy)")
    }

    /// Route changes are useful only while an exact Guardian playback span is active.
    func reportActivePlaybackRouteChange() {
        queue.async { [weak self] in
            guard let self = self, let context = self.activePlaybackContext else {
                NSLog("PLAYBACK_ROUTE_CHANGE_SKIPPED reason=no_active_playback")
                return
            }

            var routeMetadata = context.metadata
            routeMetadata["route_changed_during_playback"] = true
            self.reportPlaybackEvent(
                eventType: "started",
                queueItemId: context.queueItemId,
                traceId: context.traceId,
                triggerType: context.triggerType,
                metadata: routeMetadata
            )
        }
    }

    // MARK: - Audio Injection with Pre-download + Retry

    /// Inject remote audio with pre-download and retry logic.
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
            let resolvedTraceId = traceId ?? eventId
            var playbackMetadata = metadata ?? [:]
            playbackMetadata["playback_source"] = playbackMetadata["playback_source"] ?? "remote_audio_url"
            playbackMetadata["capture_policy"] = Self.capturePolicy
            playbackMetadata["loop_suppression_hook"] = "guardian_playback_span"
            let playbackContext = PlaybackContext(
                queueItemId: eventId,
                traceId: resolvedTraceId,
                triggerType: triggerType,
                metadata: playbackMetadata
            )

            NSLog("INJECT_START #\(seq) (\(filename)) id=\(eventId) trace=\(resolvedTraceId) ts=\(Date().timeIntervalSince1970)")
            AppDelegate.refreshGuardianOutputRoute(reason: "guardian_remote_audio")

            self.downloadAndInjectAudio(
                remoteURL: audioURL,
                context: playbackContext,
                seq: seq,
                filename: filename,
                attempt: 1
            )
        }
    }

    private func downloadAndInjectAudio(
        remoteURL: URL,
        context: PlaybackContext,
        seq: Int,
        filename: String,
        attempt: Int
    ) {
        guard self.isActive else {
            NSLog("INJECT_FAILED #\(seq) (\(filename)) reason=not_active")
            reportGuardianPlaybackFailure(context: context, error: "not_active")
            self.failedInjections += 1
            return
        }

        let downloadStart = Date()
        let request = URLRequest(url: remoteURL, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 10.0)
        let task = URLSession.shared.downloadTask(with: request) { [weak self] tempURL, response, error in
            guard let self = self else { return }

            if let error = error {
                self.queue.async {
                    NSLog("DOWNLOAD_FAILED #\(seq) (\(filename)) attempt=\(attempt) error=\(error.localizedDescription)")
                    if attempt < 3 {
                        self.retryDownloadAndInject(
                            remoteURL: remoteURL,
                            context: context,
                            seq: seq,
                            filename: filename,
                            attempt: attempt + 1
                        )
                    } else {
                        self.reportGuardianPlaybackFailure(context: context, error: "download_failed:\(error.localizedDescription)")
                        self.failedInjections += 1
                    }
                }
                return
            }

            guard let tempURL = tempURL else {
                self.queue.async {
                    NSLog("DOWNLOAD_FAILED #\(seq) (\(filename)) reason=no_temp_file")
                    self.reportGuardianPlaybackFailure(context: context, error: "download_failed:no_temp_file")
                    self.failedInjections += 1
                }
                return
            }

            let httpStatus = (response as? HTTPURLResponse)?.statusCode ?? 200
            guard (200..<300).contains(httpStatus) else {
                self.queue.async {
                    NSLog("DOWNLOAD_FAILED #\(seq) (\(filename)) http_status=\(httpStatus)")
                    self.reportGuardianPlaybackFailure(context: context, error: "download_failed:http_\(httpStatus)")
                    self.failedInjections += 1
                }
                return
            }

            let fileSize = (try? FileManager.default.attributesOfItem(atPath: tempURL.path)[.size] as? NSNumber)?.intValue ?? 0
            guard fileSize > 0 else {
                self.queue.async {
                    NSLog("DOWNLOAD_FAILED #\(seq) (\(filename)) reason=empty_file")
                    self.reportGuardianPlaybackFailure(context: context, error: "download_failed:empty_file")
                    self.failedInjections += 1
                }
                return
            }

            do {
                let localURL = self.cacheDir.appendingPathComponent(self.cacheFilename(for: context.queueItemId, remoteURL: remoteURL))
                if FileManager.default.fileExists(atPath: localURL.path) {
                    try FileManager.default.removeItem(at: localURL)
                }
                try FileManager.default.moveItem(at: tempURL, to: localURL)

                self.queue.async {
                    let latencyMs = Int(Date().timeIntervalSince(downloadStart) * 1000)
                    NSLog("DOWNLOAD_COMPLETE #\(seq) (\(filename)) bytes=\(fileSize) latency_ms=\(latencyMs)")
                    self.injectLocalAudio(
                        localURL: localURL,
                        context: context,
                        seq: seq,
                        filename: filename,
                        downloadLatencyMs: latencyMs
                    )
                }
            } catch {
                self.queue.async {
                    NSLog("DOWNLOAD_FAILED #\(seq) (\(filename)) reason=cache_move_error error=\(error.localizedDescription)")
                    self.reportGuardianPlaybackFailure(context: context, error: "download_failed:cache_move_error:\(error.localizedDescription)")
                    self.failedInjections += 1
                }
            }
        }
        task.resume()
    }

    private func retryDownloadAndInject(
        remoteURL: URL,
        context: PlaybackContext,
        seq: Int,
        filename: String,
        attempt: Int
    ) {
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + Double(attempt) * 0.5) { [weak self] in
            self?.queue.async {
                self?.downloadAndInjectAudio(
                    remoteURL: remoteURL,
                    context: context,
                    seq: seq,
                    filename: filename,
                    attempt: attempt
                )
            }
        }
    }

    private func injectLocalAudio(
        localURL: URL,
        context: PlaybackContext,
        seq: Int,
        filename: String,
        downloadLatencyMs: Int
    ) {
        guard let player = self.audioPlayer, self.isActive else {
            NSLog("INJECT_FAILED #\(seq) (\(filename)) reason=not_active_after_download")
            reportGuardianPlaybackFailure(context: context, error: "not_active_after_download")
            failedInjections += 1
            return
        }

        let audioItem = AVPlayerItem(url: localURL)
        let itemQueuedAt = Date()

        player.publisher(for: \.currentItem)
            .sink { [weak self, weak audioItem] currentItem in
                guard let self = self, let audioItem = audioItem, currentItem === audioItem else { return }
                self.reportGuardianPlaybackStartedIfNeeded(
                    item: audioItem,
                    context: context,
                    readyLatencyMs: Int(Date().timeIntervalSince(itemQueuedAt) * 1000),
                    downloadLatencyMs: downloadLatencyMs
                )
            }
            .store(in: &cancellables)

        // Observe when playback completes
        NotificationCenter.default
            .publisher(for: .AVPlayerItemDidPlayToEndTime, object: audioItem)
            .first()
            .sink { [weak self] _ in
                guard let self = self else { return }
                NSLog("PLAYBACK_COMPLETE #\(seq) (\(filename)) ts=\(Date().timeIntervalSince1970)")
                self.reportGuardianPlaybackCompleted(item: audioItem, context: context)
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
                self.reportGuardianPlaybackFailure(context: context, error: error, item: audioItem)
            }
            .store(in: &cancellables)

        audioItem.publisher(for: \.status)
            .sink { [weak self, weak audioItem] status in
                guard let self = self, let audioItem = audioItem else { return }
                if status == .readyToPlay {
                    NSLog("ITEM_READY #\(seq) (\(filename)) queued_latency_ms=\(Int(Date().timeIntervalSince(itemQueuedAt) * 1000))")
                    if player.currentItem === audioItem {
                        self.reportGuardianPlaybackStartedIfNeeded(
                            item: audioItem,
                            context: context,
                            readyLatencyMs: Int(Date().timeIntervalSince(itemQueuedAt) * 1000),
                            downloadLatencyMs: downloadLatencyMs
                        )
                    }
                } else if status == .failed {
                    let error = audioItem.error?.localizedDescription ?? "item_failed"
                    NSLog("ITEM_FAILED #\(seq) (\(filename)) error=\(error)")
                    self.reportGuardianPlaybackFailure(context: context, error: error, item: audioItem)
                    self.queue.async { self.failedInjections += 1 }
                }
            }
            .store(in: &cancellables)

        let afterItem = player.currentItem
        if player.canInsert(audioItem, after: afterItem) {
            player.insert(audioItem, after: afterItem)
            NSLog("INJECT_OK #\(seq) (\(filename)) position=after_current depth=\(player.items().count) ts=\(Date().timeIntervalSince1970)")
        } else if player.canInsert(audioItem, after: nil) {
            player.insert(audioItem, after: nil)
            NSLog("INJECT_OK #\(seq) (\(filename)) position=end depth=\(player.items().count) ts=\(Date().timeIntervalSince1970)")
        } else {
            NSLog("INJECT_FAILED #\(seq) (\(filename)) reason=cannot_insert")
            reportGuardianPlaybackFailure(context: context, error: "cannot_insert", item: audioItem)
            failedInjections += 1
            return
        }

        if player.currentItem === audioItem {
            reportGuardianPlaybackStartedIfNeeded(
                item: audioItem,
                context: context,
                readyLatencyMs: Int(Date().timeIntervalSince(itemQueuedAt) * 1000),
                downloadLatencyMs: downloadLatencyMs
            )
        }

        // Ensure player is actually playing
        if player.rate == 0 {
            player.play()
        }
    }

    private func reportGuardianPlaybackStartedIfNeeded(
        item: AVPlayerItem,
        context: PlaybackContext,
        readyLatencyMs: Int,
        downloadLatencyMs: Int
    ) {
        queue.async { [weak self] in
            guard let self = self else { return }
            let itemId = ObjectIdentifier(item)
            guard !self.playbackStartedItems.contains(itemId) else { return }
            guard item.status == .readyToPlay else {
                NSLog("PLAYBACK_START_DEFERRED item=\(context.queueItemId) status=\(item.status.rawValue)")
                return
            }

            self.playbackStartedItems.insert(itemId)
            self.playbackStartedAt[itemId] = Date()
            self.activePlaybackContext = context

            var eventMetadata = context.metadata
            eventMetadata["ready_latency_ms"] = readyLatencyMs
            eventMetadata["download_latency_ms"] = downloadLatencyMs
            self.reportPlaybackEvent(
                eventType: "started",
                queueItemId: context.queueItemId,
                traceId: context.traceId,
                triggerType: context.triggerType,
                metadata: eventMetadata
            )
        }
    }

    private func reportGuardianPlaybackCompleted(item: AVPlayerItem, context: PlaybackContext) {
        queue.async { [weak self] in
            guard let self = self else { return }
            let itemId = ObjectIdentifier(item)
            let durationMs = self.playbackStartedAt[itemId].map {
                Int(Date().timeIntervalSince($0) * 1000)
            } ?? 0

            self.reportPlaybackEvent(
                eventType: "completed",
                queueItemId: context.queueItemId,
                traceId: context.traceId,
                triggerType: context.triggerType,
                durationMs: durationMs,
                metadata: context.metadata
            )
            self.playbackStartedAt.removeValue(forKey: itemId)
            self.playbackStartedItems.remove(itemId)
            if self.activePlaybackContext?.queueItemId == context.queueItemId {
                self.activePlaybackContext = nil
            }
            self.successfulInjections += 1
        }
    }

    private func reportGuardianPlaybackFailure(
        context: PlaybackContext,
        error: String,
        item: AVPlayerItem? = nil
    ) {
        var eventMetadata = context.metadata
        eventMetadata["error"] = error
        reportPlaybackEvent(
            eventType: "failed",
            queueItemId: context.queueItemId,
            traceId: context.traceId,
            triggerType: context.triggerType,
            durationMs: 0,
            metadata: eventMetadata
        )

        if let item = item {
            let itemId = ObjectIdentifier(item)
            queue.async { [weak self] in
                self?.playbackStartedAt.removeValue(forKey: itemId)
                self?.playbackStartedItems.remove(itemId)
                if self?.activePlaybackContext?.queueItemId == context.queueItemId {
                    self?.activePlaybackContext = nil
                }
            }
        }
    }

    private func cacheFilename(for queueItemId: String, remoteURL: URL) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
        let safeId = String(queueItemId.map { character in
            character.unicodeScalars.allSatisfy { allowed.contains($0) } ? character : "_"
        })
        let ext = remoteURL.pathExtension.isEmpty ? "mp3" : remoteURL.pathExtension
        return "\(safeId).\(ext)"
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
