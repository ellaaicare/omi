# ios-1 Agent State

## What I Changed

### Task 1: Fix Navigation Black Screen Bug

**File: `app/lib/pages/home/page.dart`**
- Added imports for `uuid`, `ServerMessage`, and `message.dart` (lines 11-13)
- Replaced the `Navigator.push` block in the `case "chat":` deep link handler (lines 274-302). Previously, a new `ChatPage(isPivotBottom: false)` was pushed via `Navigator.push`, creating a duplicate on top of the IndexedStack tab. Now, the IndexedStack at index 1 handles display (already set on line 206), and if an `autoMessage` exists (e.g., daily reflection notification), it's injected directly via `MessageProvider.addMessage()` with an 800ms delay to let messages load first.

**File: `app/lib/pages/chat/page.dart`**
- Changed the `leading` property in `_buildAppBar` (lines 926-944). When `widget.isPivotBottom` is true (tab mode in IndexedStack), the back button is replaced with `SizedBox(width: 48)` to maintain layout spacing. When false (pushed as a standalone route), the original back button with `Navigator.pop()` is preserved. This prevents the black screen caused by popping the entire app when backing out of a tab.

### Task 2: Hide DailyScoreWidget and OutOfCreditsWidget for Ella

**File: `app/lib/pages/conversations/widgets/daily_score_widget.dart`**
- Added `const bool isEllaApp = true;` (top-level, with TODO comment to replace with flavor check)
- Added early return `if (isEllaApp) return const SizedBox.shrink();` at the start of `build()` method

**File: `app/lib/pages/home/widgets/out_of_credits_widget.dart`**
- Same pattern: added `const bool isEllaApp = true;` and early return `SizedBox.shrink()` when true

### Task 7: Update Onboarding Screens with Images and Light Theme Styling

**File: `app/lib/pages/onboarding/ella/ella_welcome.dart`**
- Replaced the placeholder icon circle (heart icon in teal circle) with `ella_onboarding_1.png` (grandmother+grandchild illustration), 240px height with ClipRRect rounded corners
- Restructured layout: image is above the Expanded content area (title, description, name field, button). Step indicator and image are in the fixed top area; text/form content is in the Expanded bottom area with Spacer to push the form to the middle-bottom
- Updated text field styling from dark fill (`bgTertiary`, no border) to light theme (white fill, `bgTertiary` border, teal focused border with 1.5px width)

**File: `app/lib/pages/onboarding/ella/ella_connect.dart`**
- Added `ella_onboarding_2.png` (elderly woman holding wearable device) between the step indicator row and the title text, 220px height with ClipRRect
- Replaced `Spacer(flex: 2)` before the title with `SizedBox(height: 16)` + image + `SizedBox(height: 24)` to accommodate the image
- Reduced bottom `Spacer(flex: 3)` to `Spacer()` since the image takes up vertical space

**File: `app/lib/pages/onboarding/ella/ella_emergency.dart`**
- Added `ella_onboarding_3.png` (teal protective shield) between the step indicator row and the title text, 200px height with Center + ClipRRect
- Reduced top spacing from `SizedBox(height: 48)` to `SizedBox(height: 16)` + image + `SizedBox(height: 24)` to fit image
- Reduced spacing before form fields from 40px to 32px
- Updated both text fields (name and phone) from dark fill (`bgTertiary`, borderless) to light theme (white fill, `bgTertiary` enabled border, teal focused border 1.5px)

## What I Learned

1. **isEllaApp pattern**: There is no single shared `isEllaApp` constant. It's defined locally in multiple places (`mobile_app.dart` has `static const bool _isEllaApp = true;`, `wrapper.dart` has a local const). All have `// TODO: replace with flavor check` comments. When a proper flavor system is added, all these need to be consolidated.

2. **IndexedStack navigation**: The home page uses an `IndexedStack` with 3 children: `[ConversationsPage, ChatPage(isPivotBottom: true), EllaSettingsPage]`. The `HomeProvider.selectedIndex` controls which is visible. Deep links set `homePageIdx` which feeds into this.

3. **autoMessage flow**: When a notification (e.g., daily reflection) triggers a deep link to chat, it passes `autoMessage` through `HomePageWrapper -> HomePage`. The ChatPage's `initState` handles auto-messages by creating a `ServerMessage` with `MessageSender.ai` and adding it via `MessageProvider.addMessage()`. The 800ms delay is important to let `refreshMessages()` finish first.

4. **ChatPage dual usage**: `ChatPage` is used in two modes: `isPivotBottom: true` (as a tab in IndexedStack) and `isPivotBottom: false` (pushed as a route, e.g., from goal tracker advice). The `isPivotBottom` flag should control navigation behavior accordingly.

