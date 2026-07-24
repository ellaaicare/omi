import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// Ella v2 design tokens. New Ella surfaces should use these semantic names.
class EllaColors {
  EllaColors._();

  static const Color paper = Color(0xFFFAF6F0);
  static const Color card = Color(0xFFF2EBE1);
  static const Color cardDeep = Color(0xFFE9DFD2);
  static const Color ink = Color(0xFF23201C);
  static const Color inkSoft = Color(0xFF665F56);
  static const Color teal = Color(0xFF5A9E8F);
  static const Color tealDeep = Color(0xFF38695E);

  // Compatibility aliases for Ella surfaces not yet migrated to v2 names.
  static const Color primary = teal;
  static const Color primaryDark = tealDeep;
  static const Color primaryLight = teal;
  static const Color primarySubtle = card;
  static const Color bgPrimary = paper;
  static const Color bgSecondary = card;
  static const Color bgTertiary = cardDeep;
  static const Color textPrimary = ink;
  static const Color textSecondary = inkSoft;
  static const Color textTertiary = inkSoft;
  static const Color textDisabled = inkSoft;

  // Existing safety flows retain their established semantic colors.
  static const Color success = Color(0xFF2F6D5F);
  static const Color warning = Color(0xFF8B6914);
  static const Color error = Color(0xFF9B3B36);
  static const Color emergency = Color(0xFF9B3B36);
  static const Color emergencyDark = Color(0xFF7B2E2A);
  static const Color emergencyBg = Color(0xFFF2EBE1);
}

class EllaSizes {
  EllaSizes._();

  static const double screenPadding = 20;
  static const double sectionGap = 28;
  static const double cardGap = 12;
  static const double cardPadding = 20;
  static const double notePadding = 24;
  static const double cardRadius = 20;
  static const double minTouchTarget = 48;

  // Compatibility aliases.
  static const double emergencyButtonHeight = 72;
  static const double navBarHeight = 80;
  static const double guardianButtonHeight = 106;
  static const double buttonStackSpacing = 16;
  static const double listItemMinHeight = 56;
  static const double appBarButtonSize = 48;
  static const double spacingXS = 4;
  static const double spacingS = 8;
  static const double spacingM = 16;
  static const double spacingL = 24;
  static const double spacingXL = 32;
  static const double radiusSmall = 8;
  static const double radiusMedium = 12;
  static const double radiusLarge = cardRadius;
  static const double radiusCircular = 100;
  static const double iconSmall = 20;
  static const double iconMedium = 24;
  static const double iconLarge = 28;
}

class EllaTextStyles {
  EllaTextStyles._();

  static const String uiFont = 'Manrope';
  static const String noteFont = 'Fraunces';

  static const TextStyle display = TextStyle(
    fontFamily: uiFont,
    fontSize: 28,
    fontWeight: FontWeight.w600,
    height: 1.15,
    color: EllaColors.ink,
  );

  static const TextStyle noteBody = TextStyle(
    fontFamily: noteFont,
    fontSize: 22,
    fontWeight: FontWeight.w500,
    height: 1.45,
    color: EllaColors.ink,
  );

  static const TextStyle body = TextStyle(
    fontFamily: uiFont,
    fontSize: 18,
    fontWeight: FontWeight.w400,
    height: 1.4,
    color: EllaColors.ink,
  );

  static const TextStyle secondary = TextStyle(
    fontFamily: uiFont,
    fontSize: 16,
    fontWeight: FontWeight.w400,
    height: 1.4,
    color: EllaColors.inkSoft,
  );

  static const TextStyle caption = TextStyle(
    fontFamily: uiFont,
    fontSize: 13,
    fontWeight: FontWeight.w400,
    height: 1.35,
    color: EllaColors.inkSoft,
  );

  static const TextStyle eyebrow = TextStyle(
    fontFamily: uiFont,
    fontSize: 12,
    fontWeight: FontWeight.w700,
    letterSpacing: 1.5,
    height: 1.3,
    color: EllaColors.inkSoft,
  );

  static const TextStyle ellaSignOff = TextStyle(
    fontFamily: noteFont,
    fontSize: 16,
    fontWeight: FontWeight.w400,
    fontStyle: FontStyle.italic,
    height: 1.35,
    color: EllaColors.inkSoft,
  );
}

