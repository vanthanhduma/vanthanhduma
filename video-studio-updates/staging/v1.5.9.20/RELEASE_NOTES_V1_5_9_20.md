# v1.5.9.20 — Sửa lỗi khởi chạy hàng đợi tải video

## Sửa lỗi
- Sửa kết nối nút **Bắt đầu tải**: tín hiệu `QPushButton.clicked` có thể truyền thêm giá trị `checked` kiểu boolean, không được hiểu nhầm thành danh sách ID video.
- Hàm khởi chạy hàng đợi xử lý an toàn tham số boolean và chuyển về chế độ tải toàn bộ mục đang chờ.
- Khi nhấn Bắt đầu tải, worker được khởi chạy và trạng thái hàng đợi được cập nhật thay vì treo mãi ở **Đang chờ**.

## Giữ nguyên
- Giữ nguyên engine `yt-dlp`, lựa chọn chất lượng, định dạng, cookie trình duyệt và thư mục lưu.
- Giữ nguyên khả năng tải tiếp, thử lại từng mục và dữ liệu tải dở.
- Không thay đổi CTA Detector, Mask, Branding, project schema hoặc quy trình xuất video.

## Kiểm thử
- Kiểm thử hồi quy kết nối tín hiệu nút và xử lý tham số boolean.
- Chạy bộ kiểm thử Download/Batch/Branding, Windows Qt smoke và build/verify FULL + UPDATE.
