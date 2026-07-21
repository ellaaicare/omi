# Phase A TestFlight 789 simulator screenshots

Captured on 2026-07-20 from an iPhone 17 Pro simulator running iOS 26.0.
The app was rebuilt from commit
`5ff5b23c4156bf0cac011cd18d48e45692f0a33a`, the source commit reported for
TestFlight `1.0.525 (789)`, using the `prod` scheme and `Debug-prod`
configuration.

These images are source-matched simulator evidence, not screenshots of the
downloaded TestFlight binary. Simulator account and device state also differ
from Greg's physical iPhone.

- `home.png`: Home still contains both the large **Talk to Ella** action and
  the bottom **Voice** tab.
- `settings.png`: Settings still exposes a **Guardian Mode** row. Its subtitle
  remained at **Loading...** in this simulator session.
- `guardian-mode.png`: The public route still opens the full advanced Guardian
  mode picker. It does not render the approved two-state **Whisper** UI or its
  locked ON/OFF copy.
- `memories.png`: The simulator account currently has no conversations, so this
  capture cannot verify Ella source indicators on populated memory rows.

The Guardian/Whisper mismatch is the main review finding from this capture.