5. **Onboarding image sizing**: Images are sized at 200-240px height with BoxFit.contain. The images have warm, light backgrounds that blend naturally with the new EllaColors.bgPrimary (#FAFAF8). ClipRRect with 24px radius softens the edges.

6. **Light theme input styling pattern**: The theme's `inputDecorationTheme` defines white fill + bgTertiary border + teal focused border. However, the onboarding screens use explicit InputDecoration (not the theme default), so I updated each TextField individually to match: `fillColor: Colors.white`, `enabledBorder` with `bgTertiary`, `focusedBorder` with `primary` teal at 1.5px width.

## What's Still Pending

1. **Consolidate `isEllaApp`**: The constant is duplicated in 4+ places. Should be a single source of truth, ideally driven by build flavors. Low priority but creates maintenance burden.

2. **autoMessage in tab ChatPage**: The current fix injects autoMessage via MessageProvider from `home/page.dart`. The existing `ChatPage.initState` also has autoMessage handling (lines 109-140) but only fires for pushed routes since the tab ChatPage is created without autoMessage. This works correctly but is worth noting -- the tab ChatPage won't re-run initState for new deep links since it's kept alive by IndexedStack.

3. **Other Navigator.push of ChatPage**: `goal_tracker_widget.dart` (line 215-218) still pushes ChatPage with `isPivotBottom: false` and an autoMessage. This is correct behavior (it's a standalone push, not a tab duplicate), so no change needed there.

4. **Splash screen image**: `ella_splash.png` was generated but not integrated. The splash screen is configured in `pubspec.yaml` under `flutter_native_splash` and currently uses `assets/images/splash.png`. Changing this requires updating the pubspec and running `flutter pub run flutter_native_splash:create`. Not part of this task.

## Key Decisions Made

1. **autoMessage injection approach**: Rather than trying to pass autoMessage to the existing tab ChatPage (which would require a mechanism to update the already-created widget), I chose to inject the message directly into MessageProvider from `home/page.dart`. This mirrors exactly what ChatPage.initState does but avoids the need to recreate or update the tab widget.

2. **SizedBox(width: 48) for hidden back button**: Used a fixed-width SizedBox to maintain AppBar layout symmetry. The width 48 matches the standard leading widget area. An alternative would have been `automaticallyImplyLeading: false` but that wouldn't guarantee consistent spacing.

3. **Local `isEllaApp` constants**: Followed the existing codebase pattern of defining `isEllaApp` locally rather than creating a new shared constants file. This keeps the change minimal and consistent with existing code.

4. **Image heights**: Used 240px for welcome (largest, most prominent), 220px for connect, 200px for emergency (smallest, needs room for two form fields). All use BoxFit.contain to avoid cropping.

5. **Column layout (not SingleChildScrollView)**: Kept Column + Spacer layout for all three screens rather than switching to SingleChildScrollView. The Spacer collapses when keyboard opens, which handles the space. Emergency page has the tightest layout (image + 2 fields + 2 buttons) but should fit on standard iPhone screens (667pt+). On very small screens the Spacer will shrink to zero, which is acceptable.

6. **Explicit InputDecoration over theme**: The onboarding screens already had explicit InputDecoration styling. Rather than removing it to rely on the theme's `inputDecorationTheme`, I updated the explicit styles to match the light theme. This is more predictable and avoids breakage if the theme changes.

## How to Verify

1. **Black screen fix**:
   - Launch app, navigate to Chat tab -- no back button should appear
   - Send a deep link to `/chat/someAppId` -- chat tab should activate, no duplicate page pushed
   - From goal tracker, tap advice to open ChatPage -- back button should appear and work correctly

2. **Hidden widgets**:
   - Navigate to conversations/home page -- DailyScoreWidget should not be visible
   - OutOfCreditsWidget should not appear even if usage provider reports out of credits

3. **Onboarding screens with images**:
   - Welcome screen: grandmother+grandchild image at top, greeting text, name field with white fill and teal focus border, Next button
   - Connect screen: elderly woman with wearable image, Bluetooth scanning indicator below, device found card
   - Emergency screen: teal shield image at top, form fields for name/phone with white fill and teal focus border, Submit and Skip buttons
   - All screens should have warm off-white background (EllaColors.bgPrimary)
   - Text should be dark (EllaColors.textPrimary/textSecondary)

4. **Formatting check**:
   ```bash
   dart format --line-length 120 app/lib/pages/home/page.dart app/lib/pages/chat/page.dart app/lib/pages/conversations/widgets/daily_score_widget.dart app/lib/pages/home/widgets/out_of_credits_widget.dart app/lib/pages/onboarding/ella/ella_welcome.dart app/lib/pages/onboarding/ella/ella_connect.dart app/lib/pages/onboarding/ella/ella_emergency.dart
   ```
   Should report "0 changed".
