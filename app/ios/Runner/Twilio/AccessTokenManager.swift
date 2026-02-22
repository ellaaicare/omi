import Foundation

class AccessTokenManager {
    static let shared = AccessTokenManager()

    private var cachedToken: String?
    private var tokenExpiry: Date?

    private let tokenEndpoint = "https://n8n.ella-ai-care.com/webhook/twilio/token"

    private init() {}

    func getAccessToken(completion: @escaping (String?, Error?) -> Void) {
        // Check if cached token is still valid
        if let token = cachedToken,
           let expiry = tokenExpiry,
           expiry.timeIntervalSinceNow > 3600 { // >1 hour remaining
            print("AccessTokenManager: Using cached token")
            completion(token, nil)
            return
        }

        // Fetch new token
        refreshToken(completion: completion)
    }

    func refreshToken(completion: @escaping (String?, Error?) -> Void) {
        print("AccessTokenManager: Fetching new token from backend")

        // TODO: Get actual user_id from app state
        let userId = "test_user_1"

        guard let url = URL(string: tokenEndpoint) else {
            completion(nil, NSError(domain: "AccessTokenManager", code: -1, userInfo: [NSLocalizedDescriptionKey: "Invalid URL"]))
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body = ["user_id": userId]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            if let error = error {
                print("AccessTokenManager: ERROR fetching token: \(error.localizedDescription)")
                completion(nil, error)
                return
            }

            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let token = json["token"] as? String,
                  let expiresAtString = json["expires_at"] as? String else {
                print("AccessTokenManager: ERROR parsing token response")
                completion(nil, NSError(domain: "AccessTokenManager", code: -2, userInfo: [NSLocalizedDescriptionKey: "Invalid response"]))
                return
            }

            // Parse expiry date
            let dateFormatter = ISO8601DateFormatter()
            let expiry = dateFormatter.date(from: expiresAtString)

            // Cache token
            self?.cachedToken = token
            self?.tokenExpiry = expiry

            print("AccessTokenManager: Token fetched successfully")
            completion(token, nil)
        }.resume()
    }
}
