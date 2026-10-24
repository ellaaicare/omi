/// Optional native acknowledgement used by explicit live-capture Pause.
/// Legacy stop paths retain their existing best-effort behavior.
abstract interface class ConfirmedMicStop {
  Future<void> stopAndConfirm();
}
