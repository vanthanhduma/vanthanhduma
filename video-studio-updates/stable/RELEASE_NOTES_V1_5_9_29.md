# v1.5.9.29 — CTA Detection Coverage Upgrade

## Better short-window detection
- Increase the default scan cadence to 2 frames per second (one sampled frame every 0.5 seconds) in both the single-video CTA Detector and Batch CTA Processing.
- Enable balanced OCR preprocessing with adaptive-threshold fallback to improve difficult on-screen text recognition.
- Run graphic Subscribe-button recognition on every sampled frame independently of OCR, so OCR misses or timeouts do not suppress visual detections.
- Recognize compact red Subscribe buttons that contain either a play glyph or multiple high-contrast text glyphs.
- Preserve detection evidence and method labels as text, graphic, or combined results for easier manual review.

## Batch CTA consistency
- Use the same 0.5-second sampling cadence and balanced recognition mode in sequential Batch CTA scans.
- Keep independent project/cache output per source video and preserve sequential queue processing.
- Continue requiring manual review before any CTA range is selected for cutting; no automatic approval or cutting is introduced.

## Regression coverage
- Add a synthetic 4-to-8-second graphic-only Subscribe video test where OCR intentionally returns no text.
- Verify balanced mode requests the enhanced OCR fallback.
- Extend the Windows Batch CTA end-to-end test to verify the 0.5-second cache sampling cadence and run the detector regression suite.
