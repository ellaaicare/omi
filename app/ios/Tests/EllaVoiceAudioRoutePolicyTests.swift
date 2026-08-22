import Foundation

private enum TestFailure: Error, CustomStringConvertible {
  case failed(String)

  var description: String {
    switch self {
    case .failed(let message): return message
    }
  }
}

private enum FakeSessionError: Error {
  case requestedFailure
}

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
  if !condition() { throw TestFailure.failed(message) }
}

private final class FakeAudioSession: EllaVoiceAudioSessionRouting {
  enum Operation: Equatable {
    case resetOverride
    case configure(EllaVoiceAudioRouteUsage)
    case activate
    case selectSpeaker
  }

  var snapshot: EllaVoiceAudioRouteSnapshot
  var operations: [Operation] = []
  var failingOperation: Operation?
  var losesExternalDuringConfiguration = false
  var routeAfterExternalLoss: EllaVoiceAudioRoutePort = .receiver

  init(outputs: [EllaVoiceAudioRoutePort]) {
    snapshot = EllaVoiceAudioRouteSnapshot(outputs: outputs)
  }

  func routeSnapshot() -> EllaVoiceAudioRouteSnapshot {
    snapshot
  }

  func resetTransientSpeakerOverride() throws {
    operations.append(.resetOverride)
    try failIfRequested(.resetOverride)
    if snapshot.classification == .speaker {
      snapshot = EllaVoiceAudioRouteSnapshot(outputs: [.receiver])
    }
  }

  func configure(for usage: EllaVoiceAudioRouteUsage) throws {
    let operation = Operation.configure(usage)
    operations.append(operation)
    try failIfRequested(operation)
    if losesExternalDuringConfiguration {
      snapshot = EllaVoiceAudioRouteSnapshot(outputs: [routeAfterExternalLoss])
    } else if usage == .playback && snapshot.classification == .receiver {
      snapshot = EllaVoiceAudioRouteSnapshot(outputs: [.speaker])
    }
  }

  func activate() throws {
    operations.append(.activate)
    try failIfRequested(.activate)
  }

  func selectSpeaker() throws {
    operations.append(.selectSpeaker)
    try failIfRequested(.selectSpeaker)
    snapshot = EllaVoiceAudioRouteSnapshot(outputs: [.speaker])
  }

  private func failIfRequested(_ operation: Operation) throws {
    if failingOperation == operation { throw FakeSessionError.requestedFailure }
  }
}

private func testPlaybackWithoutExternalOutputSelectsSpeaker() throws {
  let session = FakeAudioSession(outputs: [.receiver])
  let outcome = EllaVoiceAudioRoutePolicy().apply(usage: .playback, session: session)

  try expect(outcome.success, "playback without an external output did not succeed")
  try expect(outcome.classification == .speaker, "playback did not verify the actual speaker route")
  try expect(
    session.operations == [.configure(.playback), .activate],
    "playback did not remain on the playback-only route"
  )
}

private func testBluetoothPlaybackPreservesExternalOutput() throws {
  for output in [EllaVoiceAudioRoutePort.bluetoothHFP, .bluetoothA2DP, .bluetoothLE] {
    let session = FakeAudioSession(outputs: [output])
    let outcome = EllaVoiceAudioRoutePolicy().apply(usage: .playback, session: session)

    try expect(outcome.success, "Bluetooth playback did not succeed")
    try expect(outcome.classification == .external, "Bluetooth was not classified as external")
    try expect(
      !session.operations.contains(.selectSpeaker),
      "Bluetooth playback was displaced by the speaker")
  }
}

private func testInteractiveExternalRoutesRemainAuthoritative() throws {
  let outputs: [EllaVoiceAudioRoutePort] = [.bluetoothHFP, .bluetoothA2DP, .airPlay]
  for output in outputs {
    let session = FakeAudioSession(outputs: [output])
    let outcome = EllaVoiceAudioRoutePolicy().apply(usage: .interactive, session: session)

    try expect(outcome.success, "interactive external route did not succeed")
    try expect(outcome.classification == .external, "interactive route was not external")
    try expect(
      session.operations == [.configure(.interactive), .resetOverride, .activate],
      "interactive external route was displaced by the speaker")
  }
}

