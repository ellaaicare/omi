/// Whether a failed necklace audio send should open another `/v4/listen`.
///
/// A socket that is already connected but still waiting on the consent lease
/// must stay up. Replacing it opens a new session about once per protocol
/// timeout (~8s) and those sessions carry no audio. That is the 03:11–03:12
/// churn: capture recovery, not the 4-minute consent refresh.
bool shouldOpenReplacementListenSocket({
  required bool socketConnected,
  required bool hasSessionAuthority,
}) =>
    !(socketConnected && !hasSessionAuthority);
