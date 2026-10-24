import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:omi/utils/l10n_extensions.dart';

enum IrisJournalFilter { all, memories, notes, reminders }

String irisJournalDayLabel(BuildContext context, DateTime value, DateTime now) {
  final date = value.toLocal(), current = now.toLocal();
  final age = DateTime.utc(current.year, current.month, current.day)
      .difference(DateTime.utc(date.year, date.month, date.day))
      .inDays;
  if (age == 0) return context.l10n.today;
  if (age == 1) return context.l10n.yesterday;
  if (age > 1 && age < 7) return DateFormat.EEEE().format(date);
  return date.year == current.year ? DateFormat.MMMMd().format(date) : DateFormat.yMMMMd().format(date);
}
