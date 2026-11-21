# v1.5.9.17 — Batch CTA project cache recovery

## Sửa lỗi Batch CTA
- Sửa khâu tìm và nạp cache CTA khi mở lại project đã quét hàng loạt.
- Nếu cache không còn đúng tên chuẩn, ứng dụng dò cache trong workspace của project và chỉ nhận cache có fingerprint trùng chính xác video nguồn.
- Không ghép nhầm CTA giữa các video; cache có fingerprint khác vẫn bị từ chối.
- Xác minh file cache, fingerprint và số ứng viên trước khi đánh dấu video Batch CTA là hoàn thành.
- Hàng đợi hiển thị rõ số ứng viên đã lưu, kể cả trường hợp quét xong nhưng không phát hiện CTA.
- Khi mở project không có cache hoặc cache không có ứng viên, CTA Detector hiển thị trạng thái cụ thể thay vì giữ thông báo cũ và bảng 0/0 khó hiểu.

## Giữ nguyên
- Không tự động duyệt hoặc cắt CTA.
- Không thay đổi video nguồn, project schema 4, mask, branding, xuất video, Auto Save, sao lưu hoặc hàng đợi tải video.
- Không xóa hay ghi đè cache của video khác; chỉ khôi phục cache có fingerprint trùng.

## Kiểm thử
- Bổ sung kiểm thử dò cache theo fingerprint khi tên file cache thay đổi.
- Bổ sung kiểm thử bảo đảm không nạp cache của video khác.
- Chạy lại Batch CTA project/queue, Qt smoke test và Windows Batch CTA E2E trước khi phát hành.
