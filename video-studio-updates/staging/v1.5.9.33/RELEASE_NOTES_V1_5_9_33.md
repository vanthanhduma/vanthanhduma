# v1.5.9.33 — CTA Benchmark 2s / 3s / 4s

## Thay đổi benchmark CTA
- Thay benchmark hai chiến lược cũ bằng ba cấu hình lấy mẫu: 2 giây/ảnh, 3 giây/ảnh và 4 giây/ảnh.
- Cả ba cấu hình dùng cùng OCR Balanced và nhận diện đồ họa, mỗi cấu hình chỉ quét một lượt toàn đoạn.
- Loại bỏ hoàn toàn cấu hình 4 giây + quét lại 1 giây khỏi benchmark.
- Bảng kết quả tiếp tục hiển thị thời gian, số ảnh mẫu, ứng viên, độ tin cậy, Precision, Recall và F1 sau khi duyệt.

## Bảo toàn dữ liệu và xử lý CTA
- Benchmark chỉ tạo kết quả thử nghiệm riêng, không ghi đè danh sách CTA chính.
- Không tự động duyệt hoặc cắt bất kỳ khoảng CTA nào.
- Không thay đổi mật độ quét CTA chính và xử lý hàng loạt trong bản phát hành này.
- Giữ nguyên dữ liệu dự án, cache, lịch sử benchmark và quy trình updater hiện có.
