import Foundation

#if os(iOS)
  import AVFoundation
#endif

enum EllaVoiceAudioRouteUsage: String {
  case playback
  case interactive
}

enum EllaVoiceAudioRoutePort: Equatable {
  case speaker
  case receiver
  case bluetoothHFP
  case bluetoothA2DP
  case bluetoothLE
  case wired
  case airPlay
  case external
}

enum EllaVoiceAudioRouteClassification: String, Equatable {
  case speaker
  case receiver
  case external
  case unavailable
}

enum EllaVoiceAudioRouteFailure: String, Equatable {
  case invalidUsage = "invalid_usage"
  case overrideResetFailed = "override_reset_failed"
  case configurationFailed = "configuration_failed"
  case activationFailed = "activation_failed"
  case speakerSelectionFailed = "speaker_selection_failed"
  case externalRouteLost = "external_route_lost"
  case routeUnavailable = "route_unavailable"
  case receiverSelected = "receiver_selected"
}

struct EllaVoiceAudioRouteSnapshot: Equatable {
  let outputs: [EllaVoiceAudioRoutePort]

  var classification: EllaVoiceAudioRouteClassification {
    if outputs.contains(where: Self.isExternal) { return .external }
    if outputs.contains(.speaker) { return .speaker }
    if outputs.contains(.receiver) { return .receiver }
    return .unavailable
  }

  private static func isExternal(_ port: EllaVoiceAudioRoutePort) -> Bool {
    switch port {
    case .bluetoothHFP, .bluetoothA2DP, .bluetoothLE, .wired, .airPlay, .external:
      return true
    case .speaker, .receiver:
      return false
    }
  }
}

struct EllaVoiceAudioRouteOutcome: Equatable {
  let usage: EllaVoiceAudioRouteUsage
  let classification: EllaVoiceAudioRouteClassification
  let failure: EllaVoiceAudioRouteFailure?

  var success: Bool {
    failure == nil && (classification == .speaker || classification == .external)
  }
}

protocol EllaVoiceAudioSessionRouting: AnyObject {
  func routeSnapshot() -> EllaVoiceAudioRouteSnapshot
  func resetTransientSpeakerOverride() throws
  func configure(for usage: EllaVoiceAudioRouteUsage) throws
  func activate() throws
  func selectSpeaker() throws
}

struct EllaVoiceAudioRoutePolicy {
  func apply(
    usage: EllaVoiceAudioRouteUsage,
    session: EllaVoiceAudioSessionRouting
  ) -> EllaVoiceAudioRouteOutcome {
    let routeBeforeConfiguration = session.routeSnapshot()
    let preservingExternalRoute = routeBeforeConfiguration.classification == .external

    do {
      try session.configure(for: usage)
    } catch {
      return failure(.configurationFailed, usage: usage, session: session)
    }

    if usage == .interactive {
      // The override API is valid only after playAndRecord is restored.
      // Clear the playback override before activation so a newly
      // connected external route can become authoritative.
      do {
        try session.resetTransientSpeakerOverride()
      } catch {
        return failure(.overrideResetFailed, usage: usage, session: session)
      }
    }

    do {
      try session.activate()
    } catch {
      return failure(.activationFailed, usage: usage, session: session)
    }

    let configuredRoute = session.routeSnapshot()
    if preservingExternalRoute {
      guard configuredRoute.classification == .external else {
        return failure(.externalRouteLost, usage: usage, session: session)
      }
    } else if configuredRoute.classification != .external {
      do {
        try session.selectSpeaker()
      } catch {
        return failure(.speakerSelectionFailed, usage: usage, session: session)
      }
    }

    let actualRoute = session.routeSnapshot().classification
    switch actualRoute {
    case .speaker, .external:
      return EllaVoiceAudioRouteOutcome(usage: usage, classification: actualRoute, failure: nil)
    case .receiver:
      return EllaVoiceAudioRouteOutcome(
        usage: usage, classification: actualRoute, failure: .receiverSelected)
    case .unavailable:
      return EllaVoiceAudioRouteOutcome(
        usage: usage, classification: actualRoute, failure: .routeUnavailable)
    }
  }

  private func failure(
    _ failure: EllaVoiceAudioRouteFailure,
    usage: EllaVoiceAudioRouteUsage,
    session: EllaVoiceAudioSessionRouting
  ) -> EllaVoiceAudioRouteOutcome {
    EllaVoiceAudioRouteOutcome(
      usage: usage,
      classification: session.routeSnapshot().classification,
      failure: failure
    )
  }
}

#if os(iOS)
  final class SystemEllaVoiceAudioSession: EllaVoiceAudioSessionRouting {
    private let audioSession: AVAudioSession

    init(audioSession: AVAudioSession = .sharedInstance()) {
      self.audioSession = audioSession
    }

    func routeSnapshot() -> EllaVoiceAudioRouteSnapshot {
      EllaVoiceAudioRouteSnapshot(outputs: audioSession.currentRoute.outputs.map(Self.routePort))
    }

    func resetTransientSpeakerOverride() throws {
      try audioSession.overrideOutputAudioPort(.none)
    }

    func configure(for usage: EllaVoiceAudioRouteUsage) throws {
      switch usage {
      case .playback, .interactive:
        // Explicit speaker selection is only supported by playAndRecord.
        // The default mode stays full range while Dart gates capture, and
        // these options let an active external route survive.
        try audioSession.setCategory(
          .playAndRecord,
          mode: .default,
          options: [.defaultToSpeaker, .allowBluetoothHFP, .allowBluetoothA2DP, .allowAirPlay]
        )
      }
    }

    func activate() throws {
      try audioSession.setActive(true, options: [])
    }

    func selectSpeaker() throws {
      try audioSession.overrideOutputAudioPort(.speaker)
    }

    private static func routePort(_ output: AVAudioSessionPortDescription)
      -> EllaVoiceAudioRoutePort
    {
      switch output.portType {
      case .builtInSpeaker:
        return .speaker
      case .builtInReceiver:
        return .receiver
      case .bluetoothHFP:
        return .bluetoothHFP
      case .bluetoothA2DP:
        return .bluetoothA2DP
      case .bluetoothLE:
        return .bluetoothLE
      case .headphones, .lineOut, .usbAudio:
        return .wired
      case .airPlay:
        return .airPlay
      default:
        return .external
      }
    }
  }
#endif
