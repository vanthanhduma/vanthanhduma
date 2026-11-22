# v1.5.9.27 — Exact Cut Range Preview

## Xem thử khoảng cắt
- Reuse the existing Bắt đầu and Kết thúc inputs; no duplicate time controls.
- Render only the selected interval to a temporary H.264/AAC MP4 clip.
- Open the temporary clip with the operating system's default media player.
- Run FFmpeg on a cancellable background QThread so the Qt interface remains responsive.
- Preserve the source video and project data. Temporary preview clips older than seven days are pruned when the next preview is created.
- Stop an in-flight FFmpeg process safely when the application is closing.

## Studio Branding
- Keep the Studio Branding refinements delivered in v1.5.9.26: compact inspector, evenly expanded Logo/Nhãn/Thời gian/Preset tabs, and improved fields, sliders, cards, and spacing.
- Preserve all existing branding controls and project behavior.

## Regression coverage
- Verify the selected time range is passed through the preview worker and encoded to the expected duration.
- Verify default-player integration, background execution, cancellation, and stale temporary-file cleanup.
