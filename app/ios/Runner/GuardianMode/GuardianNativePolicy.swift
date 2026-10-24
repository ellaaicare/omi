import Foundation
#if canImport(FirebaseAuth) && !GUARDIAN_NATIVE_POLICY_TESTS
import FirebaseAuth
#endif

final class GuardianModeAvailability {
    static let shared = GuardianModeAvailability()

    private let leaseGate = GuardianWorkLeaseGate()

    private init() {}

    var isEnabled: Bool {
        leaseGate.isEnabled
    }

    var currentUID: String? {
        leaseGate.currentUID
    }

    func configure(enabled: Bool, uid: String?) {
        leaseGate.configure(enabled: enabled, uid: uid)
    }

    func disable() {
        leaseGate.disable()
    }

    /// Observes the Firebase owner under the same lock as generation checks.
    /// A changed owner always disables Guardian and invalidates every old lease.
    @discardableResult
    func invalidateIfUIDChanged(_ uid: String?) -> Bool {
        leaseGate.invalidateIfUIDChanged(uid)
    }

    func captureLease() -> GuardianWorkLease? {
        guard let uid = currentUID, GuardianOutputStopBarrier.shared.operation(for: uid) == nil else { return nil }
        return leaseGate.captureLease()
    }

    func retireOutput(_ lease: GuardianWorkLease) -> GuardianWorkLease? {
        leaseGate.retireOutput(lease)
    }

    func captureOwnerLease() -> GuardianWorkLease? { leaseGate.captureOwnerLease() }
    func isExactOwnerLease(_ lease: GuardianWorkLease) -> Bool { leaseGate.isExactOwnerLease(lease) }

    func isCurrent(_ lease: GuardianWorkLease) -> Bool {
        leaseGate.isCurrent(lease)
    }

    func performIfCurrent(_ lease: GuardianWorkLease, _ sideEffect: () -> Bool) -> Bool {
        leaseGate.performIfCurrent(lease, sideEffect)
    }
}

struct GuardianWorkLease: Equatable, Sendable {
    fileprivate let generation: UInt64
    let uid: String
}

struct GuardianBearerCredential: Equatable, Sendable {
    let uid: String
    let token: String
}

enum GuardianCredentialError: Error {
    case unavailable
    case ownerChanged
}

/// Fetches a Firebase bearer for one exact composite lease. The Firebase user
/// is checked before and after token refresh, and the lease is checked again
/// after the await before the credential can be released to transport code.
final class GuardianFirebaseTokenBridge: @unchecked Sendable {
    typealias Provider = (String, Bool) async throws -> GuardianBearerCredential

    private let provider: Provider

    init(provider: @escaping Provider) {
        self.provider = provider
    }

    func credential(for lease: GuardianWorkLease) async throws -> GuardianBearerCredential {
        try await credential(for: lease, forcingRefresh: false)
    }

    func credential(
        for lease: GuardianWorkLease,
        forcingRefresh: Bool
    ) async throws -> GuardianBearerCredential {
        let credential = try await provider(lease.uid, forcingRefresh)
        guard credential.uid == lease.uid,
              !credential.token.isEmpty,
              GuardianModeAvailability.shared.isCurrent(lease) else {
            throw GuardianCredentialError.ownerChanged
        }
        return credential
    }

#if canImport(FirebaseAuth) && !GUARDIAN_NATIVE_POLICY_TESTS
    static let shared = GuardianFirebaseTokenBridge { expectedUID, forcingRefresh in
        guard let user = Auth.auth().currentUser, user.uid == expectedUID else {
            throw GuardianCredentialError.ownerChanged
        }
        let token = try await user.getIDToken(forcingRefresh: forcingRefresh)
        guard Auth.auth().currentUser?.uid == expectedUID else {
            throw GuardianCredentialError.ownerChanged
        }
        return GuardianBearerCredential(uid: expectedUID, token: token)
    }
#else
    static let shared = GuardianFirebaseTokenBridge { _, _ in
        throw GuardianCredentialError.unavailable
    }
#endif
}

