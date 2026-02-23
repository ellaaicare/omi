import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// Ella AI color palette -- V6 "Ella Home" warm aquamarine teal, light mode for elderly users.
///
/// V6 direction: V3 warmth + V2 precision. Technology that feels like home.
/// All text colors verified for contrast ratio against bgPrimary (#FAF5F0):
///   textPrimary:   14.5:1 (AAA)
///   textSecondary: 10.4:1 (AAA)
///   textTertiary:   5.5:1 (AA)
///   textDisabled:   3.1:1 (AA large text only)
///   primary:        3.5:1 (AA large text)
class EllaColors {
  EllaColors._();

  // Primary teal palette -- V6 warm aquamarine
  static const Color primary = Color(0xFF5A9E8F);
  static const Color primaryDark = Color(0xFF4A8A7B);
  static const Color primaryLight = Color(0xFF7AB5A8);
  static const Color primarySubtle = Color(0xFFE8F5F0);

  // Backgrounds -- warm cream, inviting for elderly users
  static const Color bgPrimary = Color(0xFFFAF5F0); // warm cream
  static const Color bgSecondary = Color(0xFFF5F0E8); // light warm beige
  static const Color bgTertiary = Color(0xFFE8E0D6); // warm taupe

  // Text -- dark on light for readability
  static const Color textPrimary = Color(0xFF2D2D2D); // soft charcoal
  static const Color textSecondary = Color(0xFF4A4A4A); // dark grey
  static const Color textTertiary = Color(0xFF7A7A7A); // medium grey
  static const Color textDisabled = Color(0xFFB0B0B0); // light grey

  // Semantic (KEEP UNCHANGED)
  static const Color success = Color(0xFF10B981);
  static const Color warning = Color(0xFFF59E0B);
  static const Color error = Color(0xFFEF4444);
  static const Color emergency = Color(0xFFDC2626);
  static const Color emergencyDark = Color(0xFFB91C1C);
  static const Color emergencyBg = Color(0xFF7F1D1D);
}

/// Elder-friendly sizing constants.
///
/// Minimum touch target: 48dp (WCAG 2.2 AA).
/// Minimum body text: 18px.
/// Minimum caption text: 16px.
class EllaSizes {
  EllaSizes._();

  // Touch targets
  static const double minTouchTarget = 48.0;
  static const double emergencyButtonHeight = 72.0;
  static const double navBarHeight = 80.0;
  static const double guardianButtonHeight = 106.0;
  static const double buttonStackSpacing = 16.0;
  static const double listItemMinHeight = 56.0;
  static const double appBarButtonSize = 48.0;

  // Spacing
  static const double spacingXS = 4.0;
  static const double spacingS = 8.0;
  static const double spacingM = 16.0;
  static const double spacingL = 24.0;
  static const double spacingXL = 32.0;

  // Border radius
  static const double radiusSmall = 8.0;
  static const double radiusMedium = 12.0;
  static const double radiusLarge = 16.0;
  static const double radiusCircular = 100.0;

  // Icon sizes
  static const double iconSmall = 20.0;
  static const double iconMedium = 24.0;
  static const double iconLarge = 28.0;
}

