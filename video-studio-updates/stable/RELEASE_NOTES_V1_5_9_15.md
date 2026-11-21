# v1.5.9.15 — Python 3.15 / PySide6 Lazy Import Compatibility

## Nguyên nhân
Python 3.15 bổ sung PEP 810 lazy imports. Khi PySide6 cài import hook, một số lazy proxy trong thư viện chuẩn có thể chưa được resolve đúng và gây lỗi `'lazy_import' object is not callable` trong các luồng concurrent/thread.

## Sửa lỗi
- Tắt lazy-import mode ngay sau khi import `sys`, trước bất kỳ import third-party nào: `sys.set_lazy_imports("none")`.
- Áp dụng guard có kiểm tra API để Python 3.11–3.14 chạy bình thường.
- Đặt guard trong Qt shell, processing engine, engine worker và downloader.

## Giữ nguyên
- Không thay đổi URL validation, yt-dlp options, chất lượng video, định dạng MP4, cookie cục bộ, hàng đợi hay thư mục tải.
- Không thay đổi engine xuất video, project schema, Auto Save hoặc dữ liệu dự án.
