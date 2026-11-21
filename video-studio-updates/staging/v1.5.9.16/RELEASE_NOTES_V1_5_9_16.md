# v1.5.9.16 — Tiếp tục tải video

## Sửa lỗi
- Video bị dừng không còn bị loại khỏi khả năng tiếp tục trong hàng đợi.
- Thêm nút **Tiếp tục** trên từng dòng tạm dừng và nút **Thử lại** trên từng mục lỗi.
- Nút tổng tự đổi thành **Tiếp tục tải** khi còn video tạm dừng; tiếp tục tuần tự các mục tạm dừng và đang chờ.

## Bảo toàn phiên tải
- Lưu theo từng mục thư mục đích, chất lượng, định dạng và lựa chọn cookie trình duyệt.
- Khi tiếp tục, dùng lại cấu hình/đường dẫn ban đầu để yt-dlp nhận lại tệp tải dở `.part` khi nguồn hỗ trợ.
- Đặt rõ `continuedl=True`, `nopart=False`, `overwrites=False`.
- Nút **Xóa mục đã xong** không xóa các mục tạm dừng.

## Giữ nguyên
- Không đổi URL validation, Facebook/YouTube support, chất lượng, định dạng, cookie cục bộ hay thứ tự tải.
- Không đổi Engine xuất video, project schema, Auto Save, CTA Detector, Branding hay dữ liệu dự án.
- Giữ nguyên quy tắc bảo toàn dữ liệu người dùng trong UPDATE.

## Kiểm thử
- Bổ sung regression tests cho cờ resume của yt-dlp và thao tác tiếp tục/thử lại trong UI.
- Workflow biên dịch source và chạy bộ test downloader trên Windows runner.