/// A composite generation + Firebase UID lease whose validation and side
/// effect execute under one lock. Once disable or owner invalidation returns,
/// no older lease can begin another side effect.
final class GuardianWorkLeaseGate {
    private let lock = NSLock()
    private var enabled = false
    private var outputSuspended = false
    private var generation: UInt64 = 0
    private var uid: String?

    var isEnabled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return enabled
    }

    var currentUID: String? {
        lock.lock()
        defer { lock.unlock() }
        return uid
    }

    @discardableResult
    func configure(enabled: Bool, uid: String?) -> UInt64 {
        let normalizedUID = Self.normalized(uid)
        lock.lock()
        defer { lock.unlock() }
        let shouldEnable = enabled && normalizedUID != nil
        if self.enabled == shouldEnable, self.uid == normalizedUID {
            return generation
        }
        generation &+= 1
        self.enabled = shouldEnable
        if self.uid != normalizedUID { outputSuspended = false }
        self.uid = normalizedUID
        return generation
    }

    @discardableResult
    func disable() -> UInt64 {
        lock.lock()
        defer { lock.unlock() }
        guard enabled else { return generation }
        generation &+= 1
        enabled = false
        return generation
    }

    @discardableResult
    func invalidateIfUIDChanged(_ uid: String?) -> Bool {
        let normalizedUID = Self.normalized(uid)
        lock.lock()
        defer { lock.unlock() }
        guard self.uid != normalizedUID else { return false }
        generation &+= 1
        enabled = false
        outputSuspended = false
        self.uid = normalizedUID
        return true
    }

    /// Invalidates every outstanding output callback without changing mode or owner.
    func retireOutput(_ lease: GuardianWorkLease) -> GuardianWorkLease? {
        lock.lock()
        defer { lock.unlock() }
        guard enabled, uid == lease.uid, generation == lease.generation else { return nil }
        generation &+= 1
        outputSuspended = true
        return GuardianWorkLease(generation: generation, uid: lease.uid)
    }

    func captureOwnerLease() -> GuardianWorkLease? {
        lock.lock(); defer { lock.unlock() }
        guard enabled, let uid else { return nil }
        return GuardianWorkLease(generation: generation, uid: uid)
    }
    func isExactOwnerLease(_ lease: GuardianWorkLease) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return enabled && generation == lease.generation && uid == lease.uid
    }

    func captureLease() -> GuardianWorkLease? {
        lock.lock()
        defer { lock.unlock() }
        guard enabled, !outputSuspended, let uid else { return nil }
        return GuardianWorkLease(generation: generation, uid: uid)
    }

    func isCurrent(_ lease: GuardianWorkLease) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return enabled && !outputSuspended && generation == lease.generation && uid == lease.uid
    }

    @discardableResult
    func performIfCurrent(_ lease: GuardianWorkLease, _ sideEffect: () -> Bool) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard enabled && !outputSuspended && generation == lease.generation && uid == lease.uid else { return false }
        return sideEffect()
    }

    private static func normalized(_ uid: String?) -> String? {
        guard let uid = uid?.trimmingCharacters(in: .whitespacesAndNewlines),
              !uid.isEmpty,
              uid != "unknown" else { return nil }
        return uid
    }
}

struct GuardianPlaybackEvent {
    let eventType: String
    let queueItemId: String?
    let traceId: String?
    let triggerType: String?
    let portType: String
    let portName: String
    let deviceUID: String
    let durationMs: Int
    let metadata: [String: Any]?
}

enum GuardianPlaybackReportOutcome: Equatable {
    case accepted(statusCode: Int)
    case rejected(statusCode: Int)
    case invalidResponse
    case transportFailed
    case cancelled
    case authorityChanged
    case unavailable

