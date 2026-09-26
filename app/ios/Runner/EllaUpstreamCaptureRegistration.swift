import Foundation

/// P1 hook. `ELLA_UPSTREAM_CAPTURE` is unset, so this does not register
/// upstream Ble or PhoneMic. Those sources stay in `upstream-owned/` and are
/// not members of the Runner target.
enum EllaUpstreamCaptureRegistration {
  static var enabled: Bool {
    #if ELLA_UPSTREAM_CAPTURE
    return true
    #else
    return false
    #endif
  }

  static func registerIfEnabled() {
    guard enabled else { return }
  }
}
