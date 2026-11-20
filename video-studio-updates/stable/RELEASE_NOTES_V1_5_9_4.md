# v1.5.9.4 — Smooth Studio Branding Drag

## Sửa độ trễ khi kéo logo
- Tách preview thành ảnh nền (video + nhãn) và lớp logo PNG trong suốt.
- Canvas Qt vẽ lớp logo riêng và cập nhật vị trí ngay trong mouseMoveEvent; thao tác kéo không gửi lệnh render sang Engine.
- Giữ nguyên điểm người dùng nắm trên logo, giới hạn logo trong khung hình và quy đổi tọa độ về video gốc.
- Chỉ gửi một yêu cầu preview cuối cùng khi nhả chuột để đồng bộ trạng thái.

## Không thay đổi
- Pipeline áp dụng Branding và xuất video; độ phân giải, codec, chất lượng và preset.
- Kích thước, độ mờ, góc xoay, vị trí preset và phạm vi thời gian hiển thị.
- CTA Detector, Batch CTA, Mask/Before-After, lưu dự án, Auto Save, backup và updater.

## Kiểm thử
- Engine test 4K xác nhận preview render ở 1280×720, ảnh nền không chứa logo đã tách, lớp logo PNG có alpha và crop đúng kích thước.
- Qt canvas test xác nhận logo di chuyển trực tiếp theo con trỏ và chỉ phát tín hiệu cập nhật một lần khi nhả chuột.
- Release workflow chạy compile, Branding tests, Batch CTA tests, đóng gói FULL/UPDATE và xác minh SHA-256.
