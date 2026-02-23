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
            let silenceItems = (0..<50).map { _ in AVPlayerItem(url: silenceURL) }
            let queuePlayer = AVQueuePlayer(items: silenceItems)
            
            self.audioPlayer = queuePlayer
            self.isActive = true
            
            // Setup observer for item endings
            setupItemEndObserver()
            
            queuePlayer.play()
            
            print("GuardianMode: Started - silent queue playing with \(silenceItems.count) items")
        }
    }
    
    /// Stop Guardian Mode - stops audio and deactivates session
    func stop() {
        queue.sync {
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
            
            if queueDepth < 10 {
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
    
    deinit {
        stop()
    }
}
