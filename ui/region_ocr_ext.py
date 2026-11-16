# -*- coding: utf-8 -*-
"""メイン画面のPDFビューア直結・範囲ドラッグOCR拡張モジュール (🔢 数字特化・小数点完全保持対応)"""
import sys
import threading
from typing import List, Dict, Any, Optional, Tuple

import fitz
from PyQt6.QtCore import (
    Qt, QObject, QEvent, QRectF, QPointF, QRunnable, QThreadPool, pyqtSignal
)
from PyQt6.QtGui import QImage, QPainter, QPen, QBrush, QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QCheckBox, QMessageBox,
    QGraphicsRectItem, QGraphicsItemGroup
)

from core.commands import ReplacePagesContentCommand
from core.geometry import (
    FITZ_LOCK,
    JAPANESE_FONT,
    extract_number_from_text,
    format_number_value,
    get_effective_cropbox,
    view_point_to_raw_pdf_from_geom,
)
from core.ocr_cleaner import (
    _DOT_LIKE_CHARS,
    clean_scanned_qimage,
    normalize_strict_numeric_text,
    run_windows_ocr_on_qimage,
    OCR_TEXT_MARKER_RGB,
    OCR_TEXT_MARKER_SRGB_INTS,
)


def _safe_remove_scene_item(scene, item) -> None:
    if item is None or scene is None:
        return
    try:
        if item.scene() is scene:
            scene.removeItem(item)
    except Exception:
        pass


def _scene_rect_to_view_rect(viewer, scene_rect: QRectF) -> Optional[fitz.Rect]:
    if (
        viewer.current_pdf is None
        or viewer.page_rect is None
        or viewer.pixmap_width <= 0
        or viewer.pixmap_height <= 0
    ):
        return None
    sx = viewer.page_rect.width() / float(viewer.pixmap_width)
    sy = viewer.page_rect.height() / float(viewer.pixmap_height)
    vr = fitz.Rect(
        viewer.page_rect.x() + scene_rect.left() * sx,
        viewer.page_rect.y() + scene_rect.top() * sy,
        viewer.page_rect.x() + scene_rect.right() * sx,
        viewer.page_rect.y() + scene_rect.bottom() * sy,
    )
    vr.normalize()
    return vr


def _view_rect_to_scene_rect(viewer, view_rect: fitz.Rect) -> QRectF:
    if (
        viewer.current_pdf is None
        or viewer.page_rect is None
        or viewer.page_rect.width() <= 0
        or viewer.page_rect.height() <= 0
    ):
        return QRectF()
    sx = float(viewer.pixmap_width) / viewer.page_rect.width()
    sy = float(viewer.pixmap_height) / viewer.page_rect.height()
    return QRectF(
        QPointF((view_rect.x0 - viewer.page_rect.x()) * sx, (view_rect.y0 - viewer.page_rect.y()) * sy),
        QPointF((view_rect.x1 - viewer.page_rect.x()) * sx, (view_rect.y1 - viewer.page_rect.y()) * sy),
    ).normalized()


def _detect_border_spanning_lines(gray_bytes: bytes, w: int, h: int) -> Tuple[List[int], List[int]]:
    if w < 60 or h < 30:
        return [], []
    raw_rows = []
    run_th_w = int(w * 0.94)
    margin_x = max(2, int(w * 0.05))
    for y in range(h):
        row = gray_bytes[y * w:(y + 1) * w]
        if min(row[:margin_x]) >= 160 or min(row[-margin_x:]) >= 160:
            continue
        if sum(1 for v in row if v < 155) >= run_th_w:
            raw_rows.append(y)

    thin_rows: List[int] = []
    if raw_rows:
        group = [raw_rows[0]]
        for y in raw_rows[1:]:
            if y == group[-1] + 1:
                group.append(y)
            else:
                if len(group) <= 4:
                    thin_rows.extend(group)
                group = [y]
        if len(group) <= 4:
            thin_rows.extend(group)

    raw_cols = []
    run_th_h = int(h * 0.92)
    margin_y = max(2, int(h * 0.05))
    for x in range(w):
        col = gray_bytes[x::w]
        if min(col[:margin_y]) >= 160 or min(col[-margin_y:]) >= 160:
            continue
        if sum(1 for v in col if v < 155) >= run_th_h:
            raw_cols.append(x)

    thin_cols: List[int] = []
    if raw_cols:
        group = [raw_cols[0]]
        for x in raw_cols[1:]:
            if x == group[-1] + 1:
                group.append(x)
            else:
                if len(group) <= 4:
                    thin_cols.extend(group)
                group = [x]
        if len(group) <= 4:
            thin_cols.extend(group)
    return thin_rows, thin_cols


