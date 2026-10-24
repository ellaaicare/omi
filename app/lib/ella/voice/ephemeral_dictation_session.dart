import 'package:omi/ella/voice/foreground_dictation_diagnostics.dart';
import 'dart:async';
import 'dart:convert';
import 'package:flutter/services.dart';
import 'package:speech_to_text/speech_recognition_result.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/standard_speech_ownership.dart';
import 'package:omi/ella/voice/dictation_draft_snapshot.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';

/// A read-only view of the actual editor, including a monotonic value revision.
class DictationEditorBinding {
  const DictationEditorBinding({required this.value, required this.lease, required this.epoch, required this.revision});
  final TextEditingValue value;
  final EllaAccountCommitLease? lease;
  final int epoch, revision;
}

/// Unregistered, one-window source proposal. No permission/init/listen default,
/// provider/backend/WAL/private store, token inventory or auto-send exists here.
class EphemeralDictationSession {
  EphemeralDictationSession({
    required StandardSpeechReceiptPort port,
    required this.draft,
    required this.draftRevision,
    required this.readEditor,
    required this.hasExactCaptureAuthority,
    required this.canStartFresh,
    required this.requestPermission,
    required this.initializeRecognition,
    required this.listen,
    required this.applyDraft,
    this.onAutomaticFinish,
    this.diagnostic,
    AudioCaptureOwnership? ownership,
    this.operationTimeout = const Duration(seconds: 12),
  }) : _speech = StandardSpeechOwnership(port: port, ownership: ownership);
  final DictationDraftSnapshot draft;
  final int draftRevision;
  final DictationEditorBinding Function() readEditor;

  /// Caller captures the actual AI/account/modal authority; UID-only grants are invalid.
  final bool Function() hasExactCaptureAuthority;

  /// Pure admission observation; must reject busy/contentful capture. No takeover.
  final bool Function() canStartFresh;
  final Future<bool> Function() requestPermission, initializeRecognition;
  final Future<void> Function() listen;
  final void Function(TextEditingValue) applyDraft;
  final void Function()? onAutomaticFinish;
  final Duration operationTimeout;
  final DictationDiagnosticAttempt? diagnostic;
  final StandardSpeechOwnership _speech;
  bool _retired = false, _startIssued = false, _finishRequested = false;
  String _words = '';
  Future<TextEditingValue?>? _finishWork;
  Future<TextEditingValue?>? _finishView;
  Future<void>? _retireWork;
  bool get hasReservation => _speech.hasReservation;
  bool get acceptsStartEffects => _speech.isCurrent && !_finishRequested && _current();
  bool _current() {
    try {
      final editor = readEditor();
      return !_retired &&
          hasExactCaptureAuthority() &&
          draft.lease.isCurrent &&
          identical(editor.lease, draft.lease) &&
          editor.epoch == draft.epoch &&
          editor.revision == draftRevision &&
          editor.value == draft.value;
    } catch (_) {
      return false;
    }
  }

  bool _fresh() {
    try {
      return canStartFresh();
    } catch (_) {
      return false;
    }
  }

  void _retainJSON(String json, {bool terminal = false}) {
    if (!_current()) return;
    try {
      final result = SpeechRecognitionResult.fromJson(Map<String, dynamic>.from(jsonDecode(json) as Map));
      if (terminal && result.finalResult && result.recognizedWords.trim().isNotEmpty)
        _words = result.recognizedWords.trim();
    } catch (_) {/* Invalid recognition text is never editor authority. */}
  }

