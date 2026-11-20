from __future__ import annotations

import json
import hashlib
import os
import sys
import time
import threading
import subprocess
import shutil
import re
from pathlib import Path
from itertools import count

from timeline_seek import parse_timecode, format_media_time as _format_media_time
from project_storage import (
    suggested_storage_root, suggested_project_file, default_output_directory, default_output_directory_for_project, ensure_project_workspace,
    project_workspace_dir, project_archive_directory, video_fingerprint,
    create_project_archive, restore_project_archive, list_project_backups,
    restore_project_backup,
)
from cta_merge import merge_boundary_candidate
from updater.qt_ui import UpdateDialog
from updater.state import should_auto_check, mark_checked
from updater.apply_helper import recover_incomplete_transactions

from PySide6.QtCore import Qt, QProcess, QTimer, QSize, Signal, QRectF, QPointF, QObject, QThread
from PySide6.QtGui import QPixmap, QPainter, QPainterPath, QPen, QColor, QFont, QFontDatabase, QIcon, QKeySequence, QShortcut, QPalette, QValidator
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QToolButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QSplitter, QScrollArea, QTabWidget,
    QSlider, QSpinBox, QDoubleSpinBox, QCheckBox, QLineEdit, QFileDialog,
    QMessageBox, QProgressBar, QSizePolicy, QStackedWidget, QComboBox,
    QButtonGroup, QGroupBox, QSpacerItem, QDialog, QDialogButtonBox, QFormLayout, QMenu,
    QInputDialog, QColorDialog, QStyle, QStyleOptionSlider, QListWidget, QListWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)

APP_DIR = Path(__file__).resolve().parent


def _load_app_version() -> str:
    """Read app version from PHIEN_BAN.txt so future updates can be tiny."""
    try:
        version_text = (APP_DIR / "PHIEN_BAN.txt").read_text(encoding="utf-8", errors="replace")
        match = re.search(r"v?(\d+\.\d+\.\d+\.\d+)", version_text)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "1.5.8.0"


APP_VERSION = _load_app_version()
WORKER_SCRIPT = APP_DIR / "engine_worker.py"
ASSETS = APP_DIR / "assets"

BG = "#07131c"
PANEL = "#0e202b"
PANEL2 = "#132b38"
PANEL3 = "#193d4d"
BORDER = "#214958"
TEXT = "#edf7f8"
MUTED = "#9db6bf"
TEAL = "#19d4d5"
TEAL_HOVER = "#5ce6e3"
DANGER = "#df6179"
SIDEBAR_MIN_WIDTH = 238
SIDEBAR_WIDTH = 256
SIDEBAR_MAX_WIDTH = 268


def _qt_button_label(text: str) -> str:
    """Escape literal ampersands so Qt does not treat them as mnemonics."""
    return str(text).replace("&", "&&")


