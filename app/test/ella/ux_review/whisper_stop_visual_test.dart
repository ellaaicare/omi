import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';
import 'package:omi/ella/widgets/iris_whisper_stop_bar.dart';
import 'thick_review_regressions_test.dart' as review;

class _PlayingFixture extends WhisperDeliveryRuntime {
  _PlayingFixture()
      : super.testing(
            transport: (_, __, ___, ____) async => {},
            read: (_) async => null,
            write: (_, __) async {},
            authorityProvider: () => null,
            channel: const MethodChannel('fictional-visual-output-only'));
  bool active = true;
  int stops = 0;
  @override
  bool get playing => active;
  @override
  Future<void> stopOutput({bool resume = true}) async {
    stops++;
    active = false;
    notifyListeners();
  }
}

void main() {
  review.main();
  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.5]) {
      testWidgets('synthetic pinned Stop readable and exact action dark=$dark scale=$scale', (tester) async {
        tester.view.physicalSize = const Size(393, 852);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final runtime = _PlayingFixture();
        await tester.pumpWidget(
            review.app(Scaffold(bottomNavigationBar: IrisWhisperStopBar(runtime: runtime)), dark: dark, scale: scale));
        await tester.pumpAndSettle();
        final button = find.byKey(const Key('iris-global-whisper-stop'));
        final span = tester.widget<RichText>(find.descendant(of: button, matching: find.byType(RichText)).first).text;
        final color = span.style!.color!;
        expect(review.ratio(color, EllaPalette(dark).bgSecondary), greaterThanOrEqualTo(4.5));
        expect(tester.getSize(button).height, greaterThanOrEqualTo(44));
        await review.shot(tester, 'synthetic-pinned-stop-${dark ? 'dark' : 'light'}-$scale');
        await tester.tap(button);
        await tester.pumpAndSettle();
        expect(runtime.stops, 1);
        expect(button, findsNothing);
        await tester.pumpWidget(const SizedBox.shrink());
        runtime.dispose();
        expect(tester.takeException(), isNull);
      });
    }
  }
}
