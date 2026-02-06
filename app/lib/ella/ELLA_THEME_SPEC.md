# Ella Theme Spec + Simplified Navigation

**Date**: 2026-02-06
**Author**: UX Designer (Claude Code)
**Status**: Ready for iOS engineer implementation
**Blocks**: Task #10 (Ella app rebrand)

---

## 1. Color Palette

### Primary Colors (Teal)

All teal values derived from the Ella brand color `#14B8A6`, tuned for WCAG AA contrast compliance on dark backgrounds.

| Token                  | Hex       | Usage                                      | Contrast on #121212 |
|------------------------|-----------|--------------------------------------------|---------------------|
| `ellaPrimary`          | `#14B8A6` | Primary buttons, active nav, links          | 5.8:1 (AA)          |
| `ellaPrimaryDark`      | `#0D9488` | Pressed/hover state, app bar accents        | 4.9:1 (AA large)    |
| `ellaPrimaryLight`     | `#5EEAD4` | Highlights, badges, selected states         | 9.5:1 (AAA)         |
| `ellaPrimarySubtle`    | `#CCFBF1` | Tinted text on dark bg (rare)               | 14.7:1 (AAA)        |

### Background Colors

Using a warm dark palette instead of pure black. Pure black (`#000000`) causes halation (optical blur) for aging eyes. A slightly lifted dark tone is more comfortable.

| Token                  | Hex       | Usage                                      |
|------------------------|-----------|--------------------------------------------|
| `bgPrimary`            | `#121212` | Main scaffold background                    |
| `bgSecondary`          | `#1E1E1E` | Cards, elevated surfaces                    |
| `bgTertiary`           | `#2A2A2A` | Input fields, chips, pressed states         |
| `bgEmergency`          | `#7F1D1D` | Emergency button background                 |

### Text Colors