class MinutePositionSpinBox(QDoubleSpinBox):
    """Keep minute-based storage for compatibility, display/edit as MM:SS."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDecimals(8)
        self.setSingleStep(1.0 / 60.0)
        self.setKeyboardTracking(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def textFromValue(self, value: float) -> str:
        return _format_media_time(float(value) * 60.0)

    def validate(self, text: str, position: int):
        raw = str(text or '').strip().replace(',', '.')
        if not raw:
            return (QValidator.State.Intermediate, text, position)
        if ':' not in raw:
            return super().validate(text, position)
        match = re.fullmatch(r"(\d{1,7}):(\d{0,2})(?:\.(\d+))?", raw)
        if match:
            seconds_text = match.group(2)
            if not seconds_text:
                return (QValidator.State.Intermediate, text, position)
            try:
                seconds = float(seconds_text + ('.' + match.group(3) if match.group(3) else ''))
                if seconds < 60:
                    return (QValidator.State.Acceptable, text, position)
                return (QValidator.State.Invalid, text, position)
            except ValueError:
                return (QValidator.State.Invalid, text, position)
        match = re.fullmatch(r"(\d{1,4}):(\d{0,2}):(\d{0,2})(?:\.(\d+))?", raw)
        if match:
            hour_text, minute_text, second_text, fraction = match.groups()
            if not minute_text or not second_text:
                return (QValidator.State.Intermediate, text, position)
            try:
                minute, second = int(minute_text), float(second_text + ('.' + fraction if fraction else ''))
                if minute < 60 and second < 60:
                    return (QValidator.State.Acceptable, text, position)
                return (QValidator.State.Invalid, text, position)
            except ValueError:
                return (QValidator.State.Invalid, text, position)
        if re.fullmatch(r"\d{0,7}:?\d{0,2}(?:\.\d*)?", raw):
            return (QValidator.State.Intermediate, text, position)
        return (QValidator.State.Invalid, text, position)

    def valueFromText(self, text: str) -> float:
        raw = str(text or "").strip().replace(",", ".")
        if ":" in raw:
            parts = raw.split(":")
            try:
                if len(parts) == 2:
                    minutes = int(parts[0])
                    seconds = float(parts[1])
                    if minutes < 0 or seconds < 0 or seconds >= 60:
                        raise ValueError
                    return minutes + seconds / 60.0
                if len(parts) == 3:
                    hours = int(parts[0])
                    minutes = int(parts[1])
                    seconds = float(parts[2])
                    if hours < 0 or minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60:
                        raise ValueError
                    return hours * 60.0 + minutes + seconds / 60.0
            except ValueError:
                return self.value()
        # Retain compatibility with existing project/config values entered as decimal minutes.
        try:
            value = float(raw)
            return value if value >= 0 else self.value()
        except ValueError:
            return self.value()


QSS = f"""
QWidget {{
  background: {BG}; color: {TEXT};
  font-size: 9.8pt;
  selection-background-color: #145a66; selection-color: #f4feff;
}}
QMainWindow {{ background: {BG}; }}
QDialog {{ background: #0b1d29; }}
QFrame#Header {{
  background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #0b1f2b, stop:0.55 #102c3a, stop:1 #0a1b26);
  border: 1px solid #1e4654; border-radius: 14px;
}}
QFrame#ProjectBar {{
  background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #132c39, stop:1 #102532);
  border: 1px solid #285463; border-radius: 14px;
}}
QFrame#Sidebar {{
  background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #102632, stop:1 #0b1c26);
  border: 1px solid #1f4654; border-radius: 14px;
}}
QFrame#Inspector {{
  background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #112b38, stop:1 #0d202b);
  border: 1px solid #285462; border-radius: 14px;
}}
QFrame#CenterPanel {{ background: #091923; border: 1px solid #173b49; border-radius: 14px; }}
QFrame#StatusBar {{
  background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #102834, stop:1 #0d222d);
  border: 1px solid #214958; border-radius: 11px;
}}
QFrame#ProjectCard {{ background: #132d39; border: 1px solid #285462; border-radius: 12px; }}
QFrame#PreviewFrame {{ background: #02090e; border: 1px solid #2a6270; border-radius: 14px; }}
QLabel#BrandTitle {{ font-size: 15.5pt; font-weight: 750; letter-spacing: 0.1px; color: #f4fbfc; }}
QLabel#SectionTitle {{ font-size: 9.4pt; font-weight: 750; color: #cfe2e7; padding: 2px 0 5px 0; }}
QLabel#ProjectTitle {{ font-size: 12pt; font-weight: 750; color: #f4fbfc; }}
QLabel#ProjectKicker {{ color: #6ee7e2; font-size: 7.5pt; font-weight: 800; letter-spacing: 1px; }}
QLabel#VersionBadge {{ color: #a9dce0; background: #0a202a; border: 1px solid #235361; border-radius: 8px; padding: 5px 8px; font-size: 8.5pt; font-weight: 650; }}
QLabel#Muted {{ color: #9bb3c0; }}
QLabel#SaveState {{ color: #65d8b0; font-weight: 650; }}
QPushButton, QToolButton {{
  background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #1a3a49, stop:1 #132d3b);
  border: 1px solid #2b5b6b; border-radius: 10px;
  padding: 8px 14px; color: #e9f5f6; font-weight: 650; min-height: 36px;
}}
QPushButton:hover, QToolButton:hover {{
  background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #235064, stop:1 #193c4c);
  border-color: #4aa7ad; color: #f5ffff;
}}
QPushButton:pressed, QToolButton:pressed {{ background: #174052; border-color: #3b9eaa; }}
QPushButton:checked, QToolButton:checked {{
  background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #15515c, stop:1 #103b49);
  border: 1px solid {TEAL}; color: #f2ffff;
}}
QPushButton:disabled, QToolButton:disabled {{ background: #10212b; border-color: #203a49; color: #6e8794; }}
QPushButton#Primary {{
  background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #21d5dc, stop:1 #10b9c3);
  color: #04212a; border: 1px solid #38e3e7; font-weight: 750;
}}
QPushButton#Primary:hover {{
  background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #48e6e8, stop:1 #1bcbd2);
  border-color: #70f1ef; color: #031c24;
}}
QPushButton#Primary:pressed {{ background: #0ea7b1; border-color: #28cbd1; }}
QPushButton#Secondary {{
  background: #163a49; border-color: #2b5b6b; color: #e9f5f6;
}}
QPushButton#Secondary:hover {{ background: #235064; border-color: #5bcfd0; }}
QPushButton#Destructive {{
  background: #4a2936; border-color: #9b5266; color: #ffe9ed;
}}
QPushButton#Destructive:hover {{ background: #633342; border-color: #dd7990; }}
QPushButton#Tertiary {{
  background: transparent; border-color: transparent; color: #a9c4cc;
  padding-left: 9px; padding-right: 9px;
}}
QPushButton#Tertiary:hover {{ background: #122d3a; border-color: #285665; color: #efffff; }}
QPushButton#Success {{
  background: #1c9d82; border-color: #59dfbd; color: #041c1b; font-weight: 750;
}}
QPushButton#Success:hover {{ background: #2ab99b; border-color: #8af4d9; }}
QPushButton#Nav {{
  text-align: left; min-height: 44px; padding: 8px 10px;
  border: 1px solid transparent; background: transparent; border-radius: 10px;
  font-size: 9.7pt; font-weight: 550;
}}
QPushButton#Nav:hover {{ background: #153543; border-color: #285665; }}
QPushButton#Nav:checked {{
  background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #17616a, stop:1 #123d4b);
  border: 1px solid #39c7c8; border-left: 3px solid {TEAL_HOVER}; color: #c4fffc; font-weight: 750;
}}
QPushButton#Tool {{ min-height: 43px; }}
QPushButton#Danger {{ background: #472a35; border-color: #854b5a; color: #ffe9ed; }}
QPushButton#Danger:hover {{ background: #59313f; border-color: #b45e72; }}
QPushButton#Quiet {{ background: transparent; border: 1px solid transparent; min-width: 32px; padding-left: 6px; padding-right: 6px; }}
QTabWidget::pane {{ border: none; top: 0px; }}
QTabBar::tab {{ background: transparent; border: none; border-bottom: 2px solid transparent; padding: 10px 13px; margin-right: 4px; color: #9ab2bf; min-width: 72px; }}
QTabBar::tab:selected {{ background: #123b49; color: #f4feff; border-bottom: 2px solid {TEAL}; border-top-left-radius: 8px; border-top-right-radius: 8px; }}
QSlider::groove:horizontal {{ height: 5px; background: #254858; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {TEAL}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {TEAL_HOVER}; border: 1px solid #d8ffff; width: 15px; margin: -5px 0; border-radius: 7px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
  background: #102a38; border: 1px solid #2c6173; border-radius: 8px;
  padding: 8px 10px; color: #f1fbfc; selection-background-color: #176575;
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{ border-color: #357286; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {TEAL}; background: #15394a; }}
QLineEdit[readOnly="true"] {{ background: #0f2734; color: #d9edf0; }}
QLineEdit::placeholder {{ color: #86aab4; }}
QComboBox QAbstractItemView {{
  background: #102b38; color: #edf7f8; border: 1px solid #2b6473;
  selection-background-color: #17616a; selection-color: #f4ffff;
}}
QTableWidget {{
  background: #0d2431; alternate-background-color: #12303e;
  color: #e8f4f6; border: 1px solid #214958; border-radius: 10px;
  gridline-color: #173b49; selection-background-color: #17616a;
  selection-color: #f4ffff; outline: none;
}}
QTableWidget::item {{ padding: 8px 7px; border-bottom: 1px solid #153847; }}
QHeaderView::section {{
  background: #132d39; color: #b9d1d8; border: none;
  border-bottom: 1px solid #2b6473; padding: 10px 8px;
  font-size: 8.5pt; font-weight: 750;
}}
QLabel#CtaPreview {{
  background: #02090e; border: 2px solid #2a9ca4; border-radius: 14px;
  color: #91abb6; padding: 8px; font-size: 10pt;
}}
QLabel#CtaCaption {{
  background: #102733; border: 1px solid #285462; border-radius: 10px;
  color: #d7eaed; padding: 7px 9px; font-size: 8.8pt;
}}
/* CTA uses an explicit Vietnamese-safe UI font and normal weights so text in
   the result table remains readable on machines with a different system font. */
QTableWidget#CtaTable {{
  font-size: 10pt; font-weight: 500;
}}
QTableWidget#CtaTable QHeaderView::section {{
  font-size: 9.5pt; font-weight: 600;
}}
QLabel#CtaCaption, QLabel#CtaPreview {{
  font-weight: 500;
}}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid #527484; border-radius: 4px; background: #081a25; }}
QCheckBox::indicator:checked {{ background: {TEAL}; border: 1px solid {TEAL_HOVER}; }}
QProgressBar {{ background: #1d3d4c; border: none; border-radius: 4px; height: 7px; text-align: center; }}
QProgressBar::chunk {{ background: {TEAL}; border-radius: 4px; }}
QScrollArea {{ border: 1px solid #193f4d; border-radius: 10px; background: #0c202c; }}
QScrollArea > QWidget > QWidget {{ background: #0c202c; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 3px; }}
QScrollBar::handle:vertical {{ background: #315a6a; min-height: 24px; border-radius: 4px; }}
QScrollBar::handle:vertical:hover {{ background: #4a8392; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 3px; }}
QScrollBar::handle:horizontal {{ background: #315a6a; min-width: 24px; border-radius: 4px; }}
QScrollBar::handle:horizontal:hover {{ background: #4a8392; }}
QSplitter::handle {{ background: #153444; width: 5px; height: 5px; }}
QSplitter::handle:hover {{ background: #168b98; }}
QGroupBox {{ border: 1px solid #1c4859; border-radius: 9px; margin-top: 10px; padding: 9px 8px 7px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #c6d9e1; font-weight: 650; }}
QToolTip {{ background: #102d3c; color: #f0f8fb; border: 1px solid #2b6778; padding: 7px 9px; }}
"""


class PreviewCanvas(QWidget):
    selectionReady = Signal(list, str)
    strokeReady = Signal(list, bool)
    videoDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PreviewFrame")
        self.setMinimumSize(420, 250)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.pixmap = QPixmap()
        self.frame_width = 0
        self.frame_height = 0
        self.mode = "lasso"
        self.brush_size = 24
        self.points: list[tuple[float, float]] = []
        self.dragging = False
        self.image_rect = QRectF()
        self._erase = False
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path and Path(path).suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path and Path(path).suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}:
                    self.videoDropped.emit(path)
                    event.acceptProposedAction()
                    return
        event.ignore()

    def set_frame(self, path: str, width: int, height: int):
        if not path or not Path(path).exists():
            self.pixmap = QPixmap()
            self.frame_width = 0
            self.frame_height = 0
            self.update()
            return
        pm = QPixmap(path)
        if not pm.isNull():
            self.pixmap = pm
            self.frame_width = max(1, int(width or pm.width()))
            self.frame_height = max(1, int(height or pm.height()))
            self.update()

    def set_tool(self, mode: str, brush_size: int = 24):
        self.mode = mode
        self.brush_size = max(4, int(brush_size))
        self.points = []
        self.dragging = False
        self.setCursor(Qt.CursorShape.CrossCursor if mode in {"lasso", "rect"} else Qt.CursorShape.PointingHandCursor)

    def _fit_rect(self) -> QRectF:
        if self.pixmap.isNull():
            return QRectF()
        ww, wh = self.width(), self.height()
        scale = min(ww / self.pixmap.width(), wh / self.pixmap.height())
        dw, dh = self.pixmap.width() * scale, self.pixmap.height() * scale
        return QRectF((ww - dw) / 2, (wh - dh) / 2, dw, dh)

    def _to_frame(self, p):
        rect = self.image_rect
        if rect.width() <= 0 or rect.height() <= 0:
            return [0.0, 0.0]
        x = (p.x() - rect.left()) / rect.width() * self.frame_width
        y = (p.y() - rect.top()) / rect.height() * self.frame_height
        return [max(0.0, min(self.frame_width - 1.0, x)), max(0.0, min(self.frame_height - 1.0, y))]

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#030a10"))
        self.image_rect = self._fit_rect()
        if not self.pixmap.isNull():
            painter.drawPixmap(self.image_rect, self.pixmap, self.pixmap.rect())
        else:
            painter.setPen(QColor(TEXT))
            center_x = self.width() / 2
            center_y = self.height() / 2
            painter.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
            painter.drawText(QRectF(0, center_y - 58, self.width(), 28),
                             Qt.AlignmentFlag.AlignCenter, "Bắt đầu bằng cách mở video")
            painter.setFont(QFont("Segoe UI", 9))
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(0, center_y - 27, self.width(), 24),
                             Qt.AlignmentFlag.AlignCenter, "Kéo thả video vào đây · hoặc dùng nút Mở video trên thanh công cụ")
        if self.dragging and self.points:
            pen = QPen(QColor(TEAL), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            if self.mode == "rect" and len(self.points) >= 2:
                a, b = self.points[0], self.points[-1]
                p1 = self._from_frame(a)
                p2 = self._from_frame(b)
                painter.drawRect(QRectF(QPointF(*p1), QPointF(*p2)).normalized())
            else:
                pts = [self._from_frame(p) for p in self.points]
                for a, b in zip(pts, pts[1:]):
                    painter.drawLine(QPointF(*a), QPointF(*b))
                if self.mode == "lasso" and len(pts) > 2:
                    painter.drawLine(QPointF(*pts[-1]), QPointF(*pts[0]))
        painter.end()

    def _from_frame(self, p):
        rect = self.image_rect
        return (rect.left() + p[0] / max(1, self.frame_width) * rect.width(),
                rect.top() + p[1] / max(1, self.frame_height) * rect.height())

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.pixmap.isNull():
            return
        if not self.image_rect.contains(event.position()):
            return
        self.dragging = True
        self.points = [self._to_frame(event.position())]
        self._erase = self.mode == "erase"
        self.update()

    def mouseMoveEvent(self, event):
        if not self.dragging:
            return
        if self.mode in {"lasso", "brush", "erase"}:
            point = self._to_frame(event.position())
            if not self.points or abs(point[0] - self.points[-1][0]) + abs(point[1] - self.points[-1][1]) >= 2:
                self.points.append(point)
        elif self.mode == "rect":
            if len(self.points) == 1:
                self.points.append(self._to_frame(event.position()))
            else:
                self.points[-1] = self._to_frame(event.position())
        self.update()

    def mouseReleaseEvent(self, event):
        if not self.dragging:
            return
        if self.mode == "rect" and self.points:
            end = self._to_frame(event.position())
            start = self.points[0]
            x0, x1 = sorted((start[0], end[0]))
            y0, y1 = sorted((start[1], end[1]))
            points = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            if x1 - x0 >= 5 and y1 - y0 >= 5:
                self.selectionReady.emit(points, "rect")
        elif self.mode == "lasso" and len(self.points) >= 3:
            self.selectionReady.emit(self.points[:], "lasso")
        elif self.mode in {"brush", "erase"} and self.points:
            self.strokeReady.emit(self.points[:], self.mode == "erase")
        self.points = []
        self.dragging = False
        self.update()


class SceneCard(QPushButton):
    def __init__(self, index: int, scene: dict, selected: bool, on_click, parent=None):
        super().__init__(parent)
        self.index = index
        self.setCheckable(True)
        self.setChecked(selected)
        self.setFixedWidth(170)
        self.setMinimumHeight(152)
        self.setObjectName("SceneCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        root = QVBoxLayout(self)
        root.setContentsMargins(7, 7, 7, 7)
        root.setSpacing(5)
        image = QLabel()
        image.setFixedSize(154, 87)
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setStyleSheet("background:#0a1d29; border-radius:6px;")
        thumb = scene.get("thumbnail")
        if thumb and Path(thumb).exists():
            pm = QPixmap(thumb)
            image.setPixmap(pm.scaled(image.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation))
        else:
            image.setText("Đang tải ảnh…")
            image.setStyleSheet(f"background:#0a1d29; color:{MUTED}; border-radius:6px;")
        title = QLabel(f"{index + 1:02d}  {scene.get('name', f'Cảnh {index + 1}')}")
        title.setStyleSheet("font-weight:700; background:transparent;")
        times = QLabel(f"{_format_media_time(scene.get('start', 0))} – {_format_media_time(scene.get('end', 0))}")
        times.setObjectName("Muted")
        times.setStyleSheet(f"color:{MUTED}; font-size:8.5pt; background:transparent;")
        root.addWidget(image)
        root.addWidget(title)
        root.addWidget(times)
        self.clicked.connect(lambda: on_click(index))
        self.setStyleSheet(f"""
          QPushButton#SceneCard {{ text-align:left; background:#102b3a; border:1px solid {'#2aaeb5' if selected else BORDER};
          border-radius:9px; padding:4px; }}
          QPushButton#SceneCard:hover {{ background:#163a49; border:1px solid {TEAL_HOVER}; }}
          QPushButton#SceneCard:checked {{ background:#154552; border:1px solid {TEAL}; }}
        """)


class BrandingPreviewCanvas(QWidget):
    logoMoved = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background:#030a10; border:1px solid #216071; border-radius:10px;")
        self.pixmap = QPixmap()
        self.frame_width = 0
        self.frame_height = 0
        self.logo_rect = None
        self.image_rect = QRectF()
        self._dragging_logo = False
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_preview(self, path: str, frame_width: int, frame_height: int, logo_rect=None):
        pm = QPixmap(path) if path and Path(path).is_file() else QPixmap()
        if not pm.isNull():
            self.pixmap = pm
        self.frame_width = max(1, int(frame_width or pm.width() or 1))
        self.frame_height = max(1, int(frame_height or pm.height() or 1))
        self.logo_rect = tuple(int(x) for x in logo_rect) if logo_rect else None
        self.update()

    def _fit_rect(self):
        if self.pixmap.isNull():
            return QRectF()
        scale = min(self.width() / self.pixmap.width(), self.height() / self.pixmap.height())
        dw, dh = self.pixmap.width() * scale, self.pixmap.height() * scale
        return QRectF((self.width()-dw)/2, (self.height()-dh)/2, dw, dh)

    def _logo_widget_rect(self):
        if not self.logo_rect or self.image_rect.isNull():
            return QRectF()
        x0, y0, x1, y1 = self.logo_rect
        sx = self.image_rect.width() / max(1, self.frame_width)
        sy = self.image_rect.height() / max(1, self.frame_height)
        return QRectF(self.image_rect.left()+x0*sx, self.image_rect.top()+y0*sy,
                      max(1.0, (x1-x0)*sx), max(1.0, (y1-y0)*sy))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#030a10"))
        self.image_rect = self._fit_rect()
        if not self.pixmap.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(self.image_rect, self.pixmap, self.pixmap.rect())
            logo_rect = self._logo_widget_rect()
            if not logo_rect.isNull():
                pen = QPen(QColor(TEAL), 2, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(logo_rect.adjusted(-3, -3, 3, 3), 4, 4)
        else:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Đang tạo Preview Branding…")
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        target = self._logo_widget_rect()
        if not target.isNull() and target.adjusted(-5, -5, 5, 5).contains(event.position()):
            self._dragging_logo = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        if not self._dragging_logo or self.image_rect.isNull():
            return
        x = (event.position().x() - self.image_rect.left()) / max(1.0, self.image_rect.width())
        y = (event.position().y() - self.image_rect.top()) / max(1.0, self.image_rect.height())
        self.logoMoved.emit(max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))
        event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging_logo = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        event.accept()


class FitPixmapLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = QPixmap()
        self._placeholder = "Không có ảnh để hiển thị"
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 220)
        self.setStyleSheet("background:#030a10; border:1px solid #216071; border-radius:10px;")

    def set_source(self, path: str, placeholder: str | None = None):
        if placeholder is not None:
            self._placeholder = str(placeholder)
        self._source = QPixmap(path) if path and Path(path).is_file() else QPixmap()
        self._refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self):
        if self._source.isNull():
            self.setPixmap(QPixmap())
            self.setText(self._placeholder)
            return
        self.setText("")
        scaled = self._source.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(scaled)


class CompareDialog(QDialog):
    def __init__(self, result: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Xem trước / Sau · Inpaint")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setModal(False)
        self.resize(1180, 680)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)
        heading = QLabel("SO SÁNH KHUNG HÌNH TRƯỚC / SAU")
        heading.setStyleSheet(f"font-size:13pt; font-weight:700; color:{TEAL_HOVER};")
        outer.addWidget(heading)
        images = QHBoxLayout()
        images.setSpacing(12)
        left_col = QVBoxLayout(); right_col = QVBoxLayout()
        left_title = QLabel("TRƯỚC · VIDEO GỐC"); left_title.setStyleSheet("font-weight:700;")
        right_title = QLabel("SAU · ĐÃ ÁP DỤNG INPAINT"); right_title.setStyleSheet("font-weight:700;")
        self.before = FitPixmapLabel(); self.after = FitPixmapLabel()
        self._temp_paths = [result.get("before_path", ""), result.get("after_path", "")]
        self.before.set_source(self._temp_paths[0])
        self.after.set_source(self._temp_paths[1])
        left_col.addWidget(left_title); left_col.addWidget(self.before, 1)
        right_col.addWidget(right_title); right_col.addWidget(self.after, 1)
        images.addLayout(left_col, 1); images.addLayout(right_col, 1)
        outer.addLayout(images, 1)
        time_text = f"Thời điểm: {self._fmt_time(result.get('frame_time', 0))}  ·  Mask: {int(result.get('mask_pixels', 0)):,} px  ·  Expand: {int(result.get('expansion', 0))} px"
        meta = QLabel(time_text); meta.setObjectName("Muted")
        note = QLabel(result.get("note", "Ảnh sau được tạo bằng cùng hàm Inpaint của pipeline xuất."))
        note.setObjectName("Muted"); note.setWordWrap(True)
        outer.addWidget(meta); outer.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.close)
        outer.addWidget(buttons)

    def closeEvent(self, event):
        for value in getattr(self, "_temp_paths", []):
            try:
                if value:
                    Path(value).unlink(missing_ok=True)
            except Exception:
                pass
        super().closeEvent(event)

    @staticmethod
    def _fmt_time(value):
        return _format_media_time(value)


class BrandingDialog(QWidget):
    ANCHORS = [
        ("↖", "top_left"), ("↑", "top_center"), ("↗", "top_right"),
        ("←", "center_left"), ("◎", "center"), ("→", "center_right"),
        ("↙", "bottom_left"), ("↓", "bottom_center"), ("↘", "bottom_right"),
    ]
    ANCHOR_LABELS = {
        "top_left":"Trên trái", "top_center":"Trên giữa", "top_right":"Trên phải",
        "center_left":"Giữa trái", "center":"Chính giữa", "center_right":"Giữa phải",
        "bottom_left":"Dưới trái", "bottom_center":"Dưới giữa", "bottom_right":"Dưới phải",
    }

    def __init__(self, state: dict, send_command, parent=None):
        super().__init__(parent)
        self.setObjectName("BrandingWorkspace")
        self.send_command = send_command
        self.state = dict(state or {})
        self.config = dict(self.state.get("config") or {})
        self.logo_path = str(self.state.get("logo_path", "") or "")
        self.label_text = str(self.state.get("label_text", "Tập 1") or "Tập 1")
        self.preview_token = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(220)
        self._preview_timer.timeout.connect(self._request_preview)
        self._preset_names = list(self.state.get("preset_names", []))
        self._anchor_buttons = {}
        self._build()
        self._sync_anchor_buttons()

    def load_state(self, state: dict):
        self.state = dict(state or {})
        self.config = dict(self.state.get("config") or {})
        self.logo_path = str(self.state.get("logo_path", "") or "")
        self.label_text = str(self.state.get("label_text", "Tập 1") or "Tập 1")
        self.logo_path_label.setText(Path(self.logo_path).name if self.logo_path else "Chưa chọn logo")
        self.logo_path_label.setToolTip(self.logo_path)
        c = self.config
        widgets = [
            (self.logo_enabled, bool(c.get("logo_enabled", True))),
            (self.logo_size, int(float(c.get("logo_width_pct", 10.5)))),
            (self.logo_opacity, int(c.get("logo_opacity", 100))),
            (self.logo_rotation, int(c.get("logo_rotation", 0))),
            (self.label_enabled, bool(c.get("label_enabled", True))),
            (self.label_text_edit, self.label_text),
            (self.label_scale, int(c.get("label_scale", 100))),
            (self.label_opacity, int(c.get("label_opacity", 100))),
            (self.show_from, float(c.get("show_from", 0)) / 60.0),
            (self.show_to, float(c.get("show_to", self.state.get("duration", 0))) / 60.0),
        ]
        for widget, value in widgets:
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox): widget.setChecked(value)
            elif isinstance(widget, QLineEdit): widget.setText(str(value))
            else: widget.setValue(value)
            widget.blockSignals(False)
        idx = self.label_anchor.findData(str(c.get("label_anchor", "top_center")))
        self.label_anchor.blockSignals(True); self.label_anchor.setCurrentIndex(max(0, idx)); self.label_anchor.blockSignals(False)
        self.text_color = str(c.get("label_text_color", "#FFFFFF"))
        self.bg_color = str(c.get("label_bg_color", "#990000"))
        self.scene_scope.setEnabled(self.state.get("scene_index") is not None)
        self.scene_scope.setChecked(bool(self.state.get("scene_has_override", False)))
        self._preset_names = list(self.state.get("preset_names", []))
        self.preset_combo.clear(); self.preset_combo.addItems(self._preset_names)
        self._sync_anchor_buttons()
        self.status.setText("Sẵn sàng")
        self.apply_btn.setEnabled(True)
        self._schedule_preview()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(9)
        head = QHBoxLayout()
        title = QLabel("Studio Branding"); title.setObjectName("BrandTitle")
        head.addWidget(title); head.addStretch(1)
        outer.addLayout(head)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        preview_panel = QWidget(); pv = QVBoxLayout(preview_panel); pv.setContentsMargins(0, 0, 8, 0)
        self.canvas = BrandingPreviewCanvas()
        self.canvas.logoMoved.connect(self._logo_dragged)
        pv.addWidget(self.canvas, 1)
        splitter.addWidget(preview_panel)

        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setMinimumWidth(350); scroll.setMaximumWidth(430)
        controls = QWidget(); cv = QVBoxLayout(controls); cv.setContentsMargins(8, 0, 4, 0); cv.setSpacing(7)
        scroll.setWidget(controls); splitter.addWidget(scroll); splitter.setSizes([760, 390])
        outer.addWidget(splitter, 1)

        # Logo group
        logo_group = QGroupBox("Logo")
        lg = QVBoxLayout(logo_group)
        self.logo_path_label = QLabel(Path(self.logo_path).name if self.logo_path else "Chưa chọn logo")
        self.logo_path_label.setObjectName("Muted"); self.logo_path_label.setWordWrap(True)
        logo_row = QHBoxLayout()
        self.choose_logo_btn = QPushButton("Chọn ảnh logo…"); self.choose_logo_btn.clicked.connect(self._choose_logo)
        self.logo_enabled = QCheckBox("Hiển thị logo")
        self.logo_enabled.setChecked(bool(self.config.get("logo_enabled", True)))
        logo_row.addWidget(self.choose_logo_btn); logo_row.addWidget(self.logo_enabled)
        lg.addWidget(self.logo_path_label); lg.addLayout(logo_row)
        self.logo_size = self._add_slider(lg, "Kích thước logo", 3, 40, int(float(self.config.get("logo_width_pct", 10.5))), "%")
        self.logo_opacity = self._add_slider(lg, "Độ trong suốt logo", 0, 100, int(self.config.get("logo_opacity", 100)), "%")
        rot_row = QHBoxLayout(); rot_row.addWidget(QLabel("Xoay logo")); self.logo_rotation = QSpinBox(); self.logo_rotation.setRange(-180,180); self.logo_rotation.setValue(int(self.config.get("logo_rotation",0))); rot_row.addWidget(self.logo_rotation); rot_row.addWidget(QLabel("độ")); rot_row.addStretch(1); lg.addLayout(rot_row)
        lg.addWidget(QLabel("Vị trí logo"))
        grid = QGridLayout(); grid.setSpacing(4)
        for i, (symbol, anchor) in enumerate(self.ANCHORS):
            btn = QPushButton(symbol); btn.setCheckable(True); btn.setFixedSize(42, 32)
            btn.clicked.connect(lambda _=False, a=anchor: self._set_logo_anchor(a))
            self._anchor_buttons[anchor] = btn
            grid.addWidget(btn, i//3, i%3)
        lg.addLayout(grid)
        cv.addWidget(logo_group)

        # Label group
        label_group = QGroupBox("Nhãn tập / phần")
        lab = QVBoxLayout(label_group)
        self.label_enabled = QCheckBox("Hiển thị nhãn"); self.label_enabled.setChecked(bool(self.config.get("label_enabled", True)))
        lab.addWidget(self.label_enabled)
        text_row = QHBoxLayout(); text_row.addWidget(QLabel("Nội dung")); self.label_text_edit = QLineEdit(self.label_text); text_row.addWidget(self.label_text_edit,1); lab.addLayout(text_row)
        self.label_scale = self._add_slider(lab, "Kích thước nhãn", 40, 180, int(self.config.get("label_scale",100)), "%")
        self.label_opacity = self._add_slider(lab, "Độ trong suốt nhãn", 0, 100, int(self.config.get("label_opacity",100)), "%")
        anchor_row = QHBoxLayout(); anchor_row.addWidget(QLabel("Vị trí nhãn")); self.label_anchor = QComboBox()
        for key, label in self.ANCHOR_LABELS.items(): self.label_anchor.addItem(label, key)
        idx = self.label_anchor.findData(str(self.config.get("label_anchor", "top_center")))
        self.label_anchor.setCurrentIndex(max(0,idx)); anchor_row.addWidget(self.label_anchor,1); lab.addLayout(anchor_row)
        color_row = QHBoxLayout()
        self.text_color = str(self.config.get("label_text_color", "#FFFFFF"))
        self.bg_color = str(self.config.get("label_bg_color", "#990000"))
        self.text_color_btn = QPushButton("Màu chữ"); self.text_color_btn.clicked.connect(lambda: self._choose_color("text"))
        self.bg_color_btn = QPushButton("Màu nền"); self.bg_color_btn.clicked.connect(lambda: self._choose_color("background"))
        color_row.addWidget(self.text_color_btn); color_row.addWidget(self.bg_color_btn); lab.addLayout(color_row)
        cv.addWidget(label_group)

        # Time / scope / presets
        timing_group = QGroupBox("Thời gian hiển thị")
        tg = QGridLayout(timing_group)
        tg.addWidget(QLabel("Từ"),0,0); tg.addWidget(QLabel("Đến"),0,1)
        self.show_from = MinutePositionSpinBox(); self.show_from.setRange(0, 99999); self.show_from.setValue(float(self.config.get("show_from",0))/60.0)
        self.show_to = MinutePositionSpinBox(); self.show_to.setRange(0, 99999); self.show_to.setValue(float(self.config.get("show_to",self.state.get("duration",0)))/60.0)
        tg.addWidget(self.show_from,1,0); tg.addWidget(self.show_to,1,1)
        cv.addWidget(timing_group)
        self.scene_scope = QCheckBox(f"Chỉ áp dụng cho {self.state.get('scene_name') or 'phân cảnh đang chọn'}")
        self.scene_scope.setEnabled(self.state.get("scene_index") is not None)
        self.scene_scope.setChecked(bool(self.state.get("scene_has_override", False)))
        cv.addWidget(self.scene_scope)
        preset_group = QGroupBox("Preset Branding")
        pg = QVBoxLayout(preset_group)
        self.preset_combo = QComboBox(); self.preset_combo.addItems(self._preset_names); pg.addWidget(self.preset_combo)
        pr = QHBoxLayout()
        self.preset_save = QPushButton("Lưu preset"); self.preset_save.clicked.connect(self._save_preset)
        self.preset_load = QPushButton("Nạp"); self.preset_load.clicked.connect(self._load_preset)
        self.preset_delete = QPushButton("Xóa"); self.preset_delete.clicked.connect(self._delete_preset)
        pr.addWidget(self.preset_save); pr.addWidget(self.preset_load); pr.addWidget(self.preset_delete); pg.addLayout(pr)
        cv.addWidget(preset_group)
        cv.addStretch(1)

        # Preview changes are debounced to keep the editor responsive.
        for widget, signal in [
            (self.logo_enabled, self.logo_enabled.toggled),
            (self.label_enabled, self.label_enabled.toggled),
            (self.logo_rotation, self.logo_rotation.valueChanged),
            (self.label_anchor, self.label_anchor.currentIndexChanged),
            (self.show_from, self.show_from.valueChanged),
            (self.show_to, self.show_to.valueChanged),
            (self.label_text_edit, self.label_text_edit.textChanged),
        ]:
            signal.connect(lambda *_: self._schedule_preview())
        for slider in (self.logo_size, self.logo_opacity, self.label_scale, self.label_opacity):
            slider.valueChanged.connect(lambda *_: self._schedule_preview())

        footer = QHBoxLayout()
        self.status = QLabel("Sẵn sàng"); self.status.setObjectName("Muted")
        footer.addWidget(self.status,1)
        self.apply_btn = QPushButton("Áp dụng"); self.apply_btn.setObjectName("Primary"); self.apply_btn.clicked.connect(self._apply)
        footer.addWidget(self.apply_btn)
        outer.addLayout(footer)

    def _add_slider(self, layout, title, low, high, value, suffix):
        row = QHBoxLayout(); row.addWidget(QLabel(title)); row.addStretch(1)
        value_label = QLabel(f"{value}{suffix}"); value_label.setObjectName("Muted")
        row.addWidget(value_label); layout.addLayout(row)
        slider = QSlider(Qt.Orientation.Horizontal); slider.setRange(low,high); slider.setValue(max(low,min(high,value)))
        slider.valueChanged.connect(lambda v, label=value_label, s=suffix: label.setText(f"{v}{s}"))
        layout.addWidget(slider)
        return slider

    def _config(self):
        cfg = dict(self.config)
        cfg.update({
            "logo_enabled": self.logo_enabled.isChecked(),
            "logo_anchor": str(cfg.get("logo_anchor", "top_right")),
            "logo_width_pct": float(self.logo_size.value()),
            "logo_opacity": int(self.logo_opacity.value()),
            "logo_rotation": int(self.logo_rotation.value()),
            "label_enabled": self.label_enabled.isChecked(),
            "label_scale": int(self.label_scale.value()),
            "label_opacity": int(self.label_opacity.value()),
            "label_anchor": str(self.label_anchor.currentData() or "top_center"),
            "label_text_color": self.text_color,
            "label_bg_color": self.bg_color,
            "show_from": float(self.show_from.value()) * 60.0,
            "show_to": float(self.show_to.value()) * 60.0,
        })
        return cfg

    def _sync_anchor_buttons(self):
        selected = str(self.config.get("logo_anchor", "top_right"))
        for key, button in self._anchor_buttons.items():
            button.setChecked(key == selected)

    def _set_logo_anchor(self, anchor):
        self.config["logo_anchor"] = anchor
        self.config.pop("logo_x", None); self.config.pop("logo_y", None)
        self._sync_anchor_buttons(); self._schedule_preview()

    def _logo_dragged(self, x, y):
        self.config["logo_anchor"] = "custom"
        self.config["logo_x"] = float(x); self.config["logo_y"] = float(y)
        self._sync_anchor_buttons(); self._schedule_preview()

    def _choose_logo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Chọn logo", self.logo_path,
            "Ảnh logo (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif *.ico)")
        if path:
            self.logo_path = path
            self.logo_path_label.setText(Path(path).name)
            self.logo_path_label.setToolTip(path)
            self._schedule_preview()

    def _choose_color(self, target):
        current = self.text_color if target == "text" else self.bg_color
        color = QColorDialog.getColor(QColor(current), self, "Chọn màu chữ" if target == "text" else "Chọn màu nền")
        if color.isValid():
            if target == "text": self.text_color = color.name().upper()
            else: self.bg_color = color.name().upper()
            self._schedule_preview()

    def _schedule_preview(self):
        self._preview_timer.start()

    def _request_preview(self):
        self.preview_token += 1
        self.send_command("branding_preview", config=self._config(), logo_path=self.logo_path,
                          label_text=self.label_text_edit.text().strip() or self.label_text,
                          preview_token=self.preview_token)

    def receive_preview(self, result):
        if not result:
            return
        preview_path = str(result.get("preview_path", "") or "")
        if int(result.get("preview_token", -1)) != self.preview_token:
            try: Path(preview_path).unlink(missing_ok=True)
            except Exception: pass
            return
        self.canvas.set_preview(preview_path, result.get("frame_width", 0),
                                result.get("frame_height", 0), result.get("logo_rect"))
        # QPixmap is an in-memory copy; remove the temporary JPEG to avoid
        # accumulating one file per slider movement.
        try: Path(preview_path).unlink(missing_ok=True)
        except Exception: pass

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Lưu Preset Branding", "Tên preset:")
        if ok and name.strip():
            self.send_command("branding_preset_save", name=name.strip(), config=self._config(), logo_path=self.logo_path)
            self.status.setText("Đang lưu preset…")

    def _load_preset(self):
        name = self.preset_combo.currentText().strip()
        if name:
            self.send_command("branding_preset_load", name=name)
            self.status.setText("Đang nạp preset…")

    def _delete_preset(self):
        name = self.preset_combo.currentText().strip()
        if name and QMessageBox.question(self, "Xóa preset", f"Bạn có chắc muốn xóa preset '{name}'?") == QMessageBox.StandardButton.Yes:
            self.send_command("branding_preset_delete", name=name)
            self.status.setText("Đang xóa preset…")

    def receive_preset_save(self, result):
        self._preset_names = list(result.get("preset_names", []))
        self.preset_combo.clear(); self.preset_combo.addItems(self._preset_names)
        idx = self.preset_combo.findText(str(result.get("name", "")))
        if idx >= 0: self.preset_combo.setCurrentIndex(idx)
        self.status.setText("Đã lưu preset. Logo đã được sao chép vào thư mục preset.")

    def receive_preset_delete(self, result):
        self._preset_names = list(result.get("preset_names", []))
        self.preset_combo.clear(); self.preset_combo.addItems(self._preset_names)
        self.status.setText("Đã xóa preset.")

    def receive_preset_load(self, result):
        self.config = dict(result.get("config", {}))
        self.logo_path = str(result.get("logo_path", "") or "")
        self.logo_path_label.setText(Path(self.logo_path).name if self.logo_path else "Chưa chọn logo")
        c = self.config
        self.logo_enabled.setChecked(bool(c.get("logo_enabled", True)))
        self.logo_size.setValue(int(float(c.get("logo_width_pct",10.5))))
        self.logo_opacity.setValue(int(c.get("logo_opacity",100)))
        self.logo_rotation.setValue(int(c.get("logo_rotation",0)))
        self.label_enabled.setChecked(bool(c.get("label_enabled",True)))
        self.label_scale.setValue(int(c.get("label_scale",100)))
        self.label_opacity.setValue(int(c.get("label_opacity",100)))
        idx = self.label_anchor.findData(str(c.get("label_anchor","top_center")))
        self.label_anchor.setCurrentIndex(max(0,idx))
        self.text_color = str(c.get("label_text_color","#FFFFFF"))
        self.bg_color = str(c.get("label_bg_color","#990000"))
        self.show_from.setValue(float(c.get("show_from",0))/60.0)
        self.show_to.setValue(float(c.get("show_to",self.state.get("duration",0)))/60.0)
        self._sync_anchor_buttons(); self._schedule_preview()
        self.status.setText(f"Đã nạp preset: {result.get('name','')}.")

    def _apply(self):
        cfg = self._config()
        if cfg["show_to"] < cfg["show_from"]:
            QMessageBox.warning(self, "Thời gian không hợp lệ", "Mốc Đến phải lớn hơn hoặc bằng mốc Từ."); return
        if cfg.get("logo_enabled") and (not self.logo_path or not Path(self.logo_path).is_file()):
            QMessageBox.warning(self, "Thiếu logo", "Logo đang bật nhưng không tìm thấy file ảnh."); return
        self.apply_btn.setEnabled(False); self.status.setText("Đang áp dụng Branding…")
        self.send_command("branding_apply", config=cfg, logo_path=self.logo_path,
                          scene_scope=self.scene_scope.isChecked(),
                          label_text=self.label_text_edit.text().strip() or self.label_text)

    def apply_acknowledged(self, result):
        self.status.setText(result.get("message", "Đã áp dụng Branding."))
        self.apply_btn.setEnabled(True)

    def apply_failed(self):
        self.apply_btn.setEnabled(True)
        self.status.setText("Không áp dụng được. Kiểm tra thông báo lỗi.")


class DownloadTask(QObject):
    job_started = Signal(int, str)
    progress = Signal(int, int, float, float, str)
    job_finished = Signal(int, bool, str)
    completed = Signal()

    def __init__(self, jobs: list[dict], output_dir: str, browser: str | None,
                 max_height: int | None, prefer_mp4: bool, cancel_event: threading.Event):
        super().__init__()
        self.jobs = jobs
        self.output_dir = output_dir
        self.browser = browser
        self.max_height = max_height
        self.prefer_mp4 = prefer_mp4
        self.cancel_event = cancel_event

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            from download_engine import download_video, DownloadCancelled
            for job in self.jobs:
                if self.cancel_event.is_set():
                    break
                job_id = int(job["id"])
                self.job_started.emit(job_id, str(job["url"]))
                try:
                    result = download_video(
                        str(job["url"]), self.output_dir,
                        browser=self.browser,
                        max_height=self.max_height,
                        prefer_mp4=self.prefer_mp4,
                        cancel_event=self.cancel_event,
                        progress_callback=lambda data, jid=job_id: self.progress.emit(
                            jid,
                            int(data.get("percent", 0)),
                            float(data.get("speed") or 0),
                            float(data.get("eta") or 0),
                            str(data.get("status", "downloading")),
                        ),
                    )
                    self.job_finished.emit(job_id, True, str(result.get("title") or "Đã tải xong"))
                except DownloadCancelled:
                    self.job_finished.emit(job_id, False, "Đã dừng theo yêu cầu")
                    break
                except Exception as exc:
                    if self.cancel_event.is_set():
                        self.job_finished.emit(job_id, False, "Đã dừng theo yêu cầu")
                        break
                    self.job_finished.emit(job_id, False, str(exc)[:500])
        except Exception as exc:
            # Report a setup-level failure against every not-yet-started row.
            for job in self.jobs:
                if self.cancel_event.is_set():
                    break
                self.job_finished.emit(int(job["id"]), False, str(exc)[:500])
                self.cancel_event.set()
        finally:
            self.completed.emit()




class MergeTask(QThread):
    progress = Signal(float, str)
    succeeded = Signal(str)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(self, input_paths: list[str], output_path: str, prefer_gpu: bool = True):
        super().__init__()
        self.input_paths = list(input_paths)
        self.output_path = str(output_path)
        self.prefer_gpu = bool(prefer_gpu)
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            # Reuse the existing validated FFmpeg merge pipeline.
            from app import merge_videos, ExportCancelled
            result = merge_videos(
                self.input_paths, self.output_path,
                lambda pct, message: self.progress.emit(float(pct), str(message)),
                self.cancel_event, prefer_gpu=self.prefer_gpu, overwrite=True,
            )
            if self.cancel_event.is_set():
                self.cancelled.emit("Đã dừng ghép video.")
            else:
                self.succeeded.emit(str(result))
        except Exception as exc:
            try:
                from app import ExportCancelled
                if isinstance(exc, ExportCancelled) or self.cancel_event.is_set():
                    self.cancelled.emit(str(exc) or "Đã dừng ghép video.")
                    return
            except Exception:
                if self.cancel_event.is_set():
                    self.cancelled.emit(str(exc) or "Đã dừng ghép video.")
                    return
            self.failed.emit(str(exc)[:1800])


class ClickSeekSlider(QSlider):
    """Horizontal seek slider whose groove click jumps directly to that position."""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            groove = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, option,
                QStyle.SubControl.SC_SliderGroove, self
            )
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, option,
                QStyle.SubControl.SC_SliderHandle, self
            )
            position = event.position().toPoint()
            if self.rect().contains(position) and not handle.contains(position):
                half_handle = max(1, handle.width() // 2)
                start = groove.left() + half_handle
                end = groove.right() - half_handle
                span = max(1, end - start)
                x = max(start, min(end, position.x()))
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), x - start, span, option.upsideDown
                )
                self.setValue(value)
                # A groove click is a complete seek action, not a page-step.
                self.sliderPressed.emit()
                self.sliderReleased.emit()
                event.accept()
                return
        super().mousePressEvent(event)


class ProjectStorageSignals(QObject):
    completed = Signal(dict)


class ProjectBackupHistoryDialog(QDialog):
    """Select one of the eight rolling snapshots for safe recovery as a new project."""

    def __init__(self, backups: list[Path], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lịch sử sao lưu dự án")
        self.setMinimumSize(560, 390)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        title = QLabel("Các phiên bản dự phòng")
        title.setStyleSheet("font-size:14pt;font-weight:750;color:#edf7f8;")
        layout.addWidget(title)
        note = QLabel(
            "Chọn một mốc để khôi phục thành dự án mới. Dự án đang mở sẽ không bị ghi đè. "
            "Auto Save không tạo thêm mốc trong danh sách này."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#9db6bf;")
        layout.addWidget(note)
        self.items = QListWidget()
        self.items.setSpacing(4)
        for path in backups:
            try:
                modified = time.strftime("%d/%m/%Y · %H:%M:%S", time.localtime(path.stat().st_mtime))
            except OSError:
                modified = "Không rõ thời gian"
            item = QListWidgetItem(f"{path.stem}\n{modified}")
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.items.addItem(item)
        if backups:
            self.items.setCurrentRow(0)
        else:
            empty = QListWidgetItem("Chưa có bản sao lưu thủ công nào.")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.items.addItem(empty)
        layout.addWidget(self.items, 1)
        buttons = QDialogButtonBox()
        self.restore_button = buttons.addButton("Khôi phục thành dự án mới", QDialogButtonBox.ButtonRole.AcceptRole)
        self.restore_button.setEnabled(bool(backups))
        cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.restore_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def selected_backup(self) -> str:
        item = self.items.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""


class MainWindow(QMainWindow):
    def _make_vector_icon(self, name: str, size: int = 18) -> QIcon:
        """Draw consistent, crisp outline icons with Qt primitives (no font glyph dependency)."""
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(size / 24.0, size / 24.0)
        pen = QPen(QColor("#a8e7ea"), 1.8, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()

        def poly(points, close=False):
            if not points:
                return
            path.moveTo(*points[0])
            for point in points[1:]:
                path.lineTo(*point)
            if close:
                path.closeSubpath()

        if name in {"folder", "folder_open"}:
            poly([(3,8),(3,5),(4,4),(9,4),(11,7),(20,7),(21,8),(21,18),(20,20),(4,20),(3,19)], True)
            if name == "folder_open":
                path.moveTo(3,10); path.lineTo(21,10)
                path.moveTo(3,10); path.lineTo(5,18); path.lineTo(19,18); path.lineTo(21,10)
        elif name in {"undo", "redo"}:
            if name == "undo":
                poly([(9,5),(4,9),(9,13)])
                path.moveTo(4,9); path.lineTo(12,9); path.cubicTo(18,9,20,12,20,18)
            else:
                poly([(15,5),(20,9),(15,13)])
                path.moveTo(20,9); path.lineTo(12,9); path.cubicTo(6,9,4,12,4,18)
        elif name == "merge":
            path.moveTo(4,6); path.lineTo(9,6); path.lineTo(15,12); path.lineTo(20,12)
            path.moveTo(4,18); path.lineTo(9,18); path.lineTo(15,12)
            poly([(16,8),(20,12),(16,16)])
            painter.drawEllipse(2.5,4.5,3,3); painter.drawEllipse(2.5,16.5,3,3)
        elif name == "save":
            painter.drawRoundedRect(4,3,16,18,2,2)
            painter.drawRect(8,3,8,6)
            painter.drawRoundedRect(7,13,10,7,1,1)
            painter.drawLine(10,15,14,15)
        elif name == "export":
            painter.drawRoundedRect(4,3,16,18,2,2)
            path.moveTo(12,16); path.lineTo(12,7)
            poly([(8.5,10.5),(12,7),(15.5,10.5)])
            path.moveTo(8,18); path.lineTo(16,18)
        elif name == "download":
            path.moveTo(12,3); path.lineTo(12,15)
            poly([(7,10),(12,15),(17,10)])
            path.moveTo(4,17); path.lineTo(4,20); path.lineTo(20,20); path.lineTo(20,17)
        elif name == "edit":
            painter.drawRoundedRect(3,4,18,16,2,2)
            painter.drawLine(7,8,7,16); painter.drawLine(11,8,11,16)
            painter.drawLine(15,8,15,16); painter.drawLine(19,8,19,16)
        elif name == "layers":
            poly([(12,3),(21,8),(12,13),(3,8)], True)
            poly([(4,12),(12,17),(20,12)])
            poly([(4,16),(12,21),(20,16)])
        elif name == "tools":
            path.moveTo(4,6); path.lineTo(20,6)
            path.moveTo(4,12); path.lineTo(20,12)
            path.moveTo(4,18); path.lineTo(20,18)
            painter.drawEllipse(8,4,4,4); painter.drawEllipse(14,10,4,4); painter.drawEllipse(6,16,4,4)
        elif name == "settings":
            painter.drawEllipse(7,7,10,10); painter.drawEllipse(10,10,4,4)
            for x1,y1,x2,y2 in [(12,2,12,6),(12,18,12,22),(2,12,6,12),(18,12,22,12),
                                (5,5,8,8),(16,16,19,19),(5,19,8,16),(16,8,19,5)]:
                painter.drawLine(x1,y1,x2,y2)
        elif name == "lasso":
            poly([(5,5),(10,3),(17,5),(20,10),(18,17),(12,20),(6,17),(3,12),(5,5)])
            painter.drawEllipse(15.5,15.5,4,4)
        elif name == "rectangle":
            painter.setPen(QPen(QColor("#a8e7ea"), 1.8, Qt.PenStyle.DashLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawRoundedRect(3.5,4.5,17,15,2,2)
            painter.drawLine(3,8,3,5); painter.drawLine(18,20,21,20)
        elif name == "brush":
            path.moveTo(14,4); path.lineTo(20,10); path.lineTo(11,19); path.lineTo(6,18); path.lineTo(5,13); path.closeSubpath()
            painter.drawLine(13,5,19,11)
            path.moveTo(6,18); path.cubicTo(5,21,2,21,3,18)
        elif name == "eraser":
            poly([(8,4),(20,16),(14,22),(3,11),(3,8),(7,4)], True)
            path.moveTo(8,4); path.lineTo(13,9)
            path.moveTo(9,16); path.lineTo(14,21)
        elif name == "plus":
            path.moveTo(12,4); path.lineTo(12,20); path.moveTo(4,12); path.lineTo(20,12)
        elif name == "scissors":
            painter.drawEllipse(3,4,6,6); painter.drawEllipse(3,14,6,6)
            path.moveTo(8,8); path.lineTo(20,19); path.moveTo(8,16); path.lineTo(20,5)
        elif name == "trash":
            painter.drawRoundedRect(6,7,12,14,2,2)
            path.moveTo(4,5); path.lineTo(20,5); path.moveTo(9,5); path.lineTo(10,3); path.lineTo(14,3); path.lineTo(15,5)
            path.moveTo(10,10); path.lineTo(10,18); path.moveTo(14,10); path.lineTo(14,18)
        elif name == "sparkles":
            poly([(9,3),(10.5,8),(15,10),(10.5,11.5),(9,17),(7.5,11.5),(3,10),(7.5,8)], True)
            poly([(18,13),(19,16),(22,17),(19,18),(18,21),(17,18),(14,17),(17,16)], True)
        elif name == "scan":
            path.moveTo(8,3); path.lineTo(4,3); path.lineTo(3,4); path.lineTo(3,8)
            path.moveTo(16,3); path.lineTo(20,3); path.lineTo(21,4); path.lineTo(21,8)
            path.moveTo(3,16); path.lineTo(3,20); path.lineTo(4,21); path.lineTo(8,21)
            path.moveTo(21,16); path.lineTo(21,20); path.lineTo(20,21); path.lineTo(16,21)
            painter.drawEllipse(8,8,8,8)
        elif name == "megaphone":
            poly([(3,10),(17,5),(17,19),(3,14)], True)
            path.moveTo(17,8); path.lineTo(21,6); path.moveTo(17,16); path.lineTo(21,18)
            path.moveTo(6,15); path.lineTo(8,21); path.lineTo(12,21); path.lineTo(10,16)
        elif name == "check":
            poly([(4,12),(9,17),(20,6)])
        elif name == "preview":
            painter.drawRoundedRect(3,5,18,14,3,3)
            path.moveTo(10,9); path.lineTo(15,12); path.lineTo(10,15); path.closeSubpath()
        elif name == "pause":
            painter.drawRoundedRect(6,4,4,16,1,1); painter.drawRoundedRect(14,4,4,16,1,1)
        else:
            painter.drawEllipse(4,4,16,16)
        painter.drawPath(path)
        painter.end()
        return QIcon(pix)

    def _set_vector_icon(self, button: QPushButton, name: str, size: int = 18) -> bool:
        try:
            icon = self._make_vector_icon(name, size)
            if icon.isNull():
                return False
            button.setIcon(icon)
            button.setIconSize(QSize(size, size))
            return True
        except Exception:
            return False

    def _set_standard_icon(self, button: QPushButton, icon_name: str, size: int = 18) -> bool:
        """Compatibility wrapper mapping legacy Qt names to one modern vector icon family."""
        mapping = {
            "SP_DialogOpenButton": "folder_open", "SP_DirOpenIcon": "folder_open",
            "SP_DirIcon": "layers", "SP_ArrowBack": "undo", "SP_ArrowForward": "redo",
            "SP_FileDialogListView": "merge", "SP_DialogSaveButton": "save",
            "SP_MediaPlay": "export", "SP_MediaPause": "pause",
            "SP_FileDialogDetailedView": "edit", "SP_ComputerIcon": "tools",
            "SP_FileDialogInfoView": "settings",
        }
        vector_name = mapping.get(icon_name)
        if vector_name:
            return self._set_vector_icon(button, vector_name, size)
        try:
            standard = getattr(QStyle.StandardPixmap, icon_name, None)
            if standard is None:
                return False
            icon = self.style().standardIcon(standard)
            if icon.isNull():
                return False
            button.setIcon(icon)
            button.setIconSize(QSize(size, size))
            return True
        except Exception:
            return False

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Góc Review Phim – Video Studio | v{APP_VERSION}")
        self.setMinimumSize(1240, 740)
        self.resize(1648, 940)
        self.state: dict = {}
        self.preview_revision = -1
        self.request_ids = count(1)
        self.pending_requests: dict[int, str] = {}
        self._stdout_buffer = ""
        self._building_scene_cards = False
        self._last_video_path = ""
        self._last_scenes_signature = None
        self._range_source_key = None
        self._range_user_dirty = False
        self._updating_range_inputs = False
        self.engine_ready = False
        self._closing = False
        self._worker_failure_reported = False
        self._worker_log_path = APP_DIR / "QT_ENGINE_LOG.txt"
        self.project_storage_signals = ProjectStorageSignals(self)
        self.project_storage_signals.completed.connect(self._on_project_storage_completed)
        self._project_storage_thread = None
        self._archive_busy = False
        self._pending_archive_include_video: bool | None = None
        self._pending_archive_project_path = ""
        self.download_items: dict[int, dict] = {}
        self._download_ids = count(1)
        self._download_thread = None
        self._download_task = None
        self._download_cancel_event = None
        self.branding_dialog = None
        self.compare_dialog = None
        self._compare_temp_paths = []
        self.cta_candidates = []
        self.cta_cache_video_key = ""
        self.cta_cache_project_key = ""
        self.cta_cache_metadata = {}
        self.cta_scan_preserve_existing = False
        self.cta_boundary_merge_keys: set[tuple] = set()
        self.cta_boundary_merge_gap = 2.25
        self.cta_cache_save_timer = QTimer(self)
        self.cta_cache_save_timer.setSingleShot(True)
        self.cta_cache_save_timer.timeout.connect(self._save_cta_cache_now)
        self._cta_pending_live_payload = None
        self.cta_live_refresh_timer = QTimer(self)
        self.cta_live_refresh_timer.setSingleShot(True)
        self.cta_live_refresh_timer.setInterval(2500)
        self.cta_live_refresh_timer.timeout.connect(self._flush_cta_live_update)
        # CTA results stay in timeline order by default.  The duration header
        # is the single, discoverable sort control; no extra toolbar button is
        # needed for this one-table action.
        self.cta_sort_mode = "position"
        self.cta_sort_desc = False
        self.cta_selected_rows = set()
        self.cta_preview_active = False
        self.cta_preview_cursor = 0.0
        self.cta_preview_end = 0.0
        self.cta_preview_request_id = None
        self.cta_preview_token = ""
        self.video_preview_active = False
        self.video_preview_request_id = None
        self.video_preview_token = ""
        self.source_player_process = None
        self.manual_cut_ranges = []
        self.cta_preview_timer = QTimer(self)
        self.cta_preview_timer.setInterval(180)
        self.cta_preview_timer.timeout.connect(self._cta_preview_tick)
        self.cta_scan_active = False
        self.cta_scan_mode = "balanced"
        self.cta_scan_paused = False
        self.cta_pause_pending = False
        self.cta_phase_label = "Sẵn sàng quét CTA Detector"
        self._cta_table_updating = False
        self.mask_task_active = False
        self.batch_files = []
        self.merge_files: list[str] = []
        self.merge_worker = None
        self.batch_running = False
        self.batch_output_folder = str(Path.home() / "Videos" / "GocReview_Batch")
        self._pending_update = None
        self._update_apply_started = False
        self._setup_ui()
        self._setup_shortcuts()
        self._start_worker()
        self.update_dialog = UpdateDialog(
            APP_VERSION, APP_DIR / "updater" / "keys" / "update-public-key.pem", self
        )
        self.update_dialog.update_available.connect(self._on_update_available)
        self.update_dialog.no_update.connect(self._on_update_not_available)
        self.update_dialog.accepted.connect(self._on_update_package_ready)
        self._update_idle_timer = QTimer(self)
        self._update_idle_timer.setInterval(1000)
        self._update_idle_timer.timeout.connect(self._maybe_apply_pending_update)
        QTimer.singleShot(3500, self._check_updates_automatically)

    def _setup_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(12)
        self.setCentralWidget(central)

        # Compact app identity row.
        header = QFrame(); header.setObjectName("Header"); header.setFixedHeight(58)
        h = QHBoxLayout(header); h.setContentsMargins(13, 6, 13, 6); h.setSpacing(12)
        mark = QLabel()
        icon_path = ASSETS / "goc_review_logo_v2.png"
        if not icon_path.exists():
            icon_path = ASSETS / "app_icon.ico"
        icon = QPixmap(str(icon_path))
        if not icon.isNull():
            mark.setPixmap(icon.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            mark.setText("🎬")
        mark.setFixedSize(42, 42); mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(mark)
        title = QLabel("Góc Review Phim Video Studio"); title.setObjectName("BrandTitle")
        h.addWidget(title)
        version = QLabel(f"v{APP_VERSION}  ·  STUDIO")
        version.setObjectName("VersionBadge")
        h.addWidget(version)
        self.update_btn = QPushButton("Kiểm tra & cập nhật")
        self.update_btn.setObjectName("Secondary")
        self.update_btn.setMinimumHeight(38)
        self.update_btn.setToolTip("Bấm một lần để kiểm tra; nếu có bản mới, phần mềm tự tải, xác minh và cài đặt")
        self._set_vector_icon(self.update_btn, "download", 16)
        self.update_btn.clicked.connect(self._open_update_center)
        h.addWidget(self.update_btn)
        h.addStretch(1)
        self.autosave_toggle = QCheckBox("Tự lưu")
        self.autosave_toggle.setChecked(True)
        self.autosave_toggle.setToolTip("Tự động lưu và khôi phục dự án")
        self.autosave_toggle.toggled.connect(lambda value: self.send("set_autosave", value=value))
        h.addWidget(self.autosave_toggle)
        self.save_state = QLabel("●  Đang khởi động engine…"); self.save_state.setObjectName("SaveState")
        h.addWidget(self.save_state)
        outer.addWidget(header)

        # The top action bar has exactly four groups: Undo/Redo, Open Video,
        # Project menu, and Export. All project operations live in one menu.
        project_bar = QFrame(); project_bar.setObjectName("ProjectBar"); project_bar.setFixedHeight(76)
        p = QHBoxLayout(project_bar); p.setContentsMargins(16, 8, 16, 8); p.setSpacing(8)

        self.project_title = QLabel("Chưa có dự án")
        self.project_title.setObjectName("ProjectTitle")
        self.project_title.setMaximumWidth(240)
        self.project_title.setToolTip("Tên dự án đang mở")
        self.video_title = QLabel("Chọn video nguồn để bắt đầu")
        self.video_title.setObjectName("Muted")
        self.video_title.setMaximumWidth(220)
        self.video_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        identity = QVBoxLayout(); identity.setSpacing(1)
        identity.addWidget(self.project_title); identity.addWidget(self.video_title)
        p.addLayout(identity)
        p.addStretch(1)

        def add_action_separator():
            sep = QFrame(); sep.setFrameShape(QFrame.Shape.VLine); sep.setFixedHeight(34)
            sep.setStyleSheet(f"color:{BORDER}; background:{BORDER}; max-width:1px;")
            p.addWidget(sep)

        undo = QPushButton(); self._set_standard_icon(undo, "SP_ArrowBack")
        undo.setObjectName("Quiet"); undo.setFixedSize(38, 36); undo.setToolTip("Hoàn tác · Ctrl+Z")
        undo.clicked.connect(lambda: self.send("undo")); p.addWidget(undo)
        redo = QPushButton(); self._set_standard_icon(redo, "SP_ArrowForward")
        redo.setObjectName("Quiet"); redo.setFixedSize(38, 36); redo.setToolTip("Làm lại · Ctrl+Y")
        redo.clicked.connect(lambda: self.send("redo")); p.addWidget(redo)

        add_action_separator()
        self.open_video_btn = QPushButton("Mở video"); self.open_video_btn.setObjectName("Secondary")
        self._set_standard_icon(self.open_video_btn, "SP_DialogOpenButton")
        self.open_video_btn.setToolTip("Chọn video nguồn · Ctrl+I")
        self.open_video_btn.clicked.connect(self.choose_video); p.addWidget(self.open_video_btn)

        add_action_separator()
        self.project_menu_btn = QToolButton(); self.project_menu_btn.setText("Dự án")
        self.project_menu_btn.setObjectName("Secondary")
        self.project_menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._set_vector_icon(self.project_menu_btn, "folder_open", 17)
        project_menu = QMenu(self.project_menu_btn)
        project_menu.addAction("Mở dự án…").triggered.connect(self.open_project)
        project_menu.addAction("Lưu dự án").triggered.connect(self.save_project)
        project_menu.addSeparator()
        self.project_archive_action = project_menu.addAction("Đóng gói ZIP (kèm video gốc)")
        self.project_archive_action.setEnabled(False)
        self.project_archive_action.triggered.connect(self.archive_project)
        self.project_restore_action = project_menu.addAction("Nhập dự án từ ZIP…")
        self.project_restore_action.triggered.connect(self.restore_project_from_zip)
        project_menu.addSeparator()
        self.project_backup_history_action = project_menu.addAction("Lịch sử sao lưu…")
        self.project_backup_history_action.setEnabled(False)
        self.project_backup_history_action.triggered.connect(self.show_project_backup_history)
        self.project_menu_btn.setMenu(project_menu)
        self.project_menu_btn.setToolTip("Mở, lưu, đóng gói hoặc nhập dự án")
        p.addWidget(self.project_menu_btn)

        p.addStretch(1)
        self.export_top_btn = QPushButton("Xuất video"); self._set_vector_icon(self.export_top_btn, "export", 18)
        self.export_top_btn.setObjectName("Primary")
        self.export_top_btn.setToolTip("Xuất toàn bộ video hiện tại")
        self.export_top_btn.clicked.connect(lambda: self.send("export_full")); p.addWidget(self.export_top_btn)
        outer.addWidget(project_bar)

        # Sidebar + stacked workspace: editor remains untouched while the downloader
        # gets a dedicated, uncluttered full-width page.
        self.project_bar = project_bar
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(5)
        self.sidebar = self._build_sidebar()
        self.workspace_stack = QStackedWidget()
        self.editor_workspace = QWidget()
        editor_layout = QHBoxLayout(self.editor_workspace)
        editor_layout.setContentsMargins(0, 0, 0, 0); editor_layout.setSpacing(8)
        inner_splitter = QSplitter(Qt.Orientation.Horizontal)
        inner_splitter.setChildrenCollapsible(False); inner_splitter.setHandleWidth(5)
        self.center = self._build_center()
        self.inspector = self._build_inspector()
        inner_splitter.addWidget(self.center); inner_splitter.addWidget(self.inspector)
        inner_splitter.setSizes([990, 420]); inner_splitter.setStretchFactor(0, 1); inner_splitter.setStretchFactor(1, 0)
        editor_layout.addWidget(inner_splitter)
        self.workspace_stack.addWidget(self.editor_workspace)
        self.download_page = self._build_download_page()
        self.batch_page = self._build_batch_page()
        self.merge_page = self._build_merge_page()
        self.project_tabs = QTabWidget()
        self.project_tabs.addTab(self.download_page, "Tải video")
        self.project_tabs.addTab(self.batch_page, "Xử lý hàng loạt")
        self.project_tabs.addTab(self.merge_page, "Ghép video")
        self.workspace_stack.addWidget(self.project_tabs)
        # CTA Subscribe is a first-class workspace.  It used to be a second tab
        # inside the editor timeline, which made discovery ambiguous and forced
        # the thumbnail/controls into a cramped lower strip.  Keep one owner for
        # the feature and give it the full workspace width.
        self.cta_page = self._build_cta_page()
        self.workspace_stack.addWidget(self.cta_page)
        self.branding_dialog = BrandingDialog({}, self.send_command, self)
        self.workspace_stack.addWidget(self.branding_dialog)
        splitter.addWidget(self.sidebar); splitter.addWidget(self.workspace_stack)
        splitter.setSizes([SIDEBAR_WIDTH, 1600 - SIDEBAR_WIDTH])
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        outer.addWidget(splitter, 1)

        # Bottom status is intentionally quiet and information-dense.
        footer = QFrame(); footer.setObjectName("StatusBar"); footer.setFixedHeight(40)
        f = QHBoxLayout(footer); f.setContentsMargins(14, 4, 14, 4); f.setSpacing(18)
        self.ready_label = QLabel("●  Sẵn sàng"); self.ready_label.setStyleSheet("color:#68cba1; font-weight:700;")
        self.meta_label = QLabel("Chưa có thông tin video"); self.meta_label.setObjectName("Muted")
        self.progress = QProgressBar(); self.progress.setFixedWidth(130); self.progress.setRange(0, 100); self.progress.setValue(0)
        self.gpu_label = QLabel("GPU (NVENC): kiểm tra khi xuất"); self.gpu_label.setStyleSheet("color:#68cba1; font-weight:600;")
        f.addWidget(self.ready_label); f.addWidget(self.meta_label, 1); f.addWidget(self.progress); f.addWidget(self.gpu_label)
        outer.addWidget(footer)

    def _build_sidebar(self):
        panel = QFrame(); panel.setObjectName("Sidebar")
        panel.setMinimumWidth(SIDEBAR_MIN_WIDTH); panel.setMaximumWidth(SIDEBAR_MAX_WIDTH)
        panel.resize(SIDEBAR_WIDTH, panel.height())
        layout = QVBoxLayout(panel); layout.setContentsMargins(11, 14, 11, 12); layout.setSpacing(6)
        self.nav_group = QButtonGroup(self); self.nav_group.setExclusive(True)
        self.nav_buttons = {}

        def section(text):
            label = QLabel(text); label.setObjectName("SectionTitle")
            label.setMinimumHeight(28)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            # The former opaque full-width blocks made these category labels
            # look like clipped buttons. Keep them as quiet section headings.
            label.setStyleSheet(
                f"background:transparent; color:{MUTED}; font-size:8pt; "
                "font-weight:750; border-bottom:1px solid #174253; "
                "padding:8px 2px 4px 3px;"
            )
            layout.addWidget(label)

        def add_nav(icon_name, label, key):
            # QPushButton interprets a single '&' as a keyboard mnemonic.
            # Double it to render the literal Vietnamese section separator.
            btn = QPushButton(_qt_button_label(label))
            btn.setToolTip(label)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setMinimumHeight(44)
            if icon_name in {"download", "megaphone", "scan", "export", "layers"}:
                self._set_vector_icon(btn, icon_name, 19)
            else:
                self._set_standard_icon(btn, icon_name, 19)
            btn.setIconSize(QSize(19, 19)); btn.setObjectName("Nav"); btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, k=key: self._navigate(k))
            self.nav_group.addButton(btn); layout.addWidget(btn); self.nav_buttons[key] = btn
            return btn

        section("CÔNG CỤ VIDEO")
        add_nav("tools", "Công cụ video", "downloads")
        section("BIÊN TẬP")
        add_nav("megaphone", "CTA Detector", "cta")
        add_nav("scan", "Làm sạch · Mask", "mask")
        section("THƯƠNG HIỆU")
        add_nav("layers", "Studio Branding", "branding")
        section("Thành phẩm")
        add_nav("export", "Xuất & kiểm tra", "export")

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine); sep.setStyleSheet(f"color:{BORDER};")
        layout.addSpacing(7); layout.addWidget(sep); layout.addStretch(1)

        # The former "Mẹo nhanh" block is intentionally removed. This compact
        # card is reserved for actual runtime state only.
        status_card = QFrame(); status_card.setObjectName("ProjectCard")
        status_layout = QVBoxLayout(status_card); status_layout.setContentsMargins(10, 8, 10, 8); status_layout.setSpacing(5)
        status_title = QLabel("Trạng thái hệ thống"); status_title.setObjectName("Muted")
        status_title.setStyleSheet(f"color:{MUTED}; font-size:7.6pt; font-weight:700;")
        status_layout.addWidget(status_title)
        self.sidebar_engine_status = QLabel("● Engine đang khởi động")
        self.sidebar_engine_status.setStyleSheet("color:#d5ad70; font-size:8pt; font-weight:650;")
        status_layout.addWidget(self.sidebar_engine_status)
        self.sidebar_autosave_status = QLabel("● Auto Save: đang bật")
        self.sidebar_autosave_status.setStyleSheet("color:#68cba1; font-size:8pt;")
        status_layout.addWidget(self.sidebar_autosave_status)
        self.resource_hint = QLabel("● Tác vụ nền: ưu tiên thấp")
        self.resource_hint.setObjectName("Muted"); self.resource_hint.setWordWrap(True)
        self.resource_hint.setStyleSheet(f"color:{MUTED}; font-size:8pt;")
        status_layout.addWidget(self.resource_hint)
        layout.addWidget(status_card)
        return panel

    def _build_download_page(self):
        page = QFrame(); page.setObjectName("CenterPanel")
        root = QVBoxLayout(page); root.setContentsMargins(14, 9, 14, 9); root.setSpacing(11)

        heading = QHBoxLayout(); heading.setSpacing(12)
        glyph = QLabel(); glyph.setPixmap(self._make_vector_icon("download", 34).pixmap(34, 34)); glyph.setFixedSize(42, 42)
        heading.addWidget(glyph)
        title_col = QVBoxLayout(); title_col.setSpacing(2)
        title = QLabel("Tải video"); title.setObjectName("BrandTitle")
        title_col.addWidget(title); heading.addLayout(title_col); heading.addStretch(1)
        root.addLayout(heading)

        link_group = QFrame(); link_group.setObjectName("Inspector")
        link_layout = QVBoxLayout(link_group); link_layout.setContentsMargins(15, 13, 15, 13); link_layout.setSpacing(9)
        label = QLabel("Liên kết video"); label.setObjectName("SectionTitle"); link_layout.addWidget(label)
        link_row = QHBoxLayout(); link_row.setSpacing(8)
        self.download_url_edit = QLineEdit(); self.download_url_edit.setPlaceholderText("Dán link Facebook Reel hoặc YouTube…")
        self.download_url_edit.setClearButtonEnabled(True)
        self.download_url_edit.returnPressed.connect(self._download_add_url)
        paste_btn = QPushButton("Dán"); paste_btn.setObjectName("Secondary"); self._set_vector_icon(paste_btn, "edit", 16); paste_btn.clicked.connect(self._download_paste)
        add_btn = QPushButton("Thêm vào danh sách"); self._set_vector_icon(add_btn, "plus", 16); add_btn.clicked.connect(self._download_add_url)
        add_btn.setObjectName("Primary")
        link_row.addWidget(self.download_url_edit, 1); link_row.addWidget(paste_btn); link_row.addWidget(add_btn)
        link_layout.addLayout(link_row)
        root.addWidget(link_group)

        options = QFrame(); options.setObjectName("Inspector")
        options_layout = QGridLayout(options); options_layout.setContentsMargins(15, 9, 15, 9); options_layout.setHorizontalSpacing(10); options_layout.setVerticalSpacing(7)
        options_layout.addWidget(QLabel("Chất lượng"), 0, 0)
        self.download_quality = QComboBox(); self.download_quality.addItem("Cao nhất (tự động)", None); self.download_quality.addItem("Tối đa 1080p", 1080); self.download_quality.addItem("Tối đa 720p", 720)
        options_layout.addWidget(self.download_quality, 0, 1)
        options_layout.addWidget(QLabel("Định dạng"), 0, 2)
        self.download_format = QComboBox(); self.download_format.addItem("Tự động (khuyến nghị)", "auto"); self.download_format.addItem("Ưu tiên MP4", "mp4")
        options_layout.addWidget(self.download_format, 0, 3)
        options_layout.addWidget(QLabel("Cookie trình duyệt"), 1, 0)
        self.download_browser = QComboBox(); self.download_browser.addItem("Không dùng", None); self.download_browser.addItem("Chrome", "chrome"); self.download_browser.addItem("Edge", "edge"); self.download_browser.addItem("Firefox", "firefox")
        self.download_browser.setToolTip("Chỉ đọc phiên đăng nhập cục bộ khi bạn chủ động chọn. Không gửi cookie ra máy chủ của ứng dụng.")
        options_layout.addWidget(self.download_browser, 1, 1)
        options_layout.addWidget(QLabel("Thư mục lưu"), 1, 2)
        folder_row = QHBoxLayout(); self.download_folder_edit = QLineEdit(str(Path.home() / "Downloads" / "GocReview_Downloads")); self.download_folder_edit.setReadOnly(True)
        folder_btn = QPushButton("Chọn…"); folder_btn.clicked.connect(self._download_choose_folder)
        folder_row.addWidget(self.download_folder_edit, 1); folder_row.addWidget(folder_btn)
        options_layout.addLayout(folder_row, 1, 3)
        root.addWidget(options)

        queue_header = QHBoxLayout(); qtitle = QLabel("Danh sách tải"); qtitle.setObjectName("SectionTitle"); queue_header.addWidget(qtitle); queue_header.addStretch(1)
        self.download_count_label = QLabel("0 mục"); self.download_count_label.setObjectName("Muted"); queue_header.addWidget(self.download_count_label)
        root.addLayout(queue_header)

        self.download_table = QTableWidget(0, 3)
        self.download_table.setHorizontalHeaderLabels(["Video / nguồn", "Trạng thái", "Tiến độ"])
        self.download_table.verticalHeader().setVisible(False)
        self.download_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.download_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.download_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.download_table.setAlternatingRowColors(True)
        self.download_table.setShowGrid(False)
        header_view = self.download_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.download_table.setColumnWidth(2, 210)
        self.download_table.setMinimumHeight(180)
        root.addWidget(self.download_table, 1)

        controls = QHBoxLayout(); controls.setSpacing(8)
        self.download_start_btn = QPushButton("Bắt đầu tải"); self._set_vector_icon(self.download_start_btn, "download", 17); self.download_start_btn.setObjectName("Success")
        self.download_start_btn.clicked.connect(self._download_start_queue)
        self.download_stop_btn = QPushButton("Dừng tải"); self.download_stop_btn.setObjectName("Destructive"); self.download_stop_btn.setEnabled(False)
        self.download_stop_btn.clicked.connect(self._download_stop)
        self.download_clear_btn = QPushButton("Xóa mục đã xong"); self.download_clear_btn.setObjectName("Tertiary"); self.download_clear_btn.clicked.connect(self._download_clear_finished)
        self.download_remove_btn = QPushButton("Bỏ mục chờ"); self.download_remove_btn.setObjectName("Tertiary"); self.download_remove_btn.clicked.connect(self._download_remove_selected_waiting)
        self.download_open_folder_btn = QPushButton("Mở thư mục"); self.download_open_folder_btn.setObjectName("Secondary"); self.download_open_folder_btn.clicked.connect(self._download_open_folder)
        controls.addWidget(self.download_start_btn); controls.addWidget(self.download_stop_btn); controls.addStretch(1)
        controls.addWidget(self.download_remove_btn); controls.addWidget(self.download_open_folder_btn); controls.addWidget(self.download_clear_btn)
        root.addLayout(controls)
        self.download_summary = QLabel("Sẵn sàng · Tải lần lượt để hạn chế nghẽn mạng và tài nguyên")
        self.download_summary.setObjectName("Muted"); root.addWidget(self.download_summary)
        return page

    def _build_batch_page(self):
        page = QFrame(); page.setObjectName("CenterPanel")
        root = QVBoxLayout(page); root.setContentsMargins(14, 10, 14, 10); root.setSpacing(10)
        title = QLabel("Xử lý hàng loạt"); title.setObjectName("BrandTitle"); root.addWidget(title)
        box = QFrame(); box.setObjectName("Inspector"); layout = QVBoxLayout(box); layout.setContentsMargins(14, 12, 14, 12); layout.setSpacing(8)
        head = QHBoxLayout(); label = QLabel("Hàng đợi video"); label.setObjectName("SectionTitle"); head.addWidget(label); head.addStretch(1)
        self.batch_count_label = QLabel("0 video"); self.batch_count_label.setObjectName("Muted"); head.addWidget(self.batch_count_label); layout.addLayout(head)
        self.batch_table = QTableWidget(0, 3); self.batch_table.setHorizontalHeaderLabels(["Video", "Trạng thái", "Tiến độ"])
        self.batch_table.verticalHeader().setVisible(False); self.batch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.batch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.batch_table.setAlternatingRowColors(True); self.batch_table.setShowGrid(False)
        self.batch_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch); self.batch_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents); self.batch_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed); self.batch_table.setColumnWidth(2, 160); self.batch_table.setMinimumHeight(250); layout.addWidget(self.batch_table, 1)
        controls = QHBoxLayout(); controls.setSpacing(7)
        add = QPushButton("Thêm video"); self._set_vector_icon(add, "plus", 17); add.setObjectName("Primary"); add.clicked.connect(self._batch_add_files); controls.addWidget(add)
        remove = QPushButton("Xóa"); remove.setObjectName("Tertiary"); remove.clicked.connect(self._batch_remove_selected); controls.addWidget(remove)
        up = QPushButton("Lên"); up.setObjectName("Tertiary"); up.clicked.connect(lambda: self._batch_move_selected(-1)); controls.addWidget(up)
        down = QPushButton("Xuống"); down.setObjectName("Tertiary"); down.clicked.connect(lambda: self._batch_move_selected(1)); controls.addWidget(down); controls.addStretch(1); layout.addLayout(controls); root.addWidget(box, 1)
        opts = QFrame(); opts.setObjectName("Inspector"); grid = QGridLayout(opts); grid.setContentsMargins(14, 10, 14, 10); grid.addWidget(QLabel("Thư mục xuất"), 0, 0)
        self.batch_folder_edit = QLineEdit(self.batch_output_folder); grid.addWidget(self.batch_folder_edit, 0, 1); folder = QPushButton("Chọn…"); folder.setObjectName("Secondary"); folder.clicked.connect(self._batch_choose_folder); grid.addWidget(folder, 0, 2)
        root.addWidget(opts)
        self.batch_progress = QProgressBar(); self.batch_progress.setRange(0, 100); root.addWidget(self.batch_progress)
        self.batch_status = QLabel("Chưa có video trong hàng đợi."); self.batch_status.setObjectName("Muted"); self.batch_status.setWordWrap(True); root.addWidget(self.batch_status)
        bottom = QHBoxLayout(); self.batch_start_btn = QPushButton("Bắt đầu xử lý"); self.batch_start_btn.setObjectName("Primary"); self.batch_start_btn.clicked.connect(self._batch_start); bottom.addWidget(self.batch_start_btn); self.batch_stop_btn = QPushButton("Dừng hàng đợi"); self.batch_stop_btn.setObjectName("Destructive"); self.batch_stop_btn.setEnabled(False); self.batch_stop_btn.clicked.connect(self._batch_stop); bottom.addWidget(self.batch_stop_btn); bottom.addStretch(1); root.addLayout(bottom)
        return page

    def _build_merge_page(self):
        page = QFrame(); page.setObjectName("CenterPanel")
        root = QVBoxLayout(page); root.setContentsMargins(16, 14, 16, 14); root.setSpacing(10)
        title = QLabel("Ghép video"); title.setObjectName("BrandTitle"); root.addWidget(title)

        panel = QFrame(); panel.setObjectName("Inspector")
        layout = QVBoxLayout(panel); layout.setContentsMargins(14, 12, 14, 12); layout.setSpacing(8)
        head = QHBoxLayout(); self.merge_count_label = QLabel("0 video"); self.merge_count_label.setObjectName("Muted")
        head.addWidget(QLabel("Thứ tự ghép")); head.addStretch(1); head.addWidget(self.merge_count_label); layout.addLayout(head)
        self.merge_list = QListWidget(); self.merge_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.merge_list.setAlternatingRowColors(True); self.merge_list.setMinimumHeight(240)
        layout.addWidget(self.merge_list, 1)
        controls = QHBoxLayout(); controls.setSpacing(7)
        self.merge_add_btn = QPushButton("Thêm video"); self._set_vector_icon(self.merge_add_btn, "plus", 17)
        self.merge_add_btn.setObjectName("Primary"); self.merge_add_btn.clicked.connect(self._merge_add_files)
        self.merge_remove_btn = QPushButton("Bỏ chọn"); self.merge_remove_btn.setObjectName("Tertiary"); self.merge_remove_btn.clicked.connect(self._merge_remove_selected)
        self.merge_up_btn = QPushButton("Lên"); self.merge_up_btn.setObjectName("Tertiary"); self.merge_up_btn.clicked.connect(lambda: self._merge_move_selected(-1))
        self.merge_down_btn = QPushButton("Xuống"); self.merge_down_btn.setObjectName("Tertiary"); self.merge_down_btn.clicked.connect(lambda: self._merge_move_selected(1))
        self.merge_clear_btn = QPushButton("Xóa danh sách"); self.merge_clear_btn.setObjectName("Tertiary"); self.merge_clear_btn.clicked.connect(self._merge_clear)
        self.merge_edit_buttons = [self.merge_add_btn, self.merge_remove_btn, self.merge_up_btn, self.merge_down_btn, self.merge_clear_btn]
        for button in (self.merge_add_btn, self.merge_remove_btn, self.merge_up_btn, self.merge_down_btn): controls.addWidget(button)
        controls.addStretch(1); controls.addWidget(self.merge_clear_btn); layout.addLayout(controls)
        root.addWidget(panel, 1)

        output = QFrame(); output.setObjectName("Inspector")
        form = QGridLayout(output); form.setContentsMargins(14, 10, 14, 10); form.setHorizontalSpacing(9); form.setVerticalSpacing(8)
        form.addWidget(QLabel("Tên file"), 0, 0)
        self.merge_name_edit = QLineEdit("video_ghep.mp4"); form.addWidget(self.merge_name_edit, 0, 1, 1, 2)
        form.addWidget(QLabel("Thư mục lưu"), 1, 0)
        default_folder = Path.home() / "Videos"
        if not default_folder.is_dir(): default_folder = Path.home()
        self.merge_folder_edit = QLineEdit(str(default_folder)); form.addWidget(self.merge_folder_edit, 1, 1)
        self.merge_folder_btn = QPushButton("Chọn…"); self.merge_folder_btn.setObjectName("Secondary"); self.merge_folder_btn.clicked.connect(self._merge_choose_folder); form.addWidget(self.merge_folder_btn, 1, 2)
        root.addWidget(output)

        self.merge_progress = QProgressBar(); self.merge_progress.setRange(0, 100); self.merge_progress.setValue(0); root.addWidget(self.merge_progress)
        self.merge_status = QLabel("Sẵn sàng"); self.merge_status.setObjectName("Muted"); root.addWidget(self.merge_status)
        actions = QHBoxLayout()
        self.merge_start_btn = QPushButton("Ghép video"); self.merge_start_btn.setObjectName("Primary"); self._set_vector_icon(self.merge_start_btn, "merge", 17); self.merge_start_btn.clicked.connect(self._merge_start)
        self.merge_stop_btn = QPushButton("Dừng"); self.merge_stop_btn.setObjectName("Danger"); self.merge_stop_btn.setEnabled(False); self.merge_stop_btn.clicked.connect(self._merge_cancel)
        actions.addStretch(1); actions.addWidget(self.merge_start_btn); actions.addWidget(self.merge_stop_btn); root.addLayout(actions)
        return page

    def _merge_add_files(self):
        if self.merge_worker is not None and self.merge_worker.isRunning():
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Chọn video cần ghép", str(Path.home()),
            "Video (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;Tất cả (*.*)"
        )
        was_empty = not self.merge_files
        existing = {os.path.normcase(str(Path(item).resolve())) for item in self.merge_files}
        added = False
        for path in paths:
            resolved = str(Path(path).resolve())
            key = os.path.normcase(resolved)
            if key not in existing:
                self.merge_files.append(resolved); existing.add(key); added = True
        if added and was_empty:
            first = Path(self.merge_files[0])
            if not self.merge_name_edit.text().strip() or self.merge_name_edit.text().strip() == "video_ghep.mp4":
                self.merge_name_edit.setText(f"{first.stem}_ghep.mp4")
            if Path(self.merge_folder_edit.text()).expanduser() in {Path.home(), Path.home() / "Videos"}:
                self.merge_folder_edit.setText(str(first.parent))
        self._merge_refresh_list()

    def _merge_refresh_list(self):
        self.merge_list.clear()
        for index, path in enumerate(self.merge_files, 1):
            item = QListWidgetItem(f"{index:02d}  ·  {Path(path).name}")
            item.setToolTip(path); self.merge_list.addItem(item)
        self.merge_count_label.setText(f"{len(self.merge_files)} video")
        self.merge_start_btn.setEnabled(len(self.merge_files) >= 2 and not (self.merge_worker and self.merge_worker.isRunning()))

    def _merge_remove_selected(self):
        if self.merge_worker is not None and self.merge_worker.isRunning():
            return
        indexes = sorted({index.row() for index in self.merge_list.selectionModel().selectedRows()}, reverse=True)
        if not indexes and self.merge_list.currentRow() >= 0: indexes = [self.merge_list.currentRow()]
        for index in indexes:
            if 0 <= index < len(self.merge_files): self.merge_files.pop(index)
        self._merge_refresh_list()

    def _merge_move_selected(self, direction: int):
        if self.merge_worker is not None and self.merge_worker.isRunning():
            return
        row = self.merge_list.currentRow(); target = row + int(direction)
        if not (0 <= row < len(self.merge_files) and 0 <= target < len(self.merge_files)): return
        self.merge_files[row], self.merge_files[target] = self.merge_files[target], self.merge_files[row]
        self._merge_refresh_list(); self.merge_list.setCurrentRow(target)

    def _merge_clear(self):
        if self.merge_worker and self.merge_worker.isRunning(): return
        self.merge_files.clear(); self._merge_refresh_list(); self.merge_status.setText("Đã xóa danh sách")

    def _merge_choose_folder(self):
        initial = self.merge_folder_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu video ghép", initial)
        if folder: self.merge_folder_edit.setText(str(Path(folder).resolve()))

    def _merge_start(self):
        if self.merge_worker and self.merge_worker.isRunning(): return
        if len(self.merge_files) < 2:
            QMessageBox.warning(self, "Thiếu video", "Hãy chọn ít nhất 2 video để ghép."); return
        name = self.merge_name_edit.text().strip()
        if not name: name = "video_ghep.mp4"
        if not name.lower().endswith(".mp4"): name += ".mp4"
        if any(ch in name for ch in '<>:"/\\|?*'):
            QMessageBox.warning(self, "Tên file không hợp lệ", "Tên file chứa ký tự không được hỗ trợ."); return
        folder = Path(self.merge_folder_edit.text().strip()).expanduser()
        if not folder.exists() or not folder.is_dir():
            QMessageBox.warning(self, "Thư mục không hợp lệ", "Hãy chọn một thư mục lưu hợp lệ."); return
        output = (folder / name).resolve()
        normalized_output = os.path.normcase(str(output))
        if any(os.path.normcase(str(Path(source).resolve())) == normalized_output for source in self.merge_files):
            QMessageBox.warning(self, "Trùng file", "File đầu ra không được trùng với video nguồn."); return
        if output.exists() and QMessageBox.question(self, "File đã tồn tại", f"Ghi đè file này?\n{output}") != QMessageBox.StandardButton.Yes:
            return
        self.merge_name_edit.setText(name)
        self.merge_progress.setValue(0); self.merge_status.setText("Đang chuẩn bị ghép…")
        self.merge_start_btn.setEnabled(False); self.merge_stop_btn.setEnabled(True)
        for widget in [*self.merge_edit_buttons, self.merge_name_edit, self.merge_folder_edit, self.merge_folder_btn]:
            widget.setEnabled(False)
        prefer_gpu = bool(self.state.get("prefer_gpu", True))
        worker = MergeTask(list(self.merge_files), str(output), prefer_gpu=prefer_gpu)
        self.merge_worker = worker
        worker.progress.connect(self._merge_progress_update)
        worker.succeeded.connect(self._merge_succeeded)
        worker.failed.connect(self._merge_failed)
        worker.cancelled.connect(self._merge_cancelled)
        worker.finished.connect(self._merge_thread_finished)
        worker.start()

    def _merge_progress_update(self, pct: float, message: str):
        self.merge_progress.setValue(max(0, min(100, int(pct))))
        self.merge_status.setText(str(message))

    def _merge_succeeded(self, output_path: str):
        self.merge_progress.setValue(100); self.merge_status.setText(f"Đã ghép xong · {Path(output_path).name}")
        self._notice("Ghép video hoàn tất", output_path, "success")

    def _merge_failed(self, message: str):
        self.merge_status.setText("Ghép video thất bại")
        self._notice("Lỗi ghép video", message, "error")

    def _merge_cancelled(self, message: str):
        self.merge_status.setText("Đã dừng ghép video")

    def _merge_thread_finished(self):
        worker = self.merge_worker
        self.merge_start_btn.setEnabled(len(self.merge_files) >= 2)
        self.merge_stop_btn.setEnabled(False)
        for widget in [*self.merge_edit_buttons, self.merge_name_edit, self.merge_folder_edit, self.merge_folder_btn]:
            widget.setEnabled(True)
        self.merge_worker = None
        if worker is not None:
            worker.deleteLater()

    def _merge_cancel(self):
        if self.merge_worker and self.merge_worker.isRunning():
            self.merge_stop_btn.setEnabled(False); self.merge_status.setText("Đang dừng…")
            self.merge_worker.cancel()

    def _open_merge_workspace(self):
        self._navigate("downloads")
        self.project_tabs.setCurrentIndex(2)

    def _open_batch_workspace(self):
        self._navigate("downloads")
        self.project_tabs.setCurrentIndex(1)

    def _batch_add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Chọn video cần xử lý", str(Path.home()), "Video (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;Tất cả (*.*)")
        for item in files:
            if item not in self.batch_files: self.batch_files.append(item)
        self._batch_refresh_table()

    def _batch_refresh_table(self):
        self.batch_table.setRowCount(0)
        for path in self.batch_files:
            row = self.batch_table.rowCount(); self.batch_table.insertRow(row); name = QTableWidgetItem(Path(path).name); name.setToolTip(path); self.batch_table.setItem(row, 0, name); self.batch_table.setItem(row, 1, QTableWidgetItem("Đang chờ")); self.batch_table.setItem(row, 2, QTableWidgetItem("0%"))
        self.batch_count_label.setText(f"{len(self.batch_files)} video"); self.batch_start_btn.setEnabled(bool(self.batch_files) and not self.batch_running)

    def _batch_remove_selected(self):
        row = self.batch_table.currentRow()
        if 0 <= row < len(self.batch_files): self.batch_files.pop(row); self._batch_refresh_table()

    def _batch_move_selected(self, delta):
        row = self.batch_table.currentRow(); target = row + delta
        if not (0 <= row < len(self.batch_files) and 0 <= target < len(self.batch_files)): return
        self.batch_files[row], self.batch_files[target] = self.batch_files[target], self.batch_files[row]; self._batch_refresh_table(); self.batch_table.selectRow(target)

    def _batch_choose_folder(self):
        selected = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu kết quả", self.batch_folder_edit.text() or str(Path.home()))
        if selected: self.batch_folder_edit.setText(selected); self.batch_output_folder = selected

    def _batch_start(self):
        if self.batch_running or not self.batch_files: return
        self.batch_output_folder = self.batch_folder_edit.text().strip(); self.batch_running = True; self.batch_start_btn.setEnabled(False); self.batch_stop_btn.setEnabled(True); self.batch_progress.setValue(0); self.batch_status.setText(f"Đang chuẩn bị {len(self.batch_files)} video…")
        if not self.send("batch_start", files=list(self.batch_files), output_folder=self.batch_output_folder): self._batch_finished({"cancelled": True, "success": [], "failed": []})

    def _batch_stop(self):
        if self.batch_running: self.batch_status.setText("Đang dừng sau video hiện tại…"); self.batch_stop_btn.setEnabled(False); self.send("batch_cancel")

    def _batch_progress_update(self, payload):
        pct = max(0, min(100, int(float(payload.get("percent", 0))))); index = int(payload.get("index", 0)); self.batch_progress.setValue(pct); self.batch_status.setText(str(payload.get("message", "Đang xử lý…")))
        if 0 <= index < self.batch_table.rowCount(): self.batch_table.setItem(index, 1, QTableWidgetItem("Đang xử lý")); self.batch_table.setItem(index, 2, QTableWidgetItem(f"{pct}%"))

    def _batch_finished(self, payload):
        self.batch_running = False; self.batch_start_btn.setEnabled(bool(self.batch_files)); self.batch_stop_btn.setEnabled(False); success = len(payload.get("success", [])); failed = len(payload.get("failed", [])); self.batch_status.setText(("Đã dừng" if payload.get("cancelled") else "Hoàn tất") + f" · {success} thành công, {failed} lỗi.")

    def _batch_error(self, message):
        self.batch_running = False; self.batch_start_btn.setEnabled(bool(self.batch_files)); self.batch_stop_btn.setEnabled(False); self.batch_status.setText("Không thể chạy hàng loạt: " + str(message or "Lỗi không xác định")); self._notice("Xử lý hàng loạt", str(message or ""), "error")

    def _download_paste(self):
        self.download_url_edit.setText(QApplication.clipboard().text().strip())
        self.download_url_edit.setFocus()

    def _download_choose_folder(self):
        current = self.download_folder_edit.text().strip()
        selected = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu video", current or str(Path.home()))
        if selected:
            self.download_folder_edit.setText(selected)

    def _download_open_folder(self):
        import os
        folder = Path(self.download_folder_edit.text().strip()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            import subprocess; subprocess.Popen(["open", str(folder)])
        else:
            import subprocess; subprocess.Popen(["xdg-open", str(folder)])

    def _download_add_url(self):
        url = self.download_url_edit.text().strip()
        try:
            from download_engine import validate_video_url, detect_platform
            validate_video_url(url)
        except Exception as exc:
            self._notice("Link chưa hợp lệ", str(exc), "warning")
            return
        if any(item["url"] == url for item in self.download_items.values()):
            self._notice("Link đã có", "Liên kết này đã nằm trong danh sách tải.", "info")
            return
        job_id = next(self._download_ids)
        platform = detect_platform(url)
        row = self.download_table.rowCount(); self.download_table.insertRow(row)
        link_item = QTableWidgetItem(f"{platform}  ·  {url}"); link_item.setData(Qt.ItemDataRole.UserRole, job_id)
        self.download_table.setItem(row, 0, link_item)
        status_item = QTableWidgetItem("Đang chờ"); self.download_table.setItem(row, 1, status_item)
        bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(0); bar.setFormat("Chờ tải")
        self.download_table.setCellWidget(row, 2, bar)
        self.download_items[job_id] = {"id": job_id, "url": url, "platform": platform, "status": "queued", "row": row, "bar": bar, "status_item": status_item}
        self.download_url_edit.clear()
        self._download_refresh_count()
        self.download_summary.setText(f"Đã thêm {platform}. Bạn có thể thêm nhiều link trước khi bắt đầu.")

    def _download_refresh_count(self):
        waiting = sum(1 for x in self.download_items.values() if x["status"] == "queued")
        active = sum(1 for x in self.download_items.values() if x["status"] == "downloading")
        done = sum(1 for x in self.download_items.values() if x["status"] == "done")
        self.download_count_label.setText(f"{len(self.download_items)} mục · {waiting} chờ · {active} đang tải · {done} xong")

    def _download_start_queue(self):
        if self._download_thread is not None:
            return
        jobs = [{"id": x["id"], "url": x["url"]} for x in self.download_items.values() if x["status"] == "queued"]
        if not jobs:
            self._notice("Danh sách trống", "Hãy dán link Facebook hoặc YouTube rồi thêm vào danh sách.", "info")
            return
        folder = Path(self.download_folder_edit.text().strip()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._notice("Không tạo được thư mục", str(exc), "error"); return
        try:
            import yt_dlp  # noqa: F401
        except Exception:
            self._notice("Thiếu engine tải video", "Chạy CAP_NHAT_THU_VIEN.bat trong thư mục ứng dụng để cài yt-dlp, sau đó mở lại chức năng Tải Video.", "warning")
            return
        self._download_cancel_event = threading.Event()
        self._download_thread = QThread(self)
        self._download_task = DownloadTask(
            jobs, str(folder), self.download_browser.currentData(),
            self.download_quality.currentData(), self.download_format.currentData() == "mp4",
            self._download_cancel_event,
        )
        self._download_task.moveToThread(self._download_thread)
        self._download_thread.started.connect(self._download_task.run)
        self._download_task.job_started.connect(self._download_on_job_started)
        self._download_task.progress.connect(self._download_on_progress)
        self._download_task.job_finished.connect(self._download_on_job_finished)
        self._download_task.completed.connect(self._download_thread.quit)
        self._download_task.completed.connect(self._download_task.deleteLater)
        self._download_thread.finished.connect(self._download_thread_finished)
        self._download_thread.finished.connect(self._download_thread.deleteLater)
        self.download_start_btn.setEnabled(False); self.download_stop_btn.setEnabled(True)
        self.download_summary.setText("Đang tải lần lượt… không đóng ứng dụng trong khi đang xử lý.")
        self._download_thread.start()

    def _download_on_job_started(self, job_id: int, _url: str):
        item = self.download_items.get(job_id)
        if not item: return
        item["status"] = "downloading"; item["status_item"].setText("Đang tải")
        item["bar"].setFormat("Đang phân tích…")
        self._download_refresh_count()

    def _download_on_progress(self, job_id: int, percent: int, speed: float, eta: float, phase: str):
        item = self.download_items.get(job_id)
        if not item: return
        bar = item["bar"]
        if phase == "downloading":
            bar.setValue(max(0, min(100, percent)))
            speed_text = f"{speed / 1024 / 1024:.1f} MB/s" if speed > 0 else "đang tính tốc độ"
            eta_text = f" · còn {int(eta)}s" if eta > 0 else ""
            bar.setFormat(f"%p% · {speed_text}{eta_text}")
        elif phase == "finished":
            bar.setValue(100); bar.setFormat("Đang ghép / hoàn tất…")

    def _download_on_job_finished(self, job_id: int, success: bool, message: str):
        item = self.download_items.get(job_id)
        if not item: return
        item["status"] = "done" if success else ("cancelled" if "dừng" in message.lower() else "error")
        item["status_item"].setText("Hoàn thành" if success else ("Đã dừng" if item["status"] == "cancelled" else "Lỗi"))
        if success:
            item["bar"].setValue(100); item["bar"].setFormat("Hoàn thành")
            self.download_summary.setText(f"Đã tải xong: {message}")
        else:
            item["bar"].setFormat("Đã dừng" if item["status"] == "cancelled" else "Thất bại")
            item["status_item"].setToolTip(message)
            self.download_summary.setText(f"Tải không thành công: {message}")
        self._download_refresh_count()

    def _download_stop(self):
        if self._download_task is not None:
            self._download_task.cancel()
            self.download_stop_btn.setEnabled(False)
            self.download_summary.setText("Đang gửi yêu cầu dừng…")

    def _download_thread_finished(self):
        self._download_thread = None; self._download_task = None; self._download_cancel_event = None
        self.download_start_btn.setEnabled(True); self.download_stop_btn.setEnabled(False)
        queued = sum(1 for x in self.download_items.values() if x["status"] == "queued")
        self.download_summary.setText("Đã xử lý xong danh sách." if not queued else f"Đã dừng. Còn {queued} mục đang chờ, có thể tiếp tục.")
        self._download_refresh_count()
        if self._pending_update:
            self._maybe_apply_pending_update()

    def _download_remove_selected_waiting(self):
        removable = set()
        for model_index in self.download_table.selectionModel().selectedRows():
            cell = self.download_table.item(model_index.row(), 0)
            job_id = cell.data(Qt.ItemDataRole.UserRole) if cell else None
            item = self.download_items.get(job_id)
            if item and item["status"] == "queued":
                removable.add(job_id)
        for row in reversed(range(self.download_table.rowCount())):
            cell = self.download_table.item(row, 0)
            job_id = cell.data(Qt.ItemDataRole.UserRole) if cell else None
            if job_id in removable:
                self.download_table.removeRow(row)
        for job_id in removable:
            self.download_items.pop(job_id, None)
        for row in range(self.download_table.rowCount()):
            cell = self.download_table.item(row, 0)
            job_id = cell.data(Qt.ItemDataRole.UserRole) if cell else None
            if job_id in self.download_items:
                self.download_items[job_id]["row"] = row
        self._download_refresh_count()

    def _download_clear_finished(self):
        removable = {job_id for job_id, item in self.download_items.items() if item["status"] in {"done", "error", "cancelled"}}
        for row in reversed(range(self.download_table.rowCount())):
            cell = self.download_table.item(row, 0)
            job_id = cell.data(Qt.ItemDataRole.UserRole) if cell else None
            if job_id in removable:
                self.download_table.removeRow(row)
        for job_id in removable:
            self.download_items.pop(job_id, None)
        for row in range(self.download_table.rowCount()):
            cell = self.download_table.item(row, 0)
            job_id = cell.data(Qt.ItemDataRole.UserRole) if cell else None
            if job_id in self.download_items:
                self.download_items[job_id]["row"] = row
        self._download_refresh_count()

    def _build_cta_page(self):
        page = QFrame(); page.setObjectName("CenterPanel")
        root = QVBoxLayout(page); root.setContentsMargins(8, 5, 8, 5); root.setSpacing(6)

        controls = QHBoxLayout(); controls.setSpacing(7)
        self.cta_interval = QLabel("1 ảnh / 2 giây")
        self.cta_interval.setObjectName("Muted")
        controls.addWidget(self.cta_interval)
        self.cta_scan_btn = QPushButton("Quét toàn video")
        self.cta_scan_btn.setObjectName("Primary")
        self.cta_scan_btn.setToolTip("Bắt đầu một lượt quét mới từ đầu video; thay thế danh sách kết quả hiện tại.")
        self.cta_scan_btn.clicked.connect(self._cta_start_scan)
        controls.addWidget(self.cta_scan_btn)
        self.cta_rescan_btn = QPushButton("Quét phần còn lại")
        self.cta_rescan_btn.setObjectName("Secondary")
        self.cta_rescan_btn.setToolTip("Quét từ checkpoint đã lưu, giữ kết quả cũ và nối các CTA bị chia ở ranh giới hai lần quét.")
        self.cta_rescan_btn.clicked.connect(self._cta_scan_additional)
        controls.addWidget(self.cta_rescan_btn)
        self.cta_clear_btn = QPushButton("Xóa tất cả")
        self.cta_clear_btn.setObjectName("Destructive")
        self.cta_clear_btn.setToolTip("Xóa kết quả CTA và cache của video hiện tại.")
        self.cta_clear_btn.clicked.connect(self._cta_clear_all)
        controls.addWidget(self.cta_clear_btn)
        self.cta_pause_btn = QPushButton("Tạm dừng")
        self.cta_pause_btn.setObjectName("Secondary")
        self.cta_pause_btn.setEnabled(False)
        self.cta_pause_btn.clicked.connect(self._cta_toggle_pause)
        controls.addWidget(self.cta_pause_btn)
        self.cta_stop_btn = QPushButton("Dừng quét")
        self.cta_stop_btn.setObjectName("Destructive")
        self.cta_stop_btn.setEnabled(False)
        self.cta_stop_btn.clicked.connect(self._cta_stop_scan)
        controls.addWidget(self.cta_stop_btn)
        self.cta_cut_btn = QPushButton("Cắt các khoảng đã chọn")
        self.cta_cut_btn.setObjectName("Primary")
        self.cta_cut_btn.setEnabled(False)
        self.cta_cut_btn.clicked.connect(self._cta_cut_selected)
        controls.addWidget(self.cta_cut_btn)
        controls.addStretch(1); root.addLayout(controls)

        self.cta_status = QLabel("Sẵn sàng")
        self.cta_status.setObjectName("Muted")
        self.cta_status.setWordWrap(True)
        self.cta_status.setMaximumHeight(46)
        root.addWidget(self.cta_status)

        search_row = QHBoxLayout(); search_row.setSpacing(7)
        search_row.addWidget(QLabel("Tìm trong kết quả"))
        self.cta_search = QLineEdit()
        self.cta_search.setPlaceholderText("Tìm LIKE, SHARE, SUBSCRIBE hoặc chữ OCR…")
        self.cta_search.setClearButtonEnabled(True)
        self.cta_search.textChanged.connect(self._cta_apply_filters)
        search_row.addWidget(self.cta_search, 1)
        search_row.addWidget(QLabel("Lọc"))
        self.cta_filter = QComboBox()
        self.cta_filter.addItems(["Tất cả", "LIKE", "SHARE", "SUBSCRIBE"])
        self.cta_filter.currentIndexChanged.connect(self._cta_apply_filters)
        search_row.addWidget(self.cta_filter)
        self.cta_filter_info = QLabel("0/0 kết quả")
        self.cta_filter_info.setObjectName("Muted")
        search_row.addWidget(self.cta_filter_info)
        root.addLayout(search_row)

        self.cta_progress = QProgressBar()
        self.cta_progress.setRange(0, 100); self.cta_progress.setValue(0)
        root.addWidget(self.cta_progress)

        body = QSplitter(Qt.Orientation.Horizontal); body.setChildrenCollapsible(False)
        self.cta_table = QTableWidget(0, 8)
        self.cta_table.setObjectName("CtaTable")
        self.cta_table.setHorizontalHeaderLabels([
            "Khung hình", "Chọn", "Thời gian", "Dài",
            "Từ khóa", "OCR đọc được", "Tin cậy", "Vị trí",
        ])
        header = self.cta_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.cta_table.setColumnWidth(0, 112)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.cta_table.setColumnWidth(1, 56)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.cta_table.setColumnWidth(3, 62)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.cta_table.setColumnWidth(4, 86)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        for col in (6, 7):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.cta_table.setColumnWidth(6, 68)
        self.cta_table.setColumnWidth(7, 66)
        self.cta_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cta_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.cta_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cta_table.setWordWrap(False)
        self.cta_table.verticalHeader().setDefaultSectionSize(78)
        self.cta_table.itemChanged.connect(self._cta_item_changed)
        self.cta_table.currentCellChanged.connect(self._cta_current_changed)
        header.sectionClicked.connect(self._cta_sort_clicked)
        header.setToolTip("Mỗi dòng có ảnh bằng chứng tại khung hình OCR. Bấm cột Dài để sắp xếp.")
        self._update_cta_sort_header()
        body.addWidget(self.cta_table)

        preview_panel = QFrame(); preview_panel.setObjectName("Inspector")
        pv = QVBoxLayout(preview_panel); pv.setContentsMargins(12, 12, 12, 12); pv.setSpacing(8)
        pv.addWidget(QLabel("Ảnh khung hình nhận diện"))
        self.cta_preview = FitPixmapLabel()
        self.cta_preview.setObjectName("CtaPreview")
        self.cta_preview.setMinimumSize(320, 200)
        self.cta_preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.cta_preview.set_source("", "Ảnh mẫu sẽ hiển thị sau khi có kết quả CTA")
        pv.addWidget(self.cta_preview, 1)
        self.cta_preview_caption = QLabel("Chưa chọn kết quả.")
        self.cta_preview_caption.setObjectName("CtaCaption")
        self.cta_preview_caption.setWordWrap(True)
        self.cta_preview_caption.setMinimumHeight(66)
        self.cta_preview_caption.setMaximumHeight(112)
        self.cta_preview_caption.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.cta_preview_caption.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pv.addWidget(self.cta_preview_caption)
        preview_controls = QHBoxLayout(); preview_controls.setSpacing(7)
        self.cta_play_btn = QPushButton("Xem đoạn đã chọn")
        self.cta_play_btn.setObjectName("Secondary")
        self.cta_play_btn.setEnabled(False)
        self.cta_play_btn.setToolTip("Phát video gốc từ mốc bắt đầu đến mốc kết thúc của CTA đang chọn")
        self.cta_play_btn.clicked.connect(self._cta_toggle_preview)
        preview_controls.addWidget(self.cta_play_btn)
        self.cta_play_status = QLabel("Chưa phát đoạn")
        self.cta_play_status.setObjectName("Muted")
        preview_controls.addWidget(self.cta_play_status, 1)
        pv.addLayout(preview_controls)
        edit_row = QGridLayout()
        start_label = QLabel("Bắt đầu")
        end_label = QLabel("Kết thúc")
        start_label.setToolTip("MM:SS")
        end_label.setToolTip("MM:SS")
        edit_row.addWidget(start_label, 0, 0)
        edit_row.addWidget(end_label, 0, 1)
        self.cta_start = MinutePositionSpinBox()
        self.cta_start.setRange(0, 999999); self.cta_start.setEnabled(False)
        self.cta_end = MinutePositionSpinBox()
        self.cta_end.setRange(0, 999999); self.cta_end.setEnabled(False)
        edit_row.addWidget(self.cta_start, 1, 0); edit_row.addWidget(self.cta_end, 1, 1)
        self.cta_start.setToolTip("Thời điểm bắt đầu · MM:SS")
        self.cta_end.setToolTip("Thời điểm kết thúc · MM:SS")
        pv.addLayout(edit_row)
        body.addWidget(preview_panel); body.setSizes([820, 620]); root.addWidget(body, 1)
        return page


    def _cta_clear_all(self):
        if self.cta_scan_active:
            self._notice("CTA đang quét", "Hãy dừng hoặc chờ quét xong trước khi xóa toàn bộ kết quả.", "warning")
            return
        if not self.cta_candidates:
            self.cta_status.setText("Không có kết quả CTA để xóa.")
            return
        answer = QMessageBox.question(
            self, "Xóa tất cả kết quả CTA",
            f"Bạn có chắc muốn xóa toàn bộ {len(self.cta_candidates)} khoảng CTA và cache của video hiện tại không?\n\nVideo nguồn không bị thay đổi.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.cta_live_refresh_timer.stop()
        self._cta_pending_live_payload = None
        self.cta_cache_save_timer.stop()
        self._cta_stop_preview()
        cache_file = self._cta_cache_file()
        if cache_file is not None:
            try:
                cache_file.unlink(missing_ok=True)
                shutil.rmtree(cache_file.parent / "thumbnails" / cache_file.stem, ignore_errors=True)
            except OSError as exc:
                self._notice("Không xóa được cache CTA", str(exc), "warning")
                return

        self.cta_candidates = []
        self.cta_selected_rows.clear()
        self.cta_cache_metadata = {}
        self._cta_table_updating = True
        try:
            self.cta_table.setRowCount(0)
            self.cta_table.clearSelection()
        finally:
            self._cta_table_updating = False
        self.cta_preview.set_source("", "Chưa có kết quả CTA")
        self.cta_preview_caption.setText("Chưa chọn ứng viên.")
        self.cta_play_status.setText("Chưa phát đoạn")
        self.cta_play_btn.setText("Xem đoạn đã chọn")
        self.cta_play_btn.setEnabled(False)
        self.cta_cut_btn.setEnabled(False)
        self.cta_start.setValue(0.0)
        self.cta_end.setValue(0.0)
        self.cta_start.setEnabled(False)
        self.cta_end.setEnabled(False)
        self.cta_progress.setValue(0)
        self.cta_filter_info.setText("0/0 kết quả")
        self.cta_status.setText("Đã xóa toàn bộ kết quả CTA và cache của video hiện tại.")

    def _cta_cache_file(self, video_path: str | None = None, project_path: str | None = None) -> Path | None:
        path = Path(video_path or self.state.get("video_path", "")).expanduser()
        try:
            stat = path.stat()
            identity = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".casefold()
        except OSError:
            return None
        active_project = self.state.get("project_path", "") if project_path is None else project_path
        if active_project:
            try:
                digest = video_fingerprint(path)[:24]
            except OSError:
                return None
            root = ensure_project_workspace(active_project) / "Analysis" / "CTA"
            root.mkdir(parents=True, exist_ok=True)
            return root / f"video_{digest}.json"
        digest = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:24]
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "GocReviewVideoStudio" / "cta_cache"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{digest}.json"

    def _cta_thumbnail_dir(self, cache_file: Path) -> Path:
        folder = cache_file.parent / "thumbnails" / cache_file.stem
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def _cta_cache_candidate(candidate: dict) -> dict:
        allowed = {
            "scan_id", "start", "end", "duration", "sample_time", "confidence", "peak_confidence",
            "samples", "kind", "method", "text", "label", "terms", "evidence", "animated",
            "x", "y", "w", "h", "location", "thumbnail", "selected",
        }
        result = {}
        for key in allowed:
            if key not in candidate:
                continue
            value = candidate[key]
            if key in {"evidence", "terms"}:
                result[key] = [str(item) for item in (value or [])]
            elif key in {"start", "end", "duration", "sample_time", "confidence", "peak_confidence", "x", "y", "w", "h"}:
                try:
                    result[key] = float(value) if key not in {"x", "y", "w", "h"} else int(value)
                except (TypeError, ValueError):
                    continue
            elif key == "samples":
                try:
                    result[key] = max(1, int(value or 1))
                except (TypeError, ValueError):
                    result[key] = 1
            elif key == "selected":
                result[key] = bool(value)
            else:
                result[key] = value
        return result

    def _schedule_cta_cache_save(self):
        # Save progress even before the first CTA has been detected. Otherwise
        # a timeout/stop on an early frame loses the only resume checkpoint.
        if self.state.get("video_path"):
            self.cta_cache_save_timer.start(900)

    def _save_cta_cache_now(self):
        video_path = str(self.state.get("video_path", "") or "")
        cache_file = self._cta_cache_file(video_path)
        if cache_file is None:
            return
        thumbnail_root = self._cta_thumbnail_dir(cache_file)
        cached_candidates = []
        selected_keys = {
            self._cta_candidate_key(self.cta_candidates[row])
            for row in self.cta_selected_rows
            if 0 <= row < len(self.cta_candidates)
        }
        for candidate in self.cta_candidates:
            item = self._cta_cache_candidate(candidate)
            item["selected"] = self._cta_candidate_key(candidate) in selected_keys
            preview_path = str(candidate.get("preview_path", "") or "")
            if preview_path and Path(preview_path).is_file():
                try:
                    sample = float(candidate.get("sample_time", candidate.get("start", 0)) or 0)
                except (TypeError, ValueError):
                    sample = 0.0
                scan_id = str(candidate.get("scan_id", "cta") or "cta")
                thumb_name = f"cta_{scan_id}_{int(round(sample * 1000)):012d}.jpg"
                destination = thumbnail_root / thumb_name
                try:
                    if Path(preview_path).resolve() != destination.resolve():
                        shutil.copy2(preview_path, destination)
                    item["thumbnail"] = str(destination.relative_to(cache_file.parent).as_posix())
                except (OSError, ValueError):
                    pass
            cached_candidates.append(item)
        try:
            fingerprint = video_fingerprint(video_path) if video_path and Path(video_path).is_file() else ""
        except OSError:
            fingerprint = ""
        payload = {
            "schema_version": 4,
            "video_path": video_path,
            "video_fingerprint": fingerprint,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "complete": bool(self.cta_cache_metadata.get("complete", False)),
            "samples": int(self.cta_cache_metadata.get("samples", 0) or 0),
            "pass1_samples": int(self.cta_cache_metadata.get("pass1_samples", 0) or 0),
            "pass2_samples": int(self.cta_cache_metadata.get("pass2_samples", 0) or 0),
            "sample_interval": float(self.cta_cache_metadata.get("sample_interval", 0) or 0),
            "pass1_interval": float(self.cta_cache_metadata.get("pass1_interval", 0) or 0),
            "pass2_interval": float(self.cta_cache_metadata.get("pass2_interval", 0) or 0),
            "sample_cap_reached": bool(self.cta_cache_metadata.get("sample_cap_reached", False)),
            "pass2_sample_cap_reached": bool(self.cta_cache_metadata.get("pass2_sample_cap_reached", False)),
            "resume_time": float(self.cta_cache_metadata.get("resume_time", 0) or 0),
            "resume_phase": str(self.cta_cache_metadata.get("resume_phase", "") or ""),
            "candidates": cached_candidates,
        }
        temp_file = cache_file.with_suffix(".tmp")
        try:
            temp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_file.replace(cache_file)
        except OSError as exc:
            self._append_engine_log(f"CTA cache save failed: {exc}")

    def _migrate_cta_cache(self, video_path: str, old_project: str, new_project: str):
        """Move the first unsaved video's CTA cache into its newly saved project."""
        if not video_path or not new_project or old_project:
            return
        old_file = self._cta_cache_file(video_path, project_path="")
        new_file = self._cta_cache_file(video_path, project_path=new_project)
        if old_file is None or new_file is None or not old_file.is_file() or new_file.exists():
            return
        new_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(old_file.read_text(encoding="utf-8"))
        old_thumbs = old_file.parent / "thumbnails" / old_file.stem
        new_thumbs = new_file.parent / "thumbnails" / new_file.stem
        if old_thumbs.is_dir() and not new_thumbs.exists():
            shutil.copytree(old_thumbs, new_thumbs)
        for candidate in payload.get("candidates", []) or []:
            relative = str(candidate.get("thumbnail", "") or "")
            if relative:
                old_prefix = f"thumbnails/{old_file.stem}/"
                if relative.replace("\\", "/").startswith(old_prefix):
                    candidate["thumbnail"] = relative.replace("\\", "/").replace(
                        old_prefix, f"thumbnails/{new_file.stem}/", 1
                    )
        payload["video_path"] = video_path
        payload["video_fingerprint"] = video_fingerprint(video_path)
        temp_file = new_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_file.replace(new_file)

    def _load_cta_cache_for_video(self, video_path: str):
        cache_file = self._cta_cache_file(video_path)
        if cache_file is None:
            self.cta_cache_metadata = {}
            return
        if not cache_file.is_file() and self.state.get("project_path"):
            legacy_file = self._cta_cache_file(video_path, project_path="")
            if legacy_file is not None and legacy_file.is_file():
                try:
                    self._migrate_cta_cache(video_path, "", str(self.state.get("project_path", "")))
                except Exception as exc:
                    self._append_engine_log(f"Legacy CTA cache migration failed: {exc}")
        if not cache_file.is_file():
            self.cta_cache_metadata = {}
            return
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            saved_fingerprint = str(payload.get("video_fingerprint", "") or "")
            current_fingerprint = video_fingerprint(video_path)
            if saved_fingerprint and saved_fingerprint != current_fingerprint:
                self.cta_cache_metadata = {}
                self._notice("Dữ liệu CTA không khớp video",
                             "File video hiện tại không trùng fingerprint với dữ liệu CTA đã lưu. "
                             "Để tránh nạp nhầm kết quả, Video Studio đã không sử dụng cache này.", "warning")
                return
            # Upgrade older in-project caches while preserving their results.
            payload["video_path"] = video_path
            payload["video_fingerprint"] = current_fingerprint
            payload["schema_version"] = max(4, int(payload.get("schema_version", 1) or 1))
            candidates = payload.get("candidates", [])
            if not isinstance(candidates, list):
                return
            self.cta_cache_metadata = dict(payload)
            missing_thumbnails = []
            for candidate in candidates:
                relative = str(candidate.get("thumbnail", "") or "")
                if not relative:
                    missing_thumbnails.append(candidate)
                    continue
                try:
                    thumb_path = (cache_file.parent / relative).resolve()
                    thumb_path.relative_to(cache_file.parent.resolve())
                except (OSError, ValueError):
                    missing_thumbnails.append(candidate)
                    continue
                if thumb_path.is_file():
                    candidate["preview_path"] = str(thumb_path)
                else:
                    missing_thumbnails.append(candidate)
            self._cta_refresh_candidates(candidates, replace=True)
            duration = float(self.state.get("duration", 0) or 0)
            resume_time = max(0.0, float(payload.get("resume_time", 0) or 0))
            progress = 100 if payload.get("complete") else int(100 * resume_time / duration) if duration > 0 else 0
            self.cta_progress.setValue(max(0, min(100, progress)))
            state = "đã hoàn tất" if payload.get("complete") else f"đã lưu tiến độ đến {self._fmt_scan_key(resume_time)}"
            if candidates:
                self.cta_status.setText(f"Đã nạp {len(candidates)} khoảng CTA từ lần quét trước ({state}). Ảnh bằng chứng được lưu cùng dự án.")
                if missing_thumbnails:
                    # Older caches have no embedded thumbnails; rebuild only those missing.
                    self.send("cta_cache_thumbnails", candidates=missing_thumbnails)
            elif not payload.get("complete"):
                self.cta_status.setText(f"Đã khôi phục checkpoint CTA tại {self._fmt_scan_key(resume_time)}. Bấm Quét phần còn lại để tiếp tục.")
            else:
                self.cta_status.setText("Lần quét trước đã hoàn tất nhưng không phát hiện khoảng CTA.")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._append_engine_log(f"CTA cache load failed: {exc}")

    def _cta_scan_additional(self):
        if self.cta_scan_active:
            return
        duration = float(self.state.get("duration", 0) or 0)
        resume_time = self._cta_resume_time()
        if not self.cta_candidates and resume_time <= 0.0:
            self._cta_start_scan()
            return
        if self.cta_cache_metadata.get("complete") and duration and resume_time >= duration - 0.5:
            self._notice("Đã quét hết video", "Video đã được quét hết. Bấm Quét toàn video để chạy lại từ đầu; lượt mới sẽ thay thế danh sách hiện tại.", "info")
            return
        self._cta_start_scan(preserve_existing=True)

    def _cta_resume_time(self):
        try:
            cached = max(0.0, float(self.cta_cache_metadata.get("resume_time", 0) or 0))
        except (TypeError, ValueError):
            cached = 0.0
        if cached > 0.0:
            return cached
        # Migration fallback for caches created before the scan key existed.
        # It uses the latest known CTA boundary rather than restarting at 0.
        return max((float(item.get("end", 0) or 0) for item in self.cta_candidates), default=0.0)

    def _cta_start_scan(self, preserve_existing=False):
        if self.cta_scan_active:
            return
        if not self.state.get("video_path"):
            self._notice("Chưa có video", "Hãy mở video nguồn trước khi quét từ khóa CTA.", "warning"); return
        if self.state.get("busy"):
            self._notice("Đang xử lý", "Hãy đợi tác vụ hiện tại kết thúc trước khi quét CTA.", "warning"); return
        self.cta_live_refresh_timer.stop()
        self._cta_pending_live_payload = None
        old_cache_metadata = dict(self.cta_cache_metadata)
        requested_resume_from = self._cta_resume_time() if preserve_existing else 0.0
        # A partial scan can legitimately have zero CTA candidates. Keep its
        # checkpoint rather than silently starting the video again from 0.
        self.cta_scan_preserve_existing = bool(
            preserve_existing and (self.cta_candidates or requested_resume_from > 0.0)
        )
        resume_from = requested_resume_from if self.cta_scan_preserve_existing else 0.0
        if self.cta_scan_preserve_existing:
            existing = list(self.cta_candidates)
            self.cta_boundary_merge_keys = {self._cta_candidate_key(item) for item in existing}
            try:
                previous_interval = float(old_cache_metadata.get("sample_interval", 2.0) or 2.0)
            except (TypeError, ValueError):
                previous_interval = 2.0
            self.cta_boundary_merge_gap = max(1.2, min(2.4, previous_interval * 1.15))
            self._cta_refresh_candidates(existing, replace=True)
        else:
            self.cta_boundary_merge_keys = set()
            self.cta_candidates = []; self.cta_selected_rows.clear()
        self.cta_scan_active = True; self.cta_scan_paused = False
        if not self.cta_scan_preserve_existing:
            self._cta_table_updating = True
            self.cta_table.setRowCount(0)
            self._cta_table_updating = False
        duration = float(self.state.get("duration", 0) or 0)
        initial_progress = int(100 * resume_from / duration) if self.cta_scan_preserve_existing and duration > 0 else 0
        self.cta_progress.setValue(max(0, min(100, initial_progress)))
        self.cta_phase_label = "Quét toàn video · 1 ảnh mỗi 2 giây"
        self.cta_status.setText("Đang tiếp tục quét…" if self.cta_scan_preserve_existing else "Đang quét…")
        self.cta_cache_metadata = {
            **old_cache_metadata,
            "complete": False, "samples": 0, "pass1_samples": 0, "pass2_samples": 0,
            "sample_interval": 0, "resume_time": resume_from,
            "resume_phase": str(old_cache_metadata.get("resume_phase", "pass1")) if self.cta_scan_preserve_existing else "pass1",
        }
        self.cta_scan_btn.setEnabled(False); self.cta_pause_btn.setEnabled(True)
        self.cta_pause_btn.setText("Tạm dừng"); self.cta_stop_btn.setEnabled(True); self.cta_cut_btn.setEnabled(False)
        self.cta_play_btn.setEnabled(False)
        # Keep the scan cadence at two seconds per frame. Recognition quality
        # is improved inside the OCR pipeline rather than by quadrupling samples.
        interval = 2.0
        self.cta_scan_mode = "fast"
        if self.cta_scan_preserve_existing:
            self.cta_status.setText(f"Đang tiếp tục từ {self._fmt_scan_key(resume_from)}…")
        request = self.send("cta_scan_start", interval=interval, scan_mode=self.cta_scan_mode, start_at=resume_from)
        if not request:
            self._cta_scan_cancelled("Không gửi được lệnh quét tới Engine.")

    def _cta_toggle_pause(self):
        if not self.cta_scan_active:
            # If the engine ended unexpectedly, the same control becomes a
            # true Resume action backed by the saved checkpoint.
            if not self.cta_cache_metadata.get("complete") and self._cta_resume_time() > 0.0:
                self._cta_scan_additional()
            return
        if self.cta_scan_paused:
            request = self.send("cta_scan_resume")
            self.cta_status.setText("Đang tiếp tục quét từ vị trí đã tạm dừng…")
        else:
            request = self.send("cta_scan_pause")
            self.cta_status.setText("Đang tạm dừng ở cuối khung hình hiện tại…")
        if request:
            self.cta_pause_pending = True
            self.cta_pause_btn.setEnabled(False)
        else:
            self.cta_status.setText("Không gửi được lệnh điều khiển CTA Detector tới Engine.")

    def _cta_stop_scan(self):
        if not self.cta_scan_active:
            return
        self.cta_stop_btn.setEnabled(False)
        self.cta_status.setText("Đang dừng quét… các khoảng CTA đã tìm thấy sẽ được giữ lại.")
        self.send("cta_scan_cancel")

    def _cta_item_changed(self, item):
        if self._cta_table_updating or item.column() != 1:
            return
        row = item.row()
        if item.checkState() == Qt.CheckState.Checked:
            candidate = self.cta_candidates[row] if 0 <= row < len(self.cta_candidates) else {}
            confidence = float(candidate.get("confidence", 0) or 0)
            if confidence < 50.0:
                answer = QMessageBox.question(
                    self,
                    "Ứng viên CTA có độ tin cậy thấp",
                    f"OCR chỉ đạt {confidence:.0f}/100 và có thể đọc nhầm chữ. "
                    "Hãy xem ảnh bằng chứng bên phải trước khi chọn cắt.\n\n"
                    "Bạn vẫn muốn đưa khoảng này vào danh sách cắt?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self._cta_table_updating = True
                    item.setCheckState(Qt.CheckState.Unchecked)
                    self._cta_table_updating = False
                    self.cta_status.setText("Đã bỏ chọn ứng viên OCR độ tin cậy thấp.")
                    return
            self.cta_selected_rows.add(row)
        else:
            self.cta_selected_rows.discard(row)
        self.cta_cut_btn.setEnabled(bool(self.cta_selected_rows) and not self.cta_scan_active)
        self.cta_status.setText(
            f"Đã chọn {len(self.cta_selected_rows)}/{len(self.cta_candidates)} khoảng. "
            "Hãy kiểm tra ảnh mẫu và chữ OCR trước khi cắt."
        )


    def _cta_candidate_description(self, candidate):
        """Show only the OCR keyword evidence; scene/animation labels are not used."""
        terms = " · ".join(str(value) for value in (candidate.get("terms", []) or []))
        text = str(candidate.get("text", "") or "").strip()
        parts = [f"Từ khóa: {terms or candidate.get('label', 'CTA')}"]
        if text:
            parts.append(f'OCR: "{text[:100]}"')
        parts.append(f'{int(candidate.get("samples", 0) or 0)} ảnh mẫu')
        return " · ".join(parts)


    def _cta_candidate_search_blob(self, candidate):
        values = [
            candidate.get("text", ""), candidate.get("label", ""),
            candidate.get("method", ""), candidate.get("location", ""),
            self._cta_candidate_description(candidate),
        ]
        values.extend(candidate.get("terms", []) or [])
        return " ".join(str(value) for value in values).casefold()


    def _cta_candidate_matches_filter(self, candidate, search_text, filter_index):
        if search_text and search_text not in self._cta_candidate_search_blob(candidate):
            return False
        terms = {str(value).upper() for value in (candidate.get("terms", []) or [])}
        if not terms:
            terms = {
                part.strip().upper()
                for part in re.split(r"[·,/|]+", str(candidate.get("label", "")))
                if part.strip()
            }
        target = {1: "LIKE", 2: "SHARE", 3: "SUBSCRIBE"}.get(int(filter_index))
        return target in terms if target else True


    def _cta_first_visible_row(self):
        for row in range(self.cta_table.rowCount()):
            if not self.cta_table.isRowHidden(row):
                return row
        return -1

    def _cta_apply_filters(self):
        if not hasattr(self, "cta_table"):
            return
        search_text = str(self.cta_search.text() or "").strip().casefold() if hasattr(self, "cta_search") else ""
        filter_index = int(self.cta_filter.currentIndex()) if hasattr(self, "cta_filter") else 0
        visible = 0
        for row, candidate in enumerate(self.cta_candidates):
            matches = self._cta_candidate_matches_filter(candidate, search_text, filter_index)
            self.cta_table.setRowHidden(row, not matches)
            visible += int(matches)
        if hasattr(self, "cta_filter_info"):
            self.cta_filter_info.setText(f"{visible}/{len(self.cta_candidates)} kết quả")
        current = self.cta_table.currentRow()
        if current >= 0 and self.cta_table.isRowHidden(current):
            first = self._cta_first_visible_row()
            if first >= 0:
                self.cta_table.setCurrentCell(first, 1)
                self._cta_current_changed(first, 0, current, 0)

    def _cta_current_changed(self, row, _col, _prev_row, _prev_col):
        if self.cta_preview_active:
            self._cta_stop_preview()
        if row < 0 or row >= len(self.cta_candidates):
            self.cta_preview.set_source("", "Chọn một kết quả để xem ảnh OCR")
            self.cta_preview_caption.setText("Chưa chọn kết quả."); self.cta_start.setEnabled(False); self.cta_end.setEnabled(False)
            self.cta_play_btn.setEnabled(False); return
        item = self.cta_candidates[row]
        path = str(item.get("preview_path", "") or "")
        if path and Path(path).is_file():
            placeholder = "Không đọc được ảnh bằng chứng."
        elif self.cta_scan_active:
            placeholder = "Đang trích xuất ảnh bằng chứng từ khung hình OCR…"
        else:
            detail = str(item.get("thumbnail_error", "") or "").strip()
            placeholder = ("Không tạo được ảnh mẫu. " + detail[:150]) if detail else "Không có ảnh bằng chứng cho kết quả này."
        self.cta_preview.set_source(path, placeholder)
        self.cta_start.setEnabled(not self.cta_scan_active); self.cta_end.setEnabled(not self.cta_scan_active)
        self.cta_play_btn.setEnabled(bool(self.state.get("video_path")) and not self.state.get("busy"))
        self.cta_start.setValue(float(item.get("start", 0)) / 60.0); self.cta_end.setValue(float(item.get("end", 0)) / 60.0)
        terms = " · ".join(str(value) for value in (item.get("terms", []) or [])) or str(item.get("label", "CTA"))
        text = str(item.get("text", item.get("label", "CTA")))
        confidence = float(item.get("confidence", 0) or 0)
        confidence_note = (
            "\n⚠ Độ tin cậy thấp — kiểm tra kỹ ảnh trước khi chọn cắt."
            if confidence < 50.0 else ""
        )
        compact_caption = (
            f"{terms} · OCR {confidence:.0f}/100 · {item.get('location','—')}\n"
            f"Chữ nhận diện: {text}{confidence_note}"
        )
        full_caption = (
            f"Từ khóa: {terms}\n"
            f"Thời gian: {self._fmt_time(item.get('start', 0))} – {self._fmt_time(item.get('end', 0))}\n"
            f"Độ tin cậy OCR: {confidence:.0f}/100 · {item.get('samples', 0)} ảnh mẫu\n"
            f"Chữ đọc được: {text}\n"
            "Hãy đối chiếu ảnh mẫu rồi mới chọn khoảng cần cắt."
        )
        self.cta_preview_caption.setText(compact_caption)
        self.cta_preview_caption.setToolTip(full_caption)

    def _cta_preview_range(self):
        row = self.cta_table.currentRow()
        if row < 0 or row >= len(self.cta_candidates):
            return None
        try:
            start = float(self.cta_start.value()) * 60.0
            end = float(self.cta_end.value()) * 60.0
        except (TypeError, ValueError):
            return None
        if end <= start:
            return None
        return start, end

    def _cta_toggle_preview(self):
        if self.cta_preview_active:
            self._cta_stop_preview()
            return
        if not self.state.get("video_path"):
            self._notice("Chưa có video", "Hãy mở video nguồn trước khi xem đoạn CTA.", "warning")
            return
        selected = self._cta_preview_range()
        if selected is None:
            self._notice("Mốc xem chưa hợp lệ", "Mốc kết thúc phải lớn hơn mốc bắt đầu.", "warning")
            return
        self.cta_preview_active = True
        self.cta_preview_cursor, self.cta_preview_end = selected
        self.cta_preview_request_id = None
        self.cta_play_btn.setText("Dừng xem")
        self.cta_play_status.setText(f"Đang xem · {self._fmt_time(self.cta_preview_cursor)}")
        self.cta_start.setEnabled(False); self.cta_end.setEnabled(False)
        self.cta_preview_timer.stop()
        self.cta_preview_request_id = self.send(
            "cta_preview_start", start=self.cta_preview_cursor, end=self.cta_preview_end
        )

    def _cta_preview_tick(self):
        # CTA preview is streamed by one persistent decoder in the worker.
        return

    def _cta_stop_preview(self, notify_worker: bool = True):
        was_active = bool(self.cta_preview_active)
        self.cta_preview_active = False
        self.cta_preview_request_id = None
        self.cta_preview_timer.stop()
        if notify_worker and was_active:
            self.send("cta_preview_stop")
        if hasattr(self, "cta_play_btn"):
            self.cta_play_btn.setText("Xem đoạn đã chọn")
            row = self.cta_table.currentRow()
            if 0 <= row < len(self.cta_candidates):
                self.cta_preview.set_source(self.cta_candidates[row].get("preview_path", ""), "Không có thumbnail")
            self.cta_play_status.setText("Đã dừng xem đoạn")
            self.cta_start.setEnabled(not self.cta_scan_active)
            self.cta_end.setEnabled(not self.cta_scan_active)

    def _cta_cut_selected(self):
        if self.cta_scan_active:
            self._notice("CTA đang quét", "Hãy hoàn tất hoặc dừng quét trước khi cắt các khoảng CTA đã chọn.", "warning"); return
        if not self.cta_selected_rows:
            self._notice("Chưa chọn khoảng CTA", "Tick ít nhất một khoảng CTA đã kiểm tra ảnh mẫu.", "warning"); return
        ranges = []
        current = self.cta_table.currentRow()
        for row in sorted(self.cta_selected_rows):
            if not (0 <= row < len(self.cta_candidates)): continue
            item = self.cta_candidates[row]
            start, end = float(item.get("start", 0)), float(item.get("end", 0))
            if row == current:
                start, end = self.cta_start.value() * 60.0, self.cta_end.value() * 60.0
            if end <= start:
                self._notice("Mốc chưa hợp lệ", "Mốc kết thúc phải lớn hơn mốc bắt đầu.", "warning"); return
            ranges.append({"start": start, "end": end})
        total = sum(max(0, r["end"] - r["start"]) for r in ranges)
        answer = QMessageBox.question(self, "Xác nhận cắt các khoảng CTA",
            f"Đã chọn {len(ranges)} khoảng (khoảng {self._fmt_time(total)}).\n\nVideo Studio sẽ bỏ các khoảng này, giữ đồng bộ hình/tiếng và xuất MP4 mới. Video nguồn không bị thay đổi. Tiếp tục?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes: return
        self.cta_cut_btn.setEnabled(False)
        self.send("cta_cut_selected", ranges=ranges)

    def _cta_candidate_key(self, candidate):
        if candidate.get("scan_id") is not None:
            return ("scan", int(candidate.get("scan_id")))
        return ("fallback", round(float(candidate.get("start", 0)), 2),
                round(float(candidate.get("end", 0)), 2), str(candidate.get("kind", "")))

    @staticmethod
    def _cta_duration_seconds(candidate):
        """Return a stable numeric duration even for older detector payloads."""
        try:
            duration = float(candidate.get("duration", 0) or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            try:
                duration = float(candidate.get("end", 0) or 0) - float(candidate.get("start", 0) or 0)
            except (TypeError, ValueError):
                duration = 0.0
        return max(0.0, duration)

    def _update_cta_sort_header(self):
        labels = ["Ảnh mẫu", "Chọn", "Khoảng thời gian", "Dài", "Từ khóa", "Chữ OCR", "Tin cậy", "Vị trí"]
        if self.cta_sort_mode == "duration":
            labels[3] = "Dài ↓" if self.cta_sort_desc else "Dài ↑"
        self.cta_table.setHorizontalHeaderLabels(labels)

    def _cta_sort_clicked(self, column):
        if column != 3:
            return
        if self.cta_sort_mode != "duration":
            self.cta_sort_mode = "duration"
            self.cta_sort_desc = False
        else:
            self.cta_sort_desc = not self.cta_sort_desc
        self._update_cta_sort_header()
        # Re-render through the same path used by live scan snapshots so that
        # checkboxes, the preview row, and the cut action remain consistent.
        self._cta_refresh_candidates(self.cta_candidates, replace=True)

    def _cta_thumbnail_widget(self, candidate):
        """Show a compact frame preview beside every candidate row."""
        path = str(candidate.get("preview_path", "") or "")
        label = QLabel()
        label.setFixedSize(104, 60)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(
            "background:#081923;border:1px solid #254957;border-radius:6px;"
            "color:#8aa7b2;font-size:8pt;"
        )
        pixmap = QPixmap(path) if path and Path(path).is_file() else QPixmap()
        if not pixmap.isNull():
            label.setPixmap(pixmap.scaled(
                98, 54,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            label.setToolTip("Khung hình tại thời điểm OCR nhận ra từ khóa CTA")
        else:
            if self.cta_scan_active:
                label.setText("Đang tạo ảnh…")
                label.setToolTip("Đang gắn ảnh bằng chứng từ chính khung hình OCR.")
            else:
                label.setText("Ảnh lỗi")
                detail = str(candidate.get("thumbnail_error", "") or "").strip()
                label.setToolTip(detail or "Không có ảnh bằng chứng được lưu cho kết quả này.")
        return label


    def _cta_refresh_candidates(self, candidates, replace=False):
        # Preserve checkboxes/current selection when live snapshots update rows.
        selected_keys = set()
        current_key = None
        for row in self.cta_selected_rows:
            if 0 <= row < len(self.cta_candidates):
                selected_keys.add(self._cta_candidate_key(self.cta_candidates[row]))
        current_row = self.cta_table.currentRow()
        if 0 <= current_row < len(self.cta_candidates):
            current_key = self._cta_candidate_key(self.cta_candidates[current_row])

        if replace or not self.cta_candidates:
            selected_keys.update(
                self._cta_candidate_key(item) for item in candidates if bool(item.get("selected", False))
            )
            merged = {self._cta_candidate_key(item): dict(item) for item in candidates}
        else:
            merged = {self._cta_candidate_key(item): dict(item) for item in self.cta_candidates}
            for item in candidates:
                key = self._cta_candidate_key(item)
                if key in merged:
                    # Same candidate in the current live snapshot: replace its
                    # cumulative OCR data and thumbnail with the newest version.
                    merged[key] = dict(item)
                    continue
                boundary_key = None
                boundary_value = None
                if self.cta_scan_preserve_existing:
                    for old_key in self.cta_boundary_merge_keys:
                        old_candidate = merged.get(old_key)
                        if old_candidate is None:
                            continue
                        combined = merge_boundary_candidate(
                            old_candidate, item, max_gap=self.cta_boundary_merge_gap
                        )
                        if combined is not None:
                            boundary_key, boundary_value = old_key, combined
                            break
                if boundary_key is not None:
                    merged[boundary_key] = boundary_value
                else:
                    merged[key] = dict(item)
        updated = list(merged.values())
        if self.cta_sort_mode == "duration":
            updated.sort(key=self._cta_duration_seconds, reverse=self.cta_sort_desc)
        else:
            updated.sort(key=lambda item: (float(item.get("start", 0)), -float(item.get("confidence", 0))))
        self.cta_candidates = updated

        self._cta_table_updating = True
        self.cta_table.setUpdatesEnabled(False)
        self.cta_table.setRowCount(len(self.cta_candidates))
        self.cta_selected_rows.clear()
        new_current_row = -1
        try:
            for row, candidate in enumerate(self.cta_candidates):
                self.cta_table.setCellWidget(row, 0, self._cta_thumbnail_widget(candidate))
                choice = QTableWidgetItem("Chọn")
                choice.setFlags(choice.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                key = self._cta_candidate_key(candidate)
                is_selected = key in selected_keys
                choice.setCheckState(Qt.CheckState.Checked if is_selected else Qt.CheckState.Unchecked)
                self.cta_table.setItem(row, 1, choice)
                self.cta_table.setItem(row, 2, QTableWidgetItem(f"{self._fmt_time(candidate.get('start',0))} – {self._fmt_time(candidate.get('end',0))}"))
                self.cta_table.setItem(row, 3, QTableWidgetItem(self._fmt_time(candidate.get("duration", 0))))
                recognized = str(candidate.get("text", "") or candidate.get("label", "—"))
                terms = " · ".join(str(value) for value in (candidate.get("terms", []) or [])) or str(candidate.get("label", "CTA"))
                term_item = QTableWidgetItem(terms)
                term_item.setToolTip(terms)
                self.cta_table.setItem(row, 4, term_item)
                text_item = QTableWidgetItem(recognized)
                text_item.setToolTip(recognized)
                self.cta_table.setItem(row, 5, text_item)
                confidence_value = float(candidate.get("confidence", 0) or 0)
                confidence_item = QTableWidgetItem(f"{confidence_value:.0f}%")
                if confidence_value < 50.0:
                    confidence_item.setForeground(QColor("#ffb86b"))
                    confidence_item.setToolTip("Độ tin cậy OCR thấp; khi chọn cắt sẽ cần xác nhận thêm.")
                else:
                    confidence_item.setToolTip("Độ tin cậy OCR; hãy đối chiếu ảnh bằng chứng trước khi chọn cắt.")
                self.cta_table.setItem(row, 6, confidence_item)
                self.cta_table.setItem(row, 7, QTableWidgetItem(str(candidate.get("location", "—"))))
                if is_selected:
                    self.cta_selected_rows.add(row)
                if current_key is not None and key == current_key:
                    new_current_row = row
        finally:
            self._cta_table_updating = False
            self.cta_table.setUpdatesEnabled(True)
            self.cta_table.viewport().update()
        self._cta_apply_filters()
        if new_current_row >= 0 and not self.cta_table.isRowHidden(new_current_row):
            self.cta_table.setCurrentCell(new_current_row, 1)
            self._cta_current_changed(new_current_row, 0, -1, -1)
        elif self.cta_table.currentRow() < 0:
            first = self._cta_first_visible_row()
            if first >= 0:
                self.cta_table.setCurrentCell(first, 1)
                self._cta_current_changed(first, 0, -1, -1)
        elif self.cta_candidates and current_key is None and current_row < 0:
            # Do not steal keyboard focus while the user is working in other controls.
            pass
        self.cta_cut_btn.setEnabled(bool(self.cta_selected_rows) and not self.cta_scan_active)

    def _flush_cta_live_update(self):
        payload = self._cta_pending_live_payload
        self._cta_pending_live_payload = None
        if not payload:
            return
        incoming = list(payload.get("candidates", []))
        self._cta_refresh_candidates(incoming, replace=False)
        if self.cta_candidates and self.cta_table.currentRow() < 0:
            first = self._cta_first_visible_row()
            if first >= 0:
                self.cta_table.setCurrentCell(first, 1)

    def _cta_phase_update(self, payload):
        self.cta_phase_label = str(payload.get("label", "Đang quét CTA Detector"))
        if payload.get("resume_time") is not None:
            self.cta_cache_metadata.update({
                "resume_time": float(payload.get("resume_time", 0) or 0),
                "resume_phase": str(payload.get("phase", "") or "pass1"),
            })
            self._schedule_cta_cache_save()
        if self.cta_scan_active:
            self.cta_status.setText(self.cta_phase_label + " · ứng viên được cập nhật trực tiếp.")

    def _cta_live_update(self, payload):
        # Keep only the newest complete snapshot and redraw at a fixed cadence.
        # Rebuilding every table cell for every scanner callback made long scans lag.
        self._cta_pending_live_payload = dict(payload)
        if not self.cta_live_refresh_timer.isActive():
            self.cta_live_refresh_timer.start()
        self.cta_cache_metadata.update({
            "complete": False,
            "samples": int(payload.get("done", 0) or 0),
            "sample_interval": float(payload.get("sample_interval", self.cta_cache_metadata.get("sample_interval", 0)) or 0),
            "resume_time": float(payload.get("resume_time", self.cta_cache_metadata.get("resume_time", 0)) or 0),
            "resume_phase": str(payload.get("phase", self.cta_cache_metadata.get("resume_phase", "pass1")) or "pass1"),
        })
        self._schedule_cta_cache_save()
        done = int(payload.get("done", 0)); total = max(1, int(payload.get("total", 1)))
        duration = float(self.state.get("duration", 0) or 0)
        resume_time = float(payload.get("resume_time", self.cta_cache_metadata.get("resume_time", 0)) or 0)
        pct = int(100 * resume_time / duration) if duration > 0 else int(done * 100 / total)
        self.cta_progress.setValue(max(0, min(100, pct)))
        found = int(payload.get("found", len(payload.get("candidates", [])) or len(self.cta_candidates)))
        self.cta_status.setText(
            f"{self.cta_phase_label} · {self._fmt_scan_key(resume_time)} / "
            f"{self._fmt_scan_key(duration)} ({pct}%) · đã phát hiện {found} ứng viên CTA. "
            "Ảnh bằng chứng được gắn ngay khi có kết quả."
        )

    def _cta_scan_paused(self, message, checkpoint=None):
        self.cta_scan_paused = True; self.cta_pause_pending = False
        self.cta_pause_btn.setText("Tiếp tục")
        self.cta_pause_btn.setEnabled(True)
        if isinstance(checkpoint, dict) and checkpoint.get("resume_time") is not None:
            self.cta_cache_metadata.update({
                "complete": False,
                "resume_time": max(0.0, float(checkpoint.get("resume_time", 0) or 0)),
                "resume_phase": str(checkpoint.get("phase", "pass1") or "pass1"),
            })
        self.cta_status.setText(message or "Đã tạm dừng. Kết quả đã quét vẫn được giữ nguyên.")
        # Write synchronously at the Pause boundary so a later app/engine crash
        # cannot discard the exact next frame to scan.
        self._save_cta_cache_now()

    def _cta_scan_resumed(self, message):
        self.cta_scan_paused = False; self.cta_pause_pending = False
        self.cta_pause_btn.setText("Tạm dừng")
        self.cta_pause_btn.setEnabled(True)
        self.cta_status.setText(message or "Đang tiếp tục quét từ vị trí đã tạm dừng…")

    def _cta_scan_finished(self, payload):
        self.cta_live_refresh_timer.stop()
        self._cta_pending_live_payload = None
        self._cta_refresh_candidates(
            list(payload.get("candidates", [])),
            replace=not self.cta_scan_preserve_existing,
        )
        self.cta_cache_metadata = {
            "complete": True,
            "samples": int(payload.get("samples", 0) or 0),
            "pass1_samples": int(payload.get("pass1_samples", 0) or 0),
            "pass2_samples": int(payload.get("pass2_samples", 0) or 0),
            "sample_interval": float(payload.get("sample_interval", 0) or 0),
            "pass1_interval": float(payload.get("pass1_interval", 0) or 0),
            "pass2_interval": float(payload.get("pass2_interval", 0) or 0),
            "sample_cap_reached": bool(payload.get("sample_cap_reached", False)),
            "pass2_sample_cap_reached": bool(payload.get("pass2_sample_cap_reached", False)),
            "resume_time": float(payload.get("resume_time", self.state.get("duration", 0)) or 0),
            "resume_phase": "done",
        }
        self._save_cta_cache_now()
        self.cta_scan_preserve_existing = False
        self.cta_scan_active = False; self.cta_scan_paused = False; self.cta_pause_pending = False
        self.cta_progress.setValue(100)
        self.cta_scan_btn.setEnabled(True); self.cta_pause_btn.setEnabled(False)
        self.cta_pause_btn.setText("Tạm dừng"); self.cta_stop_btn.setEnabled(False)
        self.cta_cut_btn.setEnabled(bool(self.cta_selected_rows))
        sample_density = float(payload.get("sample_interval", 0) or 0)
        density_text = f"mật độ lấy mẫu khoảng {sample_density:.2f} giây/ảnh" if sample_density > 0 else "mật độ lấy mẫu không xác định"
        cap_text = " · đã chạm giới hạn an toàn số ảnh mẫu" if payload.get("sample_cap_reached") else ""
        timeout_count = int(payload.get("ocr_timeouts", 0) or 0)
        timeout_text = f" · bỏ qua {timeout_count} ảnh OCR Timeout" if timeout_count else ""
        self.cta_status.setText(
            f"Hoàn tất: {len(self.cta_candidates)} khoảng CTA · "
            f"{payload.get('samples',0)} ảnh mẫu · {density_text}{cap_text}{timeout_text}."
        )
        self.cta_status.setToolTip(str(payload.get("note", "")))
        if self.cta_candidates and self.cta_table.currentRow() < 0:
            first = self._cta_first_visible_row()
            if first >= 0:
                self.cta_table.setCurrentCell(first, 1)
        if self.cta_candidates and self.cta_table.currentRow() >= 0:
            row = self.cta_table.currentRow()
            self._cta_current_changed(row, 0, -1, -1)
        elif not self.cta_candidates:
            self._cta_current_changed(-1, -1, -1, -1)

    def _cta_scan_cancelled(self, message):
        self.cta_live_refresh_timer.stop()
        self._flush_cta_live_update()
        self.cta_scan_active = False; self.cta_scan_paused = False; self.cta_pause_pending = False
        self.cta_scan_btn.setEnabled(True)
        self.cta_stop_btn.setEnabled(False)
        self.cta_cut_btn.setEnabled(bool(self.cta_selected_rows))
        found = len(self.cta_candidates)
        self.cta_cache_metadata["complete"] = False
        self._save_cta_cache_now()
        can_resume = bool(self.state.get("video_path")) and self._cta_resume_time() > 0.0
        self.cta_pause_btn.setText("Tiếp tục" if can_resume else "Tạm dừng")
        self.cta_pause_btn.setEnabled(can_resume)
        self.cta_scan_preserve_existing = False
        if found and self.cta_table.currentRow() >= 0:
            row = self.cta_table.currentRow()
            self._cta_current_changed(row, 0, -1, -1)
        suffix = f" · giữ lại {found} khoảng Subscribe đã phát hiện." if found else ""
        self.cta_status.setText((message or "Đã dừng quét CTA Detector.") + suffix)

    def _build_center(self):
        panel = QFrame(); panel.setObjectName("CenterPanel")
        layout = QVBoxLayout(panel); layout.setContentsMargins(7, 4, 7, 4); layout.setSpacing(7)

        # One large preview surface. Before/After uses the same central area,
        # instead of tiny thumbnails in the inspector.
        self.preview_tabs = QTabWidget()
        self.preview = PreviewCanvas()
        self.preview.setMinimumSize(460, 300)
        self.preview.selectionReady.connect(lambda pts, shape: self.send("set_selection", points=pts, shape=shape))
        self.preview.strokeReady.connect(lambda pts, erase: self.send("mask_stroke", points=pts, erase=erase))
        self.preview.videoDropped.connect(self._open_dropped_video)
        self.preview_tabs.addTab(self.preview, "Video")

        compare_page = QWidget()
        compare_layout = QVBoxLayout(compare_page)
        compare_layout.setContentsMargins(8, 8, 8, 8); compare_layout.setSpacing(7)
        compare_images = QHBoxLayout(); compare_images.setSpacing(10)
        before_col = QVBoxLayout(); after_col = QVBoxLayout()
        before_title = QLabel("TRƯỚC · VIDEO GỐC"); before_title.setObjectName("SectionTitle")
        after_title = QLabel("SAU · KẾT QUẢ INPAINT"); after_title.setObjectName("SectionTitle")
        self.compare_before_large = FitPixmapLabel()
        self.compare_after_large = FitPixmapLabel()
        self.compare_before_large.setMinimumSize(220, 190)
        self.compare_after_large.setMinimumSize(220, 190)
        before_col.addWidget(before_title); before_col.addWidget(self.compare_before_large, 1)
        after_col.addWidget(after_title); after_col.addWidget(self.compare_after_large, 1)
        compare_images.addLayout(before_col, 1); compare_images.addLayout(after_col, 1)
        compare_layout.addLayout(compare_images, 1)
        self.compare_large_meta = QLabel("Chưa có ảnh so sánh. Vào Mask và chọn ‘Tạo & xem so sánh lớn’.")
        self.compare_large_meta.setObjectName("Muted"); self.compare_large_meta.setWordWrap(True)
        compare_layout.addWidget(self.compare_large_meta)
        self.preview_tabs.addTab(compare_page, "Before / After")
        self.preview_tabs.setTabToolTip(1, "Xem hai ảnh so sánh ở kích thước lớn trong vùng preview trung tâm")
        layout.addWidget(self.preview_tabs, 1)

        time_row = QHBoxLayout(); time_row.setSpacing(8)
        self.seek = ClickSeekSlider(Qt.Orientation.Horizontal); self.seek.setRange(0, 1000)
        self.seek.setSingleStep(100); self.seek.setPageStep(1000); self.seek.setTracking(False)
        self.seek.setToolTip("Nhấp vào bất kỳ vị trí nào trên thanh để nhảy tới đó; vẫn có thể kéo nút trượt để tinh chỉnh.")
        self.seek.sliderMoved.connect(self._seek_preview_position)
        self.seek.sliderReleased.connect(self._seek_released)
        self.seek_input = QLineEdit(); self.seek_input.setPlaceholderText("MM:SS")
        self.seek_input.setToolTip("MM:SS")
        self.seek_input.setFixedWidth(122); self.seek_input.returnPressed.connect(self._seek_from_timecode)
        self.seek_go_btn = QPushButton("Đi tới"); self.seek_go_btn.setFixedWidth(64)
        self.seek_go_btn.clicked.connect(self._seek_from_timecode)
        self.seek_label = QLabel("00:00:00 / 00:00:00"); self.seek_label.setObjectName("Muted")
        self.seek_label.setMinimumWidth(142); self.seek_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        time_row.addWidget(self.seek, 1); time_row.addWidget(self.seek_input); time_row.addWidget(self.seek_go_btn); time_row.addWidget(self.seek_label)
        layout.addLayout(time_row)

        return panel

    def _build_inspector(self):
        panel = QFrame(); panel.setObjectName("Inspector"); panel.setMinimumWidth(360); panel.setMaximumWidth(445)
        layout = QVBoxLayout(panel); layout.setContentsMargins(14, 13, 14, 13); layout.setSpacing(9)
        self.inspector_title = QLabel("Thuộc tính cảnh"); self.inspector_title.setObjectName("ProjectTitle")
        layout.addWidget(self.inspector_title)
        self.inspector_stack = QStackedWidget()
        self.inspector_stack.addWidget(self._build_edit_tab())
        self.inspector_stack.addWidget(self._build_mask_tab())
        self.inspector_stack.addWidget(self._build_branding_tab())
        self.inspector_stack.addWidget(self._build_effects_tab())
        self.inspector_stack.addWidget(self._build_export_tab())
        layout.addWidget(self.inspector_stack, 1)
        return panel

    def _scroll_tab(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); content = QWidget(); v = QVBoxLayout(content)
        v.setContentsMargins(14, 13, 14, 13); v.setSpacing(12); scroll.setWidget(content)
        return scroll, content, v

    def _section(self, layout, text):
        label = QLabel(text); label.setObjectName("SectionTitle"); layout.addWidget(label)

    def _build_edit_tab(self):
        scroll, content, v = self._scroll_tab()
        self._section(v, "Phân cảnh đang chọn")
        self.edit_scene_name = QLabel("Chưa chọn phân cảnh")
        self.edit_scene_name.setStyleSheet("font-size:12pt; font-weight:700; color:#eaf4f7;")
        self.edit_scene_name.setWordWrap(True); v.addWidget(self.edit_scene_name)
        self.edit_scene_range = QLabel("Thời gian: —")
        self.edit_scene_range.setObjectName("Muted"); v.addWidget(self.edit_scene_range)
        self.edit_scene_duration = QLabel("Thời lượng: —")
        self.edit_scene_duration.setObjectName("Muted"); v.addWidget(self.edit_scene_duration)
        self._section(v, "Video nguồn")
        self.edit_video_info = QLabel("Chưa có video nguồn")
        self.edit_video_info.setObjectName("Muted"); self.edit_video_info.setWordWrap(True); v.addWidget(self.edit_video_info)
        self._section(v, "Tổng quan")
        self.edit_scene_count = QLabel("Phân cảnh: 0")
        self.edit_scene_count.setObjectName("Muted"); v.addWidget(self.edit_scene_count)
        v.addStretch(1)
        return scroll

    def _build_mask_tab(self):
        scroll, content, v = self._scroll_tab()
        self._section(v, "Vùng xử lý (Mask)")
        self.mask_region_label = QLabel("Chưa tạo vùng mask"); self.mask_region_label.setObjectName("Muted")
        self.mask_region_label.setWordWrap(True); v.addWidget(self.mask_region_label)
        self._section(v, "Công cụ Mask")
        tools = QGridLayout(); self.tool_group = QButtonGroup(self); self.tool_group.setExclusive(True)
        self.tool_buttons = {}
        tool_tips = {
            "lasso": "Khoanh vùng logo hoặc watermark theo đường viền tự do.",
            "rect": "Khoanh nhanh một vùng hình chữ nhật.",
            "brush": "Tô thêm vùng cần xóa vào mask đỏ hiện có. Phải tạo mask trước.",
            "erase": "Tẩy phần mask đỏ bị lem hoặc che nhầm vùng hình ảnh.",
        }
        for i, (label, mode) in enumerate([("Khoanh vùng", "lasso"), ("Khung chữ nhật", "rect"), ("Brush", "brush"), ("Eraser", "erase")]):
            b = QPushButton(label); self._set_vector_icon(b, {"rect": "rectangle", "erase": "eraser"}.get(mode, mode), 18); b.setIconSize(QSize(18, 18))
            b.setToolTip(tool_tips[mode])
            b.setCheckable(True); b.setObjectName("Tool"); b.clicked.connect(lambda _=False, m=mode: self._set_tool(m))
            self.tool_group.addButton(b); self.tool_buttons[mode] = b; tools.addWidget(b, i // 2, i % 2)
            if i == 0: b.setChecked(True)
        v.addLayout(tools)
        brush_row = QHBoxLayout(); brush_row.addWidget(QLabel("Kích thước bút")); brush_row.addStretch(1); self.brush_value = QLabel("24 px"); brush_row.addWidget(self.brush_value); v.addLayout(brush_row)
        self.brush_slider = QSlider(Qt.Orientation.Horizontal); self.brush_slider.setRange(4, 100); self.brush_slider.setValue(24); self.brush_slider.valueChanged.connect(lambda x: (self.brush_value.setText(f"{x} px"), self.send("set_brush_size", value=x))); v.addWidget(self.brush_slider)
        self._section(v, "Điều chỉnh vùng")
        exp_row = QHBoxLayout(); exp_row.addWidget(QLabel("Expand / Shrink")); exp_row.addStretch(1); self.expansion_value = QSpinBox(); self.expansion_value.setRange(0, 30); self.expansion_value.setValue(14); self.expansion_value.valueChanged.connect(lambda x: self.send("set_expansion", value=x)); self.expansion_value.setFixedWidth(74); exp_row.addWidget(self.expansion_value); exp_row.addWidget(QLabel("px")); v.addLayout(exp_row)
        self.auto_expansion = QCheckBox("Smart Mask · tự đề xuất PX"); self.auto_expansion.setChecked(True); self.auto_expansion.toggled.connect(lambda x: self.send("set_auto_expansion", value=x)); v.addWidget(self.auto_expansion)
        self.show_mask = QCheckBox("Hiện mask màu đỏ"); self.show_mask.setChecked(True); self.show_mask.toggled.connect(lambda x: self.send("set_show_mask", value=x)); v.addWidget(self.show_mask)
        self._section(v, "Tạo & kiểm tra")
        self.auto_extract_btn = QPushButton("Tự tách nét (nếu phù hợp)"); self._set_vector_icon(self.auto_extract_btn, "sparkles", 17); self.auto_extract_btn.setObjectName("Primary"); self.auto_extract_btn.clicked.connect(lambda: self.send("auto_extract")); v.addWidget(self.auto_extract_btn)
        self.use_selection_mask_btn = QPushButton("Dùng toàn bộ vùng khoanh"); self._set_vector_icon(self.use_selection_mask_btn, "check", 17); self.use_selection_mask_btn.clicked.connect(lambda: self.send("use_selection_mask")); v.addWidget(self.use_selection_mask_btn)
        self.clear_selection_btn = QPushButton("Xóa vùng khoanh / mask"); self._set_vector_icon(self.clear_selection_btn, "trash", 17); self.clear_selection_btn.clicked.connect(lambda: self.send("clear_selection")); v.addWidget(self.clear_selection_btn)
        self.mask_busy_widgets = [
            *self.tool_buttons.values(), self.brush_slider, self.expansion_value,
            self.auto_expansion, self.show_mask, self.auto_extract_btn,
            self.use_selection_mask_btn, self.clear_selection_btn,
        ]
        self._section(v, "Before / After")
        compare_btn = QPushButton("Tạo & xem so sánh lớn"); compare_btn.setObjectName("Primary")
        compare_btn.setToolTip("Hiển thị ảnh gốc và ảnh Inpaint cạnh nhau trong vùng preview trung tâm")
        compare_btn.clicked.connect(self.open_compare_dialog); v.addWidget(compare_btn)
        self.compare_meta_label = QLabel("")
        self.compare_meta_label.setObjectName("Muted"); self.compare_meta_label.setWordWrap(True); v.addWidget(self.compare_meta_label)
        v.addStretch(1)
        return scroll

    def _build_branding_tab(self):
        # Kept as an inert compatibility slot in the inspector stack. Branding
        # is hosted as a full-width page in workspace_stack below.
        return QWidget()

    def _build_effects_tab(self):
        scroll, content, v = self._scroll_tab()
        self._section(v, "Công cụ xử lý")
        qc = QPushButton("Mở vùng kết quả QC")
        qc.clicked.connect(lambda: self._navigate("export")); v.addWidget(qc)
        v.addStretch(1)
        return scroll

    def _build_advanced_tab(self):
        scroll, content, v = self._scroll_tab()
        self._section(v, "Hiệu năng & hệ thống")
        gpu = QPushButton("Kiểm tra NVIDIA NVENC"); gpu.clicked.connect(lambda: self.send("gpu_check")); v.addWidget(gpu)
        self._section(v, "Tự động lưu")
        self.autosave_checkbox = QCheckBox("Tự động lưu và khôi phục dự án"); self.autosave_checkbox.setChecked(True); self.autosave_checkbox.toggled.connect(lambda x: self.send("set_autosave", value=x)); v.addWidget(self.autosave_checkbox)
        v.addStretch(1)
        return scroll

    def _build_export_tab(self):
        scroll, content, v = self._scroll_tab()
        self._section(v, "Thư mục xuất")
        output_row = QHBoxLayout()
        self.output_path_label = QLabel("Mặc định theo video nguồn")
        self.output_path_label.setWordWrap(True); self.output_path_label.setObjectName("Muted")
        output_row.addWidget(self.output_path_label, 1)
        outbtn = QPushButton("Chọn thư mục"); outbtn.setObjectName("Secondary")
        outbtn.clicked.connect(self.choose_output); output_row.addWidget(outbtn)
        v.addLayout(output_row)

        self._section(v, "Cắt bỏ thủ công")
        row = QGridLayout(); row.setHorizontalSpacing(8); row.setVerticalSpacing(5)
        row.addWidget(QLabel("Từ"), 0, 0); row.addWidget(QLabel("Đến"), 0, 1)
        self.minute_start = MinutePositionSpinBox(); self.minute_start.setRange(0, 99999); self.minute_start.setToolTip("MM:SS")
        self.minute_end = MinutePositionSpinBox(); self.minute_end.setRange(0, 99999); self.minute_end.setToolTip("MM:SS")
        self.minute_start.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.minute_end.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.minute_start.valueChanged.connect(self._on_range_input_changed)
        self.minute_end.valueChanged.connect(self._on_range_input_changed)
        row.addWidget(self.minute_start, 1, 0); row.addWidget(self.minute_end, 1, 1); v.addLayout(row)

        range_controls = QHBoxLayout(); range_controls.setSpacing(7)
        add_range_btn = QPushButton("Thêm khoảng bỏ"); add_range_btn.setObjectName("Secondary")
        add_range_btn.clicked.connect(self._add_manual_cut_range); range_controls.addWidget(add_range_btn)
        remove_range_btn = QPushButton("Xóa khoảng chọn"); remove_range_btn.setObjectName("Tertiary")
        remove_range_btn.clicked.connect(self._remove_manual_cut_range); range_controls.addWidget(remove_range_btn)
        v.addLayout(range_controls)
        self.manual_cut_list = QListWidget()
        self.manual_cut_list.setMinimumHeight(76); self.manual_cut_list.setMaximumHeight(138)
        self.manual_cut_list.setToolTip("Các khoảng sẽ bị loại khỏi video khi cắt.")
        v.addWidget(self.manual_cut_list)

        self.source_play_btn = QPushButton("Xem video gốc Từ–Đến")
        self.source_play_btn.setObjectName("Secondary")
        self.source_play_btn.clicked.connect(self._open_source_range_player)
        v.addWidget(self.source_play_btn)
        self.source_play_status = QLabel("")
        self.source_play_status.setObjectName("Muted"); self.source_play_status.setWordWrap(True); v.addWidget(self.source_play_status)

        self._section(v, "Xuất đoạn")
        self.export_preview_btn = QPushButton("Xem thử 12 giây")
        self.export_preview_btn.setObjectName("Secondary")
        self.export_preview_btn.clicked.connect(self._export_preview_from_range); v.addWidget(self.export_preview_btn)
        self.export_segment_btn = QPushButton("Cắt bỏ & xuất phần còn lại")
        self.export_segment_btn.setObjectName("Primary")
        self.export_segment_btn.clicked.connect(self._export_segment); v.addWidget(self.export_segment_btn)

        self._section(v, "Kiểm tra chất lượng")
        self.qc_result_label = QLabel("Chưa có báo cáo QC.")
        self.qc_result_label.setWordWrap(True); self.qc_result_label.setObjectName("Muted")
        self.qc_result_label.setMinimumHeight(45); v.addWidget(self.qc_result_label)
        self.stop_export_btn = QPushButton("Dừng tác vụ xuất")
        self.stop_export_btn.setObjectName("Danger"); self.stop_export_btn.clicked.connect(lambda: self.send("stop_export")); v.addWidget(self.stop_export_btn)
        v.addStretch(1)
        return scroll

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_project)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_project)
        QShortcut(QKeySequence("Ctrl+I"), self, activated=self.choose_video)
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=lambda: self.send("undo"))
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=lambda: self.send("redo"))

    def _check_updates_automatically(self):
        # Silent background check against the public signed-release feed.
        if not should_auto_check():
            return
        mark_checked()
        self.update_dialog.start_check(silent=True)

    def _on_update_available(self, manifest):
        version = str(manifest.get("version", ""))
        if version:
            self.update_btn.setText(f"Cập nhật v{version}")
            self.update_btn.setStyleSheet("border-color:#1b9eaa;color:#aaf5f1;font-weight:750;")
            self.update_btn.setToolTip(
                f"Đã tìm thấy Stable v{version}. Bấm một lần để tự tải, xác minh và cài đặt."
            )

    def _on_update_not_available(self):
        self.update_btn.setText("Kiểm tra & cập nhật")
        self.update_btn.setStyleSheet("")
        self.update_btn.setToolTip(
            "Bấm một lần để kiểm tra; nếu có bản mới, phần mềm tự tải, xác minh và cài đặt"
        )

    def _open_update_center(self):
        self.update_dialog.show()
        self.update_dialog.raise_()
        self.update_dialog.activateWindow()
        if self._pending_update:
            self.update_dialog.status.setText(
                "Gói cập nhật đã tải và xác minh; Video Studio sẽ tự cài sau khi tác vụ hiện tại kết thúc."
            )
            return
        if self.update_dialog.manifest:
            # A silent startup check may already have found a release. The user's
            # single click now starts the download/install without another prompt.
            self.update_dialog.start_update()
        else:
            self.update_dialog.start_check(silent=False, auto_install=True)

    def _on_update_package_ready(self):
        stage_root = self.update_dialog.stage_root
        manifest = self.update_dialog.manifest
        if not stage_root or not manifest:
            return
        self._pending_update = {"stage_root": Path(stage_root), "manifest": manifest}
        self.update_btn.setText("Gói cập nhật đã sẵn sàng")
        self.update_btn.setToolTip("Đã xác minh gói cập nhật. Ứng dụng đang chờ tác vụ hiện tại kết thúc.")
        self.update_btn.setStyleSheet("border-color:#1b9eaa;color:#aaf5f1;font-weight:750;")
        self.ready_label.setText("●  Đã tải bản mới · chờ tác vụ hoàn tất")
        self.ready_label.setStyleSheet("color:#d5ad70;font-weight:700;")
        self._update_idle_timer.start()
        self._maybe_apply_pending_update()

    def _update_is_busy(self) -> bool:
        engine_busy = bool(self.state.get("busy") or self.state.get("batch_active"))
        cta_busy = bool(self.cta_scan_active)
        downloader_busy = bool(
            self._download_thread is not None
            and self._download_thread.isRunning()
        )
        merge_busy = bool(self.merge_worker is not None and self.merge_worker.isRunning())
        pending_ipc = bool(self.pending_requests)
        return engine_busy or cta_busy or downloader_busy or merge_busy or pending_ipc

    def _maybe_apply_pending_update(self):
        if not self._pending_update or self._update_apply_started:
            self._update_idle_timer.stop()
            return
        if self._update_is_busy():
            current = []
            if self.state.get("busy"):
                current.append("Engine/Mask/Export")
            if self.state.get("batch_active"):
                current.append("Batch")
            if self.cta_scan_active:
                current.append("CTA Detector")
            if self._download_thread is not None and self._download_thread.isRunning():
                current.append("Downloader")
            if self.merge_worker is not None and self.merge_worker.isRunning():
                current.append("Ghép video")
            if self.pending_requests:
                current.append("lệnh đang chờ Engine")
            self.update_btn.setText("Đang chờ tác vụ…")
            self.update_btn.setToolTip("Đang chờ: " + ", ".join(current))
            return
        self._update_idle_timer.stop()
        self._launch_update_helper()

    def _launch_update_helper(self):
        item = self._pending_update
        if not item:
            return
        helper = APP_DIR / "updater" / "apply_helper.py"
        python_exe = Path(sys.executable)
        if python_exe.name.lower() == "pythonw.exe":
            python_exe = python_exe.with_name("python.exe")
        if not helper.is_file() or not python_exe.is_file():
            QMessageBox.critical(self, "Không thể cập nhật", "Thiếu updater helper hoặc Python trong môi trường ứng dụng. Bản hiện tại chưa bị thay đổi.")
            self._pending_update = None
            return
        stage_root = Path(item["stage_root"])
        launcher = APP_DIR / "MO_GOC_REVIEW_PHIM.vbs"
        args = [
            # Run the helper as a package module.  Calling the file by its
            # absolute path makes Python set sys.path[0] to ``updater``
            # itself, so ``from updater.core`` fails before the helper can
            # even create its diagnostic log.
            str(python_exe), "-m", "updater.apply_helper",
            "--root", str(APP_DIR),
            "--stage-root", str(stage_root),
            "--pid", str(os.getpid()),
            "--python", str(python_exe),
            "--launcher", str(launcher),
        ]
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(args, cwd=str(APP_DIR), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, creationflags=creationflags)
            self._update_apply_started = True
            self.update_btn.setText("Đang khởi động trình cập nhật…")
            self.ready_label.setText("●  Đang cập nhật · ứng dụng sẽ tự mở lại")
            QTimer.singleShot(300, self.close)
        except Exception as exc:
            QMessageBox.critical(self, "Không khởi chạy được updater", f"{exc}\n\nỨng dụng và dữ liệu hiện tại vẫn được giữ nguyên.")
            self._pending_update = None

    def _start_worker(self):
        self.worker = QProcess(self)
        self.worker.setWorkingDirectory(str(APP_DIR))
        python_exe = Path(sys.executable).with_name("python.exe")
        if not python_exe.exists():
            python_exe = Path(sys.executable)
        self.worker.setProgram(str(python_exe))
        self.worker.setArguments(["-u", str(WORKER_SCRIPT)])
        self.worker.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.worker.readyReadStandardOutput.connect(self._read_worker_stdout)
        self.worker.readyReadStandardError.connect(self._read_worker_stderr)
        self.worker.started.connect(self._worker_started)
        self.worker.errorOccurred.connect(self._on_worker_error)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()
        self._engine_watchdog = QTimer(self)
        self._engine_watchdog.setSingleShot(True)
        self._engine_watchdog.timeout.connect(self._engine_startup_timeout)
        self._engine_watchdog.start(30000)

    def _worker_started(self):
        self._worker_failure_reported = False
        self.save_state.setText("●  Engine đang khởi động…")
        self.ready_label.setText("●  Đang kết nối engine")
        if hasattr(self, "sidebar_engine_status"):
            self.sidebar_engine_status.setText("● Engine đang khởi động")
            self.sidebar_engine_status.setStyleSheet("color:#d5ad70; font-size:8pt; font-weight:650;")

    def _engine_startup_timeout(self):
        if self.engine_ready or self._closing:
            return
        process_state = self.worker.state().name if hasattr(self.worker.state(), "name") else str(self.worker.state())
        self._append_engine_log(f"Startup timeout after 30s; QProcess state={process_state}; program={self.worker.program()}")
        self.save_state.setText("●  Engine chưa phản hồi")
        self.save_state.setStyleSheet("color:#d5ad70; font-weight:700;")
        self.ready_label.setText("●  Engine chưa sẵn sàng")
        self.ready_label.setStyleSheet("color:#d5ad70; font-weight:700;")
        self.video_title.setText("Engine khởi động quá lâu · xem QT_ENGINE_LOG.txt trong thư mục phần mềm")
        self.video_title.setToolTip(str(self._worker_log_path))

    def _on_worker_error(self, error):
        try:
            detail = self.worker.errorString()
        except Exception:
            detail = str(error)
        self._append_engine_log("QProcess error: " + detail)
        self._worker_failed("Không khởi động được processing engine: " + detail)

    def _on_worker_finished(self, code, _status):
        if self._closing or self.engine_ready and code == 0:
            return
        if code != 0:
            self._worker_failed(f"Processing engine đã dừng (mã {code}).")

    def _append_engine_log(self, text):
        try:
            with open(self._worker_log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except Exception:
            pass

    def _read_worker_stderr(self):
        try:
            data = bytes(self.worker.readAllStandardError()).decode("utf-8", "replace").strip()
            if data:
                self._last_worker_error = data[-6000:]
                self._append_engine_log(data)
        except Exception:
            pass

    def _worker_failed(self, text):
        detail = getattr(self, "_last_worker_error", "")
        self.engine_ready = False
        if self._worker_failure_reported:
            self._append_engine_log("Additional worker failure: " + text)
            return
        self._worker_failure_reported = True
        self.save_state.setText("●  Engine chưa sẵn sàng")
        self.save_state.setStyleSheet("color:#d5ad70; font-weight:700;")
        self.ready_label.setText("●  Engine lỗi")
        self.ready_label.setStyleSheet("color:#d5ad70; font-weight:700;")
        if hasattr(self, "sidebar_engine_status"):
            self.sidebar_engine_status.setText("● Engine lỗi")
            self.sidebar_engine_status.setStyleSheet("color:#df6179; font-size:8pt; font-weight:650;")
        self._append_engine_log(text + ("\n" + detail if detail else ""))
        QMessageBox.critical(
            self, "Video Studio",
            text + ("\n\n" + detail if detail else "") +
            f"\n\nLog engine: {self._worker_log_path}"
        )

    def _read_worker_stdout(self):
        data = bytes(self.worker.readAllStandardOutput()).decode("utf-8", "replace")
        self._stdout_buffer += data
        while "\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except Exception:
                self._append_engine_log("Invalid worker message: " + line[:500])
                continue
            msg_type = msg.get("type")
            if msg_type == "engine_ready":
                self.engine_ready = True
                priority_mode = str(msg.get("priority_mode", "DEFAULT"))
                if hasattr(self, "resource_hint"):
                    self.resource_hint.setText("Tác vụ nền: ưu tiên thấp" if priority_mode in {"BELOW_NORMAL", "NICE+5"} else "Tác vụ nền: mặc định")
                if hasattr(self, "_engine_watchdog"):
                    self._engine_watchdog.stop()
                self.save_state.setText("✓  Engine sẵn sàng")
                self.save_state.setStyleSheet("color:#68cba1; font-weight:600;")
                if hasattr(self, "sidebar_engine_status"):
                    self.sidebar_engine_status.setText("● Engine sẵn sàng")
                    self.sidebar_engine_status.setStyleSheet("color:#68cba1; font-size:8pt; font-weight:650;")
            elif msg_type == "state":
                self._apply_state(msg.get("payload", {}))
            elif msg_type == "mask_task_started":
                self._set_mask_task_busy(True, msg.get("payload", {}).get("message", "Đang xử lý Mask nền…"))
            elif msg_type == "mask_task_done":
                self._set_mask_task_busy(False)
                payload = msg.get("payload", {})
                self.mask_region_label.setText(f"Mask đã tạo · {int(payload.get('mask_pixels', 0)):,} px")
                self.save_state.setText("●  Mask đã cập nhật · đang tự lưu")
            elif msg_type == "mask_task_error":
                self._set_mask_task_busy(False)
                self._notice("Không tạo được Mask", msg.get("message", "Tác vụ Mask chưa hoàn tất."), "warning")
            elif msg_type == "batch_progress":
                self._batch_progress_update(msg.get("payload", {}))
            elif msg_type == "batch_done":
                self._batch_finished(msg.get("payload", {}))
            elif msg_type == "batch_error":
                self._batch_error(msg.get("message", "Lỗi không xác định"))
            elif msg_type == "reply":
                request_id = msg.get("id")
                command = self.pending_requests.pop(request_id, "unknown")
                if request_id == self.cta_preview_request_id:
                    self.cta_preview_request_id = None
                if request_id == self.video_preview_request_id:
                    self.video_preview_request_id = None
                if msg.get("error"):
                    if command == "save_project" and self._pending_archive_include_video is not None:
                        self._pending_archive_include_video = None
                        self._pending_archive_project_path = ""
                    self._append_engine_log(f"Command failed: id={request_id}, cmd={command}: {msg['error']}\n{msg.get('trace', '')}")
                    if command == "branding_apply" and self.branding_dialog:
                        self.branding_dialog.apply_failed()
                    if command in {"auto_extract", "use_selection_mask"}:
                        self._set_mask_task_busy(False)
                    if command == "cta_scan_start":
                        self._cta_scan_cancelled(msg["error"])
                    if command == "cta_cut_selected":
                        self.cta_cut_btn.setEnabled(bool(self.cta_selected_rows))
                    if command == "set_frame_time" and self.cta_preview_active:
                        self._cta_stop_preview()
                    if command == "cta_preview_start" and self.cta_preview_active:
                        self._cta_stop_preview(notify_worker=False)
                    if command == "video_preview_start" and self.video_preview_active:
                        self._stop_source_range_preview(notify_worker=False)
                    if command == "batch_start":
                        self._batch_error(msg["error"])
                    self._notice("Không thực hiện được", msg["error"], "error")
                else:
                    result = msg.get("result") or {}
                    self._append_engine_log(f"Command acknowledged: id={request_id}, cmd={command}")
                    if command == "save_project" and self._pending_archive_include_video is not None:
                        include_video = bool(self._pending_archive_include_video)
                        project_path = self._pending_archive_project_path or str(self.state.get("project_path", "") or "")
                        self._pending_archive_include_video = None
                        self._pending_archive_project_path = ""
                        if project_path:
                            # The Engine has committed the .grproj. Bind the unsaved CTA cache
                            # to this new project before the archive worker snapshots it.
                            video_path = str(self.state.get("video_path", "") or "")
                            old_project = self.cta_cache_project_key
                            if video_path and not old_project:
                                self._migrate_cta_cache(video_path, old_project, project_path)
                            self.state["project_path"] = project_path
                            self.cta_cache_project_key = project_path
                            self.cta_cache_video_key = video_path
                            self._save_cta_cache_now()
                            self._start_project_archive(project_path, include_video)
                    if command == "branding_state":
                        self._show_branding_dialog(result)
                    elif command == "branding_preview":
                        if self.branding_dialog:
                            self.branding_dialog.receive_preview(result)
                        else:
                            preview_path = str(result.get("preview_path", "") or "")
                            if preview_path: Path(preview_path).unlink(missing_ok=True)
                    elif command == "branding_apply":
                        if self.branding_dialog:
                            self.branding_dialog.apply_acknowledged(result)
                            if result.get("branding_state"):
                                self._show_branding_dialog(result["branding_state"])
                        self.send("state")
                    elif command == "branding_preset_save" and self.branding_dialog:
                        self.branding_dialog.receive_preset_save(result)
                    elif command == "branding_preset_delete" and self.branding_dialog:
                        self.branding_dialog.receive_preset_delete(result)
                    elif command == "branding_preset_load" and self.branding_dialog:
                        self.branding_dialog.receive_preset_load(result)
                    elif command == "compare_frame":
                        self._show_compare_inline(result)
                    elif command == "cta_scan_start":
                        pass
                    elif command == "cta_cache_thumbnails":
                        if not self.cta_scan_active:
                            cached = list(result.get("candidates", []) or [])
                            if cached:
                                self._cta_refresh_candidates(cached, replace=False)
                                self._save_cta_cache_now()
                    elif command == "cta_preview_start":
                        self.cta_preview_request_id = None
                        self.cta_preview_token = str(result.get("token", ""))
                    elif command == "cta_preview_stop":
                        self.cta_preview_request_id = None
                    elif command == "video_preview_start":
                        self.video_preview_request_id = None
                        self.video_preview_token = str(result.get("token", ""))
                    elif command == "video_preview_stop":
                        self.video_preview_request_id = None
                    elif command == "cta_cut_selected":
                        self.cta_status.setText("Đang xuất video đã bỏ các khoảng CTA được chọn…")
            elif msg_type == "cta_phase":
                self._cta_phase_update(msg.get("payload", {}))
            elif msg_type == "cta_ocr_warning":
                self.cta_status.setText(str(msg.get("message", "Thiếu dữ liệu OCR tiếng Việt.")))
            elif msg_type == "cta_preview_frame":
                payload = msg.get("payload", {}) or {}
                if self.cta_preview_active and str(payload.get("token", "")) == self.cta_preview_token:
                    path = str(payload.get("path", ""))
                    self.cta_preview.set_source(path, "Không có khung hình xem lại")
                    self.cta_play_status.setText(
                        f"Đang xem · {self._fmt_time(payload.get('seconds', 0))} / "
                        f"{self._fmt_time(payload.get('end', self.cta_preview_end))}"
                    )
            elif msg_type == "cta_preview_done":
                payload = msg.get("payload", {}) or {}
                if self.cta_preview_active and str(payload.get("token", "")) == self.cta_preview_token:
                    self._cta_stop_preview(notify_worker=False)
            elif msg_type == "cta_preview_error":
                payload = msg.get("payload", {}) or {}
                if self.cta_preview_active and str(payload.get("token", "")) == self.cta_preview_token:
                    self._cta_stop_preview(notify_worker=False)
                    self._notice("Không xem được đoạn", str(payload.get("message", "Lỗi đọc video.")), "error")
            elif msg_type == "video_preview_frame":
                payload = msg.get("payload", {}) or {}
                if self.video_preview_active and str(payload.get("token", "")) == self.video_preview_token:
                    path = str(payload.get("path", ""))
                    self.preview.set_frame(path, 0, 0)
                    self.source_play_status.setText(
                        f"Đang phát · {self._fmt_time(payload.get('seconds', 0))} / "
                        f"{self._fmt_time(payload.get('end', 0))}"
                    )
            elif msg_type == "video_preview_done":
                payload = msg.get("payload", {}) or {}
                if self.video_preview_active and str(payload.get("token", "")) == self.video_preview_token:
                    self._stop_source_range_preview(notify_worker=False)
            elif msg_type == "video_preview_error":
                payload = msg.get("payload", {}) or {}
                if self.video_preview_active and str(payload.get("token", "")) == self.video_preview_token:
                    self._stop_source_range_preview(notify_worker=False)
                    self._notice("Không xem được video gốc", str(payload.get("message", "Lỗi đọc video.")), "error")
            elif msg_type == "cta_progress":
                payload = msg.get("payload", {})
                done = int(payload.get("done", 0)); total = max(1, int(payload.get("total", 1)))
                if payload.get("resume_time") is not None:
                    self.cta_cache_metadata.update({
                        "complete": False,
                        "resume_time": max(0.0, float(payload.get("resume_time", 0) or 0)),
                        "resume_phase": str(payload.get("phase", "pass1") or "pass1"),
                    })
                    self._schedule_cta_cache_save()
                duration = float(self.state.get("duration", 0) or 0)
                resume_time = float(self.cta_cache_metadata.get("resume_time", 0) or 0)
                pct = int(100 * resume_time / duration) if duration > 0 else int(done * 100 / total)
                self.cta_progress.setValue(max(0, min(100, pct)))
                self.cta_status.setText(f"{self.cta_phase_label} · {self._fmt_scan_key(resume_time)} / {self._fmt_scan_key(duration)} ({pct}%) · đã phát hiện {len(self.cta_candidates)} khoảng CTA.")
            elif msg_type == "cta_update":
                self._cta_live_update(msg.get("payload", {}))
            elif msg_type == "cta_paused":
                self._cta_scan_paused(msg.get("message", "Đã tạm dừng."), {
                    "resume_time": msg.get("resume_time"), "phase": msg.get("phase", "pass1")
                })
            elif msg_type == "cta_resumed":
                self._cta_scan_resumed(msg.get("message", "Đang tiếp tục quét…"))
            elif msg_type == "cta_done":
                self._cta_scan_finished(msg.get("payload", {}))
            elif msg_type == "cta_cancelled":
                self._cta_scan_cancelled(msg.get("message", "Đã dừng quét Subscribe."))
            elif msg_type == "cta_error":
                self._cta_scan_cancelled("Quét Subscribe chưa thành công: " + msg.get("message", "Lỗi không xác định"))
                self._notice("Lỗi CTA Detector", msg.get("message", ""), "error")
            elif msg_type == "export_notice":
                self.save_state.setText("✓  " + msg.get("title", "Đã xuất video"))
                self.save_state.setToolTip(msg.get("message", ""))
            elif msg_type == "notice":
                self._notice(msg.get("title", "Thông báo"), msg.get("message", ""), msg.get("kind", "info"))
            elif msg_type == "qc_report":
                report = msg.get("payload", {})
                status = str(report.get("status", "UNKNOWN")).upper()
                color = "#68cba1" if status == "PASS" else ("#d5ad70" if status == "WARN" else "#df6179")
                self.qc_result_label.setStyleSheet(f"color:{color}; font-weight:650; padding:8px; background:#102b3a; border:1px solid {BORDER}; border-radius:8px;")
                self.qc_result_label.setText(f"QC {status} · {report.get('summary', str(report))}")
                self._navigate("export")
            elif msg_type == "worker_warning":
                self._append_engine_log(msg.get("message", "worker warning"))
                self._notice("Cảnh báo engine", msg.get("message", ""), "warning")

    def _set_mask_task_busy(self, busy: bool, message: str = ""):
        self.mask_task_active = bool(busy)
        enabled = bool(self.state.get("video_path")) and not self.mask_task_active and not bool(self.state.get("busy"))
        for widget in getattr(self, "mask_busy_widgets", []):
            try:
                widget.setEnabled(enabled)
            except Exception:
                pass
        if hasattr(self, "preview"):
            self.preview.setEnabled(enabled)
        if message and hasattr(self, "mask_region_label"):
            self.mask_region_label.setText(message)
        elif not busy and hasattr(self, "mask_region_label"):
            pixels = int(self.state.get("mask_pixels", 0) or 0)
            self.mask_region_label.setText(
                f"Mask đã tạo · {pixels:,} px" if self.state.get("has_mask") else "Chưa tạo vùng mask"
            )

    def send(self, cmd: str, **kwargs):
        if not hasattr(self, "worker") or self.worker.state() != QProcess.ProcessState.Running:
            if not self._closing:
                self._notice("Engine chưa sẵn sàng",
                             "Processing engine chưa kết nối nên chưa thể thực hiện thao tác. "
                             "Kiểm tra QT_ENGINE_LOG.txt trong thư mục phần mềm.", "warning")
            return False
        request_id = next(self.request_ids)
        payload = {"id": request_id, "cmd": cmd, **kwargs}
        # ASCII-escape all Unicode before writing to the worker. On Windows,
        # child stdin may use the active ANSI code page; raw UTF-8 bytes in a
        # non-ASCII media path can otherwise kill the reader thread.
        wire = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        written = self.worker.write(wire)
        if written != len(wire):
            self._append_engine_log(f"Command write incomplete: cmd={cmd}, id={request_id}, bytes={written}/{len(wire)}")
            self._notice("Không gửi được lệnh", "Qt chưa gửi đủ lệnh đến Engine. Hãy thử lại.", "error")
            return False
        self.pending_requests[request_id] = cmd
        self._append_engine_log(f"Command sent: id={request_id}, cmd={cmd}")
        return request_id

    def _notice(self, title: str, message: str, kind="info"):
        if kind == "error": QMessageBox.critical(self, title, message)
        elif kind == "warning": QMessageBox.warning(self, title, message)
        else: QMessageBox.information(self, title, message)

    def _on_range_input_changed(self, _value=None):
        if not self._updating_range_inputs:
            self._range_user_dirty = True

    def _apply_state(self, s: dict):
        if not s: return
        new_selected_scene = s.get("selected_scene")
        if not hasattr(self, "_last_selected_scene"):
            self._last_selected_scene = new_selected_scene
        elif new_selected_scene != self._last_selected_scene:
            # Selecting a saved scene must reload its own In/Out values, not keep
            # the range that was typed for the previously selected scene.
            self._range_user_dirty = False
            self._last_selected_scene = new_selected_scene
        self.state = s
        if hasattr(self, "autosave_toggle"):
            autosave_enabled = bool(s.get("autosave", True))
            self.autosave_toggle.blockSignals(True)
            self.autosave_toggle.setChecked(autosave_enabled)
            self.autosave_toggle.blockSignals(False)
            self.sidebar_autosave_status.setText("● Auto Save: đã bật" if autosave_enabled else "● Auto Save: đã tắt")
            self.sidebar_autosave_status.setStyleSheet("color:#68cba1; font-size:8pt;" if autosave_enabled else "color:#d5ad70; font-size:8pt;")
        if hasattr(self, "sidebar_engine_status"):
            engine_text = "● Engine đang xử lý" if s.get("busy") else ("● Engine sẵn sàng" if s.get("ready") else "● Engine đang kết nối")
            engine_color = "#d5ad70" if s.get("busy") or not s.get("ready") else "#68cba1"
            self.sidebar_engine_status.setText(engine_text)
            self.sidebar_engine_status.setStyleSheet(f"color:{engine_color}; font-size:8pt; font-weight:650;")
        if hasattr(self, "batch_start_btn"):
            batch_busy = bool(s.get("batch_active"))
            self.batch_start_btn.setEnabled(bool(self.batch_files) and not batch_busy and not self.batch_running)
            self.batch_stop_btn.setEnabled(batch_busy or self.batch_running)
        if s.get("ready"):
            self.engine_ready = True
            if hasattr(self, "_engine_watchdog"):
                self._engine_watchdog.stop()
        self.project_title.setText(s.get("project_name") or "Chưa có dự án")
        self.project_title.setToolTip(str(s.get("project_path", "") or ""))
        self.video_title.setText(s.get("video_name") or "Chọn video nguồn để bắt đầu")
        dirty = bool(s.get("dirty"))
        self.save_state.setText("●  Có thay đổi chưa lưu" if dirty else "✓  Engine sẵn sàng · Đã lưu")
        self.save_state.setStyleSheet("color:#d5ad70; font-weight:600;" if dirty else "color:#68cba1; font-weight:600;")
        self.video_title.setToolTip(s.get("video_path", ""))
        if self.branding_dialog:
            logo_path = str(s.get("logo_path", "") or "")
            self.branding_dialog.logo_path_label.setText(Path(logo_path).name if logo_path else "Chưa chọn logo")
        self.output_path_label.setText(s.get("output_dir") or "Mặc định theo video nguồn")
        self.ready_label.setText("●  Engine đang xử lý" if s.get("busy") else "●  Engine sẵn sàng")
        self.ready_label.setStyleSheet("color:#d5ad70; font-weight:700;" if s.get("busy") else "color:#68cba1; font-weight:700;")
        self.meta_label.setText(s.get("meta") or "Chưa có thông tin video")
        self.gpu_label.setText(s.get("gpu_status") or "GPU (NVENC): ưu tiên khi khả dụng")
        self.progress.setValue(max(0, min(100, int(s.get("progress", 0)))))
        mask_busy = bool(s.get("mask_busy"))
        if mask_busy:
            self.mask_region_label.setText("Đang xử lý Mask ở nền… giao diện không cần chờ.")
        else:
            self.mask_region_label.setText(
                f"Mask đã tạo · {int(s.get('mask_pixels', 0)):,} px" if s.get("has_mask") else "Chưa tạo vùng mask"
            )
        self._set_mask_task_busy(mask_busy)
        source_key = (str(s.get("video_path", "") or ""), str(s.get("project_path", "") or ""))
        if source_key != self._range_source_key:
            self._range_source_key = source_key
            self._range_user_dirty = False
        video_path = str(s.get("video_path", "") or "")
        project_path = str(s.get("project_path", "") or "")
        if video_path != self.cta_cache_video_key or project_path != self.cta_cache_project_key:
            old_video = self.cta_cache_video_key
            old_project = self.cta_cache_project_key
            same_video = bool(video_path) and video_path == old_video
            if same_video and project_path and not old_project:
                try:
                    self._migrate_cta_cache(video_path, old_project, project_path)
                except OSError as exc:
                    self._append_engine_log(f"CTA cache migration skipped: {exc}")
            if video_path != old_video:
                self.cta_candidates = []
                self.cta_selected_rows.clear()
            self.cta_cache_video_key = video_path
            self.cta_cache_project_key = project_path
            self.cta_candidates = []
            self.cta_selected_rows.clear()
            if video_path:
                self._load_cta_cache_for_video(video_path)
            else:
                self.cta_cache_metadata = {}
        if not self._range_user_dirty:
            self._updating_range_inputs = True
            try:
                self.minute_start.setValue(float(s.get("minute_start", 0) or 0))
                self.minute_end.setValue(float(s.get("minute_end", 0) or 0))
            except (TypeError, ValueError):
                pass
            finally:
                self._updating_range_inputs = False
        duration = max(0.0, float(s.get("duration", 0) or 0))
        current = max(0.0, float(s.get("frame_time", 0) or 0))
        self.seek.blockSignals(True)
        self.seek.setRange(0, max(1, int(round(duration * 1000))))
        # State snapshots arrive repeatedly from the engine. Do not snap the
        # thumb back to the old playhead while the user is actively dragging.
        if not self.seek.isSliderDown():
            self.seek.setValue(int(round(current * 1000)))
        self.seek.blockSignals(False)
        if self.seek.isSliderDown():
            preview_time = self.seek.value() / 1000.0
            self.seek_label.setText(f"{self._fmt_time(preview_time)} / {self._fmt_time(duration)}")
        else:
            self.seek_label.setText(f"{self._fmt_time(current)} / {self._fmt_time(duration)}")
        if not self.seek_input.hasFocus() and not self.seek.isSliderDown():
            self.seek_input.setText(_format_media_time(current))
        has_video = bool(s.get("video_path"))
        busy = bool(s.get("busy")) or mask_busy
        batch_active = bool(s.get("batch_active"))
        self.seek.setEnabled(has_video and not busy)
        self.seek_input.setEnabled(has_video and not busy)
        self.seek_go_btn.setEnabled(has_video and not busy)
        self.minute_start.setEnabled(has_video and not busy)
        self.minute_end.setEnabled(has_video and not busy)
        self.export_top_btn.setEnabled(has_video and not busy)
        if hasattr(self, "project_archive_action"):
            self.project_archive_action.setEnabled(has_video and not busy and not self._archive_busy)
        if hasattr(self, "project_backup_history_action"):
            self.project_backup_history_action.setEnabled(bool(s.get("project_path")) and not self._archive_busy)
        self.export_preview_btn.setEnabled(has_video and not busy)
        self.export_segment_btn.setEnabled(has_video and not busy)
        self.source_play_btn.setEnabled(has_video and not busy)
        self.stop_export_btn.setEnabled(busy and not batch_active)
        if hasattr(self, "cta_scan_btn"):
            self.cta_scan_btn.setEnabled(has_video and not busy and not self.cta_scan_active)
            can_continue_scan = (not bool(self.cta_cache_metadata.get("complete"))
                                 and self._cta_resume_time() > 0.0)
            self.cta_rescan_btn.setEnabled(
                has_video and not busy and not self.cta_scan_active
                and bool(self.cta_candidates or can_continue_scan)
            )
            self.cta_clear_btn.setEnabled(has_video and not busy and not self.cta_scan_active)
            self.cta_pause_btn.setEnabled(has_video and not busy and self.cta_scan_active and not self.cta_pause_pending)
            self.cta_stop_btn.setEnabled(has_video and not busy and self.cta_scan_active)
            self.cta_cut_btn.setEnabled(bool(self.cta_selected_rows) and not busy and not self.cta_scan_active)
            self.cta_play_btn.setEnabled(has_video and not busy and self.cta_table.currentRow() >= 0)
        self.stop_export_btn.setToolTip("Hàng loạt có nút dừng riêng trong Công cụ video." if batch_active else "Dừng tác vụ xuất hiện tại")
        if int(s.get("frame_revision", -1)) != self.preview_revision:
            self.preview_revision = int(s.get("frame_revision", -1))
            self.preview.set_frame(s.get("preview_path", ""), s.get("width", 0), s.get("height", 0))
        self._update_edit_summary(s)
        if self._pending_update:
            self._maybe_apply_pending_update()

    def _refresh_scene_cards(self, scenes, selected):
        signature = tuple((x.get("name"), round(x.get("start", 0), 2), x.get("thumbnail", "")) for x in scenes), selected
        if signature == self._last_scenes_signature: return
        self._last_scenes_signature = signature
        while self.scene_row.count():
            item = self.scene_row.takeAt(0)
            widget = item.widget()
            if widget: widget.deleteLater()
        for i, scene in enumerate(scenes):
            card = SceneCard(i, scene, i == selected, self._select_scene)
            self.scene_row.addWidget(card)
        self.scene_row.addStretch(1)

    def _select_scene(self, index: int):
        self._range_user_dirty = False
        self.send("select_scene", index=index)

    def open_branding_dialog(self):
        self._navigate("branding")

    def _show_branding_dialog(self, state):
        if self.branding_dialog:
            self.branding_dialog.load_state(state)
            for name, btn in self.nav_buttons.items():
                btn.setChecked(name == "branding")
            self.workspace_stack.setCurrentIndex(3)
            self.project_bar.setVisible(True)

    def send_command(self, cmd, **kwargs):
        return self.send(cmd, **kwargs)

    def open_compare_dialog(self):
        self.send("compare_frame")

    def _show_compare_inline(self, result):
        for old_path in self._compare_temp_paths:
            try:
                Path(old_path).unlink(missing_ok=True)
            except Exception:
                pass
        before_path = str(result.get("before_path", "") or "")
        after_path = str(result.get("after_path", "") or "")
        self._compare_temp_paths = [path for path in (before_path, after_path) if path]
        self.compare_before_large.set_source(before_path)
        self.compare_after_large.set_source(after_path)
        meta = (
            f"{self._fmt_time(result.get('frame_time',0))} · Mask {int(result.get('mask_pixels',0)):,} px · "
            f"Expand {int(result.get('expansion',0))} px. " + str(result.get("note", ""))
        )
        self.compare_meta_label.setText(meta)
        self.compare_large_meta.setText(meta)
        self._navigate("mask")
        self.preview_tabs.setCurrentIndex(1)

    def capture_frame(self):
        path, _ = QFileDialog.getSaveFileName(self, "Lưu ảnh khung hình", "GocReview_Frame.png", "PNG (*.png);;JPEG (*.jpg *.jpeg)")
        if path: self.send("capture_preview", path=path)

    def _navigate(self, key: str):
        pages = {
            "edit": (0, "Thuộc tính cảnh"),
            "mask": (1, "Làm sạch · Mask & Before / After"),
            "processing": (3, "Công cụ xử lý"),
            "export": (4, "Xuất & kiểm tra chất lượng"),
        }
        if key == "downloads":
            for name, btn in self.nav_buttons.items():
                btn.setChecked(name == "downloads")
            # Project actions stay global.  Hiding this bar on the downloader
            # made the app feel like it changed modes and removed the user's
            # path back to the open project/save/export actions.
            self.workspace_stack.setCurrentIndex(1); self.project_tabs.setCurrentIndex(0); self.project_bar.setVisible(True)
            return
        if key == "cta":
            for name, btn in self.nav_buttons.items():
                btn.setChecked(name == "cta")
            self.workspace_stack.setCurrentIndex(2)
            self.project_bar.setVisible(True)
            return
        if key == "branding":
            for name, btn in self.nav_buttons.items():
                btn.setChecked(name == "branding")
            self.workspace_stack.setCurrentIndex(3)
            self.project_bar.setVisible(True)
            self.send("branding_state")
            return
        if key not in pages:
            return
        for name, btn in self.nav_buttons.items():
            btn.setChecked(name == key)
        self.workspace_stack.setCurrentIndex(0); self.project_bar.setVisible(True)
        index, title = pages[key]
        self.inspector_stack.setCurrentIndex(index)
        self.inspector_title.setText(title)

    def _update_edit_summary(self, state: dict):
        scenes = state.get("scenes", []) or []
        selected = state.get("selected_scene")
        self.edit_scene_count.setText(f"Phân cảnh: {len(scenes)}")
        info = state.get("meta") or "Chưa có thông tin video"
        self.edit_video_info.setText(info)
        if selected is None or not isinstance(selected, int) or not (0 <= selected < len(scenes)):
            self.edit_scene_name.setText("Chưa chọn phân cảnh")
            self.edit_scene_range.setText("Thời gian: —")
            self.edit_scene_duration.setText("Thời lượng: —")
            return
        scene = scenes[selected]
        start = float(scene.get("start", 0) or 0)
        end = float(scene.get("end", 0) or 0)
        self.edit_scene_name.setText(str(scene.get("name") or f"Cảnh {selected + 1}"))
        self.edit_scene_range.setText(f"Thời gian: {self._fmt_time(start)} – {self._fmt_time(end)}")
        self.edit_scene_duration.setText(f"Thời lượng: {self._fmt_time(max(0.0, end - start))}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showMaximized()
        else:
            self.showFullScreen()

    def choose_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn video nguồn", "",
            "Video (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;Tất cả tệp (*.*)"
        )
        if path:
            self.send("choose_video", path=path)

    def _open_dropped_video(self, path: str):
        if path and Path(path).is_file():
            self.send("choose_video", path=path)

    def choose_logo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Chọn logo", "", "Ảnh (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif *.ico)")
        if path: self.send("choose_logo", path=path)

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "Chọn thư mục xuất")
        if path: self.send("choose_output", path=path)

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Mở dự án", "", "Góc Review Project (*.grproj);;JSON (*.json)")
        if path: self.send("open_project", path=path)

    def _suggested_project_file(self) -> Path:
        video_path = str(self.state.get("video_path", "") or "")
        if video_path and Path(video_path).is_file():
            return suggested_project_file(video_path)
        return Path.home() / "Videos" / "dự án lưu trữ" / "VideoStudio" / "VideoStudio.grproj"

    def save_project(self, save_as=False):
        path = ""
        if save_as or not self.state.get("project_path"):
            if save_as and self.state.get("project_path"):
                default = Path(self.state.get("project_path"))
            else:
                default = self._suggested_project_file()
            try:
                default.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._notice("Không tạo được thư mục dự án", str(exc), "error")
                return False
            path, _ = QFileDialog.getSaveFileName(
                self, "Lưu dự án Video Studio", str(default),
                "Góc Review Project (*.grproj)"
            )
            if not path: return False
            if not Path(path).suffix:
                path += ".grproj"
        request = self.send("save_project", path=path, save_as=save_as)
        return request or False

    def archive_project(self):
        if self._archive_busy:
            self._notice("Đang đóng gói", "Một gói dự án đang được tạo. Hãy chờ hoàn tất.", "info")
            return
        if not self.state.get("video_path"):
            self._notice("Chưa có video", "Hãy mở video nguồn trước khi đóng gói dự án.", "warning")
            return
        if self.cta_scan_active:
            self._notice("CTA đang quét", "Hãy tạm dừng hoặc dừng quét trước khi đóng gói để bảo đảm cache và checkpoint nhất quán.", "warning")
            return
        project_path = str(self.state.get("project_path", "") or "")
        self._save_cta_cache_now()
        if not project_path or bool(self.state.get("dirty")):
            # A new project needs a name/location once. Existing projects save in place.
            self._pending_archive_include_video = True
            if project_path:
                self._pending_archive_project_path = project_path
                request = self.send("save_project", path=project_path, save_as=False)
            else:
                default = self._suggested_project_file()
                try:
                    default.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    self._pending_archive_include_video = None
                    self._notice("Không tạo được thư mục dự án", str(exc), "error")
                    return
                selected, _ = QFileDialog.getSaveFileName(
                    self, "Lưu dự án trước khi đóng gói", str(default),
                    "Góc Review Project (*.grproj)"
                )
                if not selected:
                    self._pending_archive_include_video = None
                    self._pending_archive_project_path = ""
                    return
                if not Path(selected).suffix:
                    selected += ".grproj"
                self._pending_archive_project_path = selected
                request = self.send("save_project", path=selected, save_as=False)
            if not request:
                self._pending_archive_include_video = None
                self._pending_archive_project_path = ""
                return
            self.save_state.setText("●  Đang lưu dự án trước khi đóng gói…")
            return
        self._start_project_archive(project_path, include_video=True)

    def _start_project_archive(self, project_path: str, include_video: bool = True):
        if not project_path or not Path(project_path).is_file():
            self._notice("Chưa có file dự án", "Hãy lưu dự án thành công trước khi đóng gói ZIP.", "warning")
            return
        self._save_cta_cache_now()
        self._archive_busy = True
        if hasattr(self, "project_archive_action"):
            self.project_archive_action.setEnabled(False)
            self.project_archive_action.setText("Đang đóng gói ZIP…")
        video_path = str(self.state.get("video_path", "") or "")
        def run():
            try:
                archive_path = create_project_archive(project_path, video_path, include_video=True)
                result = {"kind": "archive", "ok": True, "path": str(archive_path)}
            except Exception as exc:
                result = {"kind": "archive", "ok": False, "error": str(exc)}
            self.project_storage_signals.completed.emit(result)
        self._project_storage_thread = threading.Thread(
            target=run, name="ProjectArchiveWorker", daemon=True
        )
        self._project_storage_thread.start()

    def show_project_backup_history(self):
        project_path = str(self.state.get("project_path", "") or "")
        if not project_path or not Path(project_path).is_file():
            self._notice("Chưa có dự án", "Hãy lưu dự án ít nhất một lần để tạo lịch sử sao lưu.", "warning")
            return
        backups = list_project_backups(project_path, keep=8)
        dialog = ProjectBackupHistoryDialog(backups, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        snapshot = dialog.selected_backup
        if not snapshot:
            return
        if self.state.get("dirty"):
            answer = QMessageBox.question(
                self, "Dự án hiện tại có thay đổi",
                "Dự án đang mở có thay đổi chưa lưu. Bạn có muốn lưu trước khi khôi phục bản dự phòng không?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Yes:
                self.save_project()
                self._notice("Đã yêu cầu lưu dự án", "Sau khi lưu xong, mở lại Dự án → Lịch sử sao lưu để tiếp tục khôi phục.", "info")
                return
        self._archive_busy = True
        if hasattr(self, "project_backup_history_action"):
            self.project_backup_history_action.setEnabled(False)
        self.save_state.setText("●  Đang tạo dự án từ bản sao lưu…")
        def run():
            try:
                restored = restore_project_backup(snapshot, project_path)
                result = {"kind": "backup_restore", "ok": True, "path": str(restored)}
            except Exception as exc:
                result = {"kind": "backup_restore", "ok": False, "error": str(exc)}
            self.project_storage_signals.completed.emit(result)
        self._project_storage_thread = threading.Thread(
            target=run, name="ProjectBackupRestoreWorker", daemon=True
        )
        self._project_storage_thread.start()

    def restore_project_from_zip(self):
        if self._archive_busy:
            self._notice("Đang đóng gói", "Hãy đợi tác vụ lưu trữ hiện tại hoàn tất.", "info")
            return
        if self.state.get("dirty"):
            answer = QMessageBox.question(
                self, "Dự án chưa lưu",
                "Dự án hiện tại có thay đổi chưa lưu. Bạn có muốn lưu trước khi khôi phục dự án khác?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Yes:
                self.save_project()
                self._notice("Hãy mở lại khôi phục", "Đã gửi lệnh lưu dự án hiện tại. Sau khi lưu xong, hãy chọn lại gói ZIP để khôi phục.", "info")
                return
        archive, _ = QFileDialog.getOpenFileName(
            self, "Chọn gói dự án cần khôi phục", "", "Gói dự án Video Studio (*.zip)"
        )
        if not archive:
            return
        archive_parent = Path(archive).resolve().parent
        if archive_parent.parent.name.casefold() in {"gói zip", "goi zip", "zip"}:
            destination_root = archive_parent.parent.parent
        elif archive_parent.name.casefold() in {"gói zip", "goi zip", "zip"}:
            destination_root = archive_parent.parent
        elif archive_parent.name.casefold() == "backups":
            destination_root = archive_parent.parent
        else:
            destination_root = archive_parent
        destination_root.mkdir(parents=True, exist_ok=True)
        self._archive_busy = True
        if hasattr(self, "project_restore_action"):
            self.project_restore_action.setEnabled(False)
        self.save_state.setText("●  Đang khôi phục dự án từ ZIP…")
        def run():
            try:
                restored = restore_project_archive(archive, destination_root)
                result = {"kind": "restore", "ok": True, "path": str(restored),
                          "destination_root": str(destination_root)}
            except Exception as exc:
                result = {"kind": "restore", "ok": False, "error": str(exc)}
            self.project_storage_signals.completed.emit(result)
        self._project_storage_thread = threading.Thread(
            target=run, name="ProjectRestoreWorker", daemon=True
        )
        self._project_storage_thread.start()

    def _on_project_storage_completed(self, result: dict):
        self._archive_busy = False
        self._project_storage_thread = None
        if hasattr(self, "project_archive_action"):
            self.project_archive_action.setEnabled(
                bool(self.state.get("video_path")) and not bool(self.state.get("busy"))
            )
            self.project_archive_action.setText("Đóng gói ZIP (kèm video gốc)")
        kind = str(result.get("kind", ""))
        if hasattr(self, "project_restore_action"):
            self.project_restore_action.setEnabled(True)
        if hasattr(self, "project_backup_history_action"):
            self.project_backup_history_action.setEnabled(bool(self.state.get("project_path")))
        if not result.get("ok"):
            self._notice("Không hoàn tất thao tác lưu trữ", str(result.get("error", "Lỗi không xác định")), "error")
            return
        path = Path(str(result.get("path", "")))
        if kind == "archive":
            self.save_state.setText("✓  Đã đóng gói dự án")
            self._notice("Đã đóng gói dự án", f"Gói ZIP đã được tạo thành công:\n{path}", "info")
            return
        if kind == "backup_restore":
            self.save_state.setText("✓  Đã khôi phục bản sao lưu · đang mở…")
            self.send("open_project", path=str(path))
            self._notice("Đã khôi phục bản sao lưu", f"Một dự án mới đã được tạo từ bản dự phòng:\n{path.parent}", "info")
            return
        if kind == "restore":
            if hasattr(self, "project_restore_action"):
                self.project_restore_action.setEnabled(True)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                media = payload.setdefault("media", {})
                record = media.get("video")
                candidates = []
                if isinstance(record, dict):
                    relative = record.get("relative")
                    absolute = record.get("absolute")
                    if relative:
                        relative_path = Path(str(relative))
                        candidates.append(relative_path if relative_path.is_absolute() else path.parent / relative_path)
                    if absolute:
                        candidates.append(Path(str(absolute)).expanduser())
                elif isinstance(record, str) and record.strip():
                    candidates.append(Path(record).expanduser())
                found_video = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
                if found_video is None:
                    selected, _ = QFileDialog.getOpenFileName(
                        self, "Liên kết lại video nguồn của dự án", "",
                        "Video (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;Tất cả tệp (*.*)"
                    )
                    if not selected:
                        self._notice("Đã khôi phục dữ liệu dự án",
                                     f"Đã giải nén dự án tại:\n{path.parent}\n\n"
                                     "Chưa mở được dự án vì chưa liên kết video nguồn. Mở lại file .grproj sau khi có video.", "warning")
                        return
                    source = Path(selected).resolve()
                    media["video"] = {
                        "absolute": str(source),
                        "relative": os.path.relpath(str(source), str(path.parent)),
                    }
                    found_video = source
                if not bool(media.get("output_dir_custom", False)):
                    if found_video is not None and found_video.parent.name.casefold() in {"video nguồn", "video nguon"}:
                        output_dir = default_output_directory(found_video)
                    else:
                        destination_root = Path(str(result.get("destination_root", path.parent)))
                        output_dir = default_output_directory_for_project(path, found_video) if found_video else (
                            (destination_root.parent if destination_root.name.casefold() in {"dự án lưu trữ", "du an luu tru"} else destination_root) / "video xuất"
                        )
                    output_dir.mkdir(parents=True, exist_ok=True)
                    media["output_dir"] = {
                        "absolute": str(output_dir.resolve()),
                        "relative": os.path.relpath(str(output_dir.resolve()), str(path.parent)),
                    }
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                self.send("open_project", path=str(path))
                self.save_state.setText("✓  Đã khôi phục gói dự án · đang mở…")
            except Exception as exc:
                self._notice("Đã giải nén nhưng chưa mở được dự án", str(exc), "error")

    def _set_tool(self, mode):
        self.preview.set_tool(mode, self.brush_slider.value())
        self.send("set_mask_tool", mode=mode)

    def _seek_to_seconds(self, seconds: float):
        duration = max(0.0, float(self.state.get("duration", 0) or 0))
        if duration <= 0:
            return False
        seconds = max(0.0, min(float(seconds), max(0.0, duration - 0.05)))
        self.seek.blockSignals(True)
        self.seek.setValue(int(round(seconds * 1000)))
        self.seek.blockSignals(False)
        self.seek_input.setText(_format_media_time(seconds))
        self.send("set_frame_time", seconds=seconds, preserve_mask=True)
        return True

    def _seek_from_timecode(self):
        try:
            seconds = parse_timecode(self.seek_input.text())
        except ValueError as exc:
            self._notice("Timecode không hợp lệ", str(exc), "warning")
            self.seek_input.setFocus()
            self.seek_input.selectAll()
            return
        duration = max(0.0, float(self.state.get("duration", 0) or 0))
        if duration and seconds > duration:
            self._notice("Vượt thời lượng video", f"Video dài {self._fmt_time(duration)}. Hãy chọn mốc nhỏ hơn.", "warning")
            self.seek_input.setFocus(); self.seek_input.selectAll()
            return
        self._seek_to_seconds(seconds)
        self.seek_input.clearFocus()

    def _seek_preview_position(self, milliseconds: int):
        """Preview the chosen destination while dragging, without seeking until release."""
        duration = max(0.0, float(self.state.get("duration", 0) or 0))
        selected = max(0.0, min(float(milliseconds) / 1000.0, duration))
        self.seek_label.setText(f"{self._fmt_time(selected)} / {self._fmt_time(duration)}")

    def _seek_released(self):
        self._seek_to_seconds(self.seek.value() / 1000.0)

    def _step_frame(self, delta):
        current = float(self.state.get("frame_time", 0) or 0)
        self.send("set_frame_time", seconds=max(0, current + delta), preserve_mask=True)

    def _update_scene(self):
        if self.state.get("selected_scene") is None:
            QMessageBox.information(self, "Chưa chọn cảnh", "Hãy chọn một phân cảnh trước khi cập nhật mask.")
            return
        start = self.minute_start.value() * 60.0
        end = self.minute_end.value() * 60.0
        if end <= start:
            QMessageBox.warning(self, "Khoảng cảnh", "Mốc kết thúc phải lớn hơn mốc bắt đầu.")
            return
        self.send("update_scene", start=start, end=end)

    def _add_scene(self):
        start = self.minute_start.value() * 60.0
        end = self.minute_end.value() * 60.0
        duration = float(self.state.get("duration", 0) or 0)
        if end <= start:
            QMessageBox.warning(self, "Khoảng cảnh", "Mốc kết thúc phải lớn hơn mốc bắt đầu."); return
        if duration and end > duration + 0.05:
            QMessageBox.warning(self, "Khoảng cảnh", f"Mốc kết thúc vượt thời lượng video ({self._fmt_time(duration)})."); return
        self.send("add_scene", start=start, end=end)

    def _split_scene(self):
        self.send("split_scene", seconds=float(self.state.get("frame_time", 0)))

    def _delete_scene(self):
        if self.state.get("selected_scene") is None:
            QMessageBox.information(self, "Phân cảnh", "Hãy chọn một phân cảnh trước."); return
        if QMessageBox.question(self, "Xóa phân cảnh", "Bạn có chắc muốn xóa phân cảnh đang chọn?") == QMessageBox.StandardButton.Yes:
            self.send("delete_scene")

    def _manual_range_values(self):
        start = self.minute_start.value() * 60.0
        end = self.minute_end.value() * 60.0
        duration = float(self.state.get("duration", 0) or 0)
        if end <= start:
            raise ValueError("Mốc kết thúc phải lớn hơn mốc bắt đầu.")
        if duration and end > duration + 0.05:
            raise ValueError(f"Mốc kết thúc vượt thời lượng video ({self._fmt_time(duration)}).")
        return start, min(end, duration) if duration else end

    def _refresh_manual_cut_list(self):
        self.manual_cut_list.clear()
        for index, (start, end) in enumerate(self.manual_cut_ranges, 1):
            self.manual_cut_list.addItem(
                f"{index}. {self._fmt_time(start)} – {self._fmt_time(end)}  ·  bỏ {self._fmt_time(end - start)}"
            )

    def _add_manual_cut_range(self):
        if not self.state.get("video_path"):
            self._notice("Chưa có video", "Hãy mở video nguồn trước khi thêm khoảng bỏ.", "warning")
            return
        try:
            current = self._manual_range_values()
        except ValueError as exc:
            self._notice("Khoảng bỏ chưa hợp lệ", str(exc), "warning")
            return
        self.manual_cut_ranges.append(current)
        self.manual_cut_ranges.sort(key=lambda item: item[0])
        self._refresh_manual_cut_list()

    def _remove_manual_cut_range(self):
        row = self.manual_cut_list.currentRow()
        if 0 <= row < len(self.manual_cut_ranges):
            self.manual_cut_ranges.pop(row)
            self._refresh_manual_cut_list()

    def _open_source_range_player(self):
        if not self.state.get("video_path"):
            self._notice("Chưa có video", "Hãy mở video nguồn trước khi xem.", "warning")
            return
        try:
            start, end = self._manual_range_values()
        except ValueError as exc:
            self._notice("Khoảng xem chưa hợp lệ", str(exc), "warning")
            return
        video_path = str(self.state.get("video_path"))
        ffplay = shutil.which("ffplay")
        if ffplay:
            args = [ffplay, "-hide_banner", "-loglevel", "error", "-autoexit",
                    "-ss", f"{start:.3f}", "-i", video_path,
                    "-t", f"{max(0.1, end - start):.3f}",
                    "-window_title", "Góc Review Phim · Video gốc"]
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            try:
                self.source_player_process = subprocess.Popen(
                    args, cwd=str(APP_DIR), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags,
                )
                self.source_play_status.setText(
                    f"Đã mở trình phát video gốc · {self._fmt_time(start)} – {self._fmt_time(end)}"
                )
                return
            except OSError:
                pass
        try:
            if os.name == "nt":
                os.startfile(video_path)
            else:
                subprocess.Popen(["xdg-open", video_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.source_play_status.setText("Đã mở video gốc bằng trình phát mặc định của máy.")
        except OSError as exc:
            self._notice("Không mở được video gốc", str(exc), "error")

    def _stop_source_range_preview(self, notify_worker: bool = True):
        was_active = bool(self.video_preview_active)
        self.video_preview_active = False
        self.video_preview_request_id = None
        if hasattr(self, "source_play_btn"):
            self.source_play_btn.setText("Mở video gốc Từ–Đến")
        if hasattr(self, "source_play_status"):
            self.source_play_status.setText("Chưa mở video gốc")
        if notify_worker and was_active:
            self.send("video_preview_stop")

    def _export_preview_from_range(self):
        start = self.minute_start.value() * 60.0
        duration = float(self.state.get("duration", 0) or 0)
        if duration and start >= duration:
            QMessageBox.warning(self, "Xem thử", "Mốc bắt đầu phải nằm trong thời lượng video.")
            return
        self.send("export_preview", start=start)

    def _export_segment(self):
        ranges = list(self.manual_cut_ranges)
        if not ranges:
            try:
                ranges = [self._manual_range_values()]
            except ValueError as exc:
                QMessageBox.warning(self, "Cắt bỏ video", str(exc))
                return
        # One IPC message carries every boundary: remove all selected ranges
        # and export the remaining video, rather than exporting the ranges.
        self.send("cut_ranges", ranges=[{"start": start, "end": end} for start, end in ranges])

    def _fmt_time(self, sec):
        return _format_media_time(sec)

    def _fmt_scan_key(self, sec):
        return self._fmt_time(sec)

    def closeEvent(self, event):
        self._cta_stop_preview()
        self._stop_source_range_preview()
        if self.merge_worker is not None and self.merge_worker.isRunning():
            self._merge_cancel()
            QMessageBox.information(
                self, "Đang dừng ghép video",
                "Đã gửi yêu cầu dừng ghép. Hãy đợi tác vụ kết thúc rồi đóng ứng dụng để tránh bỏ dở file đầu ra."
            )
            event.ignore()
            return
        if self._download_task is not None:
            self._download_task.cancel()
            self.download_summary.setText("Đang dừng tải an toàn. Khi hàng đợi dừng hẳn, bạn có thể đóng ứng dụng.")
            QMessageBox.information(self, "Đang dừng tải", "Ứng dụng đã gửi yêu cầu dừng download. Hãy đợi tác vụ kết thúc rồi đóng lại để tránh làm hỏng file đang tải.")
            event.ignore()
            return
        self._closing = True
        for temp_path in getattr(self, "_compare_temp_paths", []):
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass
        self._compare_temp_paths = []
        if getattr(self, "worker", None) and self.worker.state() == QProcess.ProcessState.Running:
            self.send("shutdown")
            self.worker.waitForFinished(1200)
        event.accept()


def main():
    app = QApplication(sys.argv)
    try:
        recovered_updates = recover_incomplete_transactions(APP_DIR)
    except Exception as exc:
        QMessageBox.critical(
            None, "Không thể khôi phục cập nhật",
            "Updater phát hiện giao dịch cập nhật bị gián đoạn nhưng không thể tự khôi phục an toàn.\n\n"
            f"Chi tiết: {exc}\n\nKhông nên tiếp tục sử dụng thư mục này cho tới khi kiểm tra log updater."
        )
        return 2
    app.setApplicationName("Góc Review Phim Video Studio")
    app.setStyle("Fusion")
    # Pick a real installed UI font instead of forcing Segoe UI through the
    # stylesheet.  This keeps Vietnamese diacritics readable on machines where
    # Segoe UI is unavailable or substituted incorrectly by Qt.
    available_fonts = set(QFontDatabase.families())
    preferred_font = next(
        (name for name in ("Segoe UI", "Arial", "Tahoma", "Noto Sans", "DejaVu Sans") if name in available_fonts),
        QApplication.font().family(),
    )
    ui_font = QFont(preferred_font)
    ui_font.setStyleHint(QFont.StyleHint.SansSerif)
    ui_font.setPointSizeF(9.8)
    app.setFont(ui_font)
    logo_path = ASSETS / "goc_review_logo_v2.png"
    if logo_path.exists():
        app.setWindowIcon(QIcon(str(logo_path)))
    # Give Qt's native standard icons a light foreground on the dark studio palette.
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor("#081a25"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(PANEL))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(PANEL2))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#145a66"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(TEXT))
    app.setPalette(palette)
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.show()
    if recovered_updates:
        QTimer.singleShot(1800, lambda: QMessageBox.warning(
            win, "Đã khôi phục cập nhật bị gián đoạn",
            "Video Studio đã phát hiện một lần cập nhật bị ngắt giữa chừng và khôi phục các file ứng dụng về bản trước.\n\n"
            "Dự án và dữ liệu người dùng không bị thay đổi."
        ))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

