import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/models/escalation_policy.dart';
import 'package:omi/ella/pages/alert_channels_page.dart';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/l10n/app_localizations.dart';

EscalationPolicy _policy() {
  return EscalationPolicy.fromJson({
    'policy_version': 'test',
    'uid': 'opaque-test-user',
    'user': {
      'channels': [
        {'channel': 'imessage', 'enabled': true, 'reason': 'Phone number on file'},
        {'channel': 'email', 'enabled': true, 'reason': 'Email on file'},
      ],
    },
    'emergency_contact': {'configured': false, 'text': ''},
    'caregivers': <Object>[],
    'rules': <Object>[],
    'privacy_notes': <Object>[],
    'display': {'title': 'Alerts', 'subtitle': ''},
    'generated_at': '2026-09-18T00:00:00Z',
  });
}

void main() {
  testWidgets('does not present legacy phone-only iMessage as ready', (tester) async {
    final semanticsHandle = tester.ensureSemantics();

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: AlertChannelsPage(loadPolicy: () async => EllaServiceResult.success(_policy())),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('iMessage'), findsOneWidget);
    expect(find.text('Unavailable'), findsOneWidget);
    expect(find.text('iMessage is unavailable while Ella upgrades messaging.'), findsOneWidget);
    expect(find.text('Phone number on file'), findsNothing);
    expect(find.text('Email on file'), findsOneWidget);
    expect(find.text('Enabled'), findsOneWidget);

    final semantics = tester.getSemantics(find.byKey(const ValueKey('channel-status-imessage')));
    expect(semantics.label, 'iMessage, Unavailable');
    expect(semantics.childrenCountInTraversalOrder, 0);
    semanticsHandle.dispose();
  });
}