def _enhance_standard_luminance(qimg: QImage, binarize: bool = False) -> QImage:
    img32 = qimg.convertToFormat(QImage.Format.Format_RGB32)
    w, h = img32.width(), img32.height()
    bits = img32.constBits()
    bits.setsize(w * h * 4)
    raw = bytes(bits)
    b_arr, g_arr, r_arr = raw[0::4], raw[1::4], raw[2::4]

    if binarize:
        lut = bytes(255 if v >= 168 else 0 for v in range(256))
    else:
        lut = bytes(
            255 if v >= 212 else (0 if v <= 85 else int(round((v - 85) * 255.0 / (212 - 85))))
            for v in range(256)
        )
    gray = bytes(((r * 77 + g * 150 + b * 29) >> 8) for r, g, b in zip(r_arr, g_arr, b_arr)).translate(lut)
    return QImage(gray, w, h, w, QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_RGB32)


def _preprocess_region_qimage(
    qimg: QImage,
    clean_color: bool = False,
    binarize_mode: bool = False,
    remove_lines: bool = False,
    base_pad_px: int = 36,
) -> Tuple[QImage, float, int, int]:
    """
    通常モードで認識できた「、」（小数点）を数字特化モードでも100%そのまま認識できるよう、
    画像前処理は通常モードと完全に同一の安全な処理を使用する。
    """
    if qimg.isNull() or qimg.width() <= 0 or qimg.height() <= 0:
        return qimg, 1.0, 0, 0

    if clean_color:
        img = clean_scanned_qimage(
            qimg,
            color_drop_strength=1.8,
            white_threshold=190,
            black_threshold=130,
            despeckle_level=1 if binarize_mode else 0,
            binarize=binarize_mode,
            keep_dark_color_text=True,
        )
    else:
        img = _enhance_standard_luminance(qimg, binarize=binarize_mode)

    w, h = img.width(), img.height()
    if remove_lines and w >= 60 and h >= 30:
        bits = img.constBits()
        bits.setsize(w * h * 4)
        gray = bytes(bits)[0::4]
        thin_rows, thin_cols = _detect_border_spanning_lines(gray, w, h)
        if thin_rows or thin_cols:
            painter = QPainter(img)
            painter.setPen(QPen(QColor(255, 255, 255), 1))
            for y in thin_rows:
                painter.drawLine(0, y, w, y)
            for x in thin_cols:
                painter.drawLine(x, 0, x, h)
            painter.end()

    scale = 2.0 if h < 36 else (1.5 if h < 58 else 1.0)
    if scale > 1.0:
        img = img.scaled(
            int(round(w * scale)),
            int(round(h * scale)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    pad_x = max(base_pad_px, (320 - img.width()) // 2 + 8)
    pad_y = max(base_pad_px, (140 - img.height()) // 2 + 8)
    padded = QImage(img.width() + pad_x * 2, img.height() + pad_y * 2, QImage.Format.Format_RGB32)
    padded.fill(QColor(255, 255, 255))
    painter = QPainter(padded)
    painter.drawImage(pad_x, pad_y, img)
    painter.end()
    return padded, scale, pad_x, pad_y


def _sort_and_format_ocr_chunks(
    items: List[Tuple[str, fitz.Rect]], numeric_only: bool = False
) -> Tuple[str, List[Tuple[str, fitz.Rect]]]:
    """
    認識された (text, view_rect) リストを行（垂直方向の重なり）→ 列（X座標）の順に整列する。
    numeric_only=True の場合は、行内で隣接する小数点（「、」「.」）と数字を結合してから正規化する。
    """
    if not items:
        return "", []

    # 背の高い文字から基準行を作り、背の低い点（、や.）を同じ行へ確実に吸着させる
    tall_first = sorted(items, key=lambda x: (-x[1].height, (x[1].y0 + x[1].y1) * 0.5, x[1].x0))
    rows: List[Dict[str, Any]] = []

    for item in tall_first:
        _, r = item
        cy = (r.y0 + r.y1) * 0.5
        rh = max(2.0, r.height)
        placed = False
        for row_obj in rows:
            ry0, ry1 = row_obj["y0"], row_obj["y1"]
            ref_cy = (ry0 + ry1) * 0.5
            ref_h = max(4.0, ry1 - ry0)
            overlap = min(ry1, r.y1) - max(ry0, r.y0)
            if overlap >= -max(2.0, min(rh, ref_h) * 0.35) or abs(cy - ref_cy) <= max(rh, ref_h) * 0.65:
                row_obj["items"].append(item)
                row_obj["y0"] = min(ry0, r.y0)
                row_obj["y1"] = max(ry1, r.y1)
                placed = True
                break
        if not placed:
            rows.append({"y0": r.y0, "y1": r.y1, "items": [item]})

    rows.sort(key=lambda r: (r["y0"] + r["y1"]) * 0.5)

    ordered_items: List[Tuple[str, fitz.Rect]] = []
    text_lines: List[str] = []

    for row_obj in rows:
        row: List[Tuple[str, fitz.Rect]] = sorted(row_obj["items"], key=lambda x: x[1].x0)

        # 行内で単独の点チャンク（、や.）があれば前後の数字チャンクと連結する
        if len(row) >= 2:
            merged_row: List[Tuple[str, fitz.Rect]] = []
            i = 0
            while i < len(row):
                cur_t, cur_r = row[i]
                if (
                    cur_t.strip() in _DOT_LIKE_CHARS
                    and merged_row
                    and any(c.isdigit() for c in merged_row[-1][0])
                ):
                    prev_t, prev_r = merged_row[-1]
                    dot_ch = "." if (numeric_only or cur_t.strip() not in (",", "，")) else ","
                    if i + 1 < len(row) and any(c.isdigit() for c in row[i + 1][0]):
                        next_t, next_r = row[i + 1]
                        merged_row[-1] = (prev_t + dot_ch + next_t, prev_r | cur_r | next_r)
                        i += 2
                        continue
                    else:
                        merged_row[-1] = (prev_t + dot_ch, prev_r | cur_r)
                        i += 1
                        continue
                merged_row.append((cur_t, cur_r))
                i += 1
            row = merged_row

        if numeric_only:
            norm_row: List[Tuple[str, fitz.Rect]] = []
            for t_str, r_box in row:
                nt = normalize_strict_numeric_text(t_str, allow_standalone_dot=False)
                if nt:
                    norm_row.append((nt, r_box))
            row = norm_row
            if not row:
                continue

        ordered_items.extend(row)
        line_parts = [row[0][0]]
        for i in range(1, len(row)):
            prev_t, prev_r = row[i - 1]
            cur_t, cur_r = row[i]
            gap = cur_r.x0 - prev_r.x1
            is_dot_join = (
                prev_t.endswith((".", "、", "。", "・", ","))
                or cur_t.startswith((".", "、", "。", "・", ","))
            )
            if not is_dot_join and gap > max(4.0, prev_r.height * 0.6):
                line_parts.append("  " + cur_t)
            else:
                line_parts.append(cur_t)

        line_str = "".join(line_parts)
        if numeric_only:
            line_str = normalize_strict_numeric_text(line_str, allow_standalone_dot=False)
        if line_str:
            text_lines.append(line_str)

    return "\n".join(text_lines).strip(), ordered_items


def _remove_existing_ocr_text_in_unrotated_page(page: fitz.Page, target_raw_rect: fitz.Rect) -> None:
    try:
        raw_dict = page.get_text("rawdict")
    except Exception:
        return
    redact_rects = []
    for blk in raw_dict.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                if sp.get("color") in OCR_TEXT_MARKER_SRGB_INTS:
                    sr = fitz.Rect(sp.get("bbox", (0, 0, 0, 0)))
                    if sr.intersects(target_raw_rect):
                        redact_rects.append(sr)
    if not redact_rects:
        return
    for rr in redact_rects:
        page.add_redact_annot(rr, fill=False)
    img_none = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)
    gfx_none = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
    text_opt = getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0)
    try:
        page.apply_redactions(images=img_none, graphics=gfx_none, text=text_opt)
    except TypeError:
        page.apply_redactions(images=img_none)


class _RegionOcrWorkerSignals(QObject):
    finished = pyqtSignal(list, float, int, int, str, bool)


class _RegionOcrWorker(QRunnable):
    def __init__(
        self,
        raw_qimg: QImage,
        clean_color: bool,
        binarize_mode: bool,
        remove_lines: bool,
        numeric_only: bool,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.raw_qimg = raw_qimg.copy()
        self.clean_color = clean_color
        self.binarize_mode = binarize_mode
        self.remove_lines = remove_lines
        self.numeric_only = numeric_only
        self.cancel_event = cancel_event
        self.signals = _RegionOcrWorkerSignals()

    def run(self):
        try:
            if self.cancel_event.is_set():
                self.signals.finished.emit([], 1.0, 0, 0, "", True)
                return
            proc_img, up_scale, pad_x, pad_y = _preprocess_region_qimage(
                self.raw_qimg,
                clean_color=self.clean_color,
                binarize_mode=self.binarize_mode,
                remove_lines=self.remove_lines,
            )
            if self.cancel_event.is_set():
                self.signals.finished.emit([], 1.0, 0, 0, "", True)
                return
            chunks = run_windows_ocr_on_qimage(
                proc_img,
                fix_numeric=True,
                cancel_event=self.cancel_event,
                numeric_only=self.numeric_only,
            ) or []

            if not chunks and not self.cancel_event.is_set():
                alt_img, alt_scale, alt_px, alt_py = _preprocess_region_qimage(
                    self.raw_qimg,
                    clean_color=not self.clean_color,
                    binarize_mode=False,
                    remove_lines=False,
                )
                if not self.cancel_event.is_set():
                    alt_chunks = run_windows_ocr_on_qimage(
                        alt_img,
                        fix_numeric=True,
                        cancel_event=self.cancel_event,
                        numeric_only=self.numeric_only,
                    ) or []
                    if alt_chunks:
                        chunks = alt_chunks
                        up_scale, pad_x, pad_y = alt_scale, alt_px, alt_py

            if self.cancel_event.is_set():
                self.signals.finished.emit([], 1.0, 0, 0, "", True)
                return
            self.signals.finished.emit(chunks, up_scale, pad_x, pad_y, "", False)
        except Exception as e:
            if self.cancel_event.is_set():
                self.signals.finished.emit([], 1.0, 0, 0, "", True)
            else:
                self.signals.finished.emit([], 1.0, 0, 0, str(e), False)


class _FloatingResultToast(QFrame):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("ocrResultToast")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame#ocrResultToast { background-color: #F8F9FA; border: 2px solid #0055AA; border-radius: 7px; }"
            "QLabel { border: none; color: #1A1A1A; background: transparent; }"
            "QCheckBox { color: #1A1A1A; background: transparent; font-size: 11px; }"
            "QPushButton { background-color: #FFFFFF; color: #1A1A1A; border: 1px solid #A0A5AD; "
            "  border-radius: 4px; padding: 4px 8px; font-weight: bold; font-size: 11px; }"
            "QPushButton:hover { background-color: #E8F0FE; }"
        )
        self.setFixedSize(475, 255)
        self._build_ui()
        self.hide()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        top = QHBoxLayout()
        self.lbl_title = QLabel("🎯 範囲OCR")
        self.lbl_title.setStyleSheet("font-weight: bold; color: #0055AA; font-size: 12px;")
        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(24, 24)
        top.addWidget(self.lbl_title)
        top.addStretch()
        top.addWidget(self.btn_close)
        layout.addLayout(top)

        opt_bar = QHBoxLayout()
        opt_bar.setSpacing(5)

        self.chk_numeric = QCheckBox("🔢 数字特化")
        self.chk_numeric.setChecked(False)
        self.chk_numeric.setStyleSheet("color: #B25000; font-weight: bold; font-size: 11px;")
        self.chk_numeric.setToolTip(
            "金額・小数・日付などの数字に特化します（「、」と認識された小数点も「.」へ自動変換します）"
        )

        self.chk_clean = QCheckBox("背景色抜き")
        self.chk_clean.setChecked(False)
        self.chk_binarize = QCheckBox("白黒二値化")
        self.chk_binarize.setChecked(False)
        self.chk_lines = QCheckBox("枠線除去")
        self.chk_lines.setChecked(False)
        self.chk_embed = QCheckBox("透明文字埋込")
        self.chk_embed.setChecked(True)

        opt_bar.addWidget(self.chk_numeric)
        opt_bar.addWidget(self.chk_clean)
        opt_bar.addWidget(self.chk_binarize)
        opt_bar.addWidget(self.chk_lines)
        opt_bar.addWidget(self.chk_embed)
        layout.addLayout(opt_bar)

        self.txt_box = QTextEdit()
        self.txt_box.setStyleSheet("background: #FFFFFF; color: #111111; border: 1px solid #CCCCCC; font-size: 12px;")
        layout.addWidget(self.txt_box)

        btn_bar = QHBoxLayout()
        self.btn_copy = QPushButton("📋 コピー")
        self.btn_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.txt_box.toPlainText()))
        self.btn_retry = QPushButton("🔄 再OCR")
        self.btn_to_edit = QPushButton("⌨ 認識文字を編集")
        self.btn_to_edit.setStyleSheet(
            "QPushButton { background-color: #1971C2; color: #FFFFFF; border-color: #145A9E; }"
            "QPushButton:hover { background-color: #1864AB; }"
        )
        btn_bar.addWidget(self.btn_copy)
        btn_bar.addWidget(self.btn_retry)
        btn_bar.addWidget(self.btn_to_edit)
        layout.addLayout(btn_bar)

    def reposition(self):
        if self.parentWidget():
            pw = self.parentWidget().width()
            ph = self.parentWidget().height()
            self.move(max(10, pw - self.width() - 20), max(10, ph - self.height() - 20))

    def show_result(self, text: str, title: str):
        self.lbl_title.setText(title)
        self.txt_box.setPlainText(text)
        self.reposition()
        self.show()
        self.raise_()


