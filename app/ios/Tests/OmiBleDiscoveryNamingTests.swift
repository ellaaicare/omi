// ellaaicare/ella-ai#1280 RUN-016: pure-logic coverage for `OmiBleDiscoveryNaming`, compiled
// and run standalone with `swiftc` (no XCTest target exists for this native BLE code; this
// mirrors the pattern already used by GuardianNativePolicyTests.swift /
// AppleRemindersSyncIdempotencyTests.swift / EllaVoiceAudioRoutePolicyTests.swift).
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

// MARK: - discoveredName / discoveredNameResult (pre-existing behavior, regression coverage)

private func testDiscoveredNamePrefersAdvertisedLocalNameOverCachedName() throws {
    let name = OmiBleDiscoveryNaming.discoveredName(
        advertisedLocalName: "Friend",
        cachedName: "Stale Cached Name",
        advertisementData: [:]
    )
    try expect(name == "Friend", "advertised local name should win over cached name")
}

private func testDiscoveredNameFallsBackToCachedNameWhenNoAdvertisedName() throws {
    let name = OmiBleDiscoveryNaming.discoveredName(
        advertisedLocalName: nil,
        cachedName: "Friend",
        advertisementData: [:]
    )
    try expect(name == "Friend", "should fall back to CBPeripheral.name when no advertised local name")
}

private func testDiscoveredNameEmptyWhenNeitherSourceHasAName() throws {
    let name = OmiBleDiscoveryNaming.discoveredName(
        advertisedLocalName: nil,
        cachedName: nil,
        advertisementData: [:]
    )
    try expect(name.isEmpty, "should resolve to empty when neither source has a name")
}

private func testDiscoveredNameResultReportsWhichSourcesCarriedAName() throws {
    let result = OmiBleDiscoveryNaming.discoveredNameResult(
        advertisedLocalName: nil,
        cachedName: "Friend",
        advertisementData: [:]
    )
    try expect(result.name == "Friend", "resolved name should be the cached name")
    try expect(result.hasAdvertisedLocalName == false, "no advertised local name was present")
    try expect(result.hasPeripheralName == true, "a cached peripheral name was present")
}

// MARK: - shouldForwardRediscovery (new, RUN-016)

private func testShouldForwardRediscoveryWhenNameArrivesLate() throws {
    // The RUN-016 shape: first sighting bare (no name, no service UUIDs), a later
    // packet (e.g. the scan response) adds the name.
    try expect(
        OmiBleDiscoveryNaming.shouldForwardRediscovery(
            previousHasName: false,
            previousHasServiceUuids: false,
            newHasName: true,
            newHasServiceUuids: false
        ),
        "a late-arriving name must be forwarded"
    )
}

private func testShouldForwardRediscoveryWhenServiceUuidsArriveLate() throws {
    try expect(
        OmiBleDiscoveryNaming.shouldForwardRediscovery(
            previousHasName: false,
            previousHasServiceUuids: false,
            newHasName: false,
            newHasServiceUuids: true
        ),
        "late-arriving service UUIDs must be forwarded"
    )
}

private func testShouldNotForwardIdenticalRediscoveryWithNothingNew() throws {
    try expect(
        OmiBleDiscoveryNaming.shouldForwardRediscovery(
            previousHasName: true,
            previousHasServiceUuids: false,
            newHasName: true,
            newHasServiceUuids: false
        ) == false,
        "a repeat advertisement with no new information must not be re-forwarded"
    )
}

private func testShouldNotForwardWhenStillBareOnBothSightings() throws {
    try expect(
        OmiBleDiscoveryNaming.shouldForwardRediscovery(
            previousHasName: false,
            previousHasServiceUuids: false,
            newHasName: false,
            newHasServiceUuids: false
        ) == false,
        "a peripheral that is still bare on the second sighting must not flood Dart with re-forwards"
    )
}

private func testShouldForwardWhenNameArrivesEvenIfServiceUuidsAlreadyPresent() throws {
    try expect(
        OmiBleDiscoveryNaming.shouldForwardRediscovery(
            previousHasName: false,
            previousHasServiceUuids: true,
            newHasName: true,
            newHasServiceUuids: true
        ),
        "a newly-arrived name must be forwarded even when service UUIDs were already known"
    )
}

@main
private enum OmiBleDiscoveryNamingTests {
    static func main() throws {
        try testDiscoveredNamePrefersAdvertisedLocalNameOverCachedName()
        try testDiscoveredNameFallsBackToCachedNameWhenNoAdvertisedName()
        try testDiscoveredNameEmptyWhenNeitherSourceHasAName()
        try testDiscoveredNameResultReportsWhichSourcesCarriedAName()
        try testShouldForwardRediscoveryWhenNameArrivesLate()
        try testShouldForwardRediscoveryWhenServiceUuidsArriveLate()
        try testShouldNotForwardIdenticalRediscoveryWithNothingNew()
        try testShouldNotForwardWhenStillBareOnBothSightings()
        try testShouldForwardWhenNameArrivesEvenIfServiceUuidsAlreadyPresent()
        print("OmiBleDiscoveryNaming tests passed")
    }
}
