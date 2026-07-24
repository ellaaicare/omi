# Ella hardware visual pack

Tile artwork is exported at 64pt in SVG and PNG at 1×/2×/3×. Settings/connect glyphs are 24pt.

- `necklace-omi-*` preserves the shipping OMI renders in `app/assets/images`.
- `headset-whisper-*` is based on Ella's public system reference photo.
- OFF is grayscale at 45% opacity.
- Reconnecting reserves a lower-right breathing-dot slot.
- Low-battery keeps the ON artwork and exposes the amber caption anchor/color.
- The app should resolve artwork by `DeviceType`; do not hard-code generic necklace/headset art.

Run `node tool/build_ella_hardware_visual_pack.mjs` from `app/` to regenerate deterministic exports.
