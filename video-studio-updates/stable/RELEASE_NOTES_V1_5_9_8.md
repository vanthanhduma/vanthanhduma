# v1.5.9.8 — Download / Batch CTA Workspace Redesign

## Studio Branding
- Narrow the right control rail to 315–360 px; the preview receives more horizontal space.
- Keep every existing control: logo visibility, scale, opacity, rotation, anchor placement, label content/style, show time, scene scope, presets, and Apply.

## Download Video
- Rebuild the settings section as four consistent option cards: quality, output format, browser cookies, and destination folder.
- Put captions above controls and equalize field heights, card margins, and spacing.
- Keep all existing quality values, format values, cookie-browser values, folder selection, and download queue/action callbacks.

## Batch CTA Processing
- Use a two-column layout: the main queue and video-management actions on the left; project output folder, queue progress, Start, and Stop in a compact right rail.
- Add an empty-queue state and automatically switch to the queue table as soon as videos are added or a saved queue is restored.
- Preserve sequential processing, per-video project saving, manual CTA review, resume behavior, and safe stop behavior.

## Verification
- Add regression tests for Branding sizing, Download option-card layout and values, Batch two-column layout, empty-state switching, and all existing callbacks/Engine commands.
- Windows Qt workflow runs the new workspace tests with Branding preview, Export workspace, global UI theme, and Qt/Engine smoke tests.
- Release workflow validates FULL/UPDATE archives, payload contents, preserve rules, and SHA-256.

## Update
- UPDATE package base: v1.5.9.7.
