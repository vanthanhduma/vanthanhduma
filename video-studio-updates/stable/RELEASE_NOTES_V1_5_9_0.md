# v1.5.9.0 — Batch CTA Processing

## Batch CTA Processing
- Thay khu vực Xử lý hàng loạt trên giao diện Qt bằng quy trình quét CTA theo hàng đợi.
- Thêm từng video hoặc thêm toàn bộ thư mục; chọn một thư mục gốc để lưu các dự án.
- Quét tuần tự từng video bằng CTA Detector hiện có, không chạy nhiều video đồng thời và không đổi quy tắc nhận diện CTA.
- Tự lưu mỗi video thành một dự án `.grproj` riêng, kèm kết quả CTA, mốc thời gian, độ tin cậy, cache và thumbnail bằng chứng.
- Giữ nguyên video nguồn tại vị trí ban đầu; dự án chỉ tham chiếu đường dẫn và fingerprint, không sao chép file video dung lượng lớn.
- Lưu hàng đợi an toàn. Khi tiếp tục, video đã hoàn thành được bỏ qua; video đang quét dở được đưa lại vào hàng chờ.
- Khi toàn bộ hàng đợi hoàn thành, ứng dụng trở về workspace Video Tools. Khi người dùng dừng thủ công, trang Batch vẫn được giữ để có thể tiếp tục.

## Quyền duyệt kết quả
- Batch CTA chỉ quét và tạo dự án. Không tự duyệt ứng viên và không tự cắt video.
- Người dùng mở từng dự án, xem thumbnail/mốc thời gian rồi tự chọn các khoảng cần cắt và xuất video.
- CTA Detector vẫn chỉ phân tích nội dung hiển thị trong hình ảnh/video, không phân tích âm thanh hoặc lời nói.

## Tương thích và dữ liệu
- Giữ nguyên lệnh Batch export cũ trong engine để tương thích ngược.
- Dự án Batch CTA dùng schema project v4 và bố cục cache CTA hiện có.
- Không thay đổi video nguồn, dự án cũ, settings, branding cá nhân, thư mục downloads/renders hoặc môi trường `.venv`.

## Kiểm thử
- Windows End-to-End: tạo hai MP4 tổng hợp, OCR nhận được SUBSCRIBE và LIKE, quét tuần tự, tạo hai project độc lập và cache/thumbnail đầy đủ.
- Kiểm thử mở lại project: dùng nút Mở dự án đã lưu, xác nhận ứng viên CTA và thumbnail được nạp lại trong giao diện.
- Kiểm thử Qt/Engine và hàng đợi Batch CTA: PASS.
- Kiểm thử video tổng hợp xác nhận đường đi tự động; độ chính xác trên mọi kiểu video thực tế vẫn cần được người dùng xem lại thủ công.

## Cập nhật
- Gói UPDATE này hỗ trợ nâng cấp trực tiếp từ v1.5.8.0.
- Máy đang ở phiên bản cũ hơn cần cập nhật lên v1.5.8.0 trước khi áp dụng gói này.
- Dùng nút Kiểm tra & cập nhật trong Video Studio; không giải nén UPDATE đè lên thư mục cài đặt.
