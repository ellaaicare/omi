import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/widgets/ella_v7_companion_card.dart';
import 'package:omi/ella/widgets/ella_v7_companion_preferences.dart';
import 'thick_review_regressions_test.dart' as review;
void main(){review.main();
 testWidgets('editor opened under A cannot save its draft into replacement B',(t)async{
 final prefs=SharedPreferencesUtil();prefs.uid='fictional-a';
 await t.pumpWidget(review.app(const Scaffold(body:SingleChildScrollView(child:EllaV7CompanionCard())),dark:false));
 await t.tap(find.byKey(const Key('v7-companion-customize')));await t.pumpAndSettle();
 await t.enterText(find.byKey(const Key('v7-companion-name')),'Fictional A unsaved name');
 prefs.uid='fictional-b';await t.pumpAndSettle();
 final save=find.byKey(const Key('v7-companion-save'));await t.ensureVisible(save);await t.pumpAndSettle();await t.tap(save);await t.pumpAndSettle();
 final observed=EllaV7CompanionPreferences.shared.name;
 await t.pumpWidget(const SizedBox());
 expect(observed,isEmpty,reason:'Original editor account must be captured at open, not lazily first read at Save');
 });
}
