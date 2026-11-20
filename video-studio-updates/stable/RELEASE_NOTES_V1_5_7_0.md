# v1.5.7.0 — Studio UI Refresh & MM:SS Timeline

## Giao diện và điều hướng
- Gom thanh công cụ trên theo luồng thao tác: Mở video; nhóm Dự án (Mở dự án/Khôi phục ZIP, Lưu dự án, Đóng gói ZIP); Hoàn tác/Làm lại; Xuất video.
- Giữ nút Kiểm tra & cập nhật riêng trên header; menu Thêm chỉ còn thao tác Lưu thành/Tạo bản sao, không lặp các hành động đã có nút riêng.
- Đổi mục sidebar Dự án thành Công cụ video, gom Tải video, Xử lý hàng loạt và Ghép video vào các tab cùng workspace.
- Bỏ entry point Dựng video khỏi sidebar; không xóa pipeline xử lý hiện có.
- Đưa Studio Branding vào trang làm việc trong cửa sổ chính, không mở cửa sổ phụ.
- Đồng bộ màu Navy/Teal/Cyan và bố cục giữa Mask, Branding và Xuất & kiểm tra; tinh giản chú thích/hướng dẫn tĩnh, giữ trạng thái, tiến độ, lỗi và kết quả cần thiết.

## Công cụ video — Ghép video
- Bổ sung tab Ghép video trong Công cụ video, cho phép thêm/bỏ/sắp thứ tự clip, chọn tên file và thư mục đầu ra, theo dõi tiến độ và dừng tác vụ.
- Dùng lại pipeline FFmpeg hiện có: stream-copy khi thông số tương thích; chuẩn hóa và mã hóa lại khi cần; ưu tiên NVENC khi máy hỗ trợ.
- Khóa danh sách nguồn trong lúc ghép, ngăn đường dẫn đầu ra ghi đè một trong các video nguồn.

## Định dạng thời gian
- Chuẩn hóa các ô nhập và nhãn thời gian của Qt Studio sang MM:SS, ví dụ `01:30` và `125:30`.
- Áp dụng cho vị trí phát, CTA, thời gian Branding và khoảng cắt trong Xuất & kiểm tra.
- Giá trị nội bộ tiếp tục giữ độ chính xác số thực theo giây; chỉ chuỗi hiển thị được làm tròn tới giây.

## Kiểm tra
- `py_compile`: PASS cho các module Python chính.
- Unit tests: 9/9 PASS, gồm kiểm tra định dạng MM:SS và parse mốc thời gian.
- FFmpeg merge smoke test: PASS với hai video cùng thông số (stream-copy) và video khác độ phân giải (fallback encode), kết quả 6 giây đúng thứ tự.
- Chưa kiểm thử GUI trực tiếp trên Windows trong môi trường build do môi trường này không có PySide6; cần nghiệm thu giao diện/DPI thực tế trên Windows.
- NVENC phụ thuộc FFmpeg/driver trên máy người dùng; smoke test trong môi trường build đã dùng CPU fallback.
