# v1.5.6.41 — Project Workspace, CTA Archive & Boundary Merge

## Thay đổi chính
- Gỡ chức năng quét tự động Phát hiện lớp phủ (logo/chữ/watermark/banner) khỏi UI và worker; giữ nguyên Mask thủ công, Smart Mask, Brush/Eraser, Undo/Redo, Inpaint, Branding, Phân cảnh và Export.
- Tổ chức dự án thành workspace riêng cạnh thư mục `video nguồn`: `Media`, `Analysis/CTA`, `Assets`, `Exports`, `Backups`.
- Mặc định lưu dự án mới trong `dự án lưu trữ/<Tên dự án>/<Tên dự án>.grproj`; video xuất mặc định sang thư mục `video xuất` cạnh `video nguồn`.
- Đồng bộ logo và logo theo phân cảnh vào `Assets` để dự án có tài nguyên đi kèm.
- Lưu CTA results, checkpoint và thumbnail trong `Analysis/CTA` của dự án; cache gắn theo fingerprint nội dung video để tồn tại khi chuyển ổ đĩa/máy. Cache AppData cũ được di trú khi mở/lưu dự án.
- Bổ sung đóng gói ZIP toàn bộ workspace vào `dự án lưu trữ/Gói ZIP/<Tên dự án>`; người dùng chọn kèm hoặc không kèm video nguồn.
- Bổ sung khôi phục ZIP an toàn, không ghi đè dự án hiện có; kiểm tra đường dẫn ZIP chống path traversal.
- Tạo bản sao lưu luân phiên tối đa 8 bản `.grproj` trước các lần lưu thủ công.
- Làm rõ nút CTA: `Quét toàn video` để chạy từ đầu và `Quét phần còn lại` để tiếp tục từ checkpoint; giữ lựa chọn CTA đã duyệt khi lưu cache.
- Nối ứng viên CTA cùng vị trí/từ khóa khi xuất hiện sát ranh giới giữa hai phiên quét, hạn chế tách đôi một sự kiện kéo dài qua checkpoint.
- Sửa trạng thái hoàn tất CTA để phản ánh đúng thuật toán hiện tại: một lượt OCR lấy mẫu thưa, không hiển thị nhầm là quét hai lượt.
- Sắp xếp thanh công cụ: Mở dự án có menu mở `.grproj`/khôi phục ZIP; có nút Đóng gói ZIP; `Lưu thành / Tạo bản sao` chuyển vào menu Thêm. Cập nhật nút và trạng thái CTA để thể hiện rõ đây là một lượt OCR lấy mẫu thưa, không còn ghi nhầm “quét 2 lượt”.

## Tương thích và an toàn dữ liệu
- Giữ schema dự án `.grproj` hiện tại (schema 4) và khả năng đọc schema 1–4 cũ.
- Không xóa cache CTA cũ trong AppData sau khi di trú.
- ZIP không chứa file tạm/lock/ZIP lồng; bản Backups được mang theo nhưng không bị nhầm là file dự án chính khi khôi phục.
- Khi ZIP không kèm video, cần giữ video ở vị trí cũ hoặc liên kết lại khi mở trên máy khác. Thư mục xuất mặc định được thiết lập lại thành `video xuất` cạnh thư mục dự án lưu trữ sau khi khôi phục; thư mục xuất tùy chỉnh vẫn được giữ theo liên kết đã lưu.

## Kiểm tra
- `py_compile`: PASS cho app, worker, Qt UI, detector và module mới.
- Kiểm tra workspace, thư mục lưu trữ, video xuất, asset copy, fingerprint khi di chuyển: PASS.
- Kiểm tra ZIP có/không có video, cache CTA, thumbnail, backup, restore, tránh ghi đè và path traversal: PASS.
- Kiểm tra thuật toán nối CTA cùng vị trí/từ khóa và từ chối ứng viên không liên quan: PASS.
- Chưa kiểm thử Qt GUI trực tiếp trên Windows trong môi trường build do không có PySide6; cần nghiệm thu giao diện và FFmpeg/NVENC trên máy Windows.
