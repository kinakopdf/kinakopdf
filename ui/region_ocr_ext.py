# -*- coding: utf-8 -*-
"""
メイン画面のPDFビューア(ui/pdf_viewer.py)直結・範囲ドラッグOCR拡張モジュール (ui/region_ocr_ext.py)
[Part 3 Verified & Hardened]:
  - scene.clear() による C++ QGraphicsItem 破棄後の RuntimeError クラッシュを完全防止
  - Windows.Media.Ocr 最適解像度 (300DPI・極小領域のみ拡大・最小キャンバスサイズ保証)
  - 赤文字・青文字も消えない「標準コントラスト強調 ⇔ 網掛け色抜き」自動2段階フォールバック認識
  - 回転・CropBox 0度正規化後の安全な既存OCR透明文字クリア & Undo/Redo 完全対応
"""
import sys
import threading
from typing import List, Dict, Any, Optional, Tuple

import fitz
from PyQt6.QtCore import (
    Qt, QObject, QEvent, QRect, QRectF, QPoint, QPointF, QRunnable, QThreadPool, pyqtSignal
)
from PyQt6.QtGui import (
    QImage, QPainter, QPen, QBrush, QColor, QAction, QKeySequence, QGuiApplication
)
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QCheckBox, QMessageBox,
    QGraphicsRectItem, QGraphicsItemGroup, QGraphicsView
)

from core.commands import ReplacePagesContentCommand
from core.geometry import (
    FITZ_LOCK,
    JAPANESE_FONT,
    get_effective_cropbox,
    view_point_to_raw_pdf_from_geom,
    raw_rect_to_view_rect,
)
from core.ocr_cleaner import (
    clean_scanned_qimage,
    run_windows_ocr_on_qimage,
    OCR_TEXT_MARKER_RGB,
    OCR_TEXT_MARKER_SRGB_INT,
)


def _safe_remove_scene_item(scene, item) -> None:
    """scene.clear() 済みでC++オブジェクトが破棄されていてもRuntimeErrorを起こさず安全に除去する"""
    if item is None or scene is None:
        return
    try:
        if item.scene() is scene:
            scene.removeItem(item)
    except RuntimeError:
        # C++ object already deleted by scene.clear()
        pass
    except Exception:
        pass


def _scene_rect_to_view_rect(viewer, scene_rect: QRectF) -> Optional[fitz.Rect]:
    """PDFViewerのシーン矩形を現在のページの表示座標系(page.rect基準)へ変換する"""
    if (
        viewer.current_pdf is None
        or viewer.page_rect is None
        or viewer.pixmap_width <= 0
        or viewer.pixmap_height <= 0
    ):
        return None

    sx = viewer.page_rect.width() / float(viewer.pixmap_width)
    sy = viewer.page_rect.height() / float(viewer.pixmap_height)

    vx0 = viewer.page_rect.x() + scene_rect.left() * sx
    vy0 = viewer.page_rect.y() + scene_rect.top() * sy
    vx1 = viewer.page_rect.x() + scene_rect.right() * sx
    vy1 = viewer.page_rect.y() + scene_rect.bottom() * sy

    vr = fitz.Rect(vx0, vy0, vx1, vy1)
    vr.normalize()
    return vr


def _view_rect_to_scene_rect(viewer, view_rect: fitz.Rect) -> QRectF:
    """ページの表示座標系(page.rect基準)の矩形をPDFViewerのシーン座標矩形へ変換する"""
    if (
        viewer.current_pdf is None
        or viewer.page_rect is None
        or viewer.page_rect.width() <= 0
        or viewer.page_rect.height() <= 0
    ):
        return QRectF()

    sx = float(viewer.pixmap_width) / viewer.page_rect.width()
    sy = float(viewer.pixmap_height) / viewer.page_rect.height()

    x0 = (view_rect.x0 - viewer.page_rect.x()) * sx
    y0 = (view_rect.y0 - viewer.page_rect.y()) * sy
    x1 = (view_rect.x1 - viewer.page_rect.x()) * sx
    y1 = (view_rect.y1 - viewer.page_rect.y()) * sy
    return QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized()


