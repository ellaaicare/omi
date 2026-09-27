import 'ella_capture_uid_gate.dart';

/// Fail-closed gate for every native-to-socket audio emission boundary
/// (necklace BLE frames and phone-mic bytes alike): audio may leave the app
/// only when both a nonempty bound UID and a live policy/consent authority
/// hold. Any missing or unevaluated input means "no" — there is no default
/// permissive branch.
///
/// This is deliberately *not* a method on any capture/socket class so it has
/// exactly one implementation shared by every emission boundary, instead of a
/// copy embedded inside each vendored or forked file it guards.
bool mayEmitAudio({
  required String? boundUid,
  required bool hasConsentAuthority,
}) {
  if (!hasNonEmptyBoundUid(boundUid)) return false;
  if (!hasConsentAuthority) return false;
  return true;
}
