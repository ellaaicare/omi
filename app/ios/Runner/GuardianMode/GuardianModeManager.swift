import Foundation
import AVFoundation
import Combine

class GuardianModeManager: NSObject {
    static let shared = GuardianModeManager()
    
    private var audioPlayer: AVQueuePlayer?
    private var isActive = false
    private let queue = DispatchQueue(label: "com.ella.guardianmode")
    private var cancellables = Set<AnyCancellable>()
    
    private override init() {
        super.init()
    }
    
    /// Start Guardian Mode - begins silent audio loop
    func start() throws {
        try queue.sync {
            guard !isActive else {
                print("GuardianMode: Already active, ignoring start()")
                return
            }
            
            // Configure audio session for playback only
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            try audioSession.setActive(true)
            
            // Load silence file
            guard let silenceURL = Bundle.main.url(forResource: "silence_100ms", withExtension: "wav") else {
                throw NSError(domain: "GuardianMode", code: 1, userInfo: [
                    NSLocalizedDescriptionKey: "Silent audio file not found"
                ])
            }
            
            // Pre-queue 50 silence items (NOT using looper)
            let silenceItems = (0..<20).map { _ in AVPlayerItem(url: silenceURL) }
            let queuePlayer = AVQueuePlayer(items: silenceItems)
            
            self.audioPlayer = queuePlayer
            self.isActive = true
            
            // Setup observer for item endings
            setupItemEndObserver()
            
            queuePlayer.play()
            
            // Start polling for remote audio
            GuardianModePollingService.shared.startPolling()
            print("GuardianMode: Polling started")
            
            print("GuardianMode: Started - silent queue playing with \(silenceItems.count) items")
        }
    }
    
    /// Stop Guardian Mode - stops audio and deactivates session
    func stop() {
        queue.sync {
            // Stop polling
            GuardianModePollingService.shared.stopPolling()
            
            guard isActive else {
                print("GuardianMode: Already stopped, ignoring stop()")
                return
            }
            
            audioPlayer?.pause()
            audioPlayer?.removeAllItems()
            audioPlayer = nil
            cancellables.removeAll()  // Clean up Combine subscriptions
            isActive = false
            
            do {
                try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            } catch {
                print("GuardianMode: Error deactivating audio session: \(error)")
            }
            
            print("GuardianMode: Stopped")
        }
    }
    
    /// Get current Guardian Mode state
    func getState() -> String {
        return queue.sync {
            return isActive ? "active" : "idle"
        }
    }
    
    // MARK: - Queue Management
    
    private func setupItemEndObserver() {
        // Using Combine for modern iOS observation pattern
        // Watch ALL AVPlayerItem endings (not just the first item)
        NotificationCenter.default
            .publisher(for: .AVPlayerItemDidPlayToEndTime)
            .receive(on: RunLoop.main)
            .sink { [weak self] notification in
                self?.handleItemEnd(notification: notification)
            }
            .store(in: &cancellables)
    }
    
    private func handleItemEnd(notification: Notification) {
        queue.async { [weak self] in
            guard let self = self,
                  let player = self.audioPlayer,
                  self.isActive else { return }
            
            // Check queue depth - keep at least 10 items buffered
            let queueDepth = player.items().count
            
            if queueDepth < 5 {
                // Queue more silence to maintain buffer
                self.queueSilence()
            }
        }
    }
    
    func queueSilence() {
        queue.async { [weak self] in
            guard let self = self,
                  let player = self.audioPlayer,
                  let silenceURL = Bundle.main.url(forResource: "silence_100ms", withExtension: "wav"),
                  self.isActive else { return }
            
            let silenceItem = AVPlayerItem(url: silenceURL)
            
            // Append to end of queue
            player.insert(silenceItem, after: nil)
            
            print("GuardianMode: Queued silence (queue depth: \(player.items().count))")
        }
    }

    func injectRemoteAudio(audioURL: URL, eventId: String) {
        queue.async { [weak self] in
            guard let self = self,
                  let player = self.audioPlayer,
                  self.isActive else {
                print("GuardianMode: Cannot inject audio - not active")
                return
            }

            print("DOWNLOAD_START(\(eventId)) ts=\(Date().timeIntervalSince1970)")
            print("GuardianMode: Injecting remote audio: \(audioURL.absoluteString)")

            // Create player item from remote URL
            let audioItem = AVPlayerItem(url: audioURL)
            
            // Local cancellables set to avoid threading issues and memory growth
            var injectionObservers = Set<AnyCancellable>()
            
            // Fix race condition: Check immediate status for fast-loading items
            if audioItem.status == .readyToPlay {
                print("INJECTION(\(eventId)) ts=\(Date().timeIntervalSince1970)")
            } else if audioItem.status == .failed {
                print("INJECTION_FAILED(\(eventId)) error=\(audioItem.error?.localizedDescription ?? "unknown")")
            }

            // Also observe future status changes (for items still loading)
            audioItem.publisher(for: \.status)
                .sink { status in
                    if status == .readyToPlay {
                        print("INJECTION(\(eventId)) ts=\(Date().timeIntervalSince1970)")
                    } else if status == .failed {
                        print("INJECTION_FAILED(\(eventId)) error=\(audioItem.error?.localizedDescription ?? "unknown")")
                    }
                }
                .store(in: &injectionObservers)

            // Insert to play NEXT (after currently playing item)
            if player.canInsert(audioItem, after: player.currentItem) {
                player.insert(audioItem, after: player.currentItem)
                print("GuardianMode: Remote audio queued at position 2")
                
                // Observe when playback actually starts
                NotificationCenter.default
                    .publisher(for: .AVPlayerItemNewAccessLogEntry, object: audioItem)
                    .first()
                    .sink { _ in
                        print("PLAYBACK_START(\(eventId)) ts=\(Date().timeIntervalSince1970)")
                    }
                    .store(in: &injectionObservers)
            } else {
                print("GuardianMode: ERROR - Cannot insert audio item")
            }
            
            // Thread-safe: Move observers to main cancellables on main queue
            DispatchQueue.main.async { [weak self] in
                self?.cancellables.formUnion(injectionObservers)
            }
        }
    }
    
    deinit {
        stop()
    }
}
