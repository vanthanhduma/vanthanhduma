# v1.5.9.11 — Full Compatibility Bridge

## Mục tiêu
- Khắc phục trường hợp máy đang ở v1.5.6.41 không thể nâng cấp trực tiếp vì Stable feed chỉ cung cấp gói UPDATE dành riêng cho v1.5.9.9.
- Phát hành một gói UPDATE full snapshot, chứa toàn bộ file ứng dụng cần thiết của v1.5.9.11.
- Hỗ trợ nâng cấp trực tiếp từ v1.5.6.41, v1.5.7.0, v1.5.8.0 và các phiên bản v1.5.9.0 đến v1.5.9.10.

## An toàn dữ liệu
- Giữ nguyên .venv, projects, autosave, user_settings.json, assets/user, downloads, renders, logs và _backups.
- Không thay đổi project schema hoặc di chuyển dự án.
- Giữ xác minh HTTPS + SHA-256 cho gói và từng file payload.
- Giữ transaction backup, health-check và rollback của updater.

## Kiểm thử phát hành
- Compile các module ứng dụng và updater.
- Chạy các bộ kiểm thử Batch CTA, Export layout, Global UI theme và Download/Batch/Branding workspace.
- Kiểm tra ZIP toàn vẹn; xác minh kích thước/SHA-256 mọi file payload.
- Kiểm tra manifest có danh sách base version tương thích và không chứa bất kỳ đường dẫn dữ liệu người dùng nào.

## Cách cập nhật
- Dùng nút **Kiểm tra & cập nhật** trong ứng dụng đang ở một phiên bản được hỗ trợ.
- Không giải nén thủ công UPDATE đè lên thư mục cài đặt.
