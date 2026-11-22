# v1.5.9.34 — CTA Range Scan, Gap Watcher & Workspace Tabs

## CTA Detector workspace
- Tách CTA Detector thành hai tab con: **Quét CTA** và **So sánh hiệu năng**.
- Giữ nguyên chức năng benchmark 2 / 3 / 4 giây, bảng đánh giá và lịch sử kiểm thử; không đổi profile hay dữ liệu benchmark cũ.
- Thêm chọn vùng thời gian Từ / Đến theo MM:SS và nút Quét vùng đã chọn.
- Quét vùng chỉ bổ sung kết quả bên trong vùng, giữ nguyên các ứng viên ngoài vùng và không ghi đè checkpoint quét toàn video.
- Lưu phạm vi đã quét, vùng quét gần nhất và vùng đang tạm dừng vào cache dự án.

## CTA detection engine
- Quét chính production ở mật độ 4 giây/ảnh.
- Thêm Gap Watcher độc lập: theo dõi biến đổi hình ảnh nhẹ giữa các mốc chính mỗi 0,5 giây, chỉ chuyển khung hình có thay đổi cục bộ sang OCR + nhận diện đồ họa chuyên sâu.
- Vẫn giữ OCR Balanced, nhận diện đồ họa, ảnh bằng chứng, duyệt thủ công, Pause / Resume / Stop.
- Áp dụng cùng 4 giây + Gap Watcher cho CTA xử lý hàng loạt.
- Benchmark giữ nguyên các cấu hình 2 / 3 / 4 giây một lượt để tiếp tục làm mốc đối chứng.

## Safety and compatibility
- Không tự động duyệt hoặc cắt CTA.
- Không thay đổi dữ liệu dự án, thao tác cắt thủ công, cache lịch sử benchmark và updater transaction rules.
- Hỗ trợ cập nhật từ các bản v1.5.9.0 đến v1.5.9.33 theo danh sách phiên bản được hỗ trợ.
