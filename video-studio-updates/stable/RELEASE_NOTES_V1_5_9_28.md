# v1.5.9.28 — Unified Studio UI & Direct Source Player

## Direct source playback
- Remove the temporary FFmpeg segment-render preview; it added waiting time before opening a clip.
- Restore direct opening of the complete original source video through the operating system's default media-player association.
- Keep seeking, fullscreen, and playback controls in the user's installed media player.

## Unified interface refinement
- Apply a shared navy/teal/cyan visual system across the Header, Project Bar, Sidebar, workspaces, inspectors, cards, and status areas.
- Harmonize buttons, navigation states, tabs, inputs, checkboxes, tables, queues, sliders, progress bars, scrollbars, menus, tooltips, and dialogs.
- Improve surface hierarchy, contrast, focus/hover/selected/disabled states, borders, spacing, and corner radii without changing workspace behavior.
- Preserve all existing features, editing/export pipelines, project data, and user settings.
- Retain the Studio Branding workspace refinements from v1.5.9.26.

## Regression coverage
- Verify source playback opens the original file directly without FFmpeg range rendering.
- Verify shared visual rules cover all application workspaces and common Qt controls.
