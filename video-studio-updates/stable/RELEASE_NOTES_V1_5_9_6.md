# v1.5.9.6 — Export Typography Polish

## Typography and readability
- Standardize the Qt UI font at 10.1 pt Medium using Segoe UI, with suitable installed-font fallback.
- Make generic QLabel backgrounds transparent so section titles, field captions, and status labels inherit their parent card surface instead of showing black strips.
- Normalize font sizes and weights for section headings, export metadata, timecodes, cut-range table cells/headers, and action buttons.
- Escape the literal ampersand in “Cắt bỏ & xuất phần còn lại” so Qt does not treat it as a mnemonic.

## Preserved behavior
- No change to the Export workspace layout or its preview, seek, manual cut list, and cut timeline interactions.
- No change to Engine commands, output folder behavior, manual cut validation, source preview, QC, or FFmpeg export pipeline.
- No change to Branding, CTA Detector, Batch CTA, project persistence, Auto Save, backup, or updater behavior.

## Verification
- Add regression checks for transparent QLabel styling, the shared UI font, dedicated manual-cut table typography, and literal ampersand rendering.
- Windows CI runs Export workspace tests and the existing Qt/Engine smoke test.
- Release workflow validates FULL/UPDATE ZIP integrity and SHA-256.

## Update
- UPDATE package base: v1.5.9.5.
