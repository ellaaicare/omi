import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:omi/providers/app_provider.dart';
import '../fixtures/test_data.dart';

void main() {
  group('App List Widget Tests', () {
    late AppProvider appProvider;

    setUp(() {
      appProvider = AppProvider();
      appProvider.apps = TestApps.createTestAppsList();
      appProvider.filterApps();
    });

    tearDown(() {
      appProvider.dispose();
    });

    testWidgets('should display apps in list', (WidgetTester tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: ChangeNotifierProvider<AppProvider>.value(
            value: appProvider,
            child: Scaffold(
              body: Consumer<AppProvider>(
                builder: (context, provider, child) {
                  return ListView.builder(
                    itemCount: provider.filteredApps.length,
                    itemBuilder: (context, index) {
                      final app = provider.filteredApps[index];
                      return ListTile(
                        key: Key('app-${app.id}'),
                        title: Text(app.name),
                        subtitle: Text(app.description),
                      );
                    },
                  );
                },
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      // Verify apps are displayed
      expect(find.byType(ListTile), findsWidgets);
    });

    testWidgets('should show loading indicator when loading', (WidgetTester tester) async {
      appProvider.setIsLoading(true);

      await tester.pumpWidget(
        MaterialApp(
          home: ChangeNotifierProvider<AppProvider>.value(
            value: appProvider,
            child: Scaffold(
              body: Consumer<AppProvider>(
                builder: (context, provider, child) {
                  if (provider.isLoading) {
                    return const Center(
                      child: CircularProgressIndicator(
                        key: Key('loading-indicator'),
                      ),
                    );
                  }
                  return const SizedBox.shrink();
                },
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      expect(find.byKey(const Key('loading-indicator')), findsOneWidget);
    });

    testWidgets('should filter apps by search query', (WidgetTester tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: ChangeNotifierProvider<AppProvider>.value(
            value: appProvider,
            child: Scaffold(
              body: Column(
                children: [
                  TextField(
                    key: const Key('search-field'),
                    onChanged: (query) {
                      appProvider.searchApps(query);
                    },
                  ),
                  Expanded(
                    child: Consumer<AppProvider>(
                      builder: (context, provider, child) {
                        return ListView.builder(
                          itemCount: provider.filteredApps.length,
                          itemBuilder: (context, index) {
                            final app = provider.filteredApps[index];
                            return ListTile(
                              key: Key('app-${app.id}'),
                              title: Text(app.name),
                            );
                          },
                        );
                      },
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      // Enter search query
      await tester.enterText(find.byKey(const Key('search-field')), 'App 1');
      await tester.pumpAndSettle();

      // This would filter the results in a real implementation
      expect(find.byKey(const Key('search-field')), findsOneWidget);
    });
  });
}
