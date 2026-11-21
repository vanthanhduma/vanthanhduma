# v1.5.9.12 — Downloader Workspace Refinement

## Giao diện Tải video
- Chuyển bốn thiết lập Chất lượng video, Định dạng tệp, Cookie trình duyệt và Thư mục lưu video thành bốn thẻ cùng hàng, bố cục dọc: nhãn phía trên, điều khiển phía dưới.
- Thu gọn chiều cao khu vực thiết lập, khoảng cách giữa các khối và kích thước nút để danh sách tải có thêm không gian.
- Giảm chiều cao tối thiểu bảng hàng đợi nhưng vẫn giữ bảng co giãn theo vùng làm việc.
- Giữ nguyên tên widget, giá trị lựa chọn, thư mục mặc định, các callback, cơ chế tải, dừng tải, xóa hàng đợi và mở thư mục.

## An toàn tương thích
- Duy trì full-snapshot UPDATE hỗ trợ trực tiếp v1.5.6.41, v1.5.7.0, v1.5.8.0 và v1.5.9.0–v1.5.9.11.
- Giữ nguyên .venv, projects, autosave, user_settings.json, assets/user, downloads, renders, logs và _backups.

## Kiểm thử
- Chạy regression Download/Batch/Branding, Batch CTA, Export layout và Global UI theme.
- Compile Python modules, build FULL/UPDATE, xác minh ZIP, SHA-256 và danh sách file bảo toàn dữ liệu.
