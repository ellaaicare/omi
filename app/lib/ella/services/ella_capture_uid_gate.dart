/// Capture must never start or resume without a nonempty bound UID, on every
/// start path and every resume path (necklace and phone alike). This is the
/// single shared predicate for that check so every call site — start,
/// resume-after-reconnect, resume-after-wake — agrees on what "bound" means.
bool hasNonEmptyBoundUid(String? uid) => uid != null && uid.trim().isNotEmpty;