    var diagnosticCategory: String {
        switch self {
        case .accepted: return "http_accepted"
        case .rejected: return "http_rejected"
        case .invalidResponse: return "invalid_response"
        case .transportFailed: return "transport_failed"
        case .cancelled: return "cancelled"
        case .authorityChanged: return "authority_changed"
        case .unavailable: return "unavailable"
        }
    }

    var diagnosticHTTPStatus: Int {
        switch self {
        case .accepted(let statusCode) where (200...299).contains(statusCode):
            return statusCode
        case .rejected(let statusCode) where (100...599).contains(statusCode):
            return statusCode
        default:
            return 0
        }
    }
}

/// Holds the completion and cancellation together so cancellation finishes
/// even if a transport never calls back, and a late callback is harmless.
private final class GuardianPlaybackReportAttempt: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<URLResponse, Error>?
    private var cancellation: (() -> Void)?
    private var completed = false
    private var cancelled = false

    func install(_ continuation: CheckedContinuation<URLResponse, Error>) {
        lock.lock()
        if completed {
            lock.unlock()
            continuation.resume(throwing: CancellationError())
        } else {
            self.continuation = continuation
            lock.unlock()
        }
    }

    func finish(_ result: Result<URLResponse, Error>) {
        lock.lock()
        guard !completed else { lock.unlock(); return }
        completed = true
        let continuation = self.continuation
        self.continuation = nil
        cancellation = nil
        lock.unlock()
        continuation?.resume(with: result)
    }

    func setCancellation(_ cancellation: @escaping () -> Void) {
        lock.lock()
        let shouldCancel = cancelled
        if !completed { self.cancellation = cancellation }
        lock.unlock()
        if shouldCancel { cancellation() }
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let cancellation = self.cancellation
        self.cancellation = nil
        lock.unlock()
        finish(.failure(CancellationError()))
        cancellation?()
    }
}

/// Transport starts inside the exact lease+UID lock, as in native polling.
/// Success means an HTTP ACK under the same lease, never merely scheduling.
final class GuardianPlaybackReporter: @unchecked Sendable {
    typealias TokenProvider = (GuardianWorkLease) async throws -> GuardianBearerCredential
    typealias TransportCompletion = (Result<URLResponse, Error>) -> Void
    typealias Transport = (URLRequest, @escaping TransportCompletion) -> (() -> Void)

    private let backendURL: () -> String
    private let tokenProvider: TokenProvider
    private let transport: Transport

    init(
        backendURL: @escaping () -> String,
        tokenProvider: @escaping TokenProvider,
        transport: @escaping Transport
    ) {
        self.backendURL = backendURL
        self.tokenProvider = tokenProvider
        self.transport = transport
    }

