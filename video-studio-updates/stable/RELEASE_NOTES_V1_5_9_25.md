# v1.5.9.25 — Export & QC Workspace Refinement

## UI improvements
- Reorganize Export & QC into clear groups: video preview/playback, manual cut ranges, timeline, output destination, export actions, and quality control.
- Move the seek slider and timecode into the preview card.
- Separate range entry, add/remove actions, source-range playback, short preview, and the manual cut table into one cut-management card.
- Move the QC action and report into a dedicated quality-control group.
- Add independent vertical scrolling to the center and inspector columns so controls remain accessible on shorter windows and different Windows DPI/font settings.
- Improve spacing, card hierarchy, labels, and control sizing while preserving the existing Studio navy/teal theme.

## Compatibility and behavior
- Preserve all existing widget state names used by the rest of the application.
- Preserve Engine commands and callbacks for full export, cut-range export, range preview, output-folder selection, QC, and stopping an export.
- Preserve the existing manual-review behavior: selected ranges are excluded only when the user invokes cut-range export.
- No changes to FFmpeg export settings, output codec, resolution, frame rate, or quality pipeline.

## Regression coverage
- Extend static layout tests for grouped preview/playback, responsive scroll columns, and separate output/QC cards.
- Extend the Qt smoke test with 1650×900 geometry checks for card boundaries, range controls, and export buttons.
