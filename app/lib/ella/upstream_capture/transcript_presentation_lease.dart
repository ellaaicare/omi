/// Private in-memory presentation scope. Never serialized or used for Finish.
class TranscriptPresentationScope {
  const TranscriptPresentationScope({required this.uid, required this.bindingEpoch,
    required this.providerIdentity, required this.producerSessionKey,
    required this.mode, required this.source, required this.deviceRevision});
  final String uid;
  final int bindingEpoch;
  final Object providerIdentity;
  final String producerSessionKey;
  final String mode;
  final String source;
  final int deviceRevision;

  bool matches(TranscriptPresentationScope? current) => current != null &&
      uid == current.uid && bindingEpoch == current.bindingEpoch &&
      identical(providerIdentity, current.providerIdentity) &&
      producerSessionKey == current.producerSessionKey && mode == current.mode &&
      source == current.source && deviceRevision == current.deviceRevision;
}

/// A stopped/replaced/unauthorized viewer cannot revive on later notifications.
class TranscriptPresentationLease {
  TranscriptPresentationLease(this._origin);
  final TranscriptPresentationScope _origin;
  bool _retired = false;
  bool get retired => _retired;
  bool observe(TranscriptPresentationScope? current) {
    if (_retired) return false;
    if (!_origin.matches(current)) _retired = true;
    return !_retired;
  }
}