    @discardableResult
    func report(_ event: GuardianPlaybackEvent, lease: GuardianWorkLease) async -> GuardianPlaybackReportOutcome {
        do {
            guard !Task.isCancelled else { return .cancelled }
            let credential = try await tokenProvider(lease)
            guard credential.uid == lease.uid,
                !credential.token.isEmpty,
                GuardianModeAvailability.shared.isCurrent(lease)
            else {
                return .authorityChanged
            }
            guard !Task.isCancelled else { return .cancelled }
            guard let url = URL(string: "\(backendURL())/v1/ella/guardian/playback-event") else { return .unavailable }

            var eventMetadata = event.metadata ?? [:]
            if let triggerType = event.triggerType {
                eventMetadata["trigger_type"] = triggerType
            }
            var body: [String: Any] = [
                "uid": lease.uid,
                "event_type": event.eventType,
                "port_type": event.portType,
                "port_name": event.portName,
                "device_uid": event.deviceUID,
                "duration_ms": event.durationMs,
            ]
            if let queueItemId = event.queueItemId { body["queue_item_id"] = queueItemId }
            if let traceId = event.traceId { body["trace_id"] = traceId }
            if !eventMetadata.isEmpty { body["metadata"] = eventMetadata }
            guard let data = try? JSONSerialization.data(withJSONObject: body) else { return .unavailable }

            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue("Bearer \(credential.token)", forHTTPHeaderField: "Authorization")
            request.timeoutInterval = 3.0
            request.httpBody = data

            let attempt = GuardianPlaybackReportAttempt()
            let response = try await withTaskCancellationHandler {
                try await withCheckedThrowingContinuation { continuation in
                    attempt.install(continuation)
                    let started = GuardianModeAvailability.shared.performIfCurrent(lease) {
                        guard !Task.isCancelled else { return false }
                        let cancellation = transport(request, attempt.finish)
                        attempt.setCancellation(cancellation)
                        return true
                    }
                    if !started {
                        attempt.finish(.failure(GuardianCredentialError.ownerChanged))
                    }
                }
            } onCancel: {
                attempt.cancel()
            }
            guard !Task.isCancelled else { return .cancelled }
            guard GuardianModeAvailability.shared.isCurrent(lease) else { return .authorityChanged }
            guard let http = response as? HTTPURLResponse else { return .invalidResponse }
            return (200..<300).contains(http.statusCode)
                ? .accepted(statusCode: http.statusCode) : .rejected(statusCode: http.statusCode)
        } catch is CancellationError {
            return .cancelled
        } catch GuardianCredentialError.ownerChanged {
            return .authorityChanged
        } catch GuardianCredentialError.unavailable {
            return .unavailable
        } catch {
            if Task.isCancelled { return .cancelled }
            if !GuardianModeAvailability.shared.isCurrent(lease) { return .authorityChanged }
            return .transportFailed
        }
    }
}

enum GuardianPlaybackReadiness: Equatable {
    case ready(durationMs: Int)
    case failed(String)
    case cancelled
}

/// Production manager effect sequence. Every synchronous effect is authorized
/// together with the same composite lease, including the checks after the
/// readiness await. GuardianModeManager supplies the real AVQueuePlayer
/// closures; tests supply counters while executing this identical sequence.
final class GuardianModeManagerEffectPath {
    struct Operations {
        let perform: (GuardianWorkLease, () -> Bool) -> Bool
        let insert: () -> Bool
        let awaitReadiness: () async -> GuardianPlaybackReadiness
        let reportStarted: (Int) -> Void
        let reportFailed: (String) -> Void
        let registerCompletion: () -> Bool
        let play: () -> Bool
    }

    @discardableResult
    func execute(lease: GuardianWorkLease, operations: Operations) async -> Bool {
        guard !Task.isCancelled else { return false }
        let inserted = operations.perform(lease, operations.insert)
        guard inserted else { return false }

        let readiness = await operations.awaitReadiness()
        guard !Task.isCancelled, GuardianModeAvailability.shared.isCurrent(lease) else { return false }

        switch readiness {
        case .cancelled:
            return false
        case .failed(let error):
            _ = operations.perform(lease) {
                operations.reportFailed(error)
                return true
            }
            return false
        case .ready(let durationMs):
            let reported = operations.perform(lease) {
                operations.reportStarted(durationMs)
                return true
            }
            guard reported else { return false }
        }

        let registered = operations.perform(lease, operations.registerCompletion)
        guard registered else { return false }
        return operations.perform(lease, operations.play)
    }
}

enum GuardianNotificationLifecycle: String, CaseIterable {
    case foreground
    case background
    case terminated
}

enum GuardianNotificationDisposition: Equatable {
    case forward
    case suppress
}

/// Native policy for data-only Guardian/Ella pushes. Public and invitation
/// builds never forward these payloads to Flutter, including at cold start.
enum GuardianNotificationPolicy {
    static let notificationGroupKey = "ella.guardian.notifications"
    static let notificationCategoryIdentifier = "ELLA_GUARDIAN"

    private static let guardianTypes: Set<String> = [
        "ella_notification",
        "ella_emergency_confirmation",
        "guardian_notification",
        "guardian_alert",
        "emergency",
    ]

