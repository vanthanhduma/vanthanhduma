# v1.5.9.2 — Project Toolbar & Backup History

## Thanh công cụ dự án
- Hiển thị riêng các thao tác Mở dự án, Lưu, Lưu thành, Lịch sử sao lưu, Nhập ZIP và Đóng gói ZIP ngay trên thanh công cụ phía trên.
- Bỏ nút menu Dự án xổ xuống; các chức năng không còn bị giấu trong một ô.
- Giữ nút Mở video và Xuất video ở thanh thao tác hiện tại.
- Giữ nguyên quy trình lưu lần đầu chọn vị trí, các lần sau lưu nhanh; Lưu thành luôn tạo file dự án riêng.

## Sidebar
- Bỏ icon trang trí ở tiêu đề nhóm CÔNG CỤ VIDEO.
- Đặt icon video ngay trên nút điều hướng Công cụ video, cùng phong cách với các mục CTA Detector, Làm sạch · Mask và Studio Branding.

## Lịch sử sao lưu
- Nút Lịch sử sao lưu luôn bấm được, không còn bị khóa chỉ vì dự án chưa có đường dẫn .grproj.
- Nếu chưa có file dự án hoặc chưa có snapshot, hộp thoại vẫn mở và hiển thị hướng dẫn rõ lý do chưa có bản sao lưu.
- Không có snapshot thì nút khôi phục tiếp tục bị khóa; khi có snapshot, người dùng phải chủ động chọn một dòng.
- Khôi phục vẫn tạo một dự án mới, không ghi đè dự án hiện tại. Auto Save vẫn không tạo snapshot thủ công.

## Tương thích và kiểm thử
- Không thay đổi CTA Detector, Batch CTA, OCR, quy trình cắt duyệt thủ công hoặc engine xuất video.
- Windows Qt/Engine smoke kiểm tra các nút dự án hiển thị trực tiếp trên thanh trên, icon Công cụ video nằm đúng nút điều hướng, và hộp thoại lịch sử rỗng có thông báo rõ ràng.
- Gói UPDATE hỗ trợ nâng cấp trực tiếp từ v1.5.9.1.
