import Foundation
import AVFoundation
import Combine

/// GuardianModeManager - Guardian audio playback and polling coordinator
///
/// Architecture:
/// - Polls while Guardian Mode is active without owning the system audio route
/// - Activates AVAudioSession only while a real Guardian playback item is queued
/// - Serializes player mutations on a single DispatchQueue
/// - Reports playback events only for concrete queue items
class GuardianModeManager: NSObject, AVSpeechSynthesizerDelegate {
    static let shared = GuardianModeManager()

    private var audioPlayer: AVQueuePlayer?
    private var isActive = false
    private let queue = DispatchQueue(label: "com.ella.guardianmode")
    private var cancellables = Set<AnyCancellable>()
    private var healthTimer: DispatchSourceTimer?

    // Stats for monitoring
    private var totalInjections: Int = 0
    private var successfulInjections: Int = 0
    private var failedInjections: Int = 0
    private var injectionSequence: Int = 0  // Sequential counter for easy log tracking
    private var playbackStartedItems = Set<ObjectIdentifier>()
    private var playbackContexts: [ObjectIdentifier: PlaybackContext] = [:]
    private let speechSynthesizer = AVSpeechSynthesizer()
    private var fallbackTTSContexts: [ObjectIdentifier: (context: PlaybackContext, startedAt: Date)] = [:]

    private struct PlaybackContext {
        let queueItemId: String
        let traceId: String?
        let triggerType: String?
        let metadata: [String: Any]?
    }