    static func isGuardianPayload(_ userInfo: [AnyHashable: Any]) -> Bool {
        let payload = flattenedPayload(userInfo)
        if boolValue(payload["ella_guardian_audio"]) { return true }

        let type = normalized(payload["type"])
        if guardianTypes.contains(type) { return true }
        if normalized(payload["urgency"]) == "emergency" { return true }

        let markerKeys = [
            "type",
            "subtype",
            "notification_type",
            "trigger_type",
            "guardian_mode",
            "category",
            "source",
            "navigate_to",
        ]
        let values = markerKeys.map { normalized(payload[$0]) }.joined(separator: " ")
        return ["guardian", "whisper", "wake_word", "caregiver", "emergency"]
            .contains(where: values.contains)
    }

    static func disposition(
        for userInfo: [AnyHashable: Any],
        lifecycle: GuardianNotificationLifecycle,
        encodedDartDefines: String?
    ) -> GuardianNotificationDisposition {
        _ = lifecycle // The boundary is intentionally identical in every lifecycle.
        guard isGuardianPayload(userInfo) else { return .forward }
        return allowsGuardianDelivery(encodedDartDefines: encodedDartDefines) ? .forward : .suppress
    }

    static func allowsGuardianDelivery(encodedDartDefines: String?) -> Bool {
        guard let defines = decodeDartDefines(encodedDartDefines) else { return false }
        guard defines["ELLA_GUARDIAN_ENABLED"] == "true" else { return false }
        guard defines["ELLA_PUBLIC_BUILD"] != "true" else { return false }
        guard defines["ELLA_ENTITLEMENT_GATE"] != "true" else { return false }
        return true
    }

    static func isScopedGuardianNotification(
        userInfo: [AnyHashable: Any],
        threadIdentifier: String,
        categoryIdentifier: String
    ) -> Bool {
        threadIdentifier == notificationGroupKey ||
            categoryIdentifier == notificationCategoryIdentifier ||
            isGuardianPayload(userInfo)
    }

    private static func decodeDartDefines(_ encoded: String?) -> [String: String]? {
        guard let encoded, !encoded.isEmpty, !encoded.contains("$(") else { return [:] }
        var result: [String: String] = [:]
        for value in encoded.split(separator: ",", omittingEmptySubsequences: true) {
            guard let data = Data(base64Encoded: String(value)),
                  let decoded = String(data: data, encoding: .utf8),
                  let separator = decoded.firstIndex(of: "=") else {
                return nil
            }
            let key = String(decoded[..<separator])
            let rawValue = String(decoded[decoded.index(after: separator)...]).lowercased()
            result[key] = rawValue
        }
        return result
    }

    private static func flattenedPayload(_ userInfo: [AnyHashable: Any]) -> [String: Any] {
        var flattened: [String: Any] = [:]
        for (key, value) in userInfo {
            flattened[String(describing: key)] = value
        }
        if let nested = userInfo["data"] as? [String: Any] {
            flattened.merge(nested) { current, _ in current }
        }
        return flattened
    }

