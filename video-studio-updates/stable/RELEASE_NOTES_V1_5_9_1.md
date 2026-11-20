# v1.5.9.1 — Batch CTA & Backup History UI Refinement

## Giao diện Batch CTA
- Tổ chức lại trang Batch CTA theo các khu vực rõ ràng: tiêu đề, hàng đợi, thư mục lưu project, tiến độ và thao tác.
- Bổ sung icon cho Thêm video, Thêm thư mục, Xóa chờ, Lên/Xuống, Mở project, Bắt đầu quét và Dừng hàng đợi.
- Giữ nguyên hàng đợi tuần tự, lưu tiến độ, dừng an toàn, tiếp tục hàng đợi và tự trở về workspace khi hoàn tất.

## Lịch sử sao lưu
- Đưa nút “Khôi phục bản đã chọn” lên thanh tiêu đề, căn cùng hàng với tên hộp thoại.
- Khi mở hộp thoại không tự chọn bản sao lưu nào; nút khôi phục bị khóa cho tới khi người dùng chủ động click hoặc kích hoạt một dòng.
- Hiển thị rõ mốc đang chọn và yêu cầu xác nhận trước khi khôi phục.
- Khôi phục tạo thành một project mới và mở project đó; không ghi đè project đang mở.
- Giữ nguyên giới hạn tối đa 8 mốc sao lưu thủ công. Auto Save vẫn chỉ cập nhật project chính, không tự sinh mốc.

## Sidebar và tương thích
- Thêm icon cho tiêu đề nhóm “CÔNG CỤ VIDEO”.
- Giữ nguyên icon và chức năng của các mục điều hướng hiện có.
- Không thay đổi CTA Detector, thuật toán OCR/nhận diện, quy tắc lấy mẫu hoặc quyền duyệt/cắt thủ công.
- Không đổi workflow tải video, ghép video, xuất video, Auto Save, lưu project, ZIP hoặc Batch CTA engine.

## Cập nhật
- Gói UPDATE hỗ trợ nâng cấp trực tiếp từ v1.5.9.0.
- Máy đang ở v1.5.8.0 cần nâng cấp lên v1.5.9.0 trước khi dùng gói này.
- Dùng nút “Kiểm tra & cập nhật” trong ứng dụng; không giải nén UPDATE đè lên thư mục cài đặt.
- Máy mới vẫn có thể sử dụng gói FULL.

## Kiểm thử
- Windows Qt/Engine smoke: kiểm tra engine khởi động, Batch CTA controls, hộp thoại sao lưu không tự chọn mốc, nút khôi phục nằm trên thanh tiêu đề và chỉ bật sau thao tác chọn.
- Batch CTA Windows E2E: giữ nguyên pipeline detector, queue và project creation.
- Batch CTA queue/project tests: giữ nguyên kiểm thử lưu hàng đợi và project độc lập.
