# v1.5.9.10 — Branding Inspector Tabs

## Redesigned Studio Branding workspace
- Replace the long, continuously scrolling control column with four focused tabs: Logo, Nhãn, Thời gian, and Preset.
- Logo tab: file selection, visibility, scale, opacity, rotation, and 3×3 position controls with pointer-drag hint.
- Nhãn tab: visibility, label text, size, opacity, position, text color, and background color.
- Thời gian tab: start/end time and scene-specific scope.
- Preset tab: select, save, load, and delete Branding presets.
- Keep the preview as the primary workspace, use a stable-width inspector, allow each tab content to scroll independently, and keep Apply/status fixed below the tab panel.

## Preserved behavior
- Keep all existing widget attributes and state keys used by load_state, preset loading, preview generation, and Apply.
- Preserve direct logo dragging, debounced/coalesced preview requests, opacity/scale/rotation, anchors, scene scope, colors, and preset callbacks.
- No changes to Engine commands, render pipeline, export, downloader, Batch CTA, project persistence, Auto Save, backups, or updater logic.

## Verification
- Static regression tests assert four tab labels, inspector sizing, and preservation of all Branding controls/callbacks.
- Windows Qt smoke test creates BrandingDialog, verifies all four tabs, switches through every tab, checks controls and fixed Apply action, and retains preview coalescing test.
- Windows CI also runs Branding preview performance, Export layout, global theme, Download/Batch/Branding regression, and Qt/Engine smoke.
- Release workflow compiles and validates FULL/UPDATE packages, payload contents, preserve rules, and SHA-256.

## Update
- UPDATE package base: v1.5.9.9.
