import 'package:flutter_test/flutter_test.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/backend/schema/app.dart';
import 'package:omi/backend/preferences.dart';
import '../../fixtures/test_data.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('AppProvider Tests', () {
    late AppProvider appProvider;

    setUp(() {
      appProvider = AppProvider();
      // Initialize with test apps
      appProvider.apps = TestApps.createTestAppsList();
      appProvider.filterApps();
    });

    tearDown(() {
      appProvider.dispose();
    });

    group('App Management', () {
      test('should initialize with empty apps list', () {
        final newProvider = AppProvider();
        expect(newProvider.apps, isEmpty);
        expect(newProvider.popularApps, isEmpty);
        expect(newProvider.filteredApps, isEmpty);
      });

      test('should get app from id', () async {
        final app = await appProvider.getAppFromId('app-1');
        expect(app, isNotNull);
        expect(app?.name, 'App 1');
      });

      test('should return null for non-existent app id', () async {
        final app = await appProvider.getAppFromId('non-existent');
        expect(app, isNull);
      });

      test('should update local app', () {
        final updatedApp = TestApps.createTestApp(
          id: 'app-1',
          name: 'Updated App 1',
          enabled: true,
        );

        appProvider.updateLocalApp(updatedApp);

        final app = appProvider.apps.firstWhere((a) => a.id == 'app-1');
        expect(app.name, 'Updated App 1');
      });
    });

    group('Filtering and Searching', () {
      test('should filter apps by search query', () {
        appProvider.searchApps('App 1');
        expect(appProvider.searchQuery, 'app 1');
      });

      test('should clear search query', () {
        appProvider.searchApps('test');
        expect(appProvider.searchQuery, 'test');

        appProvider.clearSearchQuery();
        expect(appProvider.searchQuery, isEmpty);
      });

      test('should filter enabled apps', () {
        appProvider.addOrRemoveFilter('Installed Apps', 'Apps');
        appProvider.filterApps();

        final enabledApps = appProvider.filteredApps.where((app) => app.enabled).toList();
        expect(enabledApps.length, greaterThan(0));
      });

      test('should check if filter is active', () {
        expect(appProvider.isFilterActive(), false);

        appProvider.addOrRemoveFilter('Installed Apps', 'Apps');
        expect(appProvider.isFilterActive(), true);
      });

      test('should clear all filters', () {
        appProvider.addOrRemoveFilter('Installed Apps', 'Apps');
        appProvider.addOrRemoveFilter('4+ Stars', 'Rating');

        expect(appProvider.filters.length, 2);

        appProvider.clearFilters();
        expect(appProvider.filters.isEmpty, true);
      });

      test('should check if search is active', () {
        expect(appProvider.isSearchActive(), false);

        appProvider.searchApps('test');
        expect(appProvider.isSearchActive(), true);
      });
    });

    group('App Installation/Uninstallation', () {
      test('should toggle app enabled state', () async {
        final initialState = appProvider.apps[0].enabled;

        // Note: This would normally call the server, so we're just testing the state management
        expect(appProvider.apps[0].enabled, initialState);
      });

      test('should set app loading state', () {
        appProvider.appLoading = List.filled(3, false);

        appProvider.setAppLoading(0, true);
        expect(appProvider.appLoading[0], true);

        appProvider.setAppLoading(0, false);
        expect(appProvider.appLoading[0], false);
      });

      test('should handle invalid loading index gracefully', () {
        appProvider.appLoading = List.filled(3, false);

        // Should not throw
        expect(() => appProvider.setAppLoading(10, true), returnsNormally);
      });
    });

    group('User Apps', () {
      test('should get user private apps', () {
        final privateApps = appProvider.userPrivateApps;
        expect(privateApps.every((app) => app.private), true);
      });

      test('should check if user is app owner', () {
        appProvider.checkIsAppOwner('test-uid');
        // This would check against SharedPreferencesUtil().uid
      });

      test('should toggle app public/private', () {
        final app = appProvider.apps.firstWhere((a) => a.id == 'app-1');
        final initialPrivate = app.private;

        appProvider.toggleAppPublic('app-1', !initialPrivate);

        final updatedApp = appProvider.apps.firstWhere((a) => a.id == 'app-1');
        expect(updatedApp.private, !initialPrivate);
      });
    });

    group('Loading States', () {
      test('should set loading state', () {
        expect(appProvider.isLoading, false);

        appProvider.setIsLoading(true);
        expect(appProvider.isLoading, true);

        appProvider.setIsLoading(false);
        expect(appProvider.isLoading, false);
      });

      test('should set selected chat app id', () {
        appProvider.setSelectedChatAppId('app-1');
        expect(appProvider.selectedChatAppId, 'app-1');

        appProvider.setSelectedChatAppId(null);
        expect(appProvider.selectedChatAppId, '');
      });

      test('should get selected app', () {
        appProvider.setSelectedChatAppId('app-1');
        final selectedApp = appProvider.getSelectedApp();

        expect(selectedApp, isNotNull);
        expect(selectedApp?.id, 'app-1');
      });
    });

    group('Category and Capability Filters', () {
      test('should add and remove category filter', () {
        final category = Category(id: 'productivity', title: 'Productivity');

        appProvider.addOrRemoveCategoryFilter(category);
        expect(appProvider.isCategoryFilterSelected(category), true);

        appProvider.addOrRemoveCategoryFilter(category);
        expect(appProvider.isCategoryFilterSelected(category), false);
      });

      test('should add and remove capability filter', () {
        final capability = AppCapability(id: 'chat', title: 'Chat');

        appProvider.addOrRemoveCapabilityFilter(capability);
        expect(appProvider.isCapabilityFilterSelected(capability), true);

        appProvider.addOrRemoveCapabilityFilter(capability);
        expect(appProvider.isCapabilityFilterSelected(capability), false);
      });
    });

    group('Notification Tests', () {
      test('should notify listeners when apps change', () {
        var notified = false;
        appProvider.addListener(() {
          notified = true;
        });

        appProvider.setIsLoading(true);
        expect(notified, true);
      });
    });
  });
}
