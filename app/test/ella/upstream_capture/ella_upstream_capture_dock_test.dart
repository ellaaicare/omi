import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart' as fork;
import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_dock.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/upstream_capture/backend/preferences.dart' as upstream;
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/services/capture/capture_seams.dart';

const _uid = 'uid-a';

class _NoBleListeners implements CaptureBleListeners {
  @override
  void addBatchRecordingFinalizedListener(void Function(String) callback) {}

  @override
  void removeBatchRecordingFinalizedListener(void Function(String) callback) {}
}

class _DockRuntime extends EllaUpstreamCaptureRuntime {
  _DockRuntime({required super.authority, required this.capture});

  final CaptureProvider capture;

  @override
  Future<CaptureProvider> ensureBooted() async => capture;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('verified v10 authority renders the idle flag-on dock as one compact row', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await fork.SharedPreferencesUtil.init();
    await upstream.SharedPreferencesUtil.init();
    final connectivity = StreamController<bool>.broadcast();
    final provider = CaptureProvider(
      connectivity: CaptureConnectivityBoundary(
        initiallyConnected: true,
        changes: connectivity.stream,
        isConnected: () => true,
      ),
      bleListeners: _NoBleListeners(),
      preferences: upstream.SharedPreferencesUtil(),
    );
    final authority = EllaCaptureAuthority(authenticatedUid: () => _uid, sessionStartAllowed: (_) => false);
    addTearDown(() async {
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
      provider.dispose();
      authority.dispose();
      await connectivity.close();
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
      tester.platformDispatcher.clearTextScaleFactorTestValue();
    });

    final preferences = fork.SharedPreferencesUtil()..uid = _uid;
    const receiptId = '${fork.SharedPreferencesUtil.currentAiConsentReceiptPrefix}v10-receipt';
    preferences.acceptAiConsent(
      receiptId: receiptId,
      uid: _uid,
      profileBindingId: 'profile-binding-v10',
      serverDecidedAt: '2026-07-27T00:00:00Z',
      policyVersion: fork.SharedPreferencesUtil.legacyAiConsentContractVersionV10,
      processorSetHash: fork.SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
    );
    preferences.markAiConsentServerVerified(
      uid: _uid,
      receiptId: receiptId,
      policyVersion: fork.SharedPreferencesUtil.legacyAiConsentContractVersionV10,
      processorSetHash: fork.SharedPreferencesUtil.legacyAiConsentProcessorSetHashV10,
      profileBindingId: 'profile-binding-v10',
      scopeVersion: fork.SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: fork.SharedPreferencesUtil.currentAiConsentScopeHash,
    );
    tester.view.physicalSize = const Size(1320, 2868);
    tester.view.devicePixelRatio = 3;
    tester.platformDispatcher.textScaleFactorTestValue = 1.5;

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: Align(
            alignment: Alignment.bottomCenter,
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: EllaUpstreamCaptureDock(
                runtime: _DockRuntime(authority: authority, capture: provider),
                authenticatedUid: () => _uid,
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pump();
    await tester.pump();

    final phone = find.byKey(const Key('upstream-capture-record-phone'));
    final necklace = find.byKey(const Key('upstream-capture-connect-necklace'));
    expect(phone.hitTestable(), findsOneWidget);
    expect(necklace.hitTestable(), findsOneWidget);
    final phoneRect = tester.getRect(phone);
    final necklaceRect = tester.getRect(necklace);
    expect(phoneRect.right, lessThan(necklaceRect.left));
    expect(phoneRect.center.dy, closeTo(necklaceRect.center.dy, 8));
    expect(tester.getSize(find.byKey(const Key('upstream-capture-dock'))).height, lessThan(120));
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.aiConsentContractVersion, fork.SharedPreferencesUtil.legacyAiConsentContractVersionV10);
    expect(tester.takeException(), isNull);
  });
}
