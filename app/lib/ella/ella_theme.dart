import 'dart:math' as math;
import 'package:omi/ella/widgets/ella_v7_appearance.dart';
import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// Iris v5.2 light design tokens. New Ella surfaces should use these semantic names.
class EllaColors {
  EllaColors._();

  static const Color paper = Color(0xFFF3EEF6);
  static const Color elevatedCard = Color(0xFFFFFFFF);
  static const Color card = Color(0xFFF5F6FA);
  static const Color cardDeep = Color(0xFFE6EEFF);
  static const Color cardEdge = Color(0xFFCBD0DE);
  static const Color ink = Color(0xFF1F1B3A);
  static const Color inkSoft = Color(0xFF514966);
  static const Color teal = Color(0xFF5A4FD6);
  static const Color tealDeep = Color(0xFF1F1B3A);

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
  static const Color emergencyBg = Color(0xFFF5F6FA);
}

class EllaSizes {
  EllaSizes._();

  static const double screenPadding = 20;
  static const double sectionGap = 28;
  static const double cardGap = 12;
  static const double cardPadding = 20;
  static const double notePadding = 24;
  static const double cardRadius = 16;
  static const double minTouchTarget = 48;

  // Compatibility aliases.
  static const double emergencyButtonHeight = 72;
  static const double navBarHeight = 64;
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

class EllaCardSurface extends StatelessWidget {
  const EllaCardSurface({
    super.key,
    required this.child,
    this.color = EllaColors.card,
    this.borderRadius = EllaSizes.cardRadius,
  });

  static const shadow = BoxShadow(color: Color(0x0F0B1020), offset: Offset(0, 1), blurRadius: 2);

  final Widget child;
  final Color color;
  final double borderRadius;

  @override
  Widget build(BuildContext context) {
    final radius = BorderRadius.circular(borderRadius);
    final palette = EllaPalette.of(context);
    final resolvedColor = palette.resolve(color);
    return DecoratedBox(
      decoration: BoxDecoration(
        color: resolvedColor,
        borderRadius: radius,
        border: Border.all(color: palette.cardEdge),
        boxShadow: const [shadow],
      ),
      child: Material(color: Colors.transparent, borderRadius: radius, clipBehavior: Clip.antiAlias, child: child),
    );
  }
}

class EllaTextStyles {
  EllaTextStyles._();

  static const String uiFont = '.SF UI Text';
  static const String noteFont = 'InstrumentSerif';

  static const TextStyle display = TextStyle(
    fontFamily: noteFont,
    fontSize: 46,
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
    fontSize: 16,
    fontWeight: FontWeight.w400,
    height: 1.35,
    color: EllaColors.inkSoft,
  );

  static const TextStyle eyebrow = TextStyle(
    fontFamily: uiFont,
    fontSize: 16,
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
  const pressedOverlay = Color(0x1F1D47B8);
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
    // Both endpoints must be opaque: ThemeData.lerp otherwise interpolates a
    // missing light sheet color from transparent, exposing the Journal below.
    bottomSheetTheme: const BottomSheetThemeData(backgroundColor: EllaColors.card),
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
      selectionColor: Color(0x4D6278C8),
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
      elevation: 1,
      shadowColor: const Color(0x0F0B1020),
      surfaceTintColor: EllaColors.card,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(EllaSizes.cardRadius),
        side: const BorderSide(color: EllaColors.cardEdge),
      ),
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

/// Contextual Iris palette. Safety colors keep their established meaning.
class EllaPalette {
  const EllaPalette(this.dark, {this.nightMix});
  final double? nightMix;
  Color mix(Color light, Color night) => Color.lerp(light, night, nightMix ?? (dark ? 1 : 0))!;
  final bool dark;
  static EllaPalette of(BuildContext context) => EllaPalette(Theme.of(context).brightness == Brightness.dark,
      nightMix: Theme.of(context).extension<EllaV7Tone>()?.night);
  Color get paper => mix(EllaColors.paper, const Color(0xFF0B0A1C));
  Color _surface(Color day) {
    final amount = nightMix ?? (dark ? 1.0 : 0.0);
    final unify = ((.35 - (amount - .5).abs()) / .15).clamp(0.0, 1.0);
    return Color.lerp(mix(day, const Color(0xFF221F45)), paper, unify)!;
  }

