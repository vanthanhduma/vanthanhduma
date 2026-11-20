# v1.5.9.3 — Studio Branding Preview Performance

## Sửa điểm nghẽn Preview
- Preview Branding trước đây dựng logo và nhãn ở độ phân giải gốc, có thể tới 4K, quét alpha trên toàn khung hình rồi dựng lại overlay lần thứ hai trước khi thu nhỏ để hiển thị.
- Preview mới thu nhỏ khung hình xuống tối đa 1280×720 trước khi dựng lớp logo/nhãn.
- Chỉ tạo lớp logo/nhãn một lần và dùng lại để ghép ảnh xem trước.
- Giữ metadata kích thước gốc và quy đổi khung logo về tọa độ video ban đầu, nên thao tác kéo logo vẫn chính xác.
- Chỉ coalescing preview trong giao diện, tối đa một yêu cầu đang chạy và chỉ gửi trạng thái mới nhất sau khi yêu cầu hiện tại hoàn tất.
- Giảm debounce còn 120 ms; lỗi Preview hiển thị trong dòng trạng thái, không bật liên tiếp hộp thoại lỗi.

## Không thay đổi
- Độ phân giải, cấu hình overlay và chất lượng video xuất.
- Cách áp dụng logo/nhãn, preset, vị trí kéo logo, phạm vi thời gian hoặc cấu hình riêng theo phân cảnh.
- CTA Detector, Batch CTA, Mask/Before-After, lưu dự án, lịch sử backup và workflow xuất video.

## Kiểm thử
- Windows test với khung hình tổng hợp 4K: xác nhận lớp preview và compositing chỉ chạy ở 1280×720 một lần, tọa độ logo quy đổi đúng về khung gốc và file preview có kích thước chuẩn.
- Qt/Engine smoke: kiểm tra yêu cầu preview được gom khi kéo slider, không tích lũy các request cũ.
- Batch CTA Windows E2E và kiểm thử queue/project để bảo đảm không hồi quy.