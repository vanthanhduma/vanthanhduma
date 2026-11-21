# v1.5.9.15 — Python 3.15 / PySide6 Lazy Import Compatibility

## Nguyên nhân
Python 3.15 bổ sung PEP 810 lazy imports. Khi PySide6 cài import hook, lazy proxy trong thư viện có thể chưa được resolve đúng và gây lỗi trong các luồng concurrent/thread.

## Sửa lỗi tương thích
- Dùng `sys.set_lazy_imports("normal")`, là chế độ được CPython 3.15 hiện hành chấp nhận. Các import thông thường của ứng dụng vẫn được thực thi eager.
- Dùng `sys.set_lazy_imports_filter(...False)` để cả import được đánh dấu `lazy` cũng được resolve ngay.
- Đặt guard trước khi import Qt, OpenCV và thư viện bên thứ ba trong Qt shell, processing engine, engine worker và downloader.
- Bổ sung regression test xác nhận chỉ dùng mode hợp lệ và bộ lọc eager hoạt động; kiểm tra cả thứ tự trước các import quan trọng.

## Giữ nguyên
- Không thay đổi URL validation, yt-dlp options, chất lượng video, định dạng MP4, cookie cục bộ, hàng đợi hay thư mục tải.
- Không thay đổi engine xuất video, project schema, Auto Save, giao diện hoặc dữ liệu dự án.
- Không thay đổi quy tắc người dùng duyệt CTA và chọn khoảng cắt thủ công.

## Kiểm thử
- Kiểm tra guard trên cả bốn entrypoint.
- Kiểm tra bằng mô phỏng API CPython 3.15 trên runner cũ để không bỏ sót lỗi tham số.
- Trên Python 3.15, test bổ sung gọi API runtime thật và khôi phục trạng thái ban đầu sau kiểm tra.