private func testWiredPlaybackPreservesExternalOutput() throws {
  let session = FakeAudioSession(outputs: [.wired])
  let outcome = EllaVoiceAudioRoutePolicy().apply(usage: .playback, session: session)

  try expect(outcome.success, "wired playback did not succeed")
  try expect(outcome.classification == .external, "wired output was not classified as external")
  try expect(
    !session.operations.contains(.selectSpeaker), "wired playback was displaced by the speaker")
}

private func testSpeakerSelectionFailureIsTypedAndFailsClosed() throws {
  let session = FakeAudioSession(outputs: [.receiver])
  session.failingOperation = .selectSpeaker
  let outcome = EllaVoiceAudioRoutePolicy().apply(usage: .interactive, session: session)

  try expect(!outcome.success, "speaker selection failure did not fail closed")
  try expect(outcome.failure == .speakerSelectionFailed, "speaker selection failure was not typed")
  try expect(
    outcome.classification == .receiver, "failure did not report the actual receiver route")
}

private func testPlaybackFallsBackToSpeakerWhenExternalRouteDisconnects() throws {
  let session = FakeAudioSession(outputs: [.bluetoothHFP])
  session.losesExternalDuringConfiguration = true
  session.routeAfterExternalLoss = .speaker
  let outcome = EllaVoiceAudioRoutePolicy().apply(usage: .playback, session: session)

  try expect(outcome.success, "playback did not accept the verified speaker fallback")
  try expect(
    outcome.classification == .speaker,
    "playback fallback did not verify the speaker route")
  try expect(
    !session.operations.contains(.selectSpeaker),
    "playback fallback forced a speaker override instead of accepting the system route")
}

private func testInteractiveExternalRouteLossIsTypedAndFailsClosed() throws {
  let session = FakeAudioSession(outputs: [.bluetoothHFP])
  session.losesExternalDuringConfiguration = true
  let outcome = EllaVoiceAudioRoutePolicy().apply(usage: .interactive, session: session)

  try expect(!outcome.success, "lost interactive external route did not fail closed")
  try expect(outcome.failure == .externalRouteLost, "lost interactive external route was not typed")
  try expect(
    !session.operations.contains(.selectSpeaker),
    "lost interactive route silently fell back to speaker")
}

private func testInteractiveRestoreClearsOverrideAndReestablishesSpeaker() throws {
  let session = FakeAudioSession(outputs: [.receiver])
  let policy = EllaVoiceAudioRoutePolicy()
  let playback = policy.apply(usage: .playback, session: session)
  let restored = policy.apply(usage: .interactive, session: session)

  try expect(playback.success, "playback setup failed before restore")
  try expect(restored.success, "interactive restore failed")
  try expect(
    restored.classification == .speaker, "interactive restore did not verify the speaker route")
  try expect(
    session.operations == [
      .configure(.playback), .activate,
      .configure(.interactive), .resetOverride, .activate, .selectSpeaker,
    ],
    "interactive restore did not safely clear and rebuild the route"
  )
}

@main
private enum EllaVoiceAudioRoutePolicyTests {
  static func main() throws {
    let tests: [() throws -> Void] = [
      testPlaybackWithoutExternalOutputSelectsSpeaker,
      testBluetoothPlaybackPreservesExternalOutput,
      testInteractiveExternalRoutesRemainAuthoritative,
      testWiredPlaybackPreservesExternalOutput,
      testSpeakerSelectionFailureIsTypedAndFailsClosed,
      testPlaybackFallsBackToSpeakerWhenExternalRouteDisconnects,
      testInteractiveExternalRouteLossIsTypedAndFailsClosed,
      testInteractiveRestoreClearsOverrideAndReestablishesSpeaker,
    ]
    for test in tests { try test() }
    print("EllaVoiceAudioRoutePolicyTests: \(tests.count) passed, 0 skipped")
  }
}
