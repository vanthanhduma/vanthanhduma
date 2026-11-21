# v1.5.9.18 — Reload CTA cache when reopening the same project

## Sửa lỗi còn sót ở v1.5.9.17
- Khi bấm **Mở dự án** với đúng file .grproj đang mở, Qt trước đây không nạp lại CTA cache vì video_path và project_path không thay đổi.
- Mỗi lệnh mở project giờ ghi nhận đường dẫn đích; sau khi Engine báo đúng project đã mở, UI buộc nạp lại CTA cache dù đường dẫn không đổi.
- Áp dụng chung cho Mở dự án, Mở project đã lưu trong Batch CTA, khôi phục bản sao lưu và các luồng mở project khác.
- Yêu cầu reload chỉ được tiêu thụ khi trạng thái Engine trùng với file .grproj được yêu cầu, tránh reload nhầm project cũ khi mở project khác.

## Giữ nguyên
- Không tự động cắt CTA; người dùng vẫn duyệt và chọn khoảng cần bỏ.
- Không thay đổi project schema 4, đường dẫn video nguồn, Mask, Branding, xuất video, Auto Save, sao lưu hoặc hàng đợi.
- Không xóa hoặc ghi đè cache hiện có khi chỉ mở lại project.

## Kiểm thử
- Mở Batch project lần đầu và xác nhận CTA được nạp.
- Xóa trạng thái CTA trong bộ nhớ UI nhưng giữ cache và giữ nguyên cache keys.
- Mở lại chính file .grproj lần thứ hai trong cùng phiên Qt; xác nhận CTA và thumbnail được nạp trở lại.
- Chạy Windows Batch CTA E2E, Qt smoke, project/queue regression và xác minh FULL/UPDATE package.