  Future<bool> start() {
    if (_startIssued || _finishRequested || _retired) return Future.value(false);
    _startIssued = true;
    if ((draft.value.selection.isValid && !draft.value.selection.isCollapsed) ||
        (draft.value.composing.isValid && !draft.value.composing.isCollapsed) ||
        draftRevision < 0) return Future.value(false);
    if (!_current() ||
        !_fresh() ||
        !_speech.reserve(
            hasAuthority: _current,
            onCallback: (method, payload) {
              if (method == 'textRecognition' && payload is String) _retainJSON(payload);
            })) return Future.value(false);
    final receipt = Completer<bool>();
    Future<bool>.sync(() async {
      try {
        final started = await _speech.start(
          onStartFailure: diagnostic?.error,
          prepare: () async {
            if (!_current() || !_fresh() || _finishRequested) return false;
            final permission = await requestPermission();
            if (!permission || !_current() || !_fresh() || _finishRequested) return false;
            final initialized = await initializeRecognition();
            return initialized && _current() && _fresh() && !_finishRequested;
          },
          listen: listen,
          pauseFor: const Duration(seconds: 3),
          listenFor: const Duration(seconds: 60),
          onFinalResultRetained: (json) => _retainJSON(json, terminal: true),
          onListeningEnded: () {
            // Pin the original shared Finish first. Presentation callbacks can
            // join it, but cannot replace or strand its physical cleanup.
            unawaited(finish().then((_) {}).catchError((Object _) {/* Exact owner remains held. */}));
            try {
              onAutomaticFinish?.call();
            } catch (_) {/* Cleanup already admitted. */}
          },
        );
        if (!started && !_finishRequested) await retire();
        return started && _current();
      } catch (error, stack) {
        if (!_finishRequested) {
          try {
            await retire();
          } catch (_) {/* Keep exact failed owner. */}
        }
        Error.throwWithStackTrace(error, stack);
      }
    }).then(receipt.complete, onError: receipt.completeError);
    return receipt.future.timeout(operationTimeout, onTimeout: () {
      diagnostic?.fail(DictationDiagnosticFailure.startDeadline);
      _retired = true;
      _words = '';
      unawaited(_speech.end().catchError((Object _) {/* No release inference. */}));
      throw StateError('dictation_start_deadline');
    });
  }

  Future<TextEditingValue?> finish() {
    final existing = _finishWork;
    if (existing != null) return _finishView!;
    _finishRequested = true;
    final receipt = Completer<TextEditingValue?>();
    _finishWork = receipt.future;
    // Publish one bounded receipt before effects; all joiners share its original deadline.
    final view = _boundedFinishView(receipt.future);
    _finishView = view;
    Future<TextEditingValue?>.sync(() async {
      await _speech.stopListening(); // Retains same-session final text after terminal proof.
      await _speech.end(); // All input/early SDK work confirmed before releasing owner.
      if (!_current()) return null;
      final editor = readEditor();
      if (editor.revision != draftRevision || !hasExactCaptureAuthority()) return null;
      final value = draft.insert(
          transcript: _words, currentValue: editor.value, currentLease: editor.lease, currentEpoch: editor.epoch);
      _retired = true;
      _words = '';
      if (value != null) applyDraft(value); // Synchronous assignment only; never sends.
      return value;
    }).then(receipt.complete, onError: (Object error, StackTrace stack) {
      _finishWork = null; // Exact terminal retry only; never starts capture again.
      _finishView = null;
      receipt.completeError(error, stack);
    });
    return view;
  }

  Future<TextEditingValue?> _boundedFinishView(Future<TextEditingValue?> work) {
    // Deadline is anchored to the original Finish admission. Late joiners see
    // the same completed view; raw SDK cleanup and native ownership stay pinned.
    return work.timeout(operationTimeout, onTimeout: () {
      diagnostic?.fail(DictationDiagnosticFailure.finishDeadline);
      _retired = true;
      _words = '';
      throw StateError('dictation_finish_deadline');
    });
  }

  /// Account/modal/editor invalidation retires publication synchronously.
  Future<void> retire() {
    _retired = true;
    _words = '';
    final existing = _retireWork;
    if (existing != null) return existing;
    final receipt = Completer<void>();
    _retireWork = receipt.future;
    Future<void>.sync(_speech.end).then(receipt.complete, onError: (Object error, StackTrace stack) {
      _retireWork = null;
      receipt.completeError(error, stack);
    });
    return receipt.future;
  }
}
