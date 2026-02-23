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
        setupInterruptionHandling()
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
    
    /// Inject an audio clip into the playback queue
    /// - Parameter audioURL: URL to audio file to play
    func injectAudioClip(audioURL: URL) {
        queue.async {
            guard self.isActive, let player = self.audioPlayer else {
                NSLog("[GuardianMode] Cannot inject - not active")
                return
            }
            
            NSLog("[GuardianMode] Playing clip: %@", audioURL.lastPathComponent)
            
            // Stop current playback
            player.pause()
            player.removeAllItems()
            
            // Play the TTS clip
            let clipItem = AVPlayerItem(url: audioURL)
            player.replaceCurrentItem(with: clipItem)
            player.play()
            
            // Wait 3 seconds for clip to finish, then restart loop and cleanup
            DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
                // Cleanup the file
                try? FileManager.default.removeItem(at: audioURL)
                NSLog("[GuardianMode] Cleaned up file")
                
                // Restart silent loop
                self?.restartSilentLoop()
            }
        }
    }
    
    private func restartSilentLoop() {
        queue.async {
            guard self.isActive, let player = self.audioPlayer else { return }
            
            NSLog("[GuardianMode] Restarting loop")
            
            guard let silenceURL = Bundle.main.url(forResource: "silence_100ms", withExtension: "wav") else {
                return
            }
            
            // Re-queue silence items
            let silenceItems = (0..<50).map { _ in AVPlayerItem(url: silenceURL) }
            for item in silenceItems {
                player.insert(item, after: nil)
            }
            player.play()
            
            // Re-setup observer
            self.setupItemEndObserver()
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
        NotificationCenter.default
            .publisher(for: .AVPlayerItemDidPlayToEndTime, object: audioPlayer?.currentItem)
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
    
    // MARK: - Interruption Handling
    
    private func setupInterruptionHandling() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleInterruption),
            name: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance()
        )
    }
    
    @objc private func handleInterruption(notification: Notification) {
        guard let userInfo = notification.userInfo,
              let typeValue = userInfo[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: typeValue) else {
            return
        }
        
        if type == .ended, isActive {
            audioPlayer?.play()
            print("GuardianMode: Resumed after interruption")
        }
    }
    
    deinit {
        stop()
        NotificationCenter.default.removeObserver(self)
    }
}
