# Ella Elder Onboarding Flow

**Date**: 2026-02-06
**Author**: UX Designer (Claude Code)
**Status**: Ready for iOS engineer implementation
**Related**: Task #4, Task #12
**Revision**: 2 -- added BLE pairing screen and emergency contact screen per requirements

---

## Overview

The current OMI onboarding has 9 steps with small text, marketing questions ("How did you find us?"), speech profile setup, and multiple permission prompts. This is unusable for elder care.

The Ella onboarding has **3 screens**. A caregiver or family member will typically do the initial setup on the elder's phone, but the flow must be simple enough for a motivated elder to complete alone.

### Design Decision: Caregiver-First Setup

After research into Alzheimer's care workflows, the onboarding assumes a **caregiver is doing initial setup**. This means:
- Auth (Google/Apple sign-in) happens BEFORE the 3-screen flow, as a prerequisite gating step
- The elder's name is collected from the auth profile (or entered on Screen 1)
- The 3 screens focus on: understanding Ella, connecting the device, and adding a safety contact
- An elder using the device alone can still complete the flow -- every screen has large text and clear actions

### Current OMI Flow (9 steps)
1. Auth (Sign in with Google/Apple)
2. Name
3. Primary Language
4. "How did you find OMI?" (acquisition survey)
5. Permissions (location, notifications, background)
6. User Review ("Loving Omi?")
7. Welcome (connect device / continue without)
8. Find Devices (BLE scan)
9. Speech Profile

### Ella Flow (Auth + 3 screens)
0. **Auth** (pre-screen, standard Google/Apple sign-in, gating step)
1. **Welcome** (what Ella does, warm intro, enter name if not from auth)
2. **Connect Device** (BLE pairing with simple visual instructions)
3. **Emergency Contact** (name + phone number, with skip option)

---

## Pre-Screen: Auth (Sign In)

### Purpose
Gate access. Uses the same `AuthComponent` as OMI. This is not counted as one of the 3 onboarding screens because it's a standard sign-in sheet.

### Layout

```
+------------------------------------------+
|                                          |
|           (Ella logo or icon)            |
|              teal circle, 80dp           |
|                                          |
|          Welcome to Ella                 |  <- 32px, bold, white
|                                          |
|    Your personal care companion.         |  <- 20px, textSecondary
|                                          |
|                                          |
|  +------------------------------------+  |
|  |  [Apple logo] Continue with Apple  |  |  <- 56dp tall, white bg
|  +------------------------------------+  |
|                                          |
|  +------------------------------------+  |
|  |  [Google logo] Continue with Google|  |  <- 56dp tall, white bg
|  +------------------------------------+  |
|                                          |
|  By continuing, you agree to our         |  <- 16px, textTertiary
|  Terms of Service and Privacy Policy     |  <- 16px, teal links
|                                          |
+------------------------------------------+
```

### Spec

