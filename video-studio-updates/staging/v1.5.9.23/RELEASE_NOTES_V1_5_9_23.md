# v1.5.9.23 — Responsive Download Workspace

## Fixes
- Replace the fixed 128 px minimum for the Download Settings card with Qt's measured content size, so all four setting cards fit at common Windows font and DPI settings.
- Prevent the "Danh sách tải" queue heading from occupying the settings card's remaining space.
- Give the sidebar runtime status card enough measured height to show Engine, Auto Save and background task status without clipping.
- Keep the 1650×900 desktop layout compact; the queue table remains the flexible area.

## Tests
- Add a Windows Qt geometry check at 1650×900 verifying that the settings card ends before the queue heading.
- Verify the Engine, Auto Save and background-task labels remain inside the sidebar status card.
- Keep existing download, CTA, Branding, batch, export and updater tests unchanged.
