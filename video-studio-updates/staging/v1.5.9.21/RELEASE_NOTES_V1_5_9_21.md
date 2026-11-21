# v1.5.9.21 — Tăng tốc hàng đợi tải video

## Cải tiến
- Bổ sung tải đồng thời tối đa 2 video trong trang Tải video.
- Cho phép chọn 1 video hoặc 2 video cùng lúc; mặc định là 2.
- Giữ nguyên chất lượng Cao nhất (tự động), giới hạn 1080p/720p, định dạng MP4, cookie trình duyệt và thư mục lưu theo từng mục đã thêm.
- Tiến độ, tốc độ, ETA và kết quả tiếp tục được cập nhật riêng cho từng video.

## Dừng và tiếp tục an toàn
- Khi nhấn Dừng tải, các video đang chạy nhận yêu cầu hủy; mục chưa được bắt đầu vẫn ở trạng thái chờ.
- Video đã dừng vẫn giữ dữ liệu tạm để có thể tiếp tục; mục lỗi vẫn có thao tác Thử lại riêng.
- Giới hạn song song tối đa 2 để tránh mở quá nhiều kết nối và tiêu thụ tài nguyên không cần thiết.

## Lưu ý
- Tải đồng thời giúp xử lý nhiều mục trong hàng đợi, không làm tăng băng thông Internet tối đa của máy.
- Với video dung lượng lớn, lựa chọn Cao nhất (tự động) có thể cần nhiều thời gian hơn. Có thể chọn 1080p hoặc 720p khi muốn giới hạn dung lượng.

## Kiểm thử
- Kiểm tra hai tác vụ có thể chạy đồng thời.
- Kiểm tra chế độ một tác vụ vẫn tuần tự.
- Kiểm tra Dừng tải không khởi chạy các mục còn đang chờ.
- Kiểm tra giao diện và tham số song song được truyền vào worker.