    // Download cache directory
    private lazy var cacheDir: URL = {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("guardian_audio_cache")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    private override init() {
        super.init()
        speechSynthesizer.delegate = self
    }

    // MARK: - Public API

    /// Start Guardian Mode - begins network polling only. Audio starts later when
    /// a real Guardian queue item is ready to play.
    func start() throws {
        try queue.sync {
            guard !isActive else {
                NSLog("GuardianMode: Already active, repairing poller if needed")
                GuardianModePollingService.shared.ensurePolling(reason: "guardian_start_already_active")
                return
            }

            self.isActive = true
            self.totalInjections = 0
            self.successfulInjections = 0
            self.failedInjections = 0
            self.injectionSequence = 0
            self.playbackStartedItems.removeAll()
            self.playbackContexts.removeAll()

            startHealthMonitor()

            GuardianModePollingService.shared.startPolling()

            NSLog("GuardianMode: Started - polling active, audio session idle until playback")
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
            playbackContexts.removeAll()
            isActive = false

            let rate = totalInjections > 0
                ? String(format: "%.1f%%", Double(successfulInjections) / Double(totalInjections) * 100)
                : "N/A"
            NSLog("GuardianMode: Stopped (injections: \(successfulInjections)/\(totalInjections), success rate: \(rate))")

            deactivateGuardianAudioSessionIfIdle(reason: "guardian_stop")
        }
    }

    /// Get current state
    func getState() -> String {
        return queue.sync {
            return isActive ? "active" : "idle"
        }
    }

    /// Called from app lifecycle hooks. If Guardian Mode is still active, make
    /// sure the network poller survived app suspension or backend downtime. Do
    /// not reactivate AVAudioSession unless a real Guardian item is playing.
    func repairIfActive(reason: String) {
        queue.async { [weak self] in
            guard let self = self, self.isActive else { return }

            if !self.playbackStartedItems.isEmpty, let player = self.audioPlayer, player.rate == 0 {
                NSLog("GuardianMode: Repair restarting stalled player reason=\(reason)")
                player.play()
            }

            GuardianModePollingService.shared.ensurePolling(reason: reason)
        }
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
        guard self.isActive else { return }

        GuardianModePollingService.shared.ensurePolling(reason: "guardian_health_check")

        if !playbackStartedItems.isEmpty, let player = audioPlayer, player.rate == 0 {
            NSLog("GuardianMode: HEALTH WARNING - Active Guardian playback stalled, restarting playback")
            player.play()
        }

        let stats = totalInjections > 0
            ? "\(successfulInjections)/\(totalInjections)"
            : "0/0"
        NSLog(
            "GuardianMode: Health OK " +
            "(poller=\(GuardianModePollingService.shared.statusSnapshot()), " +
            "active_playback_items=\(playbackStartedItems.count), " +
            "queued_items=\(audioPlayer?.items().count ?? 0), injections=\(stats))"
        )
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
        guard let queueItemId = queueItemId, !queueItemId.isEmpty else {
            NSLog("PLAYBACK_EVENT_SKIP type=\(eventType) reason=no_queue_item")
            return
        }

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
        body["queue_item_id"] = queueItemId
        if let traceId = traceId {
            body["trace_id"] = traceId
        }
        if !eventMetadata.isEmpty {
            body["metadata"] = eventMetadata
        }
        guard let data = try? JSONSerialization.data(withJSONObject: body) else { return }
        request.httpBody = data

        URLSession.shared.dataTask(with: request) { _, _, _ in }.resume()
        NSLog("PLAYBACK_EVENT type=\(eventType) trace=\(traceId ?? "none") item=\(queueItemId) port=\(portType) device=\(portName.isEmpty ? "unknown" : portName) uid=\(uid)")
    }

    func handleAudioRouteChange(reason: UInt) {
        queue.async { [weak self] in
            guard let self = self,
                  let context = self.playbackContexts.values.first else {
                NSLog("GuardianMode: Route change ignored because no Guardian item is playing")
                return
            }

            self.reportPlaybackEvent(
                eventType: "route_changed",
                queueItemId: context.queueItemId,
                traceId: context.traceId,
                triggerType: context.triggerType,
                metadata: context.metadata
            )
            NSLog("GuardianMode: Route change reported for active Guardian playback reason=\(reason)")
        }
    }

    // MARK: - Audio Injection with Pre-download + Retry

    /// Inject remote audio with pre-download and retry logic
    func injectRemoteAudio(
        audioURL: URL,
        eventId: String,
        traceId: String? = nil,
        triggerType: String? = nil,
        metadata: [String: Any]? = nil,
        fallbackText: String? = nil
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
                    fallbackText: fallbackText,
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
        fallbackText: String?,
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

        guard self.isActive else {
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

        let activationPath: String
        do {
            activationPath = try activateGuardianAudioSessionForPlayback()
        } catch {
            NSLog("INJECT_FAILED #\(seq) (\(filename)) reason=audio_session_activate_failed error=\(error.localizedDescription)")
            var failureMetadata = metadata ?? [:]
            failureMetadata["url"] = remoteURL.absoluteString
            failureMetadata["audio_session_error"] = error.localizedDescription
            failureMetadata["fallback_tts_attempted"] = true
            reportGuardianPlaybackFailure(
                queueItemId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: failureMetadata,
                error: "audio_session_activate_failed"
            )
            speakFallbackText(
                fallbackText ?? extractFallbackText(from: metadata),
                queueItemId: eventId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: failureMetadata
            )
            await MainActor.run { self.failedInjections += 1 }
            return
        }

        let audioItem = AVPlayerItem(url: remoteURL)

        let itemQueuedAt = Date()
        let itemId = ObjectIdentifier(audioItem)
        let player: AVQueuePlayer
        let position: String

        // With the idle silence queue removed, an empty AVQueuePlayer must be
        // created with the remote item already loaded. Inserting into an empty
        // existing queue can leave the item non-current and stuck at .unknown.
        if let existingPlayer = self.audioPlayer,
           existingPlayer.currentItem != nil || !existingPlayer.items().isEmpty {
            player = existingPlayer
            let afterItem = player.currentItem
            if player.canInsert(audioItem, after: afterItem) {
                player.insert(audioItem, after: afterItem)
                position = afterItem == nil ? "end" : "after_current"
            } else if player.canInsert(audioItem, after: nil) {
                player.insert(audioItem, after: nil)
                position = "end"
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
                deactivateGuardianAudioSessionIfIdle(reason: "cannot_insert")
                await MainActor.run { self.failedInjections += 1 }
                return
            }
        } else {
            let queuePlayer = AVQueuePlayer(items: [audioItem])
            queuePlayer.actionAtItemEnd = .advance
            self.audioPlayer = queuePlayer
            player = queuePlayer
            position = "new_player_current"
        }

        player.play()

        NSLog("INJECT_OK #\(seq) (\(filename)) position=\(position) depth=\(player.items().count) ts=\(Date().timeIntervalSince1970)")
        recordPlaybackDebugEvent(
            "inject_ok",
            queueItemId: eventId,
            traceId: traceId,
            triggerType: triggerType,
            metadata: metadata,
            extra: [
                "position": position,
                "depth": player.items().count,
                "player_rate": player.rate,
                "url": remoteURL.absoluteString,
                "audio_session_activation_path": activationPath,
                "is_current_item": player.currentItem === audioItem,
                "current_item_present": player.currentItem != nil
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

        player.publisher(for: \.currentItem)
            .sink { [weak self, weak audioItem] currentItem in
                guard let self = self, let audioItem = audioItem, currentItem === audioItem else { return }
                self.queue.async {
                    guard !self.playbackStartedItems.contains(itemId) else { return }
                    self.playbackStartedItems.insert(itemId)
                    self.playbackContexts[itemId] = PlaybackContext(
                        queueItemId: eventId,
                        traceId: traceId,
                        triggerType: triggerType,
                        metadata: metadata
                    )
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
                self.queue.async {
                    player.remove(audioItem)
                    self.finishPlaybackItem(itemId, reason: "item_error")
                }
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
                self.queue.async {
                    player.remove(audioItem)
                    self.finishPlaybackItem(itemId, reason: "ready_timeout")
                }
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
            self.queue.async {
                player.remove(audioItem)
                self.finishPlaybackItem(itemId, reason: "item_failed")
            }
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
            self.queue.async {
                player.remove(audioItem)
                self.finishPlaybackItem(itemId, reason: "unexpected_status")
            }
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
                    self.finishPlaybackItem(itemId, reason: "completed")
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
                self.queue.async {
                    self.finishPlaybackItem(itemId, reason: "failed_to_play_to_end")
                }
            }
            .store(in: &cancellables)

        // Ensure player is actually playing
        if player.rate == 0 {
            player.play()
        }
    }

    @discardableResult
    private func activateGuardianAudioSessionForPlayback() throws -> String {
        let audioSession = AVAudioSession.sharedInstance()
        let attempts: [(label: String, category: AVAudioSession.Category, mode: AVAudioSession.Mode, options: AVAudioSession.CategoryOptions)] = [
            ("playback_mix", .playback, .default, [.mixWithOthers]),
            ("playback_plain", .playback, .default, []),
            ("ambient_mix", .ambient, .default, [.mixWithOthers])
        ]
        var attemptErrors: [String] = []

        for attempt in attempts {
            do {
                try audioSession.setCategory(attempt.category, mode: attempt.mode, options: attempt.options)
                try audioSession.setActive(true, options: [])
                NSLog("GuardianMode: Audio session activated path=\(attempt.label)")
                return attempt.label
            } catch {
                let errorMessage = "\(attempt.label): \(error.localizedDescription)"
                attemptErrors.append(errorMessage)
                NSLog("GuardianMode: Audio session activation attempt failed \(errorMessage)")
            }
        }

        throw NSError(domain: "GuardianMode", code: -50, userInfo: [
            NSLocalizedDescriptionKey: attemptErrors.joined(separator: " | ")
        ])
    }

    private func extractFallbackText(from metadata: [String: Any]?) -> String? {
        let keys = ["message", "text", "response_text", "response", "content", "transcript"]
        for key in keys {
            if let value = metadata?[key] as? String, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return value
            }
        }
        return nil
    }

    private func speakFallbackText(
        _ text: String?,
        queueItemId: String,
        traceId: String?,
        triggerType: String?,
        metadata: [String: Any]?
    ) {
        let fallback = text?.trimmingCharacters(in: .whitespacesAndNewlines)
        let spokenText = (fallback?.isEmpty == false)
            ? fallback!
            : "Ella has a Guardian response, but the audio could not be played."
        var fallbackMetadata = metadata ?? [:]
        fallbackMetadata["fallback_tts"] = true
        fallbackMetadata["fallback_tts_text_available"] = fallback?.isEmpty == false

        let utterance = AVSpeechUtterance(string: spokenText)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        utterance.rate = 0.5

        let utteranceId = ObjectIdentifier(utterance)
        fallbackTTSContexts[utteranceId] = (
            context: PlaybackContext(
                queueItemId: queueItemId,
                traceId: traceId,
                triggerType: triggerType,
                metadata: fallbackMetadata
            ),
            startedAt: Date()
        )

        recordPlaybackDebugEvent(
            "fallback_tts_start",
            queueItemId: queueItemId,
            traceId: traceId,
            triggerType: triggerType,
            metadata: fallbackMetadata,
            extra: ["text_length": spokenText.count]
        )
        reportPlaybackEvent(
            eventType: "fallback_tts_started",
            queueItemId: queueItemId,
            traceId: traceId,
            triggerType: triggerType,
            durationMs: 0,
            metadata: fallbackMetadata
        )
        speechSynthesizer.speak(utterance)
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        finishFallbackTTS(utterance, eventType: "fallback_tts_completed")
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        finishFallbackTTS(utterance, eventType: "fallback_tts_cancelled")
    }

    private func finishFallbackTTS(_ utterance: AVSpeechUtterance, eventType: String) {
        let utteranceId = ObjectIdentifier(utterance)
        queue.async { [weak self] in
            guard let self = self,
                  let ttsContext = self.fallbackTTSContexts.removeValue(forKey: utteranceId) else { return }

            let durationMs = Int(Date().timeIntervalSince(ttsContext.startedAt) * 1000)
            self.recordPlaybackDebugEvent(
                eventType,
                queueItemId: ttsContext.context.queueItemId,
                traceId: ttsContext.context.traceId,
                triggerType: ttsContext.context.triggerType,
                metadata: ttsContext.context.metadata,
                extra: ["duration_ms": durationMs]
            )
            self.reportPlaybackEvent(
                eventType: eventType,
                queueItemId: ttsContext.context.queueItemId,
                traceId: ttsContext.context.traceId,
                triggerType: ttsContext.context.triggerType,
                durationMs: durationMs,
                metadata: ttsContext.context.metadata
            )
            self.deactivateGuardianAudioSessionIfIdle(reason: eventType)
        }
    }

    private func finishPlaybackItem(_ itemId: ObjectIdentifier, reason: String) {
        playbackStartedItems.remove(itemId)
        playbackContexts.removeValue(forKey: itemId)
        deactivateGuardianAudioSessionIfIdle(reason: reason)
    }

    private func deactivateGuardianAudioSessionIfIdle(reason: String) {
        guard playbackStartedItems.isEmpty, playbackContexts.isEmpty, fallbackTTSContexts.isEmpty else { return }

        if let player = audioPlayer, player.items().isEmpty {
            player.pause()
            player.removeAllItems()
            audioPlayer = nil
        } else if audioPlayer?.items().isEmpty == false {
            return
        }

        do {
            try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            NSLog("GuardianMode: Audio session deactivated reason=\(reason)")
        } catch {
            NSLog("GuardianMode: Audio session deactivate failed reason=\(reason) error=\(error.localizedDescription)")
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
