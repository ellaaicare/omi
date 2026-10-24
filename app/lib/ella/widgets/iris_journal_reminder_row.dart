import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';

({List<ActionItemWithMetadata> coming, List<ActionItemWithMetadata> past, List<ActionItemWithMetadata> noDue})
    irisReminderGroups(Iterable<ActionItemWithMetadata> items, DateTime now) {
  final coming = <ActionItemWithMetadata>[], past = <ActionItemWithMetadata>[], noDue = <ActionItemWithMetadata>[];
  for (final item in items) {
    if (item.dueAt == null)
      noDue.add(item);
    else if (item.dueAt!.isBefore(now))
      past.add(item);
    else
      coming.add(item);
  }
  coming.sort((a, b) => a.dueAt!.compareTo(b.dueAt!));
  past.sort((a, b) => b.dueAt!.compareTo(a.dueAt!));
  return (coming: coming, past: past, noDue: noDue);
}

class IrisJournalReminderRow extends StatelessWidget {
  const IrisJournalReminderRow(
      {super.key,
      required this.item,
      required this.onOpen,
      required this.now,
      this.saidAt,
      this.dueColumn = false,
      this.showWeekday = false});
  final ActionItemWithMetadata item;
  final DateTime now;
  final DateTime? saidAt;
  final VoidCallback onOpen;
  final bool dueColumn, showWeekday;
  @override
  Widget build(BuildContext context) {
    final large = MediaQuery.textScalerOf(context).scale(1) >= 1.2;
    final due = item.dueAt;
    final past = due?.isBefore(now) == true;
    final date = dueColumn ? due : saidAt;
    final stamp = date == null
        ? context.l10n.irisNotShownYet
        : (dueColumn || showWeekday ? DateFormat.E().add_jm() : DateFormat.jm()).format(date.toLocal());
    final dateWidget = Text(stamp,
        key: Key('iris-reminder-date-${item.id}'),
        style: EllaTypography.of(context)
            .secondary
            .copyWith(fontSize: 16, fontWeight: dueColumn ? FontWeight.w800 : FontWeight.w400));
    final title = Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        EllaV7Glass(
            radius: 14,
            padding: const EdgeInsets.all(10),
            child: const Icon(Icons.notifications_none_rounded, size: 24)),
        const SizedBox(width: 8),
        Expanded(
            child: Text(item.description,
                style: EllaTypography.of(context).body.copyWith(fontSize: 17, fontWeight: FontWeight.w600)))
      ]),
      if (!dueColumn && due != null) ...[
        const SizedBox(height: 4),
        Text(
            past
                ? context.l10n.irisWasDue(DateFormat.E().add_jm().format(due.toLocal()))
                : context.l10n.irisDue(DateFormat.E().add_jm().format(due.toLocal())),
            key: Key('iris-reminder-due-${item.id}'),
            style: EllaTypography.of(context).body.copyWith(
                fontSize: 16,
                fontWeight: past ? FontWeight.w700 : FontWeight.w800,
                color: EllaPalette.of(context).textSecondary)),
      ],
    ]);
    return InkWell(
        onTap: onOpen,
        child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: large ? 72 : 64),
            child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 4),
                child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  SizedBox(width: large ? 100 : 76, child: dateWidget),
                  const SizedBox(width: 8),
                  Expanded(child: title),
                ]))));
  }
}
