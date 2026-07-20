import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:share_plus/share_plus.dart';

import 'package:omi/backend/schema/daily_summary.dart';
import 'package:omi/ella/ella_theme.dart';

class EllaDailyNotePage extends StatelessWidget {
  const EllaDailyNotePage({super.key, required this.summary});

  final DailySummary summary;

  String get _text => summary.overview.trim().replaceFirst(RegExp(r'^\[Ella\]\s*'), '');
  String get _date => DateFormat('EEEE · MMMM d').format(DateTime.parse(summary.date)).toUpperCase();

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          tooltip: MaterialLocalizations.of(context).backButtonTooltip,
          icon: const Icon(Icons.arrow_back_ios_new_rounded),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 40),
          children: [
            Container(
              padding: const EdgeInsets.all(EllaSizes.notePadding),
              decoration: BoxDecoration(
                color: EllaColors.card,
                borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(_date, style: EllaTextStyles.eyebrow),
                  const SizedBox(height: 16),
                  Text(_text, style: EllaTextStyles.noteBody),
                  const SizedBox(height: 24),
                  const Text('— Ella 🪽', style: EllaTextStyles.ellaSignOff),
                ],
              ),
            ),
            const SizedBox(height: 20),
            TextButton(
              onPressed: () => SharePlus.instance.share(ShareParams(text: _text)),
              style: TextButton.styleFrom(
                foregroundColor: EllaColors.tealDeep,
                minimumSize: const Size.fromHeight(EllaSizes.minTouchTarget),
                textStyle: const TextStyle(
                  fontFamily: EllaTextStyles.uiFont,
                  fontSize: 16,
                  fontWeight: FontWeight.w700,
                ),
              ),
              child: const Text('Share with family'),
            ),
          ],
        ),
      ),
    );
  }
}
