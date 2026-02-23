import Foundation
import AVFoundation

class GuardianModeManager: NSObject {
    static let shared = GuardianModeManager()
    
    private var audioPlayer: AVQueuePlayer?
    private weak var playerLooper: AVPlayerLooper?  // FIX: weak to break retain cycle
    private var isActive = false
    private let queue = DispatchQueue(label: "com.ella.guardianmode")  // FIX: thread safety
    
    private override init() {
        super.init()
        setupInterruptionHandling()  // FIX: handle interruptions
    }
    
    /// Start Guardian Mode - begins silent audio loop
    func start() throws {
        try queue.sync {  // FIX: thread-safe
            guard !isActive else {
                print("GuardianMode: Already active, ignoring start()")
                return
            }
            
            // Configure audio session for playback only
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            try audioSession.setActive(true)
            
            do {
                // Load silent audio file
                guard let silenceURL = Bundle.main.url(forResource: "silence_100ms", withExtension: "wav") else {
                    throw NSError(domain: "com.ella.GuardianMode", code: 1, userInfo: [NSLocalizedDescriptionKey: "Silent audio file not found"])
                }
                
                let playerItem = AVPlayerItem(url: silenceURL)
                let queuePlayer = AVQueuePlayer(playerItem: playerItem)
                
                // Loop the silent audio infinitely
                let looper = AVPlayerLooper(player: queuePlayer, templateItem: playerItem)
                
                self.audioPlayer = queuePlayer
                self.playerLooper = looper
                
                // Start playback
                queuePlayer.play()
                isActive = true
                
                print("GuardianMode: Started - silent loop playing")
            } catch {
                try? audioSession.setActive(false)  // FIX: cleanup on error
                throw error
            }
        }
    }
    
    /// Stop Guardian Mode - stops audio and deactivates session
    func stop() {
        queue.sync {  // FIX: thread-safe
            guard isActive else {
                print("GuardianMode: Already stopped, ignoring stop()")
                return
            }
            
            audioPlayer?.pause()
            audioPlayer = nil
            playerLooper = nil
            
            do {
                try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            } catch {
                print("GuardianMode: Error deactivating audio session: \(error)")
            }
            
            isActive = false
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
            
            let playerItem = AVPlayerItem(url: silenceURL)
            let looper = AVPlayerLooper(player: player, templateItem: playerItem)
            self.playerLooper = looper
            player.play()
        }
    }
    
    /// Get current Guardian Mode state
    func getState() -> String {
        return queue.sync {  // FIX: thread-safe
            return isActive ? "active" : "idle"
        }
    }
    
    // FIX: Add interruption handling
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
    
    // FIX: Add deinit cleanup
    deinit {
        stop()
        NotificationCenter.default.removeObserver(self)
    }
}
