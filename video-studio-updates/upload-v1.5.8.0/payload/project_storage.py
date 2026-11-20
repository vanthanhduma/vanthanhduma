from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

WORKSPACE_MARKER = ".goc_review_project.json"
WORKSPACE_DIRS = (
    "Media",
    "Analysis/CTA/thumbnails",
    "Assets",
    "Exports",
    "Backups",
)


def safe_project_name(value: str, fallback: str = "DuAn_VideoStudio") -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:100] or fallback)


def _is_video_source_folder(name: str) -> bool:
    compact = re.sub(r"\s+", " ", str(name or "").strip().casefold())
    return compact in {
        "video nguồn", "video nguon", "videos nguồn", "videos nguon",
        "video source", "video sources", "source videos", "source video",
    }


def suggested_storage_root(video_path: str | Path | None) -> Path:
    """Return the project-library folder beside the user's video-source/output folders."""
    if video_path:
        parent = Path(video_path).expanduser().resolve().parent
        base = parent.parent if _is_video_source_folder(parent.name) else parent
    else:
        base = Path.home() / "Videos"
    return base / "dự án lưu trữ"


def suggested_project_file(video_path: str | Path) -> Path:
    video = Path(video_path).expanduser().resolve()
    name = safe_project_name(video.stem)
    return suggested_storage_root(video) / name / f"{name}.grproj"


def project_workspace_dir(project_path: str | Path) -> Path:
    """Resolve the data workspace for a .grproj, including legacy single-file projects.

    New projects live in <library>/<Project>/<Project>.grproj, so their workspace
    is the containing directory. Older projects remain supported and get a
    sibling <Project>_Data directory without moving or rewriting their .grproj.
    """
    project_file = Path(project_path).expanduser().resolve()
    parent = project_file.parent
    if parent.name.casefold() == project_file.stem.casefold():
        return parent
    if (parent / WORKSPACE_MARKER).is_file() or (parent / "Analysis").is_dir() or (parent / "Assets").is_dir():
        return parent
    return parent / f"{safe_project_name(project_file.stem)}_Data"


