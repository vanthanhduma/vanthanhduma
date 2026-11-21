# v1.5.9.13 — Export & Branding UI Refinement

## Export workspace
- Bỏ nút Xuất video trên thanh thao tác dự án vì đã có nút Xuất toàn bộ video trong workspace Xuất & kiểm tra.
- Chuẩn hóa chiều cao các nút Xuất toàn bộ, Cắt bỏ & xuất phần còn lại, Kiểm tra chất lượng và Dừng tác vụ xuất.
- Nới nhẹ bề rộng bảng thiết lập xuất để tránh nhãn/nút bị chật ở độ phân giải desktop phổ biến.

## Studio Branding
- Làm rõ tiêu đề và phân cấp bảng điều khiển.
- Tăng bề rộng inspector để các trường, tab và nút chọn file thoáng hơn.
- Làm mới tab và card điều khiển theo tông navy/teal, thống nhất bo góc, viền và khoảng cách.
- Footer trạng thái và nút Áp dụng được tách rõ, nút áp dụng có kích thước nhất quán.
- Giữ nguyên các điều khiển logo, nhãn, thời gian, phạm vi cảnh, preset, preview debounce và lệnh áp dụng.

## Bảo toàn
- Không thay đổi engine xuất video, project schema, dữ liệu dự án, Auto Save hoặc cấu hình Branding.
- Không xóa hoặc thay đổi bất kỳ callback xử lý nghiệp vụ nào.