def _detect_border_spanning_lines(gray_bytes: bytes, w: int, h: int) -> Tuple[List[int], List[int]]:
    """
    マイナス記号(−)や漢数字の「一」を絶対に消さないよう、
    「画像の左右端（または上下端）近くまで 94% 以上連続して貫通している厚み4px以下の表枠線」のみを検出する。
    """
    if w < 60 or h < 30:
        return [], []

    raw_rows = []
    run_th_w = int(w * 0.94)
    for y in range(h):
        row = gray_bytes[y * w:(y + 1) * w]
        # 左右端5%以内に黒画素が存在しない行は文字（「一」など）なのでスキップ
        margin_x = max(2, int(w * 0.05))
        if min(row[:margin_x]) >= 160 or min(row[-margin_x:]) >= 160:
            continue
        dark_cnt = sum(1 for v in row if v < 155)
        if dark_cnt >= run_th_w:
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
        dark_cnt = sum(1 for v in col if v < 155)
        if dark_cnt >= run_th_h:
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
    """
    赤文字・青文字・緑文字などの有彩色文字も消さずに黒文字として鮮明化する標準輝度コントラスト強調。
    Y = (R*77 + G*150 + B*29) >> 8 を用い、明るい紙地(>=210)を白(255)へ、文字を濃く補正する。
    """
    img32 = qimg.convertToFormat(QImage.Format.Format_RGB32)
    w, h = img32.width(), img32.height()
    bits = img32.constBits()
    bits.setsize(w * h * 4)
    raw = bytes(bits)

    b_arr = raw[0::4]
    g_arr = raw[1::4]
    r_arr = raw[2::4]

    # 高速グレースケール変換とコントラストLUT
    if binarize:
        lut = bytes(255 if v >= 168 else 0 for v in range(256))
    else:
        lut = bytes(
            255 if v >= 212 else (0 if v <= 85 else int(round((v - 85) * 255.0 / (212 - 85))))
            for v in range(256)
        )

    gray = bytes(((r * 77 + g * 150 + b * 29) >> 8) for r, g, b in zip(r_arr, g_arr, b_arr)).translate(lut)
    g_qimg = QImage(gray, w, h, w, QImage.Format.Format_Grayscale8)
    return g_qimg.convertToFormat(QImage.Format.Format_RGB32)


