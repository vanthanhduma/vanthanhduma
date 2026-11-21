# v1.5.9.19 — Sửa luồng mở project trong Qt

## Sửa lỗi mở project không phản hồi
- Chuyển xác nhận thay đổi chưa lưu sang hộp thoại Qt của Video Studio; engine nền không tự bật hộp thoại Tk khi được Qt yêu cầu mở project.
- Khi video nguồn trong project không còn ở đường dẫn đã lưu, ứng dụng hỏi liên kết lại bằng hộp thoại Qt; không để hộp thoại Tk ẩn phía sau cửa sổ chính.
- Đường dẫn video được liên kết lại sẽ được lưu vào file .grproj sau khi mở thành công.
- Bấm Mở dự án, Mở project đã lưu trong Batch CTA, khôi phục sao lưu và khôi phục ZIP đều dùng chung một luồng mở project.
- Hiển thị trạng thái "Đang mở dự án…" và ghi log đường dẫn project được yêu cầu.

## Giữ nguyên
- Không xóa hoặc ghi đè cache CTA.
- Không tự động cắt CTA; người dùng vẫn duyệt và chọn khoảng cần bỏ.
- Không thay đổi project schema 4, Mask, Branding, xuất video, Auto Save hoặc hàng đợi Batch CTA.

## Kiểm thử
- Windows Qt/engine smoke test.
- Batch CTA E2E mở project lần đầu và mở lại chính project đó lần thứ hai thông qua Qt open handler.
- Batch CTA queue/project regression và build/verify FULL + UPDATE packages.
