# v1.5.9.5 — Export & Quality Workspace

## Giao diện Xuất & kiểm tra được tổ chức lại
- Tạo workspace riêng cho Xuất video và Kiểm tra chất lượng.
- Preview video và thanh vị trí phát nằm ở vùng trung tâm.
- Chuyển điều khiển cắt thủ công Từ–Đến, danh sách khoảng cắt, xem video gốc và xem thử 12 giây ra khỏi panel cuộn bên phải.
- Bổ sung timeline trực quan: cyan biểu thị phần giữ lại; vùng gạch chéo cam biểu thị khoảng sẽ bị loại. Nhấp timeline để seek tới vị trí tương ứng.
- Cột phải tập trung vào thư mục xuất, xuất toàn bộ, cắt bỏ khoảng đã chọn, kiểm tra chất lượng và dừng tác vụ.

## Bảo toàn chức năng
- Giữ nguyên các lệnh Engine hiện có: export_full, cut_ranges, export_preview, open_qc, stop_export và choose_output.
- Giữ nguyên định dạng thời gian, kiểm tra biên Từ–Đến, khả năng mở video gốc bằng ffplay và luồng xem thử.
- Không thay đổi pipeline FFmpeg/encode, xử lý Mask, Branding, âm thanh, CTA Detector, Batch CTA, dự án, Auto Save, backup hoặc updater.
- Không thêm điều khiển codec, độ phân giải, FPS hay preset giả khi backend Qt hiện chưa cung cấp những tuỳ chọn đó.

## Kiểm thử
- Static regression tests xác nhận workspace xuất riêng, preview, cut table, timeline, seek đồng bộ và các lệnh Engine cũ.
- Windows CI chạy test layout cùng Branding preview regression và Qt/Engine smoke test.
- Release workflow kiểm tra cú pháp Python, kiểm tra FULL/UPDATE ZIP, payload, preserve paths và SHA-256.

## Cập nhật
- UPDATE dành cho v1.5.9.4.
- FULL package dành cho cài đặt mới hoặc máy không ở đúng bản nền.
