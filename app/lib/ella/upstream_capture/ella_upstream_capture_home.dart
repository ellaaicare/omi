// Minimal Home-entry-point facade for the promoted upstream capture stack.
// Not upstream-owned. Exists only to give today_page.dart's connect/record/
// source actions a single, small thing to call when ELLA_UPSTREAM_CAPTURE is
// on (rule 6): "wire the minimal Home entry points... behind the flag. Don't
// build new UI." It owns one EllaUpstreamCaptureRuntime for the process and
// keeps its account binding in sync with the signed-in uid on every call —
// capture must not start before a nonempty uid session is bound, on every
// resume/restart path, not only the first one.
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/capture_source.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_adapter.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_socket_router.dart';

class EllaUpstreamCaptureHome {
  EllaUpstreamCaptureHome._internal()
      : adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: defaultEllaMayEmitAudio),
        _router = EllaUpstreamSocketRouter() {
    runtime = EllaUpstreamCaptureRuntime(
      adapter: adapter,
      routeAudio: ({required isPhone, required bytes}) => _router.route(isPhone: isPhone, bytes: bytes),
    );
  }

  static final EllaUpstreamCaptureHome instance = EllaUpstreamCaptureHome._internal();

  final EllaUpstreamCaptureAdapter adapter;
  final EllaUpstreamSocketRouter _router;
  late final EllaUpstreamCaptureRuntime runtime;

  UpstreamLiveSource get liveCaptureSource => runtime.liveCaptureSource;

  /// Re-binds the adapter's account session to the current uid whenever it
  /// has changed (sign-in, sign-out, account switch) — called before every
  /// action below so no start/resume path can run against a stale account.
  void _syncAccount() {
    final uid = SharedPreferencesUtil().uid;
    if (uid != adapter.uid) adapter.replaceSession(uid);
  }

  Future<bool> ensureConnection({bool force = false}) async {
    _syncAccount();
    if (adapter.uid.isEmpty) return false;
    return runtime.ensureConnection(force: force);
  }

  /// Starts capture for [source] if nothing is live; stops it otherwise.
  /// Phone and necklace stay mutually exclusive (the runtime/adapter enforce
  /// it) — this only picks which one a Home tap means.
  Future<bool> toggleCapture(EllaCaptureSource source) async {
    _syncAccount();
    if (adapter.uid.isEmpty) return false;
    if (runtime.liveCaptureSource == UpstreamLiveSource.phone) {
      runtime.stopPhoneMic();
      return true;
    }
    if (runtime.liveCaptureSource == UpstreamLiveSource.necklace) {
      runtime.stopNecklace();
      return true;
    }
    if (source == EllaCaptureSource.phone) {
      return runtime.startPhoneMic();
    }
    if (!await ensureConnection()) return false;
    return runtime.startNecklace();
  }
}