def _preprocess_region_qimage(
    qimg: QImage,
    clean_color: bool = False,
    binarize_mode: bool = False,
    remove_lines: bool = False,
    base_pad_px: int = 36,
) -> Tuple[QImage, float, int, int]:
    """
    切り出したQImageをWindows.Media.Ocrの最適認識条件に整える。
    戻り値: (処理後QImage, スケール倍率, 左パディングpx, 上パディングpx)
    """
    if qimg.isNull() or qimg.width() <= 0 or qimg.height() <= 0:
        return qimg, 1.0, 0, 0

    if clean_color:
        # 黄色・赤・青の背景網掛け除去モード（濃い色文字は極力残す）
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
        # 赤文字・青文字・通常黒文字をすべて維持してコントラスト強調する標準モード
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

    # 300DPI切り出し基準で、極小領域(高さ55px未満)のみ拡大する（過剰拡大による認識不能を防止）
    scale = 1.0
    if h < 36:
        scale = 2.0
    elif h < 58:
        scale = 1.5

    if scale > 1.0:
        img = img.scaled(
            int(round(w * scale)),
            int(round(h * scale)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    # Windows.Media.Ocr が小領域をノイズとして無視しないよう、最小キャンバスサイズ(320x140)を保証する
    min_canvas_w = 320
    min_canvas_h = 140
    pad_x = max(base_pad_px, (min_canvas_w - img.width()) // 2 + 8)
    pad_y = max(base_pad_px, (min_canvas_h - img.height()) // 2 + 8)

    padded = QImage(
        img.width() + pad_x * 2,
        img.height() + pad_y * 2,
        QImage.Format.Format_RGB32,
    )
    padded.fill(QColor(255, 255, 255))
    painter = QPainter(padded)
    painter.drawImage(pad_x, pad_y, img)
    painter.end()
    return padded, scale, pad_x, pad_y


def _sort_and_format_ocr_chunks(
    items: List[Tuple[str, fitz.Rect]]
) -> Tuple[str, List[Tuple[str, fitz.Rect]]]:
    """認識された (text, view_rect) リストを Y座標（行） -> X座標（列）の順に整列する"""
    if not items:
        return "", []

    sorted_by_y = sorted(items, key=lambda x: ((x[1].y0 + x[1].y1) * 0.5, x[1].x0))
    rows: List[List[Tuple[str, fitz.Rect]]] = []

    for item in sorted_by_y:
        _, r = item
        cy = (r.y0 + r.y1) * 0.5
        rh = max(4.0, r.height)
        placed = False
        for row in rows:
            ref_r = row[0][1]
            ref_cy = (ref_r.y0 + ref_r.y1) * 0.5
            ref_h = max(4.0, ref_r.height)
            if abs(cy - ref_cy) <= max(rh, ref_h) * 0.55:
                row.append(item)
                placed = True
                break
        if not placed:
            rows.append([item])

    ordered_items: List[Tuple[str, fitz.Rect]] = []
    text_lines: List[str] = []
    for row in rows:
        row.sort(key=lambda x: x[1].x0)
        ordered_items.extend(row)
        line_parts = [row[0][0]]
        for i in range(1, len(row)):
            prev_r = row[i - 1][1]
            cur_t, cur_r = row[i]
            gap = cur_r.x0 - prev_r.x1
            if gap > max(4.0, prev_r.height * 0.6):
                line_parts.append("  " + cur_t)
            else:
                line_parts.append(cur_t)
        text_lines.append("".join(line_parts))

    return "\n".join(text_lines).strip(), ordered_items


def _remove_existing_ocr_text_in_unrotated_page(
    page: fitz.Page, target_raw_rect: fitz.Rect
) -> None:
    """
    rotation=0, cropbox=mediabox に正規化済みの page に対して、
    選択範囲内の既存OCR透明テキスト(0x010101)のみを事前消去して重複を防ぐ。
    """
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
                if sp.get("color") == OCR_TEXT_MARKER_SRGB_INT:
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
    # chunks, up_scale, pad_x, pad_y, err_msg, cancelled
    finished = pyqtSignal(list, float, int, int, str, bool)


class _RegionOcrWorker(QRunnable):
    """
    QThreadPoolで非同期実行する範囲OCRワーカー。
    1回目の設定で認識結果が0件だった場合、自動的に色抜き設定を反転させてフォールバック再試行する。
    """

    def __init__(
        self,
        raw_qimg: QImage,
        clean_color: bool,
        binarize_mode: bool,
        remove_lines: bool,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.raw_qimg = raw_qimg.copy()
        self.clean_color = clean_color
        self.binarize_mode = binarize_mode
        self.remove_lines = remove_lines
        self.cancel_event = cancel_event
        self.signals = _RegionOcrWorkerSignals()

    def run(self):
        try:
            # NOTE: キャンセル時も必ず finished を emit する (cancelled=True)。
            # emit しないと _on_async_ocr_finished が走らずトーストが
            # "⏳ 範囲OCR解析中..." のまま固まる (#13)。
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
            ) or []

            # 自動2段階フォールバック：もし0件なら色抜きモードを反転して再試行
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
    """メイン画面の右下に表示される範囲OCR結果・精度調整・編集連携パネル"""

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
        self.setFixedSize(420, 240)
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
        opt_bar.setSpacing(6)
        self.chk_clean = QCheckBox("背景色抜き")
        self.chk_clean.setChecked(False)
        self.chk_clean.setToolTip("黄・赤・青の濃い網掛け背景を白く飛ばします（未チェックでも0件時は自動試行されます）")

        self.chk_binarize = QCheckBox("白黒二値化")
        self.chk_binarize.setChecked(False)
        self.chk_binarize.setToolTip("ONにすると完全な白黒2値にします")

        self.chk_lines = QCheckBox("枠線除去")
        self.chk_lines.setChecked(False)
        self.chk_lines.setToolTip("端から端まで貫通する表の細い枠線を除去します")

        self.chk_embed = QCheckBox("透明文字埋込")
        self.chk_embed.setChecked(True)
        self.chk_embed.setToolTip("認識した文字をPDFの選択位置へ透明テキストとして埋め込みます")

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

        self.btn_retry = QPushButton("🔄 設定変更して再OCR")

        self.btn_to_edit = QPushButton("⌨ 認識文字を編集")
        self.btn_to_edit.setStyleSheet(
            "QPushButton { background-color: #1971C2; color: #FFFFFF; border-color: #145A9E; }"
            "QPushButton:hover { background-color: #1864AB; }"
        )
        self.btn_to_edit.setToolTip("テキスト編集モードに切り替え、今OCRした文字（緑枠）をクリックして修正・削除します")

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
    """
    PDFViewer上で直接ドラッグ範囲OCRを実行するコントローラ（C++破棄保護・2段階フォールバック付き）
    """

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

        # ページ切替時に古い緑枠を自動クリア
        self.viewer.pageSelectionChanged.connect(self._on_page_changed)
        self.vp.installEventFilter(self)

    def on_scene_about_to_clear(self):
        """viewer.render_page() で scene.clear() が呼ばれる際に参照を安全に無効化する"""
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
        # NOTE (#8): 範囲OCRはWindows標準OCR専用のため、Windows以外では
        # OCR処理を開始せず、明確なメッセージを表示する。
        # 色抜き (clean_scanned_qimage 等の純粋Qt処理) は引き続き利用可能。
        if sys.platform != "win32":
            QMessageBox.warning(
                self.main_window,
                "Windows専用",
                "範囲OCRはWindows標準OCR（Windows.Media.Ocr）を使用するため、"
                "Windows専用です。\nこの環境では範囲OCRを利用できません。\n"
                "色抜き（カラー除去）などの画像処理は引き続き利用できます。",
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
        # Windows.Media.Ocr に最も適した 300 DPI（極小選択時のみ 380 DPI）
        dpi = 380 if (view_rect.height < 18.0 or view_rect.width < 36.0) else 300
        scale = dpi / 72.0

        with FITZ_LOCK:
            # テキスト消去済みのクリーン表示ドキュメントがあればそこから切り出す
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

        self._cancel_event = threading.Event()
        self._pending_context = {
            "doc": doc,
            "page_idx": page_idx,
            "clip_vr": clip_vr,
            "dpi": dpi,
            "orig_w": qimg.width(),
            "orig_h": qimg.height(),
        }

        self.toast.show_result("Windows標準OCRで選択範囲を解析しています...", "⏳ 範囲OCR解析中...")
        worker = _RegionOcrWorker(
            raw_qimg=qimg,
            clean_color=self.toast.chk_clean.isChecked(),
            binarize_mode=self.toast.chk_binarize.isChecked(),
            remove_lines=self.toast.chk_lines.isChecked(),
            cancel_event=self._cancel_event,
        )
        worker.signals.finished.connect(self._on_async_ocr_finished)
        self._active_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_async_ocr_finished(
        self, chunks: list, up_scale: float, pad_x: int, pad_y: int, err_msg: str,
        cancelled: bool = False,
    ):
        # NOTE: 古いワーカーの遅延シグナルが新しい範囲OCRの状態を壊さないよう、
        # 現在のワーカー以外からの emit は無視する。
        # (キャンセル済みワーカーの emit は _cancel_running_ocr 側で既に状態整理済み)
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
        px_to_view = 72.0 / float(dpi)

        raw_items: List[Tuple[str, fitz.Rect]] = []
        for ch in chunks:
            t = str(ch.get("text", "")).strip()
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

        full_text, embed_chunks_view = _sort_and_format_ocr_chunks(raw_items)
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

                    # view座標のclip_vrを未回転MediaBox座標の矩形へ変換してから安全に重複OCR文字を除去
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
                                        raw_base_pt,
                                        text_str,
                                        fontsize=est_fs,
                                        color=OCR_TEXT_MARKER_RGB,
                                        rotate=saved_rot,
                                        fontfile=JAPANESE_FONT,
                                        fontname=font_alias,
                                        render_mode=3,
                                    )
                                    inserted = True
                                except Exception:
                                    inserted = False
                            if not inserted:
                                new_page.insert_text(
                                    raw_base_pt,
                                    text_str,
                                    fontsize=est_fs,
                                    color=OCR_TEXT_MARKER_RGB,
                                    rotate=saved_rot,
                                    fontname="japan",
                                    render_mode=3,
                                )
                    finally:
                        if new_page.rotation != saved_rot:
                            new_page.set_rotation(saved_rot)
                        if saved_crop != new_page.mediabox:
                            new_page.set_cropbox(saved_crop)

                # ReplacePagesContentCommand.redo 内で render_page と notify_pages_updated が1回だけ実行される
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
            title = f"🎯 範囲OCR完了 ({page_idx + 1}P・{len(embed_chunks_view)}件・コピー済)"
            self.toast.show_result(full_text, title)
            if hasattr(self.main_window, "statusBar"):
                preview_one = full_text.replace("\n", " ")[:32]
                self.main_window.statusBar().showMessage(
                    f"🎯 範囲OCR完了: 「{preview_one}」を認識（緑枠が認識位置です / Ctrl+F検索・Ctrl+クリック電卓・Ctrl+Z対応）"
                )
        else:
            self._clear_result_highlights()
            self.toast.show_result(
                "(文字が検出されませんでした。\n・もう一回り広く囲むか、「背景色抜き」や「白黒二値化」を切り替えて「🔄 設定変更して再OCR」を押してください)",
                "⚠️ 文字未検出"
            )


def install_region_ocr_patch():
    """
    [V24 Native Integrated]
    範囲OCRモードは MainWindow および PDFViewer にネイティブ統合されました。
    後方互換性のためにのみ関数シンボルを残しています（モンキーパッチは行いません）。
    """
    return None
