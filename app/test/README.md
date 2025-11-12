# Flutter App Testing Infrastructure

This directory contains comprehensive tests for the Omi Flutter application.

## Test Structure

```
test/
├── unit/                   # Unit tests for individual components
│   └── providers/         # Provider-specific unit tests
│       ├── app_provider_test.dart
│       ├── capture_provider_test.dart
│       ├── device_provider_test.dart
│       └── message_provider_test.dart
├── widget/                # Widget tests for UI components
│   ├── app_list_widget_test.dart
│   ├── chat_widget_test.dart
│   └── device_connection_widget_test.dart
├── integration/           # End-to-end integration tests
│   ├── device_connection_test.dart
│   └── recording_flow_test.dart
├── mocks/                 # Mock classes for testing
│   ├── mock_providers.dart
│   └── mock_services.dart
└── fixtures/              # Test data and fixtures
    └── test_data.dart
```

## Running Tests

### Run all tests
```bash
flutter test
```

### Run unit tests only
```bash
flutter test test/unit
```

### Run widget tests only
```bash
flutter test test/widget
```

### Run integration tests only
```bash
flutter test test/integration
```

### Run tests with coverage
```bash
flutter test --coverage
```

### Generate HTML coverage report
```bash
flutter test --coverage
genhtml coverage/lcov.info -o coverage/html
open coverage/html/index.html  # macOS
# Or: xdg-open coverage/html/index.html  # Linux
```

## Coverage Goals

- **Minimum Coverage**: 40%
- **Target Coverage**: 60%+
- **Critical Providers**: 70%+

### Current Coverage

The test suite covers:
- ✅ **AppProvider**: App management, filtering, searching, installation
- ✅ **MessageProvider**: Message handling, file uploads, chat operations
- ✅ **DeviceProvider**: Device connection, battery monitoring, firmware updates
- ✅ **CaptureProvider**: Recording states, transcripts, device audio
- ✅ **Widget Tests**: App list, chat interface, device connection UI
- ✅ **Integration Tests**: Device connection flow, recording sessions

## Test Categories

### Unit Tests

Unit tests focus on individual provider functionality:
- State management
- Data transformations
- Business logic
- Notification handling

### Widget Tests

Widget tests verify UI components:
- Component rendering
- User interactions
- State-driven UI updates
- Loading states

### Integration Tests

Integration tests validate complete workflows:
- Device connection and disconnection
- Recording start/stop/pause
- Transcript collection
- Multi-provider interactions

## Writing New Tests

### Unit Test Template

```dart
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/providers/your_provider.dart';

void main() {
  group('YourProvider Tests', () {
    late YourProvider provider;

    setUp(() {
      provider = YourProvider();
    });

    tearDown(() {
      provider.dispose();
    });

    test('should do something', () {
      // Arrange
      // Act
      // Assert
      expect(true, true);
    });
  });
}
```

### Widget Test Template

```dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

void main() {
  testWidgets('should render widget', (WidgetTester tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: YourWidget(),
      ),
    );

    await tester.pumpAndSettle();

    expect(find.byType(YourWidget), findsOneWidget);
  });
}
```

## CI/CD Integration

Tests automatically run on:
- Push to `main`, `master`, `develop`, `claude/**` branches
- Pull requests to `main`, `master`, `develop`
- Manual workflow dispatch

See `.github/workflows/app-tests.yml` for the complete CI/CD configuration.

## Mocking Strategy

The test suite uses:
- **Mockito** for creating mock objects
- **Test fixtures** for consistent test data
- **Provider mocking** for isolated unit tests

## Best Practices

1. **Isolation**: Each test should be independent
2. **Setup/Teardown**: Use `setUp()` and `tearDown()` to maintain clean state
3. **Descriptive Names**: Use clear, descriptive test names
4. **Arrange-Act-Assert**: Follow the AAA pattern
5. **Coverage**: Aim for meaningful coverage, not just high percentages
6. **Fast Tests**: Keep tests fast and focused
7. **Maintainability**: Update tests when code changes

## Troubleshooting

### Tests failing locally but passing in CI
- Ensure you have the latest dependencies: `flutter pub get`
- Clear cache: `flutter clean && flutter pub get`

### Coverage not generating
- Ensure you're running with `--coverage` flag
- Check that `lcov` is installed for HTML reports

### Integration tests failing
- Integration tests may require a device or emulator
- Some tests are designed to run in CI/CD only

## Contributing

When adding new features:
1. Write unit tests for new providers/services
2. Add widget tests for new UI components
3. Create integration tests for new user flows
4. Ensure coverage meets minimum threshold
5. Update this README if adding new test categories