  Color get elevatedCard => _surface(EllaColors.elevatedCard);
  Color get card => _surface(EllaColors.card);
  Color get cardDeep => mix(EllaColors.cardDeep, const Color(0xFF293354));
  Color get cardEdge => mix(EllaColors.cardEdge, const Color(0xFF55546B));
  double _contrast(Color foreground, Color background) {
    final a = foreground.computeLuminance(), b = background.computeLuminance();
    return (math.max(a, b) + .05) / (math.min(a, b) + .05);
  }

  Color _readable(Color day, Color night) {
    double score(Color color) => math.min(_contrast(color, paper), _contrast(color, card));
    final preferred = score(day) >= score(night) ? day : night;
    if (score(preferred) >= 4.5) return preferred;
    return score(Colors.black) >= score(Colors.white) ? Colors.black : Colors.white;
  }

  // Blending opposing ink colours would erase text around twilight. Choose
  // readable ink against both actual blended surfaces; never fade the text.
  Color get ink => _readable(EllaColors.ink, const Color(0xFFF1ECE2));
  Color get inkSoft {
    final candidate = _readable(EllaColors.inkSoft, const Color(0xFFC9C6D6));
    return math.min(_contrast(candidate, paper), _contrast(candidate, card)) >= 4.5 ? candidate : ink;
  }

