# v1.5.9.32 — CTA Benchmark Dialog Layout Fix

## Sửa hộp thoại So sánh hiệu năng
- Đưa nội dung chọn đoạn, mô tả hai chiến lược và bảng so sánh/duyệt ứng viên vào vùng cuộn dọc.
- Ghim trạng thái, tiến trình và nút Chạy so sánh/Dừng kiểm thử/Đóng ở cuối cửa sổ để luôn truy cập được.
- Tự tính kích thước tối đa theo vùng màn hình khả dụng, căn giữa cửa sổ và giảm chiều cao khi màn hình/DPI không đủ chỗ.
- Không ép layout kéo hộp thoại vượt lên trên mép màn hình; người dùng có thể cuộn để xem phần nội dung dài.

## Bảo toàn xử lý CTA
- Giữ hai chiến lược 2 giây/ảnh một lượt và 4 giây/ảnh lượt một + 1 giây/ảnh lượt hai trong vùng nghi vấn.
- Không đổi OCR, nhận diện đồ họa, mật độ quét CTA chính, Batch CTA, cache và dữ liệu lịch sử.
- Không tự động duyệt hoặc cắt khoảng CTA.
