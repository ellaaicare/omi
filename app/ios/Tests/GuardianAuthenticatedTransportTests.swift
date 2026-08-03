import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

private enum TestFailure: Error {
    case failed(String)
}

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() {
        throw TestFailure.failed(message)
    }
}

private func response(status: Int, url: URL) throws -> HTTPURLResponse {
    guard let value = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil) else {
        throw TestFailure.failed("could not create HTTP response")
    }
    return value
}

private func testPollAddsBearerAndRetriesWithForcedRefresh() async throws {
    var refreshCalls: [Bool] = []
    var requests: [URLRequest] = []
    let transport = GuardianAuthenticatedTransport(
        tokenProvider: { forcingRefresh in
            refreshCalls.append(forcingRefresh)
            return GuardianBearerCredential(
                uid: "firebase-owner",
                token: forcingRefresh ? "fresh-token" : "stale-token"
            )
        },
        transport: { request in
            requests.append(request)
            let status = requests.count == 1 ? 401 : 200
            return (Data(#"{"url":null}"#.utf8), try response(status: status, url: request.url!))
        }
    )
    let request = try require(GuardianNativeRequestFactory.pollRequest(backendURL: "https://api.example.test"))

    _ = try await transport.data(for: request)

    try expect(refreshCalls == [false, true], "401 did not force exactly one token refresh")
    try expect(requests.count == 2, "401 did not retry exactly once")
    try expect(requests[0].value(forHTTPHeaderField: "Authorization") == "Bearer stale-token", "initial bearer missing")
    try expect(requests[1].value(forHTTPHeaderField: "Authorization") == "Bearer fresh-token", "refreshed bearer missing")
    try expect(requests[0].url?.query == nil, "poll request trusted a caller-selected UID")
}

private func testPlaybackBodyCannotCarryCallerSelectedUID() async throws {
    var recordedRequest: URLRequest?
    let transport = GuardianAuthenticatedTransport(
        tokenProvider: { _ in GuardianBearerCredential(uid: "firebase-owner", token: "firebase-token") },
        transport: { request in
            recordedRequest = request
            return (Data(), try response(status: 200, url: request.url!))
        }
    )
    let request = try require(
        GuardianNativeRequestFactory.playbackRequest(
            backendURL: "https://api.example.test",
            body: ["uid": "caller-selected", "event_type": "started", "port_type": "Speaker"]
        )
    )

    _ = try await transport.data(for: request)

    let sent = try require(recordedRequest)
    let body = try JSONSerialization.jsonObject(with: sent.httpBody ?? Data()) as? [String: Any]
    try expect(body?["uid"] == nil, "playback request retained a caller-selected UID")
    try expect(sent.value(forHTTPHeaderField: "Authorization") == "Bearer firebase-token", "playback bearer missing")
}

private func testRefreshOwnerDriftStopsBeforeRetry() async throws {
    var requestCount = 0
    let transport = GuardianAuthenticatedTransport(
        tokenProvider: { forcingRefresh in
            GuardianBearerCredential(uid: forcingRefresh ? "owner-b" : "owner-a", token: "token")
        },
        transport: { request in
            requestCount += 1
            return (Data(), try response(status: 401, url: request.url!))
        }
    )
    let request = try require(GuardianNativeRequestFactory.pollRequest(backendURL: "https://api.example.test"))

    do {
        _ = try await transport.data(for: request)
        throw TestFailure.failed("owner drift unexpectedly retried")
    } catch GuardianAuthenticationError.ownerChanged {
        try expect(requestCount == 1, "owner drift started a cross-account retry")
    }
}

private func require<T>(_ value: T?, _ message: String = "required value missing") throws -> T {
    guard let value else {
        throw TestFailure.failed(message)
    }
    return value
}

@main
private struct GuardianAuthenticatedTransportTestRunner {
    static func main() async throws {
        try await testPollAddsBearerAndRetriesWithForcedRefresh()
        try await testPlaybackBodyCannotCarryCallerSelectedUID()
        try await testRefreshOwnerDriftStopsBeforeRetry()
        print("Guardian authenticated native transport tests passed: 3")
    }
}