ThemeData ellaThemeData() {
  const pressedOverlay = Color(0x1F38695E);
  return ThemeData(
    useMaterial3: false,
    brightness: Brightness.light,
    fontFamily: EllaTextStyles.uiFont,
    colorScheme: const ColorScheme.light(
      // Legacy Omi pages use primary as their screen background.
      primary: EllaColors.paper,
      secondary: EllaColors.teal,
      surface: EllaColors.card,
      error: EllaColors.error,
      onPrimary: EllaColors.ink,
      onSecondary: EllaColors.paper,
      onSurface: EllaColors.ink,
      onError: EllaColors.paper,
      outline: EllaColors.cardDeep,
      outlineVariant: EllaColors.cardDeep,
    ),
    scaffoldBackgroundColor: EllaColors.paper,
    splashColor: pressedOverlay,
    highlightColor: pressedOverlay,
    focusColor: pressedOverlay,
    hoverColor: pressedOverlay,
    textTheme: const TextTheme(
      displayLarge: EllaTextStyles.display,
      displayMedium: EllaTextStyles.display,
      headlineMedium: TextStyle(
        fontFamily: EllaTextStyles.uiFont,
        fontSize: 24,
        fontWeight: FontWeight.w600,
        color: EllaColors.ink,
      ),
      titleLarge: TextStyle(
        fontFamily: EllaTextStyles.uiFont,
        fontSize: 22,
        fontWeight: FontWeight.w600,
        color: EllaColors.ink,
        height: 1.3,
      ),
      titleMedium: TextStyle(
        fontFamily: EllaTextStyles.uiFont,
        fontSize: 18,
        fontWeight: FontWeight.w600,
        color: EllaColors.ink,
        height: 1.4,
      ),
      bodyLarge: EllaTextStyles.body,
      bodyMedium: EllaTextStyles.body,
      bodySmall: EllaTextStyles.secondary,
      labelLarge: TextStyle(
        fontFamily: EllaTextStyles.uiFont,
        fontSize: 18,
        fontWeight: FontWeight.w600,
        color: EllaColors.ink,
        height: 1.4,
      ),
      labelMedium: EllaTextStyles.secondary,
      labelSmall: EllaTextStyles.caption,
    ),
    textSelectionTheme: const TextSelectionThemeData(
      cursorColor: EllaColors.tealDeep,
      selectionColor: Color(0x4D5A9E8F),
      selectionHandleColor: EllaColors.tealDeep,
    ),
    cupertinoOverrideTheme: const CupertinoThemeData(
      primaryColor: EllaColors.tealDeep,
      scaffoldBackgroundColor: EllaColors.paper,
      brightness: Brightness.light,
    ),
    bottomNavigationBarTheme: const BottomNavigationBarThemeData(
      backgroundColor: EllaColors.card,
      selectedItemColor: EllaColors.tealDeep,
      unselectedItemColor: EllaColors.inkSoft,
      selectedLabelStyle: TextStyle(fontFamily: EllaTextStyles.uiFont, fontSize: 14, fontWeight: FontWeight.w600),
      unselectedLabelStyle: TextStyle(fontFamily: EllaTextStyles.uiFont, fontSize: 14, fontWeight: FontWeight.w400),
      type: BottomNavigationBarType.fixed,
      showSelectedLabels: true,
      showUnselectedLabels: true,
    ),
    appBarTheme: const AppBarTheme(
      backgroundColor: EllaColors.paper,
      foregroundColor: EllaColors.ink,
      surfaceTintColor: EllaColors.paper,
      elevation: 0,
      titleTextStyle: TextStyle(
        fontFamily: EllaTextStyles.uiFont,
        fontSize: 28,
        fontWeight: FontWeight.w600,
        color: EllaColors.ink,
      ),
      systemOverlayStyle: SystemUiOverlayStyle.dark,
    ),
    cardTheme: CardThemeData(
      color: EllaColors.card,
      elevation: 0,
      surfaceTintColor: EllaColors.card,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.cardRadius)),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: EllaColors.card,
      contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        borderSide: const BorderSide(color: EllaColors.cardDeep),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        borderSide: const BorderSide(color: EllaColors.cardDeep),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        borderSide: const BorderSide(color: EllaColors.tealDeep, width: 1.5),
      ),
      hintStyle: EllaTextStyles.secondary,
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: EllaColors.tealDeep,
        foregroundColor: EllaColors.paper,
        minimumSize: const Size(double.infinity, 56),
        textStyle: const TextStyle(fontFamily: EllaTextStyles.uiFont, fontSize: 18, fontWeight: FontWeight.w600),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.cardRadius)),
      ),
    ),
    elevatedButtonTheme: ElevatedButtonThemeData(
      style: ElevatedButton.styleFrom(
        backgroundColor: EllaColors.tealDeep,
        foregroundColor: EllaColors.paper,
        minimumSize: const Size(double.infinity, 56),
        elevation: 0,
        textStyle: const TextStyle(fontFamily: EllaTextStyles.uiFont, fontSize: 18, fontWeight: FontWeight.w600),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.cardRadius)),
      ),
    ),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: EllaColors.ink,
      contentTextStyle: TextStyle(
        fontFamily: EllaTextStyles.uiFont,
        fontSize: 18,
        color: EllaColors.paper,
        fontWeight: FontWeight.w500,
      ),
    ),
    dialogTheme: DialogThemeData(
      backgroundColor: EllaColors.paper,
      surfaceTintColor: EllaColors.paper,
      titleTextStyle: const TextStyle(
        fontFamily: EllaTextStyles.uiFont,
        fontSize: 22,
        fontWeight: FontWeight.w600,
        color: EllaColors.ink,
      ),
      contentTextStyle: EllaTextStyles.body,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.cardRadius)),
    ),
  );
}
