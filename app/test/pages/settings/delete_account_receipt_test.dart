import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/settings/delete_account.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  test('completed deletion receipt parses only from the exact backend contract', () {
    final receipt = AccountDeletionReceipt.tryParseResponse(
      statusCode: 200,
      body: '''
        {
          "status": "ok",
          "deletion_receipt": {
            "request_id": "aidel_0123456789abcdef0123456789abcdef",
            "status": "completed",
            "scope": "account_and_user_data",
            "server_completed_at": "2026-07-26T20:15:00+00:00"
          }
        }
      ''',
    );

    expect(receipt?.requestId, 'aidel_0123456789abcdef0123456789abcdef');
    expect(receipt?.serverCompletedAt, DateTime.utc(2026, 7, 26, 20, 15));
  });

  test('non-200 and malformed deletion responses are not authoritative', () {
    expect(
      AccountDeletionReceipt.tryParseResponse(
        statusCode: 500,
        body: '{"deletion_receipt":{"status":"completed"}}',
      ),
      isNull,
    );
    for (final body in [
      'not-json',
      '{"status":"ok"}',
      '{"status":"failed","deletion_receipt":{"request_id":"aidel_0123456789abcdef0123456789abcdef","status":"completed","scope":"account_and_user_data","server_completed_at":"2026-07-26T20:15:00Z"}}',
      '{"status":"ok","deletion_receipt":{"request_id":"aidel_short","status":"completed","scope":"account_and_user_data","server_completed_at":"2026-07-26T20:15:00Z"}}',
      '{"status":"ok","deletion_receipt":{"request_id":"aidel_0123456789abcdef0123456789abcdef","status":"pending","scope":"account_and_user_data","server_completed_at":"2026-07-26T20:15:00Z"}}',
      '{"status":"ok","deletion_receipt":{"request_id":"aidel_0123456789abcdef0123456789abcdef","status":"completed","scope":"some_data","server_completed_at":"2026-07-26T20:15:00Z"}}',
      '{"status":"ok","deletion_receipt":{"request_id":"aidel_0123456789abcdef0123456789abcdef","status":"completed","scope":"account_and_user_data","server_completed_at":"invalid"}}',
      '{"status":"ok","deletion_receipt":{"request_id":"aidel_0123456789abcdef0123456789abcdef","status":"completed","scope":"account_and_user_data","server_completed_at":1234}}',
    ]) {
      expect(AccountDeletionReceipt.tryParseResponse(statusCode: 200, body: body), isNull);
    }
  });

  testWidgets('failed deletion preserves auth and local data and shows retry copy', (tester) async {
    var signOutCalls = 0;
    var clearWalCalls = 0;
    var clearPreferencesCalls = 0;
    await _pumpDeleteAccount(
      tester,
      request: () async => null,
      signOut: () async {
        signOutCalls++;
      },
      clearWal: () async {
        clearWalCalls++;
      },
      clearPreferences: () {
        clearPreferencesCalls++;
      },
    );

    await _confirmDeletion(tester);

    expect(signOutCalls, 0);
    expect(clearWalCalls, 0);
    expect(clearPreferencesCalls, 0);
    expect(
      find.text(
          'Ella could not confirm account deletion. Your account and local data are unchanged. Please try again.'),
      findsOneWidget,
    );
  });

  testWidgets('verified completed receipt permits sign-out and local clearing', (tester) async {
    var signOutCalls = 0;
    var clearWalCalls = 0;
    var clearPreferencesCalls = 0;
    var completionCalls = 0;
    await _pumpDeleteAccount(
      tester,
      request: () async => AccountDeletionReceipt(
        requestId: 'aidel_0123456789abcdef0123456789abcdef',
        serverCompletedAt: DateTime.utc(2026, 7, 26, 20, 15),
      ),
      signOut: () async {
        signOutCalls++;
      },
      clearWal: () async {
        clearWalCalls++;
      },
      clearPreferences: () {
        clearPreferencesCalls++;
      },
      onDeletionComplete: () {
        completionCalls++;
      },
    );

    await _confirmDeletion(tester);

    expect(signOutCalls, 1);
    expect(clearWalCalls, 1);
    expect(clearPreferencesCalls, 1);
    expect(completionCalls, 1);
  });
}

Future<void> _pumpDeleteAccount(
  WidgetTester tester, {
  required DeleteAccountRequest request,
  required Future<void> Function() signOut,
  required Future<void> Function() clearWal,
  required VoidCallback clearPreferences,
  VoidCallback? onDeletionComplete,
}) async {
  await tester.binding.setSurfaceSize(const Size(430, 932));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  await tester.pumpWidget(
    MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: DeleteAccount(
        deleteAccountRequest: request,
        signOut: signOut,
        clearWal: clearWal,
        clearPreferences: clearPreferences,
        onDeletionComplete: onDeletionComplete,
        onDeleteConfirmed: () {},
        onDeleteSucceeded: () {},
      ),
    ),
  );
}

Future<void> _confirmDeletion(WidgetTester tester) async {
  await tester.tap(find.byType(Checkbox));
  await tester.pump();
  await tester.tap(find.byType(FilledButton));
  await tester.pumpAndSettle();
  await tester.tap(find.text('Delete Now'));
  await tester.pumpAndSettle();
}
