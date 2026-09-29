import Foundation

/// Pure helpers for tagging peripherals surfaced through CoreBluetooth's
/// *retrieval* APIs (as opposed to an active scan), testable without a live
/// radio, mirroring `OmiBleDiscoveryNaming`.
///
/// ellaaicare/ella-ai#1287 RUN-018: every candidate observed in that run had
/// `hasAdvName=false` and `uuidCount=0` — ruling out the "scan-response
/// arrives late" theory (see `OmiBleDiscoveryNaming.shouldForwardRediscovery`)
/// entirely. The likely cause is a necklace already connected at the
/// CoreBluetooth level when scanning starts: a connected peripheral stops
/// advertising, so `scanForPeripherals` never surfaces it. `OmiBleManager`
/// covers that gap with three additional capture-layer sources, none of which
/// involve an active scan and so none of which carry an advertised name:
///   (a) `retrieveConnectedPeripherals(withServices:)` — tagged `retrievedConnected`
///   (b) `retrievePeripherals(withIdentifiers:)` for saved/paired device ids — tagged `retrievedKnown`
///   (c) `centralManager(_:willRestoreState:)` — tagged `restored`
enum OmiBleRetrievalSource: String {
    case retrievedConnected
    case retrievedKnown
}

/// Peripheral identity/service info gathered from a CoreBluetooth retrieval
/// call, before it is tagged with a source and turned into a `BlePeripheral`.
struct OmiBleRetrievedPeripheralInfo: Equatable {
    let uuid: String
    let name: String?
    let serviceUuids: [String]
}

enum OmiBleRetrievalTagging {
    /// Merge the results of the two retrieval queries into a single
    /// deduplicated, source-tagged candidate list.
    ///
    /// A peripheral present in both retrievals (already connected AND a known
    /// paired id) is tagged `retrievedConnected` only — it already carries the
    /// stronger evidence (CoreBluetooth itself matched it against the Omi
    /// service filter), so a redundant `retrievedKnown` entry for the same
    /// uuid would just double-count it in diagnostics and results.
    static func mergeTaggedCandidates(
        connected: [OmiBleRetrievedPeripheralInfo],
        known: [OmiBleRetrievedPeripheralInfo]
    ) -> [(info: OmiBleRetrievedPeripheralInfo, source: OmiBleRetrievalSource)] {
        var seen = Set<String>()
        var results: [(OmiBleRetrievedPeripheralInfo, OmiBleRetrievalSource)] = []

        for info in connected {
            guard seen.insert(info.uuid).inserted else { continue }
            results.append((info, .retrievedConnected))
        }
        for info in known {
            guard seen.insert(info.uuid).inserted else { continue }
            results.append((info, .retrievedKnown))
        }
        return results
    }
}
