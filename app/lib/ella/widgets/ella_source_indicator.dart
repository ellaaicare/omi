import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/display_text.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaSourceIndicator extends StatelessWidget {
  final double size;

  const EllaSourceIndicator({super.key, this.size = 16});

  @override
  Widget build(BuildContext context) {
    final label = context.l10n.ellaSummarySource;
    return Tooltip(
      message: label,
      child: Semantics(
        label: label,
        child: Icon(
          Icons.auto_awesome_rounded,
          size: size,
          color: EllaColors.primary,
        ),
      ),
    );
  }
}

class EllaSourceBadge extends StatelessWidget {
  const EllaSourceBadge({super.key});

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: EllaColors.bgSecondary,
        shape: BoxShape.circle,
        border: Border.all(color: EllaColors.bgTertiary),
      ),
      child: const Padding(
        padding: EdgeInsets.all(3),
        child: EllaSourceIndicator(size: 11),
      ),
    );
  }
}

class EllaSourceText extends StatelessWidget {
  final String value;
  final TextStyle? style;
  final int? maxLines;
  final TextOverflow overflow;
  final TextAlign? textAlign;

  const EllaSourceText(
    this.value, {
    super.key,
    this.style,
    this.maxLines,
    this.overflow = TextOverflow.clip,
    this.textAlign,
  });

  @override
  Widget build(BuildContext context) {
    final displayValue = parseEllaDisplayValue(value);
    return Text.rich(
      TextSpan(
        children: [
          if (displayValue.isEllaGenerated) ...[
            const WidgetSpan(
              alignment: PlaceholderAlignment.middle,
              child: EllaSourceIndicator(),
            ),
            const TextSpan(text: ' '),
          ],
          TextSpan(text: displayValue.text),
        ],
      ),
      style: style,
      maxLines: maxLines,
      overflow: overflow,
      textAlign: textAlign,
    );
  }
}