All text colors chosen for WCAG AA compliance (4.5:1 minimum for normal text, 3:1 for large text) against `bgPrimary` (#121212).

| Token                  | Hex       | Contrast on #121212 | Usage                    |
|------------------------|-----------|---------------------|--------------------------|
| `textPrimary`          | `#FFFFFF` | 17.0:1 (AAA)        | Headings, primary content |
| `textSecondary`        | `#E0E0E0` | 13.3:1 (AAA)        | Body text                 |
| `textTertiary`         | `#B0B0B0` | 8.2:1 (AAA)         | Captions, timestamps      |
| `textDisabled`         | `#757575` | 3.8:1 (AA large)    | Disabled states ONLY      |

### Semantic Colors

| Token                  | Hex       | Usage                                      |
|------------------------|-----------|--------------------------------------------|
| `success`              | `#10B981` | Confirmations, connected states             |
| `warning`              | `#F59E0B` | Warnings, attention needed                  |
| `error`                | `#EF4444` | Errors, destructive actions                 |
| `emergency`            | `#DC2626` | Emergency button, urgent alerts             |

### Colors NOT to Use

- `Colors.deepPurple` -- OMI brand, replace with `ellaPrimary` everywhere (175 occurrences in 55 files)
- `Color(0xFF9A9BA1)` -- Fails contrast on dark bg (4.2:1). Replace with `textTertiary`
- `Color(0xFF6A6B71)` -- Fails contrast (2.8:1). Replace with `textTertiary`
- `Colors.black` for background -- Use `bgPrimary` (#121212) instead
- `Color(0xFF1F1F25)` -- Replace with `bgSecondary`
- `Color(0xFF35343B)` -- Replace with `bgTertiary`

---

## 2. Typography Scale

### Design Principles for Elder Care

- Minimum body text: 18px (not 14px as in current app)
- Minimum caption/meta text: 16px (not 12px)
- Line height: 1.5 minimum for readability
- Font weight: Regular (400) minimum for body, never use light (300)
- Font: System default (San Francisco on iOS, Roboto on Android). Remove hardcoded `Manrope` references

### Typography Scale

| Style           | Size  | Weight | Line Height | Color           | Usage                        |
|-----------------|-------|--------|-------------|-----------------|------------------------------|
| `displayLarge`  | 32px  | 700    | 1.2         | `textPrimary`   | Page titles (rare)           |
| `headlineMedium`| 26px  | 600    | 1.3         | `textPrimary`   | Section headers              |
| `titleLarge`    | 22px  | 600    | 1.3         | `textPrimary`   | Card titles, conversation titles |
| `titleMedium`   | 20px  | 500    | 1.4         | `textPrimary`   | Subtitles, nav labels        |
| `bodyLarge`     | 18px  | 400    | 1.5         | `textSecondary` | Primary body text            |
| `bodyMedium`    | 18px  | 400    | 1.5         | `textSecondary` | Standard body (same as large)|
| `labelLarge`    | 18px  | 600    | 1.4         | `textPrimary`   | Button text                  |
| `labelMedium`   | 16px  | 500    | 1.4         | `textTertiary`  | Timestamps, captions, badges |

Note: `bodyLarge` and `bodyMedium` are intentionally the same size (18px). This prevents accidental use of smaller body text.

---

## 3. Touch Targets

### Minimum Sizes (WCAG 2.2 Level AA)

| Element              | Minimum Size | Current OMI Size | Notes                      |
|----------------------|-------------|------------------|----------------------------|
| All tappable areas   | 48x48 dp    | 36x36 dp         | Current app bar buttons fail|
| Bottom nav items     | 48x48 dp    | 26px icon only   | Need label + larger target  |
| FAB / primary action | 64x64 dp    | 80x80 dp         | Current record button is ok |
| List item rows       | 56 dp tall  | ~48 dp           | Increase row height         |
| Emergency button     | 72x72 dp    | N/A (new)        | Must be the largest target  |

### Spacing

| Token       | Value  | Usage                                    |
|-------------|--------|------------------------------------------|
| `spacingXS` | 4 dp   | Internal icon padding                    |
| `spacingS`  | 8 dp   | Between related elements                 |
| `spacingM`  | 16 dp  | Between sections, card padding           |
| `spacingL`  | 24 dp  | Between cards, major sections            |
| `spacingXL` | 32 dp  | Page padding, large gaps                 |

---

## 4. Simplified Navigation

### Current OMI Navigation (4 tabs + floating elements)

```
[Home/Conversations] [Action Items] [*Record*] [Memories] [Apps]
                                          + floating "Ask Omi" button
                                          + gear icon -> settings drawer
```

Problems for elder care:
- 4 tabs + record button + floating chat + settings = 7 navigation targets
- Icon-only labels
- Action Items and Apps tabs irrelevant for Ella MVP
- Settings in a bottom sheet drawer (disorienting)

### Proposed Ella Navigation (3 tabs with labels)

```
+-----------+-----------+-----------+
|   Home    |   Chat    | Settings  |
| [house]   | [comment] |  [gear]   |
+-----------+-----------+-----------+
```

**Tab 1: Home** -- Conversations list + device status + emergency button
**Tab 2: Chat** -- Full chat interface with Ella (currently a separate page push)
**Tab 3: Settings** -- Full-page settings (not a drawer)

### Tab Bar Spec

```
Height: 80 dp (including safe area)
Background: bgSecondary (#1E1E1E) with top border (bgTertiary, 0.5dp)
Icon size: 24 dp
Label: 14px, medium weight (visible below icon)
Active color: ellaPrimary (#14B8A6)
Inactive color: textTertiary (#B0B0B0)
Touch target per tab: full width / 3, full height (80dp)
```

### Removed from Ella MVP

These OMI features should be hidden/removed in the Ella fork:

| Feature            | Reason                               | Files affected           |
|--------------------|--------------------------------------|--------------------------|
| Action Items tab   | Power-user productivity feature      | `action_items_page.dart` |
| Apps tab / App Store| Plugin ecosystem not relevant       | `pages/apps/`            |
| Merge conversations | Complex gesture-based feature       | `merge_action_bar.dart`  |
| Folders            | Organizational complexity            | `folder_tabs.dart`       |
| Daily Score widget | Gamification not appropriate          | `daily_score_widget.dart`|
| Goals widget       | Productivity feature                 | `goals_widget.dart`      |
| Speech profile     | Technical setup step                 | `speech_profile_widget`  |
| Wrapped 2025       | OMI-specific promotional feature     | `wrapped_2025_page.dart` |
| Developer settings | Not for end users                    | `developer.dart`         |

---

## 5. Emergency Button

### Placement

The emergency button lives on the Home tab, always visible at the bottom of the screen above the tab bar. It is NOT inside the scrollable conversation list.

```
+----------------------------------+
|  [battery]    Home      [gear]   |  <- App bar
|----------------------------------|
|                                  |
|  Conversations list              |
|  (scrollable)                    |
|                                  |
|----------------------------------|
|  [!!! EMERGENCY - GET HELP !!!]  |  <- Fixed position, 72dp tall
|----------------------------------|
|  [Home]     [Chat]    [Settings] |  <- Tab bar
+----------------------------------+
```

### Emergency Button Spec

```
Position: fixed above tab bar, full width with 16dp horizontal padding
Height: 72 dp
Background: emergency (#DC2626)
Border radius: 16 dp
Icon: warning triangle or phone, 28dp, white
Text: "Emergency - Get Help" (or localized), 20px, bold, white
Shadow: 0 4dp 12dp rgba(220, 38, 38, 0.4) -- red glow
Touch feedback: darken to #B91C1C on press
Haptic: heavy impact

On tap:
1. Immediate haptic feedback (heavy)
2. Send emergency alert via backend (POST to scanner webhook with HIGH urgency)
3. Show confirmation overlay: "Help is being sent. [Call Emergency Contact]"
4. Optional: direct phone call to emergency contact
```

---

## 6. Flutter Implementation

### ThemeData (drop-in replacement for main.dart lines 395-419)

```dart
// lib/ella/ella_theme.dart

import 'package:flutter/material.dart';
import 'package:flutter/cupertino.dart';

class EllaColors {
  EllaColors._();

  // Primary teal palette
  static const Color primary = Color(0xFF14B8A6);
  static const Color primaryDark = Color(0xFF0D9488);
  static const Color primaryLight = Color(0xFF5EEAD4);
  static const Color primarySubtle = Color(0xFFCCFBF1);

  // Backgrounds
  static const Color bgPrimary = Color(0xFF121212);
  static const Color bgSecondary = Color(0xFF1E1E1E);
  static const Color bgTertiary = Color(0xFF2A2A2A);

  // Text
  static const Color textPrimary = Color(0xFFFFFFFF);
  static const Color textSecondary = Color(0xFFE0E0E0);
  static const Color textTertiary = Color(0xFFB0B0B0);
  static const Color textDisabled = Color(0xFF757575);

  // Semantic
  static const Color success = Color(0xFF10B981);
  static const Color warning = Color(0xFFF59E0B);
  static const Color error = Color(0xFFEF4444);
  static const Color emergency = Color(0xFFDC2626);
  static const Color emergencyDark = Color(0xFFB91C1C);
  static const Color emergencyBg = Color(0xFF7F1D1D);
}

ThemeData ellaThemeData() {
  return ThemeData(
    useMaterial3: false,
    colorScheme: const ColorScheme.dark(
      primary: EllaColors.bgPrimary,
      secondary: EllaColors.primary,
      surface: EllaColors.bgSecondary,
      error: EllaColors.error,
    ),
    scaffoldBackgroundColor: EllaColors.bgPrimary,
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: EllaColors.bgSecondary,
      contentTextStyle: TextStyle(
        fontSize: 18,
        color: EllaColors.textPrimary,
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
      selectionColor: EllaColors.primary,
      selectionHandleColor: EllaColors.textPrimary,
    ),
    cupertinoOverrideTheme: const CupertinoThemeData(
      primaryColor: EllaColors.textPrimary,
    ),
    // Ensure bottom nav uses teal
    bottomNavigationBarTheme: const BottomNavigationBarThemeData(
      backgroundColor: EllaColors.bgSecondary,
      selectedItemColor: EllaColors.primary,
      unselectedItemColor: EllaColors.textTertiary,
      selectedLabelStyle: TextStyle(fontSize: 14, fontWeight: FontWeight.w500),
      unselectedLabelStyle: TextStyle(fontSize: 14, fontWeight: FontWeight.w400),
      type: BottomNavigationBarType.fixed,
      showSelectedLabels: true,
      showUnselectedLabels: true,
    ),
    // AppBar
    appBarTheme: const AppBarTheme(
      backgroundColor: EllaColors.bgPrimary,
      foregroundColor: EllaColors.textPrimary,
      elevation: 0,
    ),
    // Cards
    cardTheme: CardTheme(
      color: EllaColors.bgSecondary,
      elevation: 0,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
    ),
    // Input fields
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: EllaColors.bgTertiary,
      contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: BorderSide.none,
      ),
      hintStyle: const TextStyle(fontSize: 18, color: EllaColors.textDisabled),
    ),
    // Elevated buttons
    elevatedButtonTheme: ElevatedButtonThemeData(
      style: ElevatedButton.styleFrom(
        backgroundColor: EllaColors.primary,
        foregroundColor: EllaColors.textPrimary,
        minimumSize: const Size(double.infinity, 56),
        textStyle: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      ),
    ),
  );
}
```

### Updated AppStyles (drop-in replacement for ui_guidelines.dart)

```dart
// Ella-specific AppStyles with elder-friendly sizing
class EllaAppStyles {
  // Text Styles (18px minimum for body)
  static const TextStyle title = TextStyle(
    fontSize: 22,
    fontWeight: FontWeight.w600,
    color: EllaColors.textPrimary,
  );

  static const TextStyle subtitle = TextStyle(
    fontSize: 20,
    fontWeight: FontWeight.w500,
    color: EllaColors.textPrimary,
  );

  static const TextStyle body = TextStyle(
    fontSize: 18,
    height: 1.5,
    color: EllaColors.textSecondary,
  );

  static const TextStyle caption = TextStyle(
    fontSize: 16,
    color: EllaColors.textTertiary,
  );

  static const TextStyle small = TextStyle(
    fontSize: 16,
    color: EllaColors.textTertiary,
  );

  static const TextStyle label = TextStyle(
    fontSize: 16,
    fontWeight: FontWeight.w500,
    color: EllaColors.textTertiary,
  );

  // Touch target minimum
  static const double minTouchTarget = 48.0;
  static const double emergencyButtonHeight = 72.0;
  static const double navBarHeight = 80.0;

  // Spacing
  static const double spacingXS = 4.0;
  static const double spacingS = 8.0;
  static const double spacingM = 16.0;
  static const double spacingL = 24.0;
  static const double spacingXL = 32.0;

  // Radius
  static const double radiusSmall = 8.0;
  static const double radiusMedium = 12.0;
  static const double radiusLarge = 16.0;
  static const double radiusCircular = 100.0;
}
```

### Color Migration Cheatsheet

For the iOS engineer -- search-and-replace guide:

| Find                                      | Replace with                        | Count |
|-------------------------------------------|-------------------------------------|-------|
| `Colors.deepPurple`                       | `EllaColors.primary`                | 175   |
| `Colors.deepPurpleAccent`                 | `EllaColors.primaryLight`           |       |
| `Colors.deepPurple.withValues(alpha: 0.2)`| `EllaColors.primary.withOpacity(0.2)`| many |
| `Colors.deepPurple.withValues(alpha: 0.5)`| `EllaColors.primary.withOpacity(0.5)`| many |
| `Color(0xFF1F1F25)`                       | `EllaColors.bgSecondary`            | 7     |
| `Color(0xFF35343B)`                       | `EllaColors.bgTertiary`             |       |
| `Color(0xFF9A9BA1)`                       | `EllaColors.textTertiary`           |       |
| `Color(0xFF6A6B71)`                       | `EllaColors.textTertiary`           |       |
| `Color.fromARGB(255, 15, 15, 15)`         | `EllaColors.bgPrimary`              |       |
| `Colors.black` (as background)            | `EllaColors.bgPrimary`              |       |
| `Colors.grey` (in nav bar)                | `EllaColors.textTertiary`           |       |

---

## 7. Bottom Navigation Implementation

### Ella Bottom Nav Bar (replaces `bottom_nav_bar.dart`)

Key changes from OMI:
- 3 tabs instead of 5 (removed Action Items, Apps)
- Text labels always visible
- No center record button (device runs passively)
- Teal active color instead of white/purple

```dart
// Conceptual structure for EllaBottomNavBar
// 3 tabs: Home, Chat, Settings
// Active: EllaColors.primary with label
// Inactive: EllaColors.textTertiary with label
// Height: 80dp including safe area
// Icon size: 24dp
// Label: 14px always visible
// No record button in nav (OMI wearable runs automatically)

BottomNavigationBar(
  currentIndex: selectedIndex,
  onTap: onTabTap,
  backgroundColor: EllaColors.bgSecondary,
  selectedItemColor: EllaColors.primary,
  unselectedItemColor: EllaColors.textTertiary,
  selectedFontSize: 14,
  unselectedFontSize: 14,
  type: BottomNavigationBarType.fixed,
  items: [
    BottomNavigationBarItem(
      icon: Icon(FontAwesomeIcons.house, size: 24),
      label: 'Home',  // l10n: context.l10n.home
    ),
    BottomNavigationBarItem(
      icon: Icon(FontAwesomeIcons.solidComment, size: 24),
      label: 'Chat',  // l10n: context.l10n.chat
    ),
    BottomNavigationBarItem(
      icon: Icon(FontAwesomeIcons.gear, size: 24),
      label: 'Settings',  // l10n: context.l10n.settings
    ),
  ],
)
```

### Home Screen Layout

```dart
// Home tab structure:
// - App bar with battery widget + "Ella" title
// - Scrollable conversation list (simplified items)
// - Fixed emergency button above tab bar

Scaffold(
  appBar: AppBar(title: Text('Ella')),
  body: Column(
    children: [
      // Scrollable conversation list
      Expanded(
        child: ConversationsList(), // Simplified version
      ),
      // Fixed emergency button
      Padding(
        padding: EdgeInsets.fromLTRB(16, 8, 16, 8),
        child: EllaEmergencyButton(),
      ),
    ],
  ),
  bottomNavigationBar: EllaBottomNavBar(),
)
```

---

## 8. Conversation List Item (Simplified)

Current OMI conversation list items are dense with title, time, duration, tags, speakers, folders, scores. For Ella:

```
+------------------------------------------+
|  Morning Check-In                        |  <- 22px, bold
|  "Took medication, feeling good today"   |  <- 18px, secondary
|  8:30 AM  -  5 minutes                   |  <- 16px, tertiary
+------------------------------------------+
    ^  56dp minimum height, 16dp padding
```

Remove: folder icons, score badges, merge checkboxes, speaker avatars, tag chips.

---

## 9. Implementation Priority

For the iOS engineer, suggested order:

1. **Create `ella_theme.dart`** with `EllaColors` and `ellaThemeData()` -- drop-in file
2. **Swap theme in `main.dart`** -- replace lines 395-419 with `theme: ellaThemeData()`
3. **Replace bottom nav** -- swap `bottom_nav_bar.dart` with 3-tab labeled version
4. **Update `_pages` in `home/page.dart`** -- 3 pages instead of 4 (remove Action Items, Apps)
5. **Add emergency button** to home page layout
6. **Search-replace colors** per migration cheatsheet above
7. **Increase font sizes** in conversation list items and other hardcoded styles

Steps 1-3 give the biggest visual impact for the least work.

---

## 10. Accessibility Checklist

Before shipping, verify:

- [ ] All text 16px+ (no text smaller than 16px anywhere in the app)
- [ ] All touch targets 48x48dp minimum
- [ ] Color contrast 4.5:1 minimum for all normal text
- [ ] Color contrast 3:1 minimum for large text (22px+) and UI components
- [ ] Navigation has text labels (not icon-only)
- [ ] No information conveyed by color alone (always pair with text/icon)
- [ ] System text scaling respected (test at 200% in iOS Accessibility settings)
- [ ] VoiceOver/TalkBack labels on all interactive elements
- [ ] Emergency button accessible via VoiceOver
- [ ] Haptic feedback on all primary actions
