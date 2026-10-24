import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/ella_theme.dart';

void main() {
  for (final night in [0.0, 0.25, 0.5, 0.75, 1.0]) {
    testWidgets('modal memory surface remains opaque at night=$night', (tester) async {
      final theme = ThemeData.lerp(ellaThemeData(), ellaDarkThemeData(), night);
      await tester.pumpWidget(MaterialApp(
          theme: theme,
          home: Builder(
              builder: (context) => Scaffold(
                  backgroundColor: Colors.red,
                  body: TextButton(
                      onPressed: () => showModalBottomSheet<void>(
                          context: context,
                          builder: (_) => const SizedBox(
                              height: 240, width: double.infinity, child: Center(child: Text('Synthetic sheet text')))),
                      child: const Text('Open'))))));
      await tester.tap(find.text('Open'));
      await tester.pumpAndSettle();
      final material =
          tester.widget<Material>(find.descendant(of: find.byType(BottomSheet), matching: find.byType(Material)).first);
      expect(material.color?.a, 1.0,
          reason: 'Theme interpolation must not expose underlying Journal text through modal');
      expect(find.text('Synthetic sheet text'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }
}
