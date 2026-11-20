# v1.5.9.9 — Branding / Download Workspace Polish

## Studio Branding
- Narrow the right inspector rail to 290–325 px so the central preview receives more space.
- Reduce logo anchor button dimensions and tighten spacing/margins across Logo, Label, Timing, and Preset groups.
- Retain all existing controls, values, preview debounce, drag behavior, and Apply action.

## Download Video
- Arrange the four setting cards horizontally: title and helper text on the left; selector or destination path on the right.
- Enforce minimum heights for each option card and the settings container so Qt cannot compress text and inputs into clipped rows.
- Allow the download queue table to shrink before the settings panel when vertical room is limited.
- Preserve all quality, format, browser-cookie, folder, download queue, progress, and action bindings.

## Verification
- Regression tests assert Branding sizing, compact groups, anchor buttons, Download card layout/minimum heights, and preserved callbacks.
- Windows Qt CI runs Branding preview, Export workspace, global theme, Download/Batch/Branding layout tests, and UI/Engine smoke.
- Release workflow validates Python compilation, FULL/UPDATE packages, payloads, preserve rules, and SHA-256.

## Update
- UPDATE package base: v1.5.9.8.
