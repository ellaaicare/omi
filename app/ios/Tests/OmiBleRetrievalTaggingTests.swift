// ellaaicare/ella-ai#1287 RUN-018: pure-logic coverage for `OmiBleRetrievalTagging`, compiled
// and run standalone with `swiftc` (no XCTest target exists for this native BLE code; this
// mirrors the pattern already used by OmiBleDiscoveryNamingTests.swift /
// GuardianNativePolicyTests.swift / AppleRemindersSyncIdempotencyTests.swift /
// EllaVoiceAudioRoutePolicyTests.swift).
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

// A stand-in for a real (fake, non-hardware) peripheral identifier — never a
// value that could be mistaken for a real device UUID.
private func fakeInfo(_ uuid: String, name: String? = nil, serviceUuids: [String] = []) -> OmiBleRetrievedPeripheralInfo {
    OmiBleRetrievedPeripheralInfo(uuid: uuid, name: name, serviceUuids: serviceUuids)
}

private func testMergeTagsConnectedResultsAsRetrievedConnected() throws {
    let merged = OmiBleRetrievalTagging.mergeTaggedCandidates(
        connected: [fakeInfo("aaaaaaaa-0000-0000-0000-000000000001", serviceUuids: ["19b10000-e8f2-537e-4f6c-d104768a1214"])],
        known: []
    )
    try expect(merged.count == 1, "one connected candidate should produce one merged entry")
    try expect(merged[0].source == .retrievedConnected, "a connected-retrieval candidate must be tagged retrievedConnected")
}

private func testMergeTagsKnownResultsAsRetrievedKnown() throws {
    let merged = OmiBleRetrievalTagging.mergeTaggedCandidates(
        connected: [],
        known: [fakeInfo("aaaaaaaa-0000-0000-0000-000000000002")]
    )
    try expect(merged.count == 1, "one known-device candidate should produce one merged entry")
    try expect(merged[0].source == .retrievedKnown, "a known-id-retrieval candidate must be tagged retrievedKnown")
}

private func testMergeDeduplicatesOverlapPreferringConnectedSource() throws {
    let sharedUuid = "aaaaaaaa-0000-0000-0000-000000000003"
    let merged = OmiBleRetrievalTagging.mergeTaggedCandidates(
        connected: [fakeInfo(sharedUuid, serviceUuids: ["19b10000-e8f2-537e-4f6c-d104768a1214"])],
        known: [fakeInfo(sharedUuid)]
    )
    try expect(merged.count == 1, "a peripheral present in both retrievals must appear once, not twice")
    try expect(merged[0].source == .retrievedConnected, "the stronger (connected) evidence must win when both retrievals match the same uuid")
}

private func testMergeKeepsDistinctUuidsFromBothSources() throws {
    let merged = OmiBleRetrievalTagging.mergeTaggedCandidates(
        connected: [fakeInfo("aaaaaaaa-0000-0000-0000-000000000004")],
        known: [fakeInfo("bbbbbbbb-0000-0000-0000-000000000005")]
    )
    try expect(merged.count == 2, "distinct uuids from each source must both be forwarded")
    let sources = Set(merged.map { $0.source })
    try expect(sources == [.retrievedConnected, .retrievedKnown], "each distinct candidate must keep its own source tag")
}

private func testMergeWithNoResultsIsEmpty() throws {
    let merged = OmiBleRetrievalTagging.mergeTaggedCandidates(connected: [], known: [])
    try expect(merged.isEmpty, "no retrieval results should produce no candidates")
}

private func testMergePreservesNilNameForRetrievedPeripherals() throws {
    // RUN-018: none of these sources involve an active scan, so unlike
    // didDiscover's advertisement payload, there is no adv-name at all —
    // only whatever CoreBluetooth's cached `peripheral.name` happens to be.
    let merged = OmiBleRetrievalTagging.mergeTaggedCandidates(
        connected: [fakeInfo("aaaaaaaa-0000-0000-0000-000000000006", name: nil)],
        known: []
    )
    try expect(merged[0].info.name == nil, "a retrieved peripheral with no cached name must stay nil, not synthesized")
}

@main
private enum OmiBleRetrievalTaggingTests {
    static func main() throws {
        try testMergeTagsConnectedResultsAsRetrievedConnected()
        try testMergeTagsKnownResultsAsRetrievedKnown()
        try testMergeDeduplicatesOverlapPreferringConnectedSource()
        try testMergeKeepsDistinctUuidsFromBothSources()
        try testMergeWithNoResultsIsEmpty()
        try testMergePreservesNilNameForRetrievedPeripherals()
        print("OmiBleRetrievalTagging tests passed")
    }
}