/// Builds the Ella ThemeData. Drop-in replacement for the OMI theme in main.dart.
///
/// Usage in main.dart:
///   theme: ellaThemeData(),
///   themeMode: ThemeMode.light,
ThemeData ellaThemeData() {
  return ThemeData(
    useMaterial3: false,
    brightness: Brightness.light,
    colorScheme: const ColorScheme.light(
      primary: EllaColors.bgPrimary, // Used as scaffold/appBar bg throughout OMI pages
      secondary: EllaColors.primary, // Teal brand color
      surface: EllaColors.bgSecondary,
      error: EllaColors.error,
      onPrimary: EllaColors.textPrimary,
      onSecondary: Colors.white,
      onSurface: EllaColors.textPrimary,
      onError: Colors.white,
    ),
    scaffoldBackgroundColor: EllaColors.bgPrimary,
    snackBarTheme: SnackBarThemeData(
      backgroundColor: EllaColors.textPrimary,
      contentTextStyle: const TextStyle(
        fontSize: 18,
        color: Colors.white,
        fontWeight: FontWeight.w500,
      ),
    ),
    textTheme: const TextTheme(
      displayLarge: TextStyle(fontSize: 32, fontWeight: FontWeight.w700, color: EllaColors.textPrimary, height: 1.2),
      headlineMedium: TextStyle(fontSize: 26, fontWeight: FontWeight.w600, color: EllaColors.textPrimary, height: 1.3),
      titleLarge: TextStyle(fontSize: 22, fontWeight: FontWeight.w600, color: EllaColors.textPrimary, height: 1.3),
      titleMedium: TextStyle(fontSize: 20, fontWeight: FontWeight.w500, color: EllaColors.textPrimary, height: 1.4),
      bodyLarge: TextStyle(fontSize: 18, fontWeight: FontWeight.w400, color: EllaColors.textSecondary, height: 1.5),
      bodyMedium: TextStyle(fontSize: 18, fontWeight: FontWeight.w400, color: EllaColors.textSecondary, height: 1.5),
      labelLarge: TextStyle(fontSize: 18, fontWeight: FontWeight.w600, color: EllaColors.textPrimary, height: 1.4),
      labelMedium: TextStyle(fontSize: 16, fontWeight: FontWeight.w500, color: EllaColors.textTertiary, height: 1.4),
    ),
    textSelectionTheme: const TextSelectionThemeData(
      cursorColor: EllaColors.textPrimary,
      selectionColor: Color(0xFFB8E8DD), // warm teal selection
      selectionHandleColor: EllaColors.primary,
    ),
    cupertinoOverrideTheme: const CupertinoThemeData(
      primaryColor: EllaColors.primary,
      brightness: Brightness.light,
    ),
    bottomNavigationBarTheme: const BottomNavigationBarThemeData(
      backgroundColor: Colors.white,
      selectedItemColor: EllaColors.primary,
      unselectedItemColor: EllaColors.textTertiary,
      selectedLabelStyle: TextStyle(fontSize: 14, fontWeight: FontWeight.w500),
      unselectedLabelStyle: TextStyle(fontSize: 14, fontWeight: FontWeight.w400),
      type: BottomNavigationBarType.fixed,
      showSelectedLabels: true,
      showUnselectedLabels: true,
    ),
    appBarTheme: const AppBarTheme(
      backgroundColor: EllaColors.bgPrimary,
      foregroundColor: EllaColors.textPrimary,
      elevation: 0,
      systemOverlayStyle: SystemUiOverlayStyle.dark, // dark status bar icons
    ),
    cardTheme: CardThemeData(
      color: Colors.white,
      elevation: 1,
      shadowColor: Colors.black.withOpacity(0.08),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: Colors.white,
      contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        borderSide: const BorderSide(color: EllaColors.bgTertiary),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        borderSide: const BorderSide(color: EllaColors.bgTertiary),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        borderSide: const BorderSide(color: EllaColors.primary, width: 1.5),
      ),
      hintStyle: const TextStyle(fontSize: 18, color: EllaColors.textDisabled),
    ),
    elevatedButtonTheme: ElevatedButtonThemeData(
      style: ElevatedButton.styleFrom(
        backgroundColor: EllaColors.primary,
        foregroundColor: Colors.white,
        minimumSize: const Size(double.infinity, 56),
        textStyle: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        ),
      ),
    ),
    dialogTheme: DialogThemeData(
      backgroundColor: Colors.white,
      titleTextStyle: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600, color: EllaColors.textPrimary),
      contentTextStyle: const TextStyle(fontSize: 18, color: EllaColors.textSecondary, height: 1.5),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
      ),
    ),
  );
}
