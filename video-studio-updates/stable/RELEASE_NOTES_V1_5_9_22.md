# v1.5.9.22 — Download & Branding reliability

## Tăng tốc tải, giữ nguyên lựa chọn chất lượng
- Tăng số phân đoạn DASH/HLS tải đồng thời từ 4 lên 8.
- Bật HTTP chunk 10 MiB để thử tăng thông lượng trên máy chủ hỗ trợ HTTP range; đây là tính năng thử nghiệm của yt-dlp và có thể không tăng tốc với mọi nguồn.
- Bật kiểm tra định dạng trước khi tải. Không đặt giới hạn 720p/1080p; bộ chọn Cao nhất (tự động) được giữ nguyên.
- Khi cần ghép video/audio, dùng MP4 trước nếu người dùng chọn Ưu tiên MP4; nếu không thì MKV trước. Không encode lại, không giảm chất lượng nguồn.

## Sửa lỗi file chỉ có âm thanh
- Nếu FFprobe có sẵn, kiểm tra file đầu ra sau hậu xử lý.
- File không có luồng hình sẽ không được đánh dấu hoàn tất; ứng dụng hiển thị thông báo lỗi thay vì báo tải thành công sai.
- Trả về đường dẫn file và codec hình để hỗ trợ chẩn đoán.

## Sửa Studio Branding
- Nền nhãn tự động tính theo kích thước chữ thay vì dùng bề rộng cố định.
- Chữ dài được thu cỡ hoặc xuống dòng trong giới hạn khung hình, tránh tràn khỏi nền.
- Vẫn giữ màu, độ mờ, vị trí, kích thước nhãn và nội dung người dùng đặt.

## Tác vụ hàng đợi
- Giữ cơ chế hàng đợi tối đa 2 video, tiếp tục tải và thử lại từng mục.
- Các kiểm thử hàng đợi tải và Batch CTA được chạy cùng bộ kiểm tra Windows/Qt trước phát hành.
- Không thay đổi schema dự án và dữ liệu cá nhân.

## Kiểm thử bổ sung
- File audio-only không được đánh dấu là video tải thành công.
- File có luồng hình được FFprobe chấp nhận.
- Cấu hình 8 phân đoạn/HTTP chunk không thay đổi bộ chọn Cao nhất.
- Nền nhãn bao hết chữ tiếng Việt ở nhãn dài.
