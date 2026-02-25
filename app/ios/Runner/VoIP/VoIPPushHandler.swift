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

    func handleIncomingPush(payload: PKPushPayload, completion: @escaping (Bool) -> Void) {
        print("VoIPPushHandler: Received VoIP push")

        // Parse payload
        guard let callerName = payload.dictionaryPayload["caller"] as? String,
              let accessToken = payload.dictionaryPayload["access_token"] as? String else {
            print("VoIPPushHandler: ERROR - Invalid payload format")
            completion(false)
            return
        }

        print("VoIPPushHandler: Incoming call from \(callerName)")

        // Generate call UUID
        let callUUID = UUID()

        // Store access token for when user answers
        storeToken(accessToken, for: callUUID)

        // CRITICAL: Report to CallKit within 10s or iOS terminates app
        CallKitProvider.shared.reportIncomingCall(uuid: callUUID, caller: callerName) { error in
            if let error = error {
                print("VoIPPushHandler: ERROR reporting call: \(error.localizedDescription)")
                completion(false)
            } else {
                print("VoIPPushHandler: Successfully reported incoming call")
                completion(true)
            }
        }
    }
}
