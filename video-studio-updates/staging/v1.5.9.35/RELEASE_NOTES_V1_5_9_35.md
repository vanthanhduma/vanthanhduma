# v1.5.9.35 — Embedded CTA Benchmark & Batch Retry

## CTA Detector workspace
- Đưa giao diện So sánh hiệu năng, danh sách Lịch sử kiểm thử và trang xem lại kết quả vào bên trong tab CTA hiện có.
- Tất cả thao tác benchmark/lịch sử dùng điều hướng trang con trong cùng cửa sổ Studio; không mở cửa sổ riêng.
- Giữ nguyên ba cấu hình benchmark 2 / 3 / 4 giây, bảng Precision/Recall/F1, ảnh bằng chứng, nhãn duyệt và các bản ghi lịch sử đã lưu.
- Giữ nguyên phạm vi kiểm thử theo đoạn video, kết quả benchmark tách biệt với cache CTA chính và không tự động cắt.

## CTA detection and Gap Watcher
- Giữ mật độ quét chính 4 giây/ảnh; Gap Watcher tiếp tục lấy mẫu biến đổi hình ảnh mỗi 0,5 giây giữa các mốc chính.
- Tăng khả năng phát hiện overlay nhỏ hoặc xuất hiện dần bằng cách so sánh tích lũy với khung neo gần nhất, thay vì bỏ lỡ biến đổi nhỏ ở từng bước fade-in.
- Giữ scene cut lớn ngoài luồng OCR; sau scene cut, đặt lại khung neo để tiếp tục theo dõi CTA ở cảnh mới.
- Chỉ các khung nghi vấn mới chuyển sang OCR Balanced và nhận diện đồ họa chuyên sâu; không hạ ngưỡng xác nhận CTA và không tự duyệt/cắt.
- Áp dụng cùng detector production cho Batch CTA.

## Batch CTA Processing
- Thêm cột thống kê theo từng video gồm số ảnh lượt chính, mẫu Gap Watcher, số khung được chuyển sang OCR/đồ họa, timeout OCR, số ứng viên CTA và thời gian xử lý.
- Thêm nút **Thử lại video lỗi** cho dòng đang chọn; chỉ video lỗi được chọn được đưa lại vào hàng đợi.
- Khi mở lại hàng đợi, trạng thái lỗi vẫn được giữ để người dùng xem nguyên nhân và quyết định thử lại; project đã hoàn thành không bị quét lại.
- Giữ xử lý tuần tự, lưu project/cache độc lập từng video và duyệt CTA thủ công.

## Compatibility and safety
- Không thay đổi schema project, cache CTA chính hoặc cấu trúc lịch sử benchmark cũ.
- Không sửa video nguồn, không tự động chấp nhận hoặc cắt CTA.
- UPDATE hỗ trợ từ các bản v1.5.9.0 đến v1.5.9.34; các bản nền cũ hơn tiếp tục dùng danh sách hỗ trợ của updater.