def ensure_project_workspace(project_path: str | Path) -> Path:
    root = project_workspace_dir(project_path)
    root.mkdir(parents=True, exist_ok=True)
    for relative in WORKSPACE_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    marker = root / WORKSPACE_MARKER
    if not marker.exists():
        marker.write_text(json.dumps({
            "format": "goc-review-phim-project-workspace",
            "schema_version": 1,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def default_output_directory(video_path: str | Path) -> Path:
    """Use the sibling 'video xuất' folder when the source lives in 'video nguồn'."""
    video = Path(video_path).expanduser().resolve()
    source_dir = video.parent
    if _is_video_source_folder(source_dir.name):
        return source_dir.parent / "video xuất"
    return source_dir


def default_output_directory_for_project(project_path: str | Path, video_path: str | Path) -> Path:
    """Resolve the standard sibling output folder for a project and its source.

    New workspaces are stored under <library>/dự án lưu trữ/<project>/.
    When a project archive is moved, its default exports should still go to
    <library>/video xuất instead of being written into Media/ by accident.
    """
    project_file = Path(project_path).expanduser().resolve()
    parent = project_file.parent
    library_folder = parent.parent
    if library_folder.name.casefold() in {"dự án lưu trữ", "du an luu tru"}:
        return library_folder.parent / "video xuất"
    return default_output_directory(video_path)


def _resolve_record(record: Any, project_dir: Path) -> Path | None:
    if not record:
        return None
    candidates: list[Path] = []
    if isinstance(record, str):
        candidates.append(Path(record).expanduser())
    elif isinstance(record, dict):
        relative = record.get("relative")
        absolute = record.get("absolute")
        if relative:
            p = Path(str(relative)).expanduser()
            candidates.append(p if p.is_absolute() else project_dir / p)
        if absolute:
            candidates.append(Path(str(absolute)).expanduser())
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if resolved.is_file():
                return resolved
        except (OSError, RuntimeError):
            continue
    return None


def _unique_asset_name(source: Path, destination_dir: Path) -> str:
    """Keep asset names readable while avoiding silent collisions after edits."""
    candidate = source.name
    target = destination_dir / candidate
    if not target.exists():
        return candidate
    try:
        if target.is_file() and target.read_bytes() == source.read_bytes():
            return candidate
        content_hash = hashlib.sha256(source.read_bytes()).hexdigest()[:10]
    except OSError:
        content_hash = hashlib.sha256(str(source.resolve()).encode("utf-8", "replace")).hexdigest()[:10]
    return f"{source.stem}_{content_hash}{source.suffix}"


def video_fingerprint(video_path: str | Path) -> str:
    """Return a path-independent fingerprint using file size and sampled content.

    Reading only the first, middle and final 256 KiB keeps project opening fast
    even for multi-gigabyte source videos, while allowing CTA caches to survive
    a project/video move to another drive or computer.
    """
    path = Path(video_path).expanduser().resolve()
    stat = path.stat()
    size = int(stat.st_size)
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    digest.update(b"|goc-review-video-fingerprint-v1|")
    sample_size = min(256 * 1024, size)
    offsets = sorted({0, max(0, (size - sample_size) // 2), max(0, size - sample_size)})
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            chunk = handle.read(sample_size)
            digest.update(str(offset).encode("ascii"))
            digest.update(b":")
            digest.update(chunk)
    return digest.hexdigest()


def project_archive_directory(project_path: str | Path,
                              video_path: str | Path | None = None) -> Path:
    """Keep ZIP snapshots in one clearly named folder beside the project library."""
    project_file = Path(project_path).expanduser().resolve()
    video = Path(video_path).expanduser().resolve() if video_path else None
    if video is None or not video.is_file():
        try:
            payload = json.loads(project_file.read_text(encoding="utf-8"))
            video = _resolve_record((payload.get("media") or {}).get("video"), project_file.parent)
        except Exception:
            video = None
    if video is not None and video.is_file():
        library_root = suggested_storage_root(video)
    else:
        workspace = project_workspace_dir(project_file)
        library_root = workspace.parent
        if library_root.name.casefold() not in {"dự án lưu trữ", "du an luu tru"}:
            library_root = library_root / "dự án lưu trữ"
    return library_root / "Gói ZIP" / safe_project_name(project_file.stem)


def sync_project_assets(payload: dict, project_path: str | Path) -> dict:
    """Copy logo assets into the project workspace and rewrite records to portable paths."""
    target = Path(project_path).expanduser().resolve()
    workspace = ensure_project_workspace(target)
    assets_dir = workspace / "Assets"
    project_dir = target.parent

    def sync(record: Any) -> Any:
        source = _resolve_record(record, project_dir)
        if source is None:
            return record
        name = _unique_asset_name(source, assets_dir)
        destination = assets_dir / name
        if source.resolve() != destination.resolve():
            copy_needed = not destination.is_file()
            if not copy_needed:
                try:
                    copy_needed = hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest()
                except OSError:
                    copy_needed = True
            if copy_needed:
                shutil.copy2(source, destination)
        relative = os.path.relpath(destination, project_dir)
        return {"absolute": str(destination), "relative": relative}

    media = payload.get("media")
    if isinstance(media, dict):
        media["logo"] = sync(media.get("logo"))
    for scene in payload.get("scenes", []) or []:
        if isinstance(scene, dict) and scene.get("branding_logo"):
            scene["branding_logo"] = sync(scene.get("branding_logo"))
    return payload


def list_project_backups(project_path: str | Path, keep: int = 8) -> list[Path]:
    """Return the newest manual-save snapshots for a project, newest first."""
    target = Path(project_path).expanduser().resolve()
    if not target.is_file():
        return []
    backup_dir = project_workspace_dir(target) / "Backups"
    if not backup_dir.is_dir():
        return []
    # Filenames carry a local timestamp; using them avoids filesystem mtime
    # resolution quirks and remains stable after copying a project to another disk.
    prefix = f"{safe_project_name(target.stem)}_"
    snapshots = [
        item for item in backup_dir.glob(f"{prefix}*.grproj")
        if re.fullmatch(r"\d{8}_\d{6}(?:_\d+)*", item.stem[len(prefix):])
    ]
    snapshots.sort(key=lambda item: item.name, reverse=True)
    return snapshots[:max(1, int(keep))]


def backup_project_file(project_path: str | Path, keep: int = 8) -> Path | None:
    """Keep up to eight distinct manual-save snapshots; autosaves never enter this rotation."""
    target = Path(project_path).expanduser().resolve()
    if not target.is_file() or target.name.endswith(".autosave.grproj"):
        return None
    workspace = ensure_project_workspace(target)
    backup_dir = workspace / "Backups"
    snapshots = list_project_backups(target, keep=max(keep, 100000))
    # A repeated Ctrl+S without any content change should not create duplicate snapshots.
    if snapshots:
        try:
            unchanged = hashlib.sha256(snapshots[0].read_bytes()).digest() == hashlib.sha256(target.read_bytes()).digest()
        except OSError:
            unchanged = False
        if unchanged:
            for old in snapshots[max(1, int(keep)):]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return snapshots[0]
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1_000_000_000:09d}"
    backup = backup_dir / f"{safe_project_name(target.stem)}_{stamp}.grproj"
    # Avoid same-second collisions without overwriting an earlier snapshot.
    suffix = 2
    while backup.exists():
        backup = backup_dir / f"{safe_project_name(target.stem)}_{stamp}_{suffix}.grproj"
        suffix += 1
    shutil.copy2(target, backup)
    # copy2 preserves source mtime; reset it to snapshot creation time for a
    # meaningful timestamp in the history UI and deterministic rotation.
    try:
        os.utime(backup, None)
    except OSError:
        pass
    snapshots = list_project_backups(target, keep=max(keep, 100000))
    for old in snapshots[max(1, int(keep)):]:
        try:
            old.unlink()
        except OSError:
            pass
    return backup


def restore_project_backup(snapshot_path: str | Path, project_path: str | Path,
                           destination_root: str | Path | None = None) -> Path:
    """Restore a manual snapshot as a separate project without overwriting the active project."""
    snapshot = Path(snapshot_path).expanduser().resolve()
    source_project = Path(project_path).expanduser().resolve()
    if not snapshot.is_file():
        raise FileNotFoundError(f"Không tìm thấy bản sao lưu: {snapshot}")
    if not source_project.is_file():
        raise FileNotFoundError(f"Không tìm thấy dự án gốc: {source_project}")

    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format") != "goc-review-phim-video-studio-project":
        raise ValueError("Bản sao lưu không đúng định dạng dự án Video Studio.")

    source_root = source_project.parent
    source_workspace = project_workspace_dir(source_project)
    root = Path(destination_root).expanduser().resolve() if destination_root else source_root / "Phục hồi"
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base_name = safe_project_name(source_project.stem)
    recovered_name = f"{base_name}_Phuc_hoi_{stamp}"
    recovered_dir = root / recovered_name
    suffix = 2
    while recovered_dir.exists():
        recovered_dir = root / f"{recovered_name}_{suffix}"
        suffix += 1
    recovered_dir.mkdir(parents=True, exist_ok=False)
    recovered_project = recovered_dir / f"{recovered_dir.name}.grproj"

    media = payload.setdefault("media", {})
    if not isinstance(media, dict):
        raise ValueError("Dữ liệu media trong bản sao lưu không hợp lệ.")

    def relocate_record(record: Any) -> Any:
        resolved = _resolve_record(record, source_root)
        if resolved is None:
            return record
        return {
            "absolute": str(resolved),
            "relative": os.path.relpath(resolved, recovered_dir),
        }

    video_source = _resolve_record(media.get("video"), source_root)
    media["video"] = relocate_record(media.get("video"))
    media["logo"] = relocate_record(media.get("logo"))
    media["output_dir"] = relocate_record(media.get("output_dir"))
    for scene in payload.get("scenes", []) or []:
        if isinstance(scene, dict) and scene.get("branding_logo"):
            scene["branding_logo"] = relocate_record(scene.get("branding_logo"))

    ensure_project_workspace(recovered_project)
    # Preserve detector evidence/checkpoints with the recovered editing state.
    analysis_source = source_workspace / "Analysis"
    if analysis_source.is_dir():
        shutil.copytree(
            analysis_source, project_workspace_dir(recovered_project) / "Analysis",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("*.tmp", "*.part", "*.lock"),
        )
    payload = sync_project_assets(payload, recovered_project)
    if not bool(media.get("output_dir_custom", False)) and video_source and video_source.is_file():
        output_dir = default_output_directory_for_project(recovered_project, video_source)
        output_dir.mkdir(parents=True, exist_ok=True)
        media["output_dir"] = {
            "absolute": str(output_dir.resolve()),
            "relative": os.path.relpath(output_dir.resolve(), recovered_dir),
        }
    temporary = recovered_project.with_name(recovered_project.name + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, recovered_project)
    finally:
        temporary.unlink(missing_ok=True)
    return recovered_project


def create_project_archive(project_path: str | Path, video_path: str | Path | None = None,
                           include_video: bool = True) -> Path:
    """Create a portable ZIP snapshot of the complete project workspace."""
    project_file = Path(project_path).expanduser().resolve()
    if not project_file.is_file():
        raise FileNotFoundError(f"Không tìm thấy file dự án: {project_file}")
    workspace = ensure_project_workspace(project_file)
    project_name = safe_project_name(project_file.stem)
    payload = json.loads(project_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format") != "goc-review-phim-video-studio-project":
        raise ValueError("File .grproj không đúng định dạng dự án Video Studio.")

    video_source = Path(video_path).expanduser().resolve() if video_path else None
    if video_source is None or not video_source.is_file():
        video_source = _resolve_record((payload.get("media") or {}).get("video"), project_file.parent)
    if include_video and (video_source is None or not video_source.is_file()):
        raise FileNotFoundError("Không tìm thấy video nguồn để đưa vào gói ZIP.")

    archive_dir = project_archive_directory(project_file, video_path)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    archive_path = archive_dir / f"{project_name}_{stamp}.zip"
    suffix = 2
    while archive_path.exists():
        archive_path = archive_dir / f"{project_name}_{stamp}_{suffix}.zip"
        suffix += 1

    with tempfile.TemporaryDirectory(prefix="goc_review_package_") as temporary:
        stage_root = Path(temporary) / project_name
        stage_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(project_file, stage_root / project_file.name)

        for folder_name in ("Media", "Analysis", "Assets", "Exports", "Backups"):
            source_dir = workspace / folder_name
            destination_dir = stage_root / folder_name
            if source_dir.is_dir():
                shutil.copytree(source_dir, destination_dir, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("*.tmp", "*.part", "*.lock", "*.zip"))
            else:
                destination_dir.mkdir(parents=True, exist_ok=True)

        # Include project-specific logo files and make their references relative.
        media = payload.setdefault("media", {})
        assets_dir = stage_root / "Assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        def package_asset(record: Any) -> Any:
            source = _resolve_record(record, project_file.parent)
            if source is None:
                # If it is already a relative project asset, retain the record.
                return record
            name = _unique_asset_name(source, assets_dir)
            destination = assets_dir / name
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
            return {"absolute": "", "relative": f"Assets/{name}"}

        if media.get("logo"):
            media["logo"] = package_asset(media.get("logo"))
        for scene in payload.get("scenes", []) or []:
            if isinstance(scene, dict) and scene.get("branding_logo"):
                scene["branding_logo"] = package_asset(scene.get("branding_logo"))

        if include_video and video_source is not None:
            media_dir = stage_root / "Media"
            media_dir.mkdir(parents=True, exist_ok=True)
            video_name = video_source.name
            video_destination = media_dir / video_name
            if video_source.resolve() != video_destination.resolve():
                shutil.copy2(video_source, video_destination)
            media["video"] = {"absolute": "", "relative": f"Media/{video_name}"}
        if not bool(media.get("output_dir_custom", False)):
            media["output_dir"] = {"absolute": "", "relative": "../../video xuất"}

        # Archive paths are intentionally relative and include a single project root.
        packaged_project = stage_root / project_file.name
        packaged_project.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        # A backup .grproj is opened from Backups/, so its relative media/asset
        # references must be adjusted for that extra directory level too.
        backup_root = stage_root / "Backups"
        for backup_file in backup_root.glob("*.grproj"):
            try:
                backup_payload = json.loads(backup_file.read_text(encoding="utf-8"))
                backup_media = backup_payload.get("media", {})
                if not isinstance(backup_media, dict):
                    continue
                if include_video and video_source is not None:
                    backup_media["video"] = {"absolute": "", "relative": f"../Media/{video_source.name}"}
                else:
                    old_video = backup_media.get("video")
                    if isinstance(old_video, dict) and old_video.get("relative"):
                        backup_media["video"] = {
                            "absolute": str(old_video.get("absolute", "") or ""),
                            "relative": os.path.join("..", str(old_video["relative"]).replace("\\", "/")),
                        }
                if backup_media.get("logo"):
                    backup_logo = package_asset(backup_media.get("logo"))
                    if isinstance(backup_logo, dict) and backup_logo.get("relative"):
                        backup_logo["relative"] = "../" + str(backup_logo["relative"])
                    backup_media["logo"] = backup_logo
                for backup_scene in backup_payload.get("scenes", []) or []:
                    if isinstance(backup_scene, dict) and backup_scene.get("branding_logo"):
                        record = package_asset(backup_scene.get("branding_logo"))
                        if isinstance(record, dict) and record.get("relative"):
                            record["relative"] = "../" + str(record["relative"])
                        backup_scene["branding_logo"] = record
                if not bool(backup_media.get("output_dir_custom", False)):
                    backup_media["output_dir"] = {"absolute": "", "relative": "../../../video xuất"}
                backup_file.write_text(json.dumps(backup_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # A corrupt historical backup should not prevent the current
                # project from being archived; retain it unchanged for recovery.
                continue

        media_note = (
            f"Video nguồn đã được đóng gói tại Media/{video_source.name}.\n"
            if include_video and video_source else
            "Video nguồn không nằm trong ZIP. Khi chuyển sang máy khác, hãy đặt video ở vị trí cũ hoặc liên kết lại khi mở dự án.\n"
        )
        (stage_root / "README_DU_AN.txt").write_text(
            "GÓC REVIEW PHIM VIDEO STUDIO - PROJECT ARCHIVE\n\n"
            f"Dự án: {project_name}\n"
            f"Đóng gói: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            + media_note
            + "Analysis/CTA chứa kết quả nhận diện, checkpoint và thumbnail.\n"
            + "Assets chứa logo/tài nguyên riêng của dự án.\n"
            + "File .grproj là điểm mở dự án.\n",
            encoding="utf-8",
        )
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_archive = archive_path.with_name(archive_path.name + ".tmp")
        try:
            with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=True) as archive:
                for item in sorted(stage_root.rglob("*")):
                    if item.is_file():
                        archive.write(item, item.relative_to(stage_root.parent).as_posix())
            os.replace(temporary_archive, archive_path)
        finally:
            temporary_archive.unlink(missing_ok=True)

    return archive_path


def restore_project_archive(archive_path: str | Path, destination_root: str | Path) -> Path:
    """Safely extract a project archive without path traversal or overwriting projects."""
    archive_path = Path(archive_path).expanduser().resolve()
    destination_root = Path(destination_root).expanduser().resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy gói ZIP: {archive_path}")
    destination_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive_path, "r") as archive:
        all_members = archive.infolist()
        if len(all_members) > 20000:
            raise ValueError("Gói ZIP có quá nhiều mục; đã hủy khôi phục để bảo vệ hệ thống.")
        members = [item for item in all_members if not item.is_dir()]
        expanded_total = sum(int(item.file_size) for item in members)
        if expanded_total > 100 * 1024 ** 3:
            raise ValueError("Dung lượng giải nén của gói ZIP vượt giới hạn an toàn 100 GB.")
        seen_names = set()
        for item in members:
            normalized_name = item.filename.replace("\\", "/").casefold()
            if normalized_name in seen_names:
                raise ValueError("Gói ZIP có đường dẫn trùng lặp; đã hủy khôi phục.")
            seen_names.add(normalized_name)
            if item.flag_bits & 0x1:
                raise ValueError("Gói ZIP được mã hóa bằng mật khẩu; định dạng này không được hỗ trợ.")
            if item.file_size > 80 * 1024 ** 3:
                raise ValueError("Một file trong gói ZIP vượt giới hạn an toàn 80 GB.")
            mode = (item.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError("Gói ZIP chứa symbolic link; đã hủy khôi phục.")
        # Historical .grproj snapshots live under Backups/ and are not entry points.
        project_files = [
            item for item in members
            if item.filename.lower().endswith(".grproj")
            and "backups" not in {part.casefold() for part in PurePosixPath(item.filename.replace("\\", "/")).parts}
        ]
        if len(project_files) != 1:
            raise ValueError("Gói ZIP phải chứa đúng một file dự án chính .grproj (không tính bản trong Backups).")
        for item in members:
            name = item.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if pure.is_absolute() or any(part in {"..", "", "."} for part in pure.parts):
                raise ValueError("Gói ZIP chứa đường dẫn không an toàn; đã hủy khôi phục.")
            if pure.parts and ":" in pure.parts[0]:
                raise ValueError("Gói ZIP chứa đường dẫn ổ đĩa không hợp lệ.")
            reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
            for part in pure.parts:
                device = part.split(".", 1)[0].upper()
                if part.endswith((".", " ")) or device in reserved:
                    raise ValueError("Gói ZIP chứa tên file không hợp lệ trên Windows.")

        project_member = project_files[0]
        parts = PurePosixPath(project_member.filename).parts
        folder_name = safe_project_name(parts[0] if len(parts) > 1 else archive_path.stem)
        destination = destination_root / folder_name
        index = 2
        while destination.exists():
            destination = destination_root / f"{folder_name}_{index}"
            index += 1
        destination.mkdir(parents=True, exist_ok=False)
        try:
            for item in members:
                relative = PurePosixPath(item.filename)
                # Strip the archive's common top-level folder if one exists.
                if len(relative.parts) > 1 and relative.parts[0] == parts[0]:
                    relative = PurePosixPath(*relative.parts[1:])
                target = destination.joinpath(*relative.parts)
                resolved_target = target.resolve()
                if destination.resolve() not in resolved_target.parents:
                    raise ValueError("Gói ZIP có đường dẫn thoát khỏi thư mục khôi phục.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item, "r") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            project_relative = PurePosixPath(project_member.filename)
            if len(project_relative.parts) > 1 and project_relative.parts[0] == parts[0]:
                project_relative = PurePosixPath(*project_relative.parts[1:])
            restored_project = destination.joinpath(*project_relative.parts)
            if not restored_project.is_file():
                raise FileNotFoundError("Không tìm thấy file .grproj sau khi giải nén.")
            ensure_project_workspace(restored_project)
            return restored_project
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
