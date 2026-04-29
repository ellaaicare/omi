import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/platform/platform_service.dart';

getDialog(
  BuildContext context,
  Function onCancel,
  Function onConfirm,
  String title,
  String content, {
  bool singleButton = false,
  String? okButtonText,
  String? cancelButtonText,
}) {
  final okText = okButtonText ?? context.l10n.ok;
  final cancelText = cancelButtonText ?? context.l10n.cancel;

  var actions = singleButton
      ? [
          TextButton(
            onPressed: () => onCancel(),
            child: Text(okText, style: const TextStyle(color: EllaColors.primary, fontWeight: FontWeight.w600)),
          )
        ]
      : [
          TextButton(
            onPressed: () => onCancel(),
            child: Text(cancelText, style: const TextStyle(color: EllaColors.textSecondary)),
          ),
          TextButton(
            onPressed: () => onConfirm(),
            child: Text(okText, style: const TextStyle(color: EllaColors.primary, fontWeight: FontWeight.w600)),
          ),
        ];
  if (PlatformService.isApple) {
    return CupertinoTheme(
      data: const CupertinoThemeData(
        brightness: Brightness.light,
        primaryColor: EllaColors.primary,
        textTheme: CupertinoTextThemeData(
          textStyle: TextStyle(color: EllaColors.textPrimary),
        ),
      ),
      child: CupertinoAlertDialog(
        title: Text(title, style: const TextStyle(color: EllaColors.textPrimary)),
        content: Text(content, style: const TextStyle(color: EllaColors.textSecondary)),
        actions: actions,
      ),
    );
  }
  return AlertDialog(
    backgroundColor: Colors.white,
    surfaceTintColor: Colors.transparent,
    title: Text(title, style: const TextStyle(color: EllaColors.textPrimary)),
    content: Text(content, style: const TextStyle(color: EllaColors.textSecondary)),
    actions: actions,
  );
}
