import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Test helper utilities for common test operations

/// Pump and settle with a timeout to prevent infinite loops
Future<void> pumpAndSettleWithTimeout(
  WidgetTester tester, {
  Duration timeout = const Duration(seconds: 10),
}) async {
  await tester.pumpAndSettle(const Duration(milliseconds: 100), timeout);
}

/// Create a test MaterialApp wrapper
Widget createTestApp(Widget child) {
  return MaterialApp(
    home: Scaffold(
      body: child,
    ),
  );
}

/// Wait for a specific finder to appear
Future<void> waitForFinder(
  WidgetTester tester,
  Finder finder, {
  Duration timeout = const Duration(seconds: 5),
}) async {
  final end = DateTime.now().add(timeout);

  while (DateTime.now().isBefore(end)) {
    await tester.pump(const Duration(milliseconds: 100));

    if (tester.any(finder)) {
      return;
    }
  }

  throw TestFailure('Timed out waiting for $finder');
}

/// Verify that a widget exists
void expectWidgetExists(Finder finder) {
  expect(finder, findsOneWidget);
}

/// Verify that a widget does not exist
void expectWidgetDoesNotExist(Finder finder) {
  expect(finder, findsNothing);
}

/// Tap on a widget and wait for animations
Future<void> tapAndSettle(WidgetTester tester, Finder finder) async {
  await tester.tap(finder);
  await tester.pumpAndSettle();
}

/// Enter text and wait for animations
Future<void> enterTextAndSettle(
  WidgetTester tester,
  Finder finder,
  String text,
) async {
  await tester.enterText(finder, text);
  await tester.pumpAndSettle();
}

/// Scroll until a widget is visible
Future<void> scrollUntilVisible(
  WidgetTester tester,
  Finder finder,
  Finder scrollable, {
  double delta = 100,
}) async {
  while (!tester.any(finder)) {
    await tester.drag(scrollable, Offset(0, -delta));
    await tester.pump();
  }
}

/// Mock delay for async operations in tests
Future<void> mockDelay([Duration duration = const Duration(milliseconds: 100)]) {
  return Future.delayed(duration);
}

/// Verify notification listeners are called
class ListenerTracker extends ChangeNotifier {
  int notifyCount = 0;

  void trackNotification() {
    notifyCount++;
    notifyListeners();
  }

  void reset() {
    notifyCount = 0;
  }
}

/// Create a listener tracker to verify notifyListeners calls
ListenerTracker createListenerTracker(ChangeNotifier notifier) {
  final tracker = ListenerTracker();
  notifier.addListener(tracker.trackNotification);
  return tracker;
}