| Element             | Value                                      |
|---------------------|--------------------------------------------|
| Background          | `EllaColors.bgPrimary` (#121212) solid     |
| Logo                | Teal circle with Ella mark, 80dp           |
| Title               | "Welcome to Ella" -- 32px, bold, white     |
| Subtitle            | 20px, `textSecondary`, 1.5 line height     |
| Sign-in buttons     | 56dp tall, full width (minus 32dp padding) |
| Button style        | White background, black text, 18px bold    |
| Button radius       | 16dp                                       |
| Button spacing      | 16dp between buttons                       |
| Legal text          | 16px, `textTertiary`, centered             |
| Legal links         | `ellaPrimary` color, underlined            |

### Behavior

- On successful sign-in, auto-request Bluetooth + Notification permissions (non-blocking)
- Proceed to Screen 1 regardless of permission results
- If already signed in (returning user), skip directly to Home

---

## Screen 1: Welcome

### Purpose
Warm introduction to Ella. Explain what it does in plain, caring language. Collect the elder's name if not auto-filled from auth.

### Layout

```
+------------------------------------------+
|                                          |
|  Screen 1 of 3                           |  <- 16px, textTertiary
|                                          |
|              (Ella icon)                 |  <- teal circle, 64dp
|                                          |
|         Hi there!                        |  <- 28px, bold, white
|                                          |
|    Ella is your personal care            |  <- 20px, textSecondary
|    companion. She listens to your        |    line-height 1.6
|    conversations and helps keep          |
|    you safe and connected with           |
|    your family.                          |
|                                          |
|                                          |
|  What should Ella call you?              |  <- 20px, bold, white
|                                          |
|  +------------------------------------+  |
|  |     [Pre-filled or empty name]     |  |  <- 56dp, 20px, centered
|  +------------------------------------+  |
|                                          |
|                                          |
|  +------------------------------------+  |
|  |            Next                    |  |  <- 64dp, teal bg, 20px
|  +------------------------------------+  |
|                                          |
|  [  o  .  .  ] progress dots             |  <- 3 dots, centered
|                                          |
+------------------------------------------+
```

### Spec

| Element             | Value                                      |
|---------------------|--------------------------------------------|
| Background          | `EllaColors.bgPrimary` (#121212)           |
| Step indicator      | "Screen 1 of 3" -- 16px, `textTertiary`   |
| Ella icon           | 64dp teal circle with Ella mark            |
| Greeting            | "Hi there!" -- 28px, w700, white           |
| Description         | 20px, `textSecondary`, 1.6 line height     |
| Name prompt         | "What should Ella call you?" -- 20px, w600, white |
| Input field         | 56dp tall, `bgTertiary` bg, 16dp radius    |
| Input text          | 20px, centered, white                      |
| Input placeholder   | "Your first name" -- 20px, `textDisabled`  |
| Input focus border  | 2dp, `ellaPrimary`                         |
| Next button         | 64dp tall, full-width, `ellaPrimary` bg    |
| Button text         | "Next" -- 20px, w600, white                |
| Button disabled     | `bgTertiary` bg, `textDisabled` text       |
| Progress dots       | 3 dots, 10dp each, active=`ellaPrimary`, inactive=`textDisabled` |
| Horizontal padding  | 32dp on each side (generous margins)       |

### Behavior

- Pre-fill name from Google/Apple auth `displayName` (first name only)
- Keyboard opens automatically if name is empty
- "Next" button disabled (grayed out) until name field is non-empty
- On "Next": save name via `AuthService.instance.updateGivenName()`
- Auto-request notification + location permissions (non-blocking, don't wait)
- Transition: slide left to Screen 2 (300ms ease-in-out)

### Copy Rationale

- "Hi there!" is warmer than "Welcome to Ella" for a first-person greeting
- The description uses simple words: "listens," "safe," "connected," "family"
- No technical jargon -- no mention of "AI," "wearable," "Bluetooth," "data"
- "What should Ella call you?" implies a personal relationship, not a form field

---

## Screen 2: Connect Device

### Purpose
Pair the BLE wearable device. Show simple visual instructions. The device should auto-connect if powered on nearby, but provide clear guidance if it doesn't.

### Layout -- Scanning

```
+------------------------------------------+
|                                          |
|  [<- Back]           Screen 2 of 3       |  <- 48dp back, 16px step
|                                          |
|                                          |
|         Let's connect your               |  <- 28px, bold, white
|         Ella device                      |
|                                          |
|                                          |
|         (pulsing teal circle)            |  <- 80dp, breathing animation
|         [BLE icon inside]                |
|                                          |
|    Make sure your device is              |  <- 20px, textSecondary
|    turned on and nearby.                 |    centered
|                                          |
|    Looking for your device...            |  <- 18px, ellaPrimary
|                                          |
|                                          |
|                                          |
|                                          |
|                                          |
|  +------------------------------------+  |
|  |     I don't have a device yet      |  |  <- 56dp, bgTertiary bg
|  +------------------------------------+  |
|                                          |
|  [  .  o  .  ] progress dots             |
|                                          |
+------------------------------------------+
```

### Layout -- Device Found

```
+------------------------------------------+
|                                          |
|  [<- Back]           Screen 2 of 3       |
|                                          |
|                                          |
|         Device found!                    |  <- 28px, bold, white
|                                          |
|                                          |
|  +------------------------------------+  |
|  |                                    |  |
|  |  [checkmark]  Omi DevKit 2         |  |  <- success card
|  |               Connected            |  |  <- 18px, success green
|  |                                    |  |
|  +------------------------------------+  |
|                                          |
|    Your device is ready. Ella will       |  <- 20px, textSecondary
|    listen through it and keep you        |    centered
|    company throughout the day.           |
|                                          |
|                                          |
|                                          |
|  +------------------------------------+  |
|  |            Next                    |  |  <- 64dp, teal bg
|  +------------------------------------+  |
|                                          |
|  [  .  o  .  ] progress dots             |
|                                          |
+------------------------------------------+
```

### Spec

| Element             | Value                                      |
|---------------------|--------------------------------------------|
| Background          | `EllaColors.bgPrimary` (#121212)           |
| Back button         | 48x48dp, `bgTertiary` circle, white arrow  |
| Step indicator      | "Screen 2 of 3" -- 16px, `textTertiary`, right-aligned |
| Title (scanning)    | "Let's connect your Ella device" -- 28px, w700, white |
| Title (found)       | "Device found!" -- 28px, w700, white       |
| Pulsing circle      | 80dp, `ellaPrimary` at 30% opacity, breathes 1.5s cycle |
| BLE icon            | 28dp, `ellaPrimary`, centered in circle    |
| Instructions        | 20px, `textSecondary`, centered, 1.5 line height |
| Scanning status     | "Looking for your device..." -- 18px, `ellaPrimary` |
| Device card         | `bgSecondary` bg, 16dp radius, 4dp left border `success` |
| Device name         | 20px, white, w600                          |
| Connected label     | 18px, `success` (#10B981)                  |
| Checkmark           | 24dp, `success` color                      |
| Description (found) | 20px, `textSecondary`, centered            |
| Next button         | 64dp, `ellaPrimary`, "Next", 20px w600     |
| Skip button         | 56dp, `bgTertiary` bg, "I don't have a device yet", 18px, `textSecondary` |
| Progress dots       | 3 dots, dot 2 active                       |

### Behavior

- BLE scan starts automatically when screen loads (reuse `OnboardingProvider.scanDevices()`)
- Pulsing animation runs during scan (scale 0.95-1.05, 1.5s, ease-in-out, repeat)
- When device found:
  - Stop pulsing animation
  - Swap to "Device found!" layout with slide-up animation (200ms)
  - Haptic feedback (medium impact)
  - Show device name from BLE advertisement
  - Show "Next" button (replaces skip button position)
- "I don't have a device yet" skips to Screen 3 (phone-only mode is fine for Ella chat)
- If no device found after 15 seconds, add helper text: "Having trouble? Make sure the device is charged and within arm's reach."
- Back button returns to Screen 1
- On "Next" or skip: slide left to Screen 3

### What's Different from OMI

- No device list / selection (auto-connect to nearest OMI device)
- No "Contact Support" button (confusing for elders)
- Simple visual feedback (pulsing circle) instead of complex scan list
- Always shows skip option (phone-only mode is valid for Ella)

---

## Screen 3: Emergency Contact

### Purpose
Add one emergency contact (a family member or caregiver) who can be reached when the elder taps the red emergency button on the Home screen. This is the final setup step.

### Layout

```
+------------------------------------------+
|                                          |
|  [<- Back]           Screen 3 of 3       |  <- 48dp back, 16px step
|                                          |
|                                          |
|         Add someone Ella                 |  <- 28px, bold, white
|         can call for help                |
|                                          |
|    If you ever need help, Ella           |  <- 18px, textSecondary
|    will contact this person.             |
|                                          |
|                                          |
|  Their name                              |  <- 16px, textTertiary, label
|  +------------------------------------+  |
|  |     [Contact name field]           |  |  <- 56dp, 20px
|  +------------------------------------+  |
|                                          |
|  Their phone number                      |  <- 16px, textTertiary, label
|  +------------------------------------+  |
|  |     [Phone number field]           |  |  <- 56dp, 20px
|  +------------------------------------+  |
|                                          |
|                                          |
|  +------------------------------------+  |
|  |          Get Started               |  |  <- 64dp, teal bg, 20px
|  +------------------------------------+  |
|                                          |
|  +------------------------------------+  |
|  |     Skip for now                   |  |  <- 48dp, text-only button
|  +------------------------------------+  |
|                                          |
|  [  .  .  o  ] progress dots             |
|                                          |
+------------------------------------------+
```

### Spec

| Element             | Value                                      |
|---------------------|--------------------------------------------|
| Background          | `EllaColors.bgPrimary` (#121212)           |
| Back button         | 48x48dp, `bgTertiary` circle, white arrow  |
| Step indicator      | "Screen 3 of 3" -- 16px, `textTertiary`, right-aligned |
| Title               | "Add someone Ella can call for help" -- 28px, w700, white |
| Subtitle            | 18px, `textSecondary`, 1.5 line height     |
| Field labels        | "Their name" / "Their phone number" -- 16px, `textTertiary` |
| Name input          | 56dp tall, `bgTertiary`, 16dp radius       |
| Name text           | 20px, white                                |
| Name placeholder    | "e.g., Sarah" -- 20px, `textDisabled`      |
| Phone input         | 56dp tall, `bgTertiary`, 16dp radius       |
| Phone text          | 20px, white                                |
| Phone placeholder   | "e.g., (555) 123-4567" -- 20px, `textDisabled` |
| Phone keyboard      | `TextInputType.phone` (numeric keypad)     |
| Get Started button  | 64dp, `ellaPrimary`, "Get Started", 20px w600 |
| Get Started disabled| `bgTertiary` bg, `textDisabled` text (when both fields empty) |
| Skip button         | 48dp, transparent bg, "Skip for now", 18px, `textTertiary`, underlined |
| Field spacing       | 16dp between label and field, 24dp between field groups |
| Progress dots       | 3 dots, dot 3 active                       |

### Behavior

- First field (name) auto-focuses with keyboard open
- "Get Started" is enabled when BOTH name and phone are filled
- "Skip for now" is always available -- tapping it shows a brief confirmation:
  - Snackbar: "You can add an emergency contact later in Settings."
  - Then completes onboarding
- Phone number field: `TextInputType.phone`, auto-format with dashes as user types
- On "Get Started":
  1. Validate phone number (basic check: at least 7 digits)
  2. Save emergency contact to backend (`POST /v1/users/emergency-contact`)
  3. Save locally in `SharedPreferencesUtil` for offline access
  4. Mark onboarding complete
  5. Navigate to Home (replace route stack)
- On "Skip for now":
  1. Mark onboarding complete (no contact saved)
  2. Navigate to Home (replace route stack)
- Back button returns to Screen 2
- Haptic: medium impact on "Get Started" tap

### Copy Rationale

- "Add someone Ella can call for help" -- warm, personal, not clinical
- "Their name" / "Their phone number" -- simple field labels, not "Emergency Contact Name"
- "Skip for now" implies they can do it later, reducing pressure
- Placeholder examples ("Sarah", "(555) 123-4567") help elders understand what to type

### Emergency Contact Data Model

```dart
class EmergencyContact {
  final String name;
  final String phoneNumber;

  EmergencyContact({required this.name, required this.phoneNumber});

  Map<String, dynamic> toJson() => {
    'name': name,
    'phone_number': phoneNumber,
  };
}
```

Stored in:
- Backend: `POST /v1/users/emergency-contact` (body: `{name, phone_number}`)
- Local: `SharedPreferencesUtil.emergencyContactName` and `SharedPreferencesUtil.emergencyContactPhone`
- Used by: Emergency button on Home screen (calls this number)

---

## Caregiver-Assisted Setup

### Primary Scenario (Caregiver Sets Up)

The flow is designed for a caregiver or family member to set up the device on the elder's phone:

1. **Auth**: Caregiver signs in with the elder's Apple/Google account (or creates one)
2. **Screen 1**: Caregiver enters the elder's first name
3. **Screen 2**: Caregiver pairs the wearable device
4. **Screen 3**: Caregiver adds THEIR OWN contact info as the emergency contact

This is the expected flow. The copy on Screen 3 ("Add someone Ella can call for help") works naturally from both perspectives:
- Caregiver reads it as: "I should add myself as the helper"
- Elder reads it as: "I should add my daughter/son"

### Elder Self-Setup

If the elder sets up alone, the flow still works:
- Auth with their own account
- Enter their own name
- Skip device connect if they don't have one yet
- Add a family member as emergency contact (or skip)

### Future Enhancement (Phase 2)

A "Set up for someone else" option on the Auth screen that:
- Creates a managed account
- Links to caregiver's account
- Sets up monitoring permissions
- Auto-populates caregiver as emergency contact

This is NOT in MVP scope.

---

## Implementation Notes

### Files to Create/Modify

| Action   | File                                           | Notes                          |
|----------|------------------------------------------------|--------------------------------|
| Create   | `pages/onboarding/ella/ella_onboarding.dart`   | 3-screen wrapper with PageView |
| Create   | `pages/onboarding/ella/ella_welcome.dart`      | Screen 1: Welcome + name       |
| Create   | `pages/onboarding/ella/ella_connect.dart`      | Screen 2: BLE pairing          |
| Create   | `pages/onboarding/ella/ella_emergency.dart`    | Screen 3: Emergency contact    |
| Modify   | `pages/onboarding/wrapper.dart`                | Conditional: use Ella wrapper if Ella build |

### Reusable from OMI

- `AuthComponent` -- Google/Apple sign-in logic (used as pre-screen)
- `OnboardingProvider` -- BLE scan + device connection
- `SharedPreferencesUtil` -- name storage, onboarding state
- `AuthService.instance.updateGivenName()` -- save name to backend

### New Backend Endpoints Needed

```
POST /v1/users/emergency-contact
Body: { "name": "Sarah", "phone_number": "+15551234567" }
Response: 200 OK

GET /v1/users/emergency-contact
Response: { "name": "Sarah", "phone_number": "+15551234567" }
```

### l10n Keys Needed

```
ellaOnboardingStep: "Screen {current} of {total}"
ellaWelcomeGreeting: "Hi there!"
ellaWelcomeDescription: "Ella is your personal care companion. She listens to your conversations and helps keep you safe and connected with your family."
ellaWelcomeNamePrompt: "What should Ella call you?"
ellaWelcomeNamePlaceholder: "Your first name"
ellaConnectTitle: "Let's connect your Ella device"
ellaConnectInstructions: "Make sure your device is turned on and nearby."
ellaConnectScanning: "Looking for your device..."
ellaConnectFound: "Device found!"
ellaConnectFoundDescription: "Your device is ready. Ella will listen through it and keep you company throughout the day."
ellaConnectConnected: "Connected"
ellaConnectSkip: "I don't have a device yet"
ellaConnectTrouble: "Having trouble? Make sure the device is charged and within arm's reach."
ellaEmergencyTitle: "Add someone Ella can call for help"
ellaEmergencySubtitle: "If you ever need help, Ella will contact this person."
ellaEmergencyNameLabel: "Their name"
ellaEmergencyNamePlaceholder: "e.g., Sarah"
ellaEmergencyPhoneLabel: "Their phone number"
ellaEmergencyPhonePlaceholder: "e.g., (555) 123-4567"
ellaGetStarted: "Get Started"
ellaSkipForNow: "Skip for now"
ellaSkipConfirmation: "You can add an emergency contact later in Settings."
ellaNext: "Next"
```

### Wrapper Widget Structure

```dart
/// Ella onboarding: Auth pre-screen + 3 PageView screens.
/// Uses NeverScrollableScrollPhysics (button navigation only, no swiping).
class EllaOnboarding extends StatefulWidget {
  const EllaOnboarding({super.key});

  @override
  State<EllaOnboarding> createState() => _EllaOnboardingState();
}

class _EllaOnboardingState extends State<EllaOnboarding> {
  final PageController _pageController = PageController();
  int _currentPage = 0;
  bool _isAuthenticated = false;

  void _goToPage(int page) {
    _pageController.animateToPage(
      page,
      duration: const Duration(milliseconds: 300),
      curve: Curves.easeInOut,
    );
    setState(() => _currentPage = page);
  }

  void _goNext() => _goToPage(_currentPage + 1);
  void _goBack() => _goToPage(_currentPage - 1);

  void _completeOnboarding() {
    SharedPreferencesUtil().onboardingCompleted = true;
    routeToPage(context, const HomePageWrapper(), replace: true);
  }

  @override
  Widget build(BuildContext context) {
    if (!_isAuthenticated) {
      return AuthComponent(onSignIn: () {
        setState(() => _isAuthenticated = true);
      });
    }

    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      body: SafeArea(
        child: PageView(
          controller: _pageController,
          physics: const NeverScrollableScrollPhysics(),
          onPageChanged: (index) => setState(() => _currentPage = index),
          children: [
            EllaWelcomeScreen(onNext: _goNext),
            EllaConnectScreen(onNext: _goNext, onBack: _goBack),
            EllaEmergencyScreen(
              onComplete: _completeOnboarding,
              onSkip: _completeOnboarding,
              onBack: _goBack,
            ),
          ],
        ),
      ),
    );
  }
}
```

### Progress Dots Widget

```dart
/// Simple 3-dot progress indicator for Ella onboarding.
class EllaProgressDots extends StatelessWidget {
  final int currentPage;  // 0-indexed
  final int totalPages;   // 3

  const EllaProgressDots({
    super.key,
    required this.currentPage,
    this.totalPages = 3,
  });

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: List.generate(totalPages, (index) {
        final isActive = index == currentPage;
        return Container(
          margin: const EdgeInsets.symmetric(horizontal: 6),
          width: isActive ? 12 : 10,
          height: isActive ? 12 : 10,
          decoration: BoxDecoration(
            color: isActive ? EllaColors.primary : EllaColors.textDisabled,
            shape: BoxShape.circle,
          ),
        );
      }),
    );
  }
}
```

### Transition Between Screens

- Use `PageView` with `NeverScrollableScrollPhysics` (buttons only, no swiping)
- Slide-left animation via `PageController.animateToPage()`, 300ms, ease-in-out
- Progress dots update immediately on page change
- Back button available on Screens 2 and 3

---

## Accessibility Notes

- All body text 18px+ (descriptions at 20px for extra readability)
- All input fields 56dp tall with 20px text
- All primary buttons 64dp tall (larger than standard 56dp for elder comfort)
- Skip/secondary buttons 48dp+ touch target
- Back button 48x48dp
- High contrast: white/teal text on #121212 background
- VoiceOver labels on all buttons, input fields, and progress dots
- Input fields have accessible labels matching visible labels
- Phone field triggers numeric keypad (easier for elders than full keyboard)
- No background images (faster load, no visual distraction)
- No complex animations (only simple pulsing circle and slide transitions)
- System text scaling respected (test at 200% in iOS Accessibility settings)
- Haptic feedback on primary button taps

---

## Screen Flow Summary

```
[Auth: Sign In]
       |
       v
[Screen 1: Welcome]  --  "Hi there! ... What should Ella call you?"
       |                   - Ella icon, warm description
       | Next              - Name field (pre-filled from auth)
       v
[Screen 2: Connect]  --  "Let's connect your Ella device"
       |                   - Pulsing BLE scan animation
       | Next / Skip       - Auto-connects, shows success card
       v
[Screen 3: Emergency] -- "Add someone Ella can call for help"
       |                   - Name + phone fields
       | Get Started       - Skip option with snackbar confirmation
       | / Skip
       v
[Home Screen]         --  Ella is ready.
```
