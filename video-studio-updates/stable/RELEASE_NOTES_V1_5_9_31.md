# v1.5.9.31 — Persistent CTA Benchmark History

## Lưu trữ lịch sử kiểm thử CTA
- Tự động tạo một bản ghi JSON riêng sau mỗi lần so sánh hai chiến lược CTA trên đoạn video đã chọn.
- Lưu đường dẫn/nhận diện video nguồn, thời điểm kiểm thử, khoảng bắt đầu–kết thúc, phiên bản ứng dụng, trạng thái chạy và số liệu của từng chế độ.
- Sao chép thumbnail bằng chứng vào thư mục riêng của từng lần kiểm thử; không phụ thuộc thư mục tạm của Engine.
- Lưu bền vững lựa chọn Đúng CTA/Nhận diện nhầm và số lượng CTA thật đã nhập; mở lại lịch sử vẫn xem được ảnh, bảng số liệu và tiếp tục chỉnh nhãn.
- Thêm màn hình Lịch sử kiểm thử để mở lại hoặc xóa riêng từng bản ghi. Xóa lịch sử không tác động cache, project hay danh sách CTA chính.

## Bảo toàn quy trình hiện có
- Giữ hai chế độ benchmark: 2 giây/ảnh một lượt; 4 giây/ảnh lượt một và 1 giây/ảnh ở vùng nghi vấn lượt hai.
- Giữ giới hạn mỗi lần so sánh tối đa 15 phút, cùng OCR balanced và nhận diện đồ họa.
- Không thay đổi nhịp quét CTA toàn video, Batch CTA, kết quả CTA chính hoặc quy trình duyệt thủ công trước khi cắt.
- Không tự động chọn hay cắt CTA.
- Build manifest includes the dedicated CTA benchmark history module and persistence regression tests.
