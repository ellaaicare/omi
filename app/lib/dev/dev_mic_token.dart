/// Off unless built with --dart-define=BASE_DEV_TIMED_CAPTURE=true (and Swift condition BASE_DEV_TIMED_CAPTURE).
/// Lives here, dependency-free, so the recorder service can read it without importing the test.
const bool kBaseDevTimedCapture = bool.fromEnvironment('BASE_DEV_TIMED_CAPTURE');

/// DEV-ONLY exact ownership for the Ella Base timed capture test (v4).
///
/// A token is armed for exactly ONE native start: CaptureController._devOwnedStart arms it synchronously
/// immediately before calling the recorder's start() for a test-owned capture session (its first start and
/// its own stall restarts), and disarms right after that call expression. NativeMicRecorderService mints the
/// session id synchronously inside start(), before its first await, so the armed token can only ever reach
/// that one call: there is no global lease and no other start (ordinary or concurrent) is ever tagged.
///
/// Tagged id: `testBit | token << 16 | seq` (constants shared with PhoneMicDevTokenPolicy in
/// PhoneMicController.swift). Ordinary ids are small counters and never set [testBit].
class DevMicToken {
  DevMicToken._();

  static const int testBit = 1 << 62;
  static const int tokenMask = (1 << 30) - 1;
  static const int seqMask = 0xFFFF;

  static int? _armed;
  static int _lastToken = 0;
  static int _seq = 0;

  static int? get armed => _armed;

  static void armNext(int token) {
    if (token <= 0 || token > tokenMask) throw ArgumentError.value(token, 'token');
    _armed = token;
  }

  static void disarm() => _armed = null;

  /// Session id for the next native start: tagged (and consumed) if armed, else [ordinary]().
  static int mint(int Function() ordinary) {
    final token = _armed;
    _armed = null;
    if (token == null) return ordinary();
    if (token != _lastToken) {
      _lastToken = token;
      _seq = 0;
    }
    _seq = (_seq + 1) & seqMask;
    return testBit | (token << 16) | _seq;
  }

  static bool isTest(int sessionId) => sessionId & testBit != 0;

  static int? tokenOf(int sessionId) => isTest(sessionId) ? (sessionId >> 16) & tokenMask : null;
}