    private static func normalized(_ value: Any?) -> String {
        guard let value else { return "" }
        return String(describing: value).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    private static func boolValue(_ value: Any?) -> Bool {
        value as? Bool == true || normalized(value) == "true"
    }
}

/// Durable owner-scoped Stop barrier. Only the server discard integration may
/// clear it after matching the operation receipt; no start/configure path clears it.
final class GuardianOutputStopBarrier {
    static let shared = GuardianOutputStopBarrier(defaults: .standard)
    private let defaults: UserDefaults
    private let lock = NSLock()
    private let key = "guardianOutputDiscardBarriers"
    init(defaults: UserDefaults) { self.defaults = defaults }
    func operation(for uid: String) -> String? {
        lock.lock(); defer { lock.unlock() }
        return (defaults.dictionary(forKey: key) as? [String: String])?[uid]
    }
    func record(uid: String, operation: String) {
        lock.lock(); defer { lock.unlock() }
        var barriers = defaults.dictionary(forKey: key) as? [String: String] ?? [:]
        barriers[uid] = operation
        defaults.set(barriers, forKey: key)
    }
}

/// Strict delivery media ticket, independent of legacy polling/health recovery.
struct GuardianStrictMediaTicket: Equatable {
    let uid: String
    let epoch: String
    let session: String
    let generation: Int
    let presentation: String
}

final class GuardianStrictMediaGate {
    private let lock = NSLock()
    private var ticket: GuardianStrictMediaTicket?
    private var closed = true
    private var floor = 0
    private var retiredEpochs = Set<String>()
    func activate(_ next: GuardianStrictMediaTicket) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard next.generation >= floor, !retiredEpochs.contains(next.epoch),
              ["audio", "text_only"].contains(next.presentation),
              UUID(uuidString: next.epoch)?.uuidString.lowercased() == next.epoch,
              UUID(uuidString: next.session)?.uuidString.lowercased() == next.session else { return false }
        if !closed { return ticket == next }
        ticket = next; floor = next.generation; closed = false
        return true
    }
    @discardableResult
    func close(uid: String, epoch: String?, generation: Int) -> Bool {
        lock.lock(); defer { lock.unlock() }
        if let ticket {
            guard ticket.uid == uid, (ticket.epoch == epoch || (closed && epoch == nil)), generation >= floor else { return false }
        }
        floor = max(floor, generation); closed = true
        return true
    }
    func retire() {
        lock.lock(); defer { lock.unlock() }
        if let ticket { retiredEpochs.insert(ticket.epoch) }
        ticket = nil; floor = 0; closed = true
    }
    func perform(_ expected: GuardianStrictMediaTicket, _ effect: () -> Void) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard !closed, ticket == expected else { return false }
        effect(); return true
    }
}

/// Process-local exclusion for application-owned Guardian media. It neither
/// changes account preferences nor stops an existing producer. Every producer
/// reserves before its first effect and keeps its permit through pending work.
/// OS notifications and other applications are outside this media contract.
final class GuardianDictationOutputGate: @unchecked Sendable {
    struct Permit: Hashable, Sendable { fileprivate let id: UUID }
    static let shared = GuardianDictationOutputGate()
    private let lock = NSLock()
    private var outputs = Set<Permit>()
    private var requestID: String?
    private var seenRequests = Set<String>()
    private var closedRequests = Set<String>()

    func beginOutput() -> Permit? {
        lock.lock(); defer { lock.unlock() }
        guard requestID == nil else { return nil }
        let permit = Permit(id: UUID())
        outputs.insert(permit)
        return permit
    }

    /// The caller supplies exact producer-terminal/disposal confirmation.
    /// A Stop request or an idle observation cannot release a pending permit.
    func completeOutput(_ permit: Permit) {
        lock.lock(); defer { lock.unlock() }
        outputs.remove(permit)
    }

    func acquire(request: String) -> Bool {
        guard UUID(uuidString: request)?.uuidString.lowercased() == request else { return false }
        lock.lock(); defer { lock.unlock() }
        guard !closedRequests.contains(request) else { return false }
        if requestID == request { return true }
        // Never evict old cancellation tombstones while late native messages
        // can still arrive. Exhaustion refuses a new window until process exit.
        guard seenRequests.contains(request) || seenRequests.count < 1024 else { return false }
        seenRequests.insert(request)
        guard requestID == nil, outputs.isEmpty else {
            closedRequests.insert(request)
            return false
        }
        requestID = request
        return true
    }

    func held(request: String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return requestID == request
    }

    func release(request: String) -> Bool {
        guard UUID(uuidString: request)?.uuidString.lowercased() == request else { return false }
        lock.lock(); defer { lock.unlock() }
        if closedRequests.contains(request) { return true }
        guard seenRequests.contains(request) || seenRequests.count < 1024 else { return false }
        seenRequests.insert(request)
        closedRequests.insert(request)
        // Cancellation of a not-yet-arrived request cannot close another
        // window, and permanently fences that exact delayed acquisition.
        if requestID == request { requestID = nil }
        return true
    }
}
