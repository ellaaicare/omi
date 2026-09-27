/// P1 seam in front of the vendored BasedHardware capture stack.
///
/// `ELLA_UPSTREAM_CAPTURE` defaults to false. Build 865 keeps using the legacy
/// capture provider. Tests opt in with `enabled: true`. This type does not
/// import upstream files; those stay under `upstream-owned/` until their
/// dependency graph exists in this tree.
const bool kEllaUpstreamCaptureEnabled = bool.fromEnvironment(
  'ELLA_UPSTREAM_CAPTURE',
  defaultValue: false,
);

enum UpstreamLiveSource { none, phone, necklace }

class UpstreamAudioFrame {
  const UpstreamAudioFrame({
    required this.generation,
    required this.ownerUid,
    required this.bytes,
  });

  final int generation;
  final String ownerUid;
  final List<int> bytes;
}

class EllaMemoryDraft {
  const EllaMemoryDraft({
    required this.sessionId,
    required this.ownerUid,
    required this.source,
  });

  final String sessionId;
  final String ownerUid;
  final String source;
}

/// Account binding, consent, and source exclusivity for the upstream stack.
///
/// Audio leaves the adapter only when [enabled] is true, the frame's
/// generation and uid match the current session, and [mayEmitAudio] is true.
/// BLE connect does not require that callback.
class EllaUpstreamCaptureAdapter {
  EllaUpstreamCaptureAdapter({
    required this.mayEmitAudio,
    this.enabled = kEllaUpstreamCaptureEnabled,
  });

  final bool Function() mayEmitAudio;
  final bool enabled;

  String _uid = '';
  int _generation = 0;
  bool bleConnected = false;
  bool manuallyDisconnected = false;
  bool staleBond = false;
  UpstreamLiveSource liveSource = UpstreamLiveSource.none;
  final List<UpstreamAudioFrame> emitted = <UpstreamAudioFrame>[];
  final List<EllaMemoryDraft> memories = <EllaMemoryDraft>[];

  int get generation => _generation;
  String get uid => _uid;

  void replaceSession(String uid) {
    _uid = uid;
    _generation += 1;
    liveSource = UpstreamLiveSource.none;
  }

  /// Matches upstream `manuallyDisconnected` and stale-bond suppression.
  /// [force] is `ensureConnection(..., force: true)` after a manual disconnect.
  /// A stale bond stays down until pairing recovery clears [staleBond].
  bool connectBle({bool force = false}) {
    if (!enabled || staleBond) return false;
    if (manuallyDisconnected && !force) return false;
    if (force) manuallyDisconnected = false;
    bleConnected = true;
    return true;
  }

  void manualDisconnect() {
    bleConnected = false;
    manuallyDisconnected = true;
    if (liveSource == UpstreamLiveSource.necklace) {
      liveSource = UpstreamLiveSource.none;
    }
  }

  void markStaleBond() {
    staleBond = true;
    bleConnected = false;
    if (liveSource == UpstreamLiveSource.necklace) {
      liveSource = UpstreamLiveSource.none;
    }
  }

  void clearStaleBond() {
    staleBond = false;
  }

  bool onDeviceAudio({
    required int generation,
    required String ownerUid,
    required List<int> bytes,
  }) {
    if (!_admits(generation: generation, ownerUid: ownerUid)) return false;
    if (!bleConnected || liveSource == UpstreamLiveSource.phone) return false;
    emitted.add(
      UpstreamAudioFrame(generation: generation, ownerUid: ownerUid, bytes: bytes),
    );
    return true;
  }

  /// Pendant live capture hands off to the phone. They are never both live.
  bool startPhoneMic() {
    if (!enabled || !mayEmitAudio()) return false;
    liveSource = UpstreamLiveSource.phone;
    return true;
  }

  void stopPhoneMic({bool resumeNecklace = false}) {
    if (liveSource != UpstreamLiveSource.phone) return;
    final resume = resumeNecklace && bleConnected && mayEmitAudio();
    liveSource = resume ? UpstreamLiveSource.necklace : UpstreamLiveSource.none;
  }

  bool startNecklace() {
    if (!enabled || !mayEmitAudio() || !bleConnected) return false;
    if (liveSource == UpstreamLiveSource.phone) return false;
    liveSource = UpstreamLiveSource.necklace;
    return true;
  }

  /// One Ella memory per session id and current uid. A later account does not
  /// receive it.
  bool finalizeBatch({required String sessionId, required String source}) {
    if (!enabled || !mayEmitAudio() || _uid.isEmpty) return false;
    final already = memories.any(
      (memory) => memory.sessionId == sessionId && memory.ownerUid == _uid,
    );
    if (already) return false;
    memories.add(
      EllaMemoryDraft(sessionId: sessionId, ownerUid: _uid, source: source),
    );
    return true;
  }

  List<EllaMemoryDraft> memoriesFor(String ownerUid) {
    return memories.where((memory) => memory.ownerUid == ownerUid).toList();
  }

  bool _admits({required int generation, required String ownerUid}) {
    if (!enabled || !mayEmitAudio()) return false;
    return generation == _generation && ownerUid == _uid && ownerUid.isNotEmpty;
  }
}
