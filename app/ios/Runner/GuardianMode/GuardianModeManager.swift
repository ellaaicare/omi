import Foundation
import AVFoundation

class GuardianModeManager: NSObject {
    static let shared = GuardianModeManager()
    
    private var audioPlayer: AVQueuePlayer?
    private var playerLooper: AVPlayerLooper?
    private var isActive = false
    
    private override init() {
        super.init()
    }
    
    /// Start Guardian Mode - begins silent audio loop
    func start() throws {
        guard !isActive else {
            print("GuardianMode: Already active, ignoring start()")
            return
        }
        
        // Configure audio session for playback only
        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(.playback, mode: .default, options: [.mixWithOthers])
        try audioSession.setActive(true)
        
        // Load silent audio file
        guard let silenceURL = Bundle.main.url(forResource: "silence_100ms", withExtension: "wav", subdirectory: "GuardianMode") else {
            throw NSError(domain: "GuardianMode", code: 1, userInfo: [NSLocalizedDescriptionKey: "Silent audio file not found"])
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
    }
    
    /// Stop Guardian Mode - stops audio and deactivates session
    func stop() {
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
    
    /// Inject an audio clip into the playback queue
    /// - Parameter audioURL: URL to audio file to play
    func injectAudioClip(audioURL: URL) {
        guard isActive, let player = audioPlayer else {
            print("GuardianMode: Cannot inject clip - not active")
            return
        }
        
        let clipItem = AVPlayerItem(url: audioURL)
        player.insert(clipItem, after: player.currentItem)
        
        print("GuardianMode: Injected audio clip: \(audioURL.lastPathComponent)")
    }
    
    /// Get current Guardian Mode state
    func getState() -> String {
        return isActive ? "active" : "idle"
    }
}
