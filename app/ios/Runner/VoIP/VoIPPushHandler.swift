import Foundation
import PushKit

class VoIPPushHandler {
    static let shared = VoIPPushHandler()

    // Store access tokens for pending incoming calls
    private var pendingTokens: [UUID: String] = [:]

    private init() {}

    func storeToken(_ token: String, for callUUID: UUID) {
        pendingTokens[callUUID] = token
        print("VoIPPushHandler: Stored token for call \(callUUID)")
    }

    func retrieveToken(for callUUID: UUID) -> String? {
        let token = pendingTokens[callUUID]
        pendingTokens[callUUID] = nil // Remove after retrieval
        print("VoIPPushHandler: Retrieved token for call \(callUUID)")
        return token
    }
}
