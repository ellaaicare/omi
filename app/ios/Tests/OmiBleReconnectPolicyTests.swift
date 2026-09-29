// ellaaicare/ella-ai#1287 RUN-020: pure-logic coverage for `OmiBleReconnectPolicy`, compiled
// and run standalone with `swiftc` (no XCTest target exists for this native BLE code; this
// mirrors the pattern already used by OmiBleDiscoveryNamingTests.swift /
// OmiBleRetrievalTaggingTests.swift / GuardianNativePolicyTests.swift).
import CoreBluetooth
import Foundation

private enum TestFailure: Error, CustomStringConvertible {
    case failed(String)

    var description: String {
        switch self {
        case .failed(let message): return message
        }
    }
}

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TestFailure.failed(message) }
}

private func testAlreadyConnectedPeripheralDiscoversServicesDirectly() throws {
    let decision = OmiBleReconnectPolicy.decision(for: .connected)
    try expect(
        decision == .discoverServicesDirectly,
        "an already-connected peripheral must drive service discovery directly, never a plain centralManager.connect() no-op"
    )
}

private func testDisconnectedPeripheralConnects() throws {
    let decision = OmiBleReconnectPolicy.decision(for: .disconnected)
    try expect(decision == .connect, "a disconnected known peripheral must go through centralManager.connect()")
}

private func testConnectingPeripheralConnects() throws {
    // A peripheral CoreBluetooth is already mid-connect for is not yet ready for
    // discoverServices; re-issuing connect() is the same idempotent behavior the
    // rest of OmiBleManager (e.g. reconnectStalePeripherals) relies on.
    let decision = OmiBleReconnectPolicy.decision(for: .connecting)
    try expect(decision == .connect, "a connecting peripheral must not be treated as ready for service discovery")
}

private func testDisconnectingPeripheralConnects() throws {
    let decision = OmiBleReconnectPolicy.decision(for: .disconnecting)
    try expect(decision == .connect, "a disconnecting peripheral must not be treated as ready for service discovery")
}

@main
private enum OmiBleReconnectPolicyTests {
    static func main() throws {
        try testAlreadyConnectedPeripheralDiscoversServicesDirectly()
        try testDisconnectedPeripheralConnects()
        try testConnectingPeripheralConnects()
        try testDisconnectingPeripheralConnects()
        print("OmiBleReconnectPolicy tests passed")
    }
}
