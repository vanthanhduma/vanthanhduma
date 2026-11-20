# v1.5.9.7 — Global UI Polish

## UI consistency across all workspaces
- Apply one readable type scale across Branding, Mask/Before-After, CTA Detector, Downloader, Batch CTA, Merge Video, and Export & Quality.
- Base application font: 10.5 pt Medium; use Segoe UI with installed Vietnamese-capable fallback.
- Increase and normalize section titles, button labels, tabs, input text, captions, table rows, and table headers.
- Make content-only controls transparent so checkboxes, radio buttons, sliders, generic frames, group-boxes, and stack/tab containers do not paint dark rectangles behind their text.
- Give the project/video identity area more width while retaining full path/name tooltips.

## Preserved behavior
- Keep every page's current layout, control order, and functionality.
- No changes to Branding render/drag/apply, Mask selection/inpaint, CTA detection/review/cut, Downloader, Batch CTA queue, Merge Video, Export/QC, FFmpeg settings, projects, Auto Save, backups, or updater commands.
- Keep the navy/teal/cyan Studio palette.

## Verification
- Add cross-workspace global theme regression tests.
- Windows Qt workflow runs theme tests, Export workspace tests, Branding preview regression, and Qt/Engine smoke.
- Release workflow runs Python compile/tests and validates FULL/UPDATE ZIP contents, payload hashes, preserve rules, and SHA-256.

## Update
- UPDATE package base: v1.5.9.6.