  Color get teal => mix(EllaColors.teal, const Color(0xFFB1A8FF));
  Color get tealDeep => ink;
  Color get userBubbleFill => dark ? ink : EllaColors.ink;
  Color get userBubbleInk => dark ? paper : Colors.white;
  Color get assistantBubbleFill => dark ? const Color(0xF0282640) : Colors.white;
  Color get primary => tealDeep;
  Color get primaryDark => tealDeep;
  Color get primaryLight => teal;
  Color get primarySubtle => card;
  Color get bgPrimary => paper;
  Color get bgSecondary => card;
  Color get bgTertiary => cardDeep;
  Color get textPrimary => ink;
  Color get textSecondary => inkSoft;
  Color get textTertiary => inkSoft;
  Color get textDisabled => inkSoft;
  Color get success => mix(EllaColors.success, const Color(0xFF8FEBB9));
  Color get warning => mix(EllaColors.warning, const Color(0xFFFFC861));
  Color get error => mix(EllaColors.error, const Color(0xFFFF8C98));
  Color get emergency => error;
  Color get emergencyDark => error;
  Color get emergencyBg => card;
  Color resolve(Color color) {
    if (color == EllaColors.paper) return paper;
    if (color == EllaColors.elevatedCard) return elevatedCard;
    if (color == EllaColors.card) return card;
    if (color == EllaColors.cardDeep) return cardDeep;
    if (color == EllaColors.ink) return ink;
    if (color == EllaColors.inkSoft) return inkSoft;
    if (color == EllaColors.tealDeep) return tealDeep;
    return color;
  }
}

class EllaTypography {
  EllaTypography.of(BuildContext context) : palette = EllaPalette.of(context);
  final EllaPalette palette;
  TextStyle get display => EllaTextStyles.display.copyWith(color: palette.ink);
  TextStyle get noteBody => EllaTextStyles.noteBody.copyWith(color: palette.ink);
  TextStyle get body => EllaTextStyles.body.copyWith(color: palette.ink);
  TextStyle get secondary => EllaTextStyles.secondary.copyWith(color: palette.inkSoft);
  TextStyle get caption => EllaTextStyles.caption.copyWith(color: palette.inkSoft);
  TextStyle get eyebrow => EllaTextStyles.eyebrow.copyWith(color: palette.inkSoft);
  TextStyle get ellaSignOff => EllaTextStyles.ellaSignOff.copyWith(color: palette.inkSoft);
}

ThemeData ellaDarkThemeData() {
  const p = EllaPalette(true);
  final light = ellaThemeData();
  return light.copyWith(
    brightness: Brightness.dark,
    colorScheme: ColorScheme.dark(
        primary: p.paper,
        secondary: p.teal,
        surface: p.card,
        onPrimary: p.ink,
        onSecondary: p.paper,
        onSurface: p.ink,
        error: p.error,
        onError: p.paper,
        outline: p.cardEdge,
        outlineVariant: p.cardDeep),
    scaffoldBackgroundColor: p.paper,
    textTheme: light.textTheme.apply(bodyColor: p.ink, displayColor: p.ink),
    appBarTheme: light.appBarTheme.copyWith(
        backgroundColor: p.paper,
        foregroundColor: p.ink,
        surfaceTintColor: p.paper,
        titleTextStyle: light.appBarTheme.titleTextStyle?.copyWith(color: p.ink),
        systemOverlayStyle: SystemUiOverlayStyle.light),
    cardTheme: light.cardTheme.copyWith(color: p.card, surfaceTintColor: p.card),
    inputDecorationTheme: light.inputDecorationTheme
        .copyWith(fillColor: p.card, hintStyle: EllaTextStyles.secondary.copyWith(color: p.inkSoft)),
    dialogTheme: light.dialogTheme.copyWith(
        backgroundColor: p.card,
        surfaceTintColor: p.card,
        titleTextStyle: light.dialogTheme.titleTextStyle?.copyWith(color: p.ink),
        contentTextStyle: EllaTextStyles.body.copyWith(color: p.ink)),
    bottomSheetTheme: BottomSheetThemeData(backgroundColor: p.card),
    iconTheme: IconThemeData(color: p.ink),
    cupertinoOverrideTheme:
        CupertinoThemeData(brightness: Brightness.dark, primaryColor: p.tealDeep, scaffoldBackgroundColor: p.paper),
  );
}

/// Static wash behind controls; content stays on opaque readable surfaces.
class EllaIrisBackdrop extends StatelessWidget {
  const EllaIrisBackdrop({super.key, required this.child});
  final Widget child;
  @override
  Widget build(BuildContext context) => EllaV7Atmosphere(child: child);
}

/// Display headings keep complete words; body text retains the user's scaling.
class EllaDisplayHeading extends StatelessWidget {
  const EllaDisplayHeading(this.text, {super.key, this.fontSize = 46});
  final String text;
  final double fontSize;

  @override
  Widget build(BuildContext context) => LayoutBuilder(builder: (context, constraints) {
        final scale = MediaQuery.textScalerOf(context);
        final style = EllaTypography.of(context).display.copyWith(fontSize: fontSize);
        double longestWord(TextStyle candidate) {
          var width = 0.0;
          for (final word in text.split(RegExp(r'\s+'))) {
            final painter = TextPainter(
                text: TextSpan(text: word, style: candidate),
                textDirection: Directionality.of(context),
                textScaler: scale)
              ..layout();
            if (painter.width > width) width = painter.width;
            painter.dispose();
          }
          return width;
        }

        var fitted = fontSize;
        // Only display typography may fit down, and never below the existing 20px heading.
        while (fitted > 20 && longestWord(style.copyWith(fontSize: fitted)) > constraints.maxWidth) {
          fitted = (fitted - 0.5).clamp(20.0, fontSize);
        }
        final resolved = style.copyWith(fontSize: fitted);
        final wordWidth = longestWord(resolved);
        final heading = Text(text, style: resolved);
        return Semantics(
            header: true,
            child: wordWidth <= constraints.maxWidth
                ? heading
                : SingleChildScrollView(
                    scrollDirection: Axis.horizontal,
                    child: SizedBox(width: wordWidth.ceilToDouble(), child: heading)));
      });
}
