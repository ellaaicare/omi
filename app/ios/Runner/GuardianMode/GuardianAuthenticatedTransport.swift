import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
#if canImport(FirebaseAuth) && !GUARDIAN_AUTH_TRANSPORT_TESTS
import FirebaseAuth
#endif

struct GuardianBearerCredential: Equatable {
    let uid: String
    let token: String
}

enum GuardianAuthenticationError: Error {
    case unavailable
    case ownerChanged
}

/// Reads the current Firebase subject and token as one credential. The subject
/// is checked again after the async token fetch so an account switch cannot
/// release a token under the previous owner.
final class GuardianFirebaseBearerProvider {
    typealias Provider = (_ forcingRefresh: Bool) async throws -> GuardianBearerCredential

    private let provider: Provider

    init(provider: @escaping Provider) {
        self.provider = provider
    }

    func credential(forcingRefresh: Bool) async throws -> GuardianBearerCredential {
        let credential = try await provider(forcingRefresh)
        guard !credential.uid.isEmpty, !credential.token.isEmpty else {
            throw GuardianAuthenticationError.unavailable
        }
        return credential
    }

#if canImport(FirebaseAuth) && !GUARDIAN_AUTH_TRANSPORT_TESTS
    static let shared = GuardianFirebaseBearerProvider { forcingRefresh in
        guard let user = Auth.auth().currentUser else {
            throw GuardianAuthenticationError.unavailable
        }
        let expectedUID = user.uid
        let token = try await user.getIDToken(forcingRefresh: forcingRefresh)
        guard Auth.auth().currentUser?.uid == expectedUID else {
            throw GuardianAuthenticationError.ownerChanged
        }
        return GuardianBearerCredential(uid: expectedUID, token: token)
    }
#else
    static let shared = GuardianFirebaseBearerProvider { _ in
        throw GuardianAuthenticationError.unavailable
    }
#endif
}

/// Firebase-authorized native transport matching the Flutter networking
/// contract: attach the current bearer, then force-refresh and retry once on
/// an HTTP 401. A refreshed credential must retain the exact Firebase owner.
final class GuardianAuthenticatedTransport {
    typealias TokenProvider = (_ forcingRefresh: Bool) async throws -> GuardianBearerCredential
    typealias Transport = (URLRequest) async throws -> (Data, URLResponse)

    private let tokenProvider: TokenProvider
    private let transport: Transport

    private static let session: URLSession = {
        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = 10.0
        configuration.timeoutIntervalForResource = 10.0
        configuration.waitsForConnectivity = true
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: configuration)
    }()

    init(
        tokenProvider: @escaping TokenProvider,
        transport: @escaping Transport
    ) {
        self.tokenProvider = tokenProvider
        self.transport = transport
    }

    static let shared = GuardianAuthenticatedTransport(
        tokenProvider: GuardianFirebaseBearerProvider.shared.credential,
        transport: { request in
            try await session.data(for: request)
        }
    )

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        let initial = try await tokenProvider(false)
        let firstResult = try await transport(authorize(request, token: initial.token))
        guard (firstResult.1 as? HTTPURLResponse)?.statusCode == 401 else {
            return firstResult
        }

        let refreshed = try await tokenProvider(true)
        guard refreshed.uid == initial.uid else {
            throw GuardianAuthenticationError.ownerChanged
        }
        return try await transport(authorize(request, token: refreshed.token))
    }

    private func authorize(_ request: URLRequest, token: String) -> URLRequest {
        var authorized = request
        authorized.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return authorized
    }
}

enum GuardianNativeRequestFactory {
    static func pollRequest(backendURL: String) -> URLRequest? {
        guard let url = URL(string: "\(backendURL)/v1/ella/guardian/next-audio") else {
            return nil
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 10.0
        return request
    }

    static func playbackRequest(backendURL: String, body: [String: Any]) -> URLRequest? {
        guard let url = URL(string: "\(backendURL)/v1/ella/guardian/playback-event") else {
            return nil
        }
        var subjectFreeBody = body
        subjectFreeBody.removeValue(forKey: "uid")
        guard let data = try? JSONSerialization.data(withJSONObject: subjectFreeBody) else {
            return nil
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 3.0
        request.httpBody = data
        return request
    }
}
