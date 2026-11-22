# v1.5.9.26 — Studio Branding & Source Player Refinement

## Studio Branding
- Refine the Branding workspace into a cleaner, more balanced Studio layout.
- Expand the Logo, Nhãn, Thời gian, and Preset tabs evenly across the inspector.
- Reduce inspector width and tighten card spacing to give the video preview more room.
- Improve slider progress/handles, input fields, checkboxes, card borders, and active-tab hierarchy.
- Preserve direct logo dragging and all existing logo, label, timing, scene-scope, preset, preview, and apply behavior.

## Source video playback
- Open the original video through the operating system's default media-player association instead of launching ffplay.
- Clearly label the action as opening the full source video in the default player.
- Keep playback, seeking, fullscreen exit, and close controls with the installed player.
- Preserve the in-app Engine preview and export pipeline.

## Regression coverage
- Update Branding layout checks for the responsive inspector and evenly stretched tabs.
- Add checks that source video uses the default OS player and does not depend on ffplay or a cut-range selection.
