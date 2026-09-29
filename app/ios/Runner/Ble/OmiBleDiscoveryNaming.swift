import CoreBluetooth
import Foundation

/// Pure helpers for naming a peripheral from its scan data (testable without a
/// live radio), mirroring `OmiBlePairingPolicy`.
///
/// `CBPeripheral.name` is often empty until the phone has connected once, while
/// NotePin S puts "NotePin" only in the advertisement (#18705).
enum OmiBleDiscoveryNaming {
    /// Fallback name when a NotePin advertisement carries no local name.
    /// `NativeBluetoothDiscoverer` classifies PLAUD via `name.contains("notepin")`,
    /// so this makes the device surface and route to `DeviceType.plaud`.
    static let notePinFallbackName = "NotePin"

    /// `discoveredName` plus which source(s) actually carried a name, for
    /// redacted (no names/UUIDs) discovery diagnostics on the Dart side.
    /// ellaaicare/ella-ai#1280 RUN-010 / #1287: a candidate that advertises
    /// no local name AND has no cached `peripheral.name` (e.g. the first-ever
    /// scan of a never-bonded necklace) still resolves to an empty name here;
    /// these two flags let testers see which source(s) were empty without a
    /// Mac console.
    static func discoveredNameResult(
        advertisedLocalName: String?,
        cachedName: String?,
        advertisementData: [String: Any]
    ) -> (name: String, hasAdvertisedLocalName: Bool, hasPeripheralName: Bool) {
        (
            discoveredName(
                advertisedLocalName: advertisedLocalName,
                cachedName: cachedName,
                advertisementData: advertisementData
            ),
            normalized(advertisedLocalName) != nil,
            normalized(cachedName) != nil
        )
    }

    /// PLAUD manufacturer id 93 (0x5D), little-endian in the advertisement,
    /// matching macOS discovery in `desktop/macos/.../BtDevice.swift`.
    private static let plaudManufacturerId: UInt16 = 93
    private static let notePinPayload: [UInt8] = [0x04, 0x56, 0xCF, 0x00]

    /// Resolve the scan-time name, preferring what the advertisement itself
    /// carries over `CBPeripheral`'s cached name, then the NotePin fallback.
    static func discoveredName(
        advertisedLocalName: String?,
        cachedName: String?,
        advertisementData: [String: Any]
    ) -> String {
        if let advertised = normalized(advertisedLocalName) {
            return advertised
        }
        if let cached = normalized(cachedName) {
            return cached
        }
        if isNotePinAdvertisement(advertisementData) {
            return notePinFallbackName
        }
        return ""
    }

    /// PLAUD manufacturer id with the NotePin payload.
    static func isNotePinAdvertisement(_ advertisementData: [String: Any]) -> Bool {
        guard let manufacturerData = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data,
              manufacturerData.count >= 2 + notePinPayload.count
        else {
            return false
        }
        let manufacturerId = UInt16(manufacturerData[0]) | (UInt16(manufacturerData[1]) << 8)
        guard manufacturerId == plaudManufacturerId else { return false }
        return Array(manufacturerData[2..<(2 + notePinPayload.count)]) == notePinPayload
    }

    private static func normalized(_ name: String?) -> String? {
        guard let trimmed = name?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty
        else {
            return nil
        }
        return trimmed
    }

    /// Whether a re-discovery of an already-sighted peripheral within the same
    /// scan session carries naming/UUID information the first sighting lacked,
    /// and so is worth forwarding to Dart a second time.
    ///
    /// ellaaicare/ella-ai#1280 RUN-016: with `CBCentralManagerScanOptionAllowDuplicatesKey`
    /// enabled, CoreBluetooth can deliver a peripheral's local name or service UUIDs on a
    /// later advertisement/scan-response packet than the first `didDiscover` callback for
    /// it in the current scan — a legacy `flutter_blue_plus` necklace scan is not immune to
    /// this either, but happens to get a merged first packet more often in practice. Without
    /// re-forwarding the later, fuller packet, a peripheral whose first sighting was bare
    /// (no name, no service UUID) stays rejected as `no_name` for the rest of the scan even
    /// though a later packet did carry a name. This also prevents flooding Dart with a
    /// repeat call for every identical re-advertisement once a peripheral is fully named.
    static func shouldForwardRediscovery(
        previousHasName: Bool,
        previousHasServiceUuids: Bool,
        newHasName: Bool,
        newHasServiceUuids: Bool
    ) -> Bool {
        (newHasName && !previousHasName) || (newHasServiceUuids && !previousHasServiceUuids)
    }
}
