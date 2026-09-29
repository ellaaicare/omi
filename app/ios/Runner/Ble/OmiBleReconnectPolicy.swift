import CoreBluetooth

/// Pure decision logic for `OmiBleManager.connectPeripheral`, extracted so it is
/// covered by a `swiftc`-executable test (no XCTest target exists for this native
/// BLE code) without needing a live `CBPeripheral`. See ellaaicare/ella-ai#1287 RUN-020.
///
/// Fork-owned, no upstream counterpart — not listed in `UPSTREAM_OWNED.txt`, same as
/// `OmiBleRetrievalTagging.swift`.
enum OmiBleReconnectPolicy {
    enum Decision: Equatable {
        /// Issue `centralManager.connect(peripheral, options: nil)`. CoreBluetooth
        /// will invoke `didConnect`, which drives service discovery.
        case connect

        /// The peripheral is already connected at the CoreBluetooth/system level
        /// (e.g. surfaced via `retrieveConnectedAndKnownPeripherals` or restored via
        /// `willRestoreState`). `centralManager.connect()` on an already-connected
        /// peripheral is a silent no-op that never invokes `didConnect`, so this
        /// process's own GATT session (service/characteristic discovery, then
        /// audio-notify subscription) has to be driven directly instead of assuming
        /// CoreBluetooth will hand back a ready-to-use link.
        case discoverServicesDirectly
    }

    /// Decides what `connectPeripheral` should do for a peripheral it already
    /// tracks (`peripherals[uuid]` is non-nil), given that peripheral's current
    /// `CBPeripheralState`.
    static func decision(for state: CBPeripheralState) -> Decision {
        state == .connected ? .discoverServicesDirectly : .connect
    }
}
