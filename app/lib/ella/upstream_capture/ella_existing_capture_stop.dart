import 'package:flutter/material.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'ella_upstream_capture_runtime.dart';

/// Deliberate Stop remains reachable while setup is unavailable. This widget
/// never boots/binds capture, refreshes consent, starts a producer or disconnects
/// an unknown device. Each press carries an exact existing-producer handle.
class EllaExistingCaptureStop extends StatefulWidget {
  const EllaExistingCaptureStop({super.key, this.runtime, this.authenticatedUid});
  final EllaUpstreamCaptureRuntime? runtime;
  final String Function()? authenticatedUid;
  @override State<EllaExistingCaptureStop> createState() => _EllaExistingCaptureStopState();
}
class _EllaExistingCaptureStopState extends State<EllaExistingCaptureStop> {
  bool _busy=false;
  String? _message;
  String? _messageOwner;
  int? _messageEpoch;
  EllaUpstreamCaptureRuntime get _runtime => widget.runtime ?? EllaUpstreamCaptureRuntime.instance;
  String get _uid => widget.authenticatedUid?.call() ?? WalOwnerAuthority.authenticatedUid;
  Future<void> _stop(EllaExistingCaptureStopTarget target) async {
    if (_busy) return;
    setState(() { _busy=true; _message=null; _messageOwner=target.uid; _messageEpoch=target.epoch; });
    var stopped=false;
    try { stopped=await _runtime.stopExistingCapture(target); } catch (_) { /* truthful unconfirmed result below */ }
    if (!mounted) return;
    setState(() {
      _busy=false;
      _message=_uid!=target.uid || _runtime.authority.boundUid!=target.uid ||
          _runtime.authority.bindingEpoch!=target.epoch ? null :
          stopped ? 'Recording stopped.' : 'Recording stop is not confirmed. No new recording was started.';
    });
  }
  @override Widget build(BuildContext context) {
    final runtime=_runtime;
    return AnimatedBuilder(
      animation:Listenable.merge([if(runtime.provider!=null) runtime.provider!,runtime.lastRevocation]),
      builder:(context,_) {
        final target=runtime.existingCaptureStopTarget(_uid);
        final messageCurrent=_messageOwner==_uid && runtime.authority.boundUid==_uid &&
            runtime.authority.bindingEpoch==_messageEpoch;
        final message=messageCurrent ? _message : null;
        final busy=_busy && messageCurrent;
        if(target==null && !busy && message==null) return const SizedBox.shrink();
        return Padding(padding:const EdgeInsets.symmetric(vertical:8),child:Column(
          mainAxisSize:MainAxisSize.min,crossAxisAlignment:CrossAxisAlignment.stretch,children:[
            if(target!=null || busy) OutlinedButton.icon(
              key:const Key('existing-capture-stop'),
              style:OutlinedButton.styleFrom(minimumSize:const Size(double.infinity,48)),
              onPressed:_busy || target==null ? null : ()=>_stop(target),
              icon:const Icon(Icons.stop_circle_outlined),
              label:Text(busy ? context.l10n.upstreamCaptureStopping : context.l10n.irisCaptureStop)),
            if(message!=null) Text(message,key:const Key('existing-capture-stop-result')),
          ]));
      });
  }
}