class DirectViewerRegionOCRController(QObject):
    def __init__(self, main_window, viewer):
        super().__init__(main_window)
        self.main_window = main_window
        self.viewer = viewer
        self.vp = viewer.viewport()
        self._dragging = False
        self._start_scene_pt = QPointF()
        self._rect_item: Optional[QGraphicsRectItem] = None
        self._highlight_group: Optional[QGraphicsItemGroup] = None

        self.last_scene_rect: Optional[QRectF] = None
        self.last_page_idx: int = -1

        self._cancel_event: Optional[threading.Event] = None
        self._active_worker: Optional[_RegionOcrWorker] = None
        self._pending_context: Optional[Dict[str, Any]] = None

        self.toast = _FloatingResultToast(self.vp)
        self.toast.btn_close.clicked.connect(self._on_toast_closed)
        self.toast.btn_retry.clicked.connect(self._retry_last_rect)
        self.toast.btn_to_edit.clicked.connect(self._switch_to_text_edit_mode)

        self.toast.chk_numeric.toggled.connect(self._on_option_toggled)
        self.toast.chk_clean.toggled.connect(self._on_option_toggled)
        self.toast.chk_binarize.toggled.connect(self._on_option_toggled)
        self.toast.chk_lines.toggled.connect(self._on_option_toggled)

        self.viewer.pageSelectionChanged.connect(self._on_page_changed)
        self.vp.installEventFilter(self)

    def _on_option_toggled(self, _checked: bool):
        if (
            self.toast.isVisible()
            and self.last_scene_rect is not None
            and self.viewer.current_pdf is not None
            and self.last_page_idx == self.viewer.current_page_idx
        ):
            self._retry_last_rect()

    def on_scene_about_to_clear(self):
        self._rect_item = None
        self._highlight_group = None
        self._dragging = False

    def _on_page_changed(self, _new_page_idx: int):
        self._clear_result_highlights()

    def _clear_result_highlights(self):
        if self._highlight_group is not None:
            _safe_remove_scene_item(self.viewer.scene, self._highlight_group)
            self._highlight_group = None

    def _draw_result_highlights(self, embed_chunks_view: List[Tuple[str, fitz.Rect]]):
        self._clear_result_highlights()
        if not embed_chunks_view or self.viewer.current_pdf is None:
            return
        group = QGraphicsItemGroup()
        group.setZValue(940)
        pen = QPen(QColor(0, 175, 80, 235), 2.0, Qt.PenStyle.SolidLine)
        brush = QBrush(QColor(0, 210, 95, 42))
        for _, vr in embed_chunks_view:
            s_rect = _view_rect_to_scene_rect(self.viewer, vr).adjusted(-2, -1, 2, 1)
            if not s_rect.isEmpty():
                r_item = QGraphicsRectItem(s_rect)
                r_item.setPen(pen)
                r_item.setBrush(brush)
                group.addToGroup(r_item)
        self.viewer.scene.addItem(group)
        self._highlight_group = group

    def _on_toast_closed(self):
        self._cancel_running_ocr()
        self._clear_result_highlights()
        self.toast.hide()

    def _switch_to_text_edit_mode(self):
        self._clear_result_highlights()
        if hasattr(self.main_window, "set_mode"):
            self.main_window.set_mode("type")

    def _cancel_running_ocr(self):
        if self._cancel_event is not None:
            self._cancel_event.set()
            self._cancel_event = None
        self._active_worker = None
        self._pending_context = None

    def cancel_drag(self):
        self._dragging = False
        if self._rect_item is not None:
            _safe_remove_scene_item(self.viewer.scene, self._rect_item)
            self._rect_item = None
        self._clear_result_highlights()

    def reset(self):
        self._cancel_running_ocr()
        self.cancel_drag()
        self.last_scene_rect = None
        self.last_page_idx = -1
        self.toast.hide()

    def eventFilter(self, obj, event):
        if obj is not self.vp:
            return False
        etype = event.type()
        if etype == QEvent.Type.Resize and self.toast.isVisible():
            self.toast.reposition()
            return False
        if self.viewer.current_pdf is None or self.viewer.mode != "region_ocr":
            return False

        if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            if self.toast.isVisible() and self.toast.geometry().contains(event.pos()):
                return False
            mods = event.modifiers()
            if (mods & Qt.KeyboardModifier.ControlModifier) or getattr(self.viewer, "_space_pressed", False):
                return False
            raw_scene = self.viewer.mapToScene(event.pos())
            clamped = self.viewer._clamp_to_scene(raw_scene)
            self.cancel_drag()
            self._dragging = True
            self._start_scene_pt = clamped

            item = QGraphicsRectItem(QRectF(clamped, clamped))
            item.setPen(QPen(QColor(220, 20, 20, 240), 2.2, Qt.PenStyle.DashLine))
            item.setBrush(QBrush(QColor(255, 60, 60, 40)))
            item.setZValue(1100)
            self.viewer.scene.addItem(item)
            self._rect_item = item
            return True

        elif etype == QEvent.Type.MouseMove and self._dragging:
            raw_scene = self.viewer.mapToScene(event.pos())
            clamped = self.viewer._clamp_to_scene(raw_scene)
            if self._rect_item is not None:
                try:
                    self._rect_item.setRect(QRectF(self._start_scene_pt, clamped).normalized())
                except RuntimeError:
                    self._rect_item = None
                    self._dragging = False
            return True

        elif etype == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and self._dragging:
            raw_scene = self.viewer.mapToScene(event.pos())
            clamped = self.viewer._clamp_to_scene(raw_scene)
            scene_rect = QRectF(self._start_scene_pt, clamped).normalized()
            self.cancel_drag()
            if scene_rect.width() >= 6.0 and scene_rect.height() >= 6.0:
                self.last_scene_rect = scene_rect
                self.last_page_idx = self.viewer.current_page_idx
                self.start_async_ocr_on_scene_rect(scene_rect)
            return True
        return False

    def _retry_last_rect(self):
        if (
            self.last_scene_rect is not None
            and self.viewer.current_pdf is not None
            and self.last_page_idx == self.viewer.current_page_idx
        ):
            self.start_async_ocr_on_scene_rect(self.last_scene_rect)

    def start_async_ocr_on_scene_rect(self, scene_rect: QRectF):
        if sys.platform != "win32":
            QMessageBox.warning(
                self.main_window,
                "Windows専用",
                "範囲OCRはWindows専用です。",
            )
            return
        doc = self.viewer.current_pdf
        page_idx = self.viewer.current_page_idx
        if doc is None or not (0 <= page_idx < len(doc)):
            return
        view_rect = _scene_rect_to_view_rect(self.viewer, scene_rect)
        if view_rect is None or view_rect.is_empty:
            return

        self._cancel_running_ocr()
        numeric_only = self.toast.chk_numeric.isChecked()
        # 通常モードと完全に同じDPIで切り出すことで、通常モードで認識できた「、」を確実に再現する
        dpi = 380 if (view_rect.height < 18.0 or view_rect.width < 36.0) else 300
        scale = dpi / 72.0

        with FITZ_LOCK:
            source_page = (
                self.viewer._clean_redacted_doc[0]
                if getattr(self.viewer, "_clean_redacted_doc", None) is not None
                and getattr(self.viewer, "_cached_page_idx", -1) == page_idx
                else doc[page_idx]
            )
            page_view_bound = fitz.Rect(source_page.rect)
            clip_vr = view_rect & page_view_bound
            if clip_vr.is_empty or clip_vr.width < 2.0 or clip_vr.height < 2.0:
                return
            pix = source_page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                clip=clip_vr,
                colorspace=fitz.csRGB,
                alpha=False,
            )
            qimg = QImage(
                pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
            ).copy()

        ann_list = self.viewer.page_annotations.get(page_idx, [])
        if ann_list and not qimg.isNull():
            from core import annotation_to_erase_rects
            from core.geometry import raw_rect_to_view_rect_from_geom
            erase_raw_rects = []
            for ann in ann_list:
                if ann.get("type") in ("erase", "box_erase"):
                    erase_raw_rects.extend(annotation_to_erase_rects(ann))
            if erase_raw_rects:
                with FITZ_LOCK:
                    p_obj = doc[page_idx]
                    c_rect = get_effective_cropbox(p_obj)
                    p_rect = fitz.Rect(p_obj.rect)
                    p_rot = p_obj.rotation % 360
                painter = QPainter(qimg)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(255, 255, 255)))
                for er_raw in erase_raw_rects:
                    er_view = raw_rect_to_view_rect_from_geom(c_rect, p_rect, p_rot, er_raw)
                    inter = er_view & clip_vr
                    if not inter.is_empty:
                        painter.drawRect(
                            QRectF(
                                (inter.x0 - clip_vr.x0) * scale,
                                (inter.y0 - clip_vr.y0) * scale,
                                inter.width * scale,
                                inter.height * scale,
                            )
                        )
                painter.end()

        self._cancel_event = threading.Event()
        self._pending_context = {
            "doc": doc,
            "page_idx": page_idx,
            "clip_vr": clip_vr,
            "dpi": dpi,
            "orig_w": qimg.width(),
            "orig_h": qimg.height(),
            "numeric_only": numeric_only,
        }

        mode_label = "⏳ 範囲OCR [数字特化] 解析中..." if numeric_only else "⏳ 範囲OCR解析中..."
        self.toast.show_result("Windows標準OCRで選択範囲を解析しています...", mode_label)
        worker = _RegionOcrWorker(
            raw_qimg=qimg,
            clean_color=self.toast.chk_clean.isChecked(),
            binarize_mode=self.toast.chk_binarize.isChecked(),
            remove_lines=self.toast.chk_lines.isChecked(),
            numeric_only=numeric_only,
            cancel_event=self._cancel_event,
        )
        worker.signals.finished.connect(
            self._on_async_ocr_finished, Qt.ConnectionType.QueuedConnection
        )
        self._active_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_async_ocr_finished(
        self, chunks: list, up_scale: float, pad_x: int, pad_y: int, err_msg: str,
        cancelled: bool = False,
    ):
        active = self._active_worker
        if active is not None and self.sender() is not getattr(active, "signals", None):
            return
        ctx = self._pending_context
        self._pending_context = None
        self._active_worker = None
        self._cancel_event = None

        if cancelled:
            if self.toast.isVisible():
                self.toast.show_result("（範囲OCRを中止しました）", "範囲OCR中止")
            return
        if err_msg:
            self.toast.show_result(f"エラー: {err_msg}", "❌ 範囲OCRエラー")
            return
        if ctx is None or self.viewer.current_pdf is not ctx["doc"]:
            return

        doc = ctx["doc"]
        page_idx = ctx["page_idx"]
        clip_vr: fitz.Rect = ctx["clip_vr"]
        dpi = ctx["dpi"]
        orig_w = float(ctx["orig_w"])
        orig_h = float(ctx["orig_h"])
        numeric_only = bool(ctx.get("numeric_only", False))
        px_to_view = 72.0 / float(dpi)

        raw_items: List[Tuple[str, fitz.Rect]] = []
        for ch in chunks:
            t = str(ch.get("text", "")).strip()
            if numeric_only:
                t = normalize_strict_numeric_text(t, allow_standalone_dot=True)
            if not t:
                continue
            bbox = ch.get("bbox")
            if bbox and len(bbox) == 4:
                bx0, by0, bx1, by1 = bbox
                ox0 = max(0.0, (float(bx0) - pad_x) / up_scale)
                oy0 = max(0.0, (float(by0) - pad_y) / up_scale)
                ox1 = min(orig_w, (float(bx1) - pad_x) / up_scale)
                oy1 = min(orig_h, (float(by1) - pad_y) / up_scale)
                vr = fitz.Rect(
                    clip_vr.x0 + ox0 * px_to_view,
                    clip_vr.y0 + oy0 * px_to_view,
                    clip_vr.x0 + ox1 * px_to_view,
                    clip_vr.y0 + oy1 * px_to_view,
                )
                if not vr.is_empty:
                    raw_items.append((t, vr))
            else:
                raw_items.append((t, clip_vr))

        full_text, embed_chunks_view = _sort_and_format_ocr_chunks(
            raw_items, numeric_only=numeric_only
        )
        if full_text:
            QGuiApplication.clipboard().setText(full_text)

        if self.toast.chk_embed.isChecked() and embed_chunks_view and 0 <= page_idx < len(doc):
            try:
                with FITZ_LOCK:
                    after_doc = fitz.open()
                    after_doc.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
                    new_page = after_doc[0]

                    saved_rot = new_page.rotation % 360
                    saved_crop = get_effective_cropbox(new_page)
                    full_view_rect = fitz.Rect(new_page.rect)

                    p_tl = view_point_to_raw_pdf_from_geom(
                        saved_crop, full_view_rect, saved_rot, fitz.Point(clip_vr.x0, clip_vr.y0)
                    )
                    p_br = view_point_to_raw_pdf_from_geom(
                        saved_crop, full_view_rect, saved_rot, fitz.Point(clip_vr.x1, clip_vr.y1)
                    )
                    clip_raw_rect = fitz.Rect(p_tl.x, p_tl.y, p_br.x, p_br.y)
                    clip_raw_rect.normalize()

                    if saved_rot != 0:
                        new_page.set_rotation(0)
                    if saved_crop != new_page.mediabox:
                        new_page.set_cropbox(new_page.mediabox)

                    try:
                        _remove_existing_ocr_text_in_unrotated_page(new_page, clip_raw_rect)
                        existing_fonts = {f[4] for f in (new_page.get_fonts() or []) if len(f) > 4}
                        font_alias = "jp_ocr_font"
                        idx_f = 1
                        while font_alias in existing_fonts:
                            idx_f += 1
                            font_alias = f"jp_ocr_font_{idx_f}"

                        fitz_font = None
                        if JAPANESE_FONT:
                            try:
                                fitz_font = fitz.Font(fontfile=JAPANESE_FONT)
                            except Exception:
                                fitz_font = None

                        for text_str, vr in embed_chunks_view:
                            box_w = max(2.0, vr.width)
                            box_h = max(4.0, vr.height)
                            est_fs = max(4.5, min(72.0, box_h * 0.86))
                            if fitz_font is not None:
                                try:
                                    unit_w = fitz_font.text_length(text_str, fontsize=1.0)
                                    if unit_w > 0.1:
                                        est_fs = max(4.0, min(est_fs * 1.15, box_w / unit_w))
                                except Exception:
                                    pass

                            view_base_pt = fitz.Point(vr.x0, vr.y0 + box_h * 0.84)
                            raw_base_pt = view_point_to_raw_pdf_from_geom(
                                saved_crop, full_view_rect, saved_rot, view_base_pt
                            )
                            inserted = False
                            if JAPANESE_FONT:
                                try:
                                    new_page.insert_text(
                                        raw_base_pt, text_str, fontsize=est_fs,
                                        color=OCR_TEXT_MARKER_RGB, rotate=saved_rot,
                                        fontfile=JAPANESE_FONT, fontname=font_alias, render_mode=3,
                                    )
                                    inserted = True
                                except Exception:
                                    inserted = False
                            if not inserted:
                                new_page.insert_text(
                                    raw_base_pt, text_str, fontsize=est_fs,
                                    color=OCR_TEXT_MARKER_RGB, rotate=saved_rot,
                                    fontname="japan", render_mode=3,
                                )
                    finally:
                        if new_page.rotation != saved_rot:
                            new_page.set_rotation(saved_rot)
                        if saved_crop != new_page.mediabox:
                            new_page.set_cropbox(saved_crop)

                cmd = ReplacePagesContentCommand(
                    doc=doc,
                    page_indices=[page_idx],
                    new_pages_doc=after_doc,
                    page_annotations=self.viewer.page_annotations,
                    prev_page_idx=self.viewer.current_page_idx,
                    take_ownership=True,
                )
                cmd.redo(self.viewer)
                self.viewer.history.push(cmd)
            except Exception as e:
                QMessageBox.warning(self.main_window, "範囲OCR埋込エラー", f"PDFへの反映中にエラーが発生しました:\n{e}")

        if full_text:
            if self.viewer.current_page_idx == page_idx:
                self._draw_result_highlights(embed_chunks_view)
            sum_suffix = ""
            if numeric_only:
                nums = [
                    val
                    for val in (extract_number_from_text(t) for t, _ in embed_chunks_view)
                    if val is not None
                ]
                if len(nums) >= 2:
                    sum_suffix = f"・合計: {format_number_value(sum(nums))}"
            mode_tag = "範囲OCR[数字特化]" if numeric_only else "範囲OCR"
            title = f"🎯 {mode_tag}完了 ({page_idx + 1}P・{len(embed_chunks_view)}件{sum_suffix}・コピー済)"
            self.toast.show_result(full_text, title)
            if hasattr(self.main_window, "statusBar"):
                preview_one = full_text.replace("\n", " ")[:32]
                self.main_window.statusBar().showMessage(
                    f"🎯 {mode_tag}完了: 「{preview_one}」を認識{sum_suffix}（Ctrl+F検索・Ctrl+クリック電卓・Ctrl+Z対応）"
                )
        else:
            self._clear_result_highlights()
            msg = (
                "(数字が検出されませんでした。\n・もう一回り広く囲むか、「背景色抜き」を切り替えて試してください)"
                if numeric_only
                else "(文字が検出されませんでした。\n・もう一回り広く囲むか、「背景色抜き」を切り替えて試してください)"
            )
            self.toast.show_result(msg, "⚠️ 文字未検出")


def install_region_ocr_patch():
    return None
