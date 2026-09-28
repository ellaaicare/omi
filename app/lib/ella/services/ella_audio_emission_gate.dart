import 'package:omi/ella/services/ai_consent_active_session_lease.dart';

/// The single fail-closed predicate for "may this audio frame leave the capture
/// pipeline right now" (ellaaicare/ella-ai#1280).
///
/// Every native-to-socket (and native-to-WAL) audio boundary of the upstream
/// capture stack evaluates it per frame through the Ella adapters in
/// `lib/ella/upstream_capture/`. All of the following must hold:
///
/// * [boundUid] is a nonempty account id — the account the capture session was
///   bound to when it started;
/// * [lease] belongs to that same account ([AiConsentActiveSessionLease.uid]);
/// * the lease holds CURRENT authority ([AiConsentActiveSessionLease.hasCurrentAuthority]):
///   it is started, not stopped or lost, and the persisted consent grant still
///   belongs to the signed-in uid. This is live state, not a historical boolean;
/// * the lease is still in the session generation the caller observed when it
///   started ([expectedGeneration]). A stopped, lost, or restarted lease mints a
///   new generation, so a stale or superseded session can never emit.
///
/// Anything else — including a null/blank uid — returns false.
bool mayEmitAudio({
  required String? boundUid,
  required AiConsentActiveSessionLease lease,
  required int expectedGeneration,
}) {
  final uid = boundUid?.trim();
  if (uid == null || uid.isEmpty) return false;
  if (lease.uid != uid) return false;
  if (!lease.hasCurrentAuthority) return false;
  if (expectedGeneration <= 0 || lease.generation != expectedGeneration) return false;
  return true;
}
