# -*- coding: utf-8 -*-
"""高DPI対応PDFページキャンバス (PDFViewer) およびピクセル等倍描画アイテム"""
import copy
import math
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import fitz
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QGraphicsEllipseItem,
    QGraphicsItemGroup,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import (
    ERASE_WIDTH,
    HIGHLIGHT_OPACITY,
    TEXT_LINE_SPACING,
    adjust_updated_after_on_remove,
    annotation_to_erase_rects,
    build_pdf_token_replacement_annotation,
    build_text_annotation,
    plan_text_annotation_removal,
    clip_stroke_by_rects,
    collect_all_erased_raw_rects,
    collect_text_only_redact_raw_rects,
    split_text_only_redact_raw_rects,
    find_draw_annotation_at_point,
    find_draws_along_eraser_stroke,
    find_highlight_annotation_at_point,
    find_highlights_in_rect,
    find_pdf_token_at_point,
    find_text_annotation_at_point,
    find_text_targets_in_rect,
    get_later_user_erase_rects,
    get_visible_draw_segments,
    is_highlight_annotation_visible,
    is_rect_substantially_erased,
    logger,
    point_to_segment_distance,
)
from ui import rgb_tuple_to_qcolor
from core.text_tools import spans_are_scan_overlay
from core.batch_apply import (
    SCOPE_ALL,
    SCOPE_CURRENT,
    SCOPE_SELECTED,
    format_batch_result,
    partition_compatible_pages,
    resolve_target_pages,
)
from core.commands import (
    AddAnnotationCommand,
    AddAnnotationsMultiPageCommand,
    CropPageCommand,
    CropPagesCommand,
    EditAnnotationCommand,
    HistoryManager,
    RemoveAnnotationCommand,
    RemoveMultipleAnnotationsCommand,
    ReplacePageAnnotationsCommand,
    RotatePageCommand,
)
from core.fold import (
    FOLD_HORIZONTAL,
    FOLD_VERTICAL,
    clamp_fold_band_to_axis,
    clamp_fold_band_to_page,
    decide_fold_orientation,
    fold_band_pixel_cols,
    fold_band_pixel_rows,
    fold_display_height,
    fold_display_width,
    folded_x_to_view_x,
    folded_y_to_view_y,
    validate_fold_band,
    validate_fold_band_axis,
    view_coord_in_band,
    view_rect_intersects_band_axis,
    view_x_to_folded_x,
    view_y_to_folded_y,
)
from core.geometry import (
    DEFAULT_COLOR,
    DRAW_WIDTH,
    FITZ_LOCK,
    RGBTuple,
    JAPANESE_FONT,
    MAX_DRAW_WIDTH,
    MAX_DYNAMIC_PDF_SCALE,
    MAX_FONT_SIZE,
    MIN_DRAW_WIDTH,
    MIN_DYNAMIC_PDF_SCALE,
    MIN_FONT_SIZE,
    RENDER_SCALE,
    TEXT_FONT_SIZE,
    WHEEL_PAGE_TURN_COOLDOWN,
    clamp_render_scales,
    create_configured_preview_font,
    estimate_text_bbox_in_pdf,
    extract_number_from_text,
    format_pt,
    get_effective_cropbox,
    get_preview_font_family,
    raw_pdf_to_view_point_from_geom,
    raw_rect_to_view_rect,
    raw_rect_to_view_rect_from_geom,
    srgb_int_to_rgb_tuple,
    text_annotation_rotation,
    view_point_to_raw_pdf,
    view_point_to_raw_pdf_from_geom,
    view_rect_to_raw_rect,
)
from core.pdf_engine import PDFDocumentManager
from core.text_select import select_words_in_rect, words_to_text
from ui.dialogs import PreciseTextAnnotationItem, TextEditDeleteDialog


class _TextCopyPopup(QWidget):
    """テキスト選択後に選択範囲の近くに出すフローティングの「📋 コピー」ボタン。

    Popup ウィンドウなので、外側のクリックで自動的に閉じる。Esc でも閉じる。
    コピーボタンを押すと on_copy（クリップボードへコピー）を呼んでから閉じる。
    閉じるときは on_closed を一度だけ呼ぶ（選択ハイライトの解除は呼び出し側で行う）。
    """

    def __init__(self, parent, on_copy, on_closed):
        super().__init__(
            parent,
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint,
        )
        self._on_copy = on_copy
        self._on_closed = on_closed
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        self._button = QPushButton("📋 コピー", self)
        self._button.setToolTip("選択中のテキストをクリップボードにコピーします")
        self._button.clicked.connect(self._handle_copy_clicked)
        layout.addWidget(self._button)
        self.setStyleSheet(
            "QWidget { background-color: #ffffff; border: 1px solid #8a97a5; border-radius: 6px; }"
            "QPushButton { background-color: #e8f2ff; color: #0b3d91; border: 1px solid #7fb0e8; "
            "  border-radius: 4px; padding: 6px 14px; font-weight: bold; font-size: 13px; }"
            "QPushButton:hover { background-color: #d3e6ff; }"
            "QPushButton:pressed { background-color: #bcd8ff; }"
        )

    def _handle_copy_clicked(self):
        if self._on_copy is not None:
            self._on_copy()
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        super().closeEvent(event)
        if self._on_closed is not None:
            callback, self._on_closed = self._on_closed, None
            callback()


class CrispPagePixmapItem(QGraphicsRectItem):
    def __init__(self, pixmap, scene_w, scene_h):
        super().__init__(0.0, 0.0, float(scene_w), float(scene_h))
        self._pixmap = pixmap
        self._scene_w = float(scene_w)
        self._scene_h = float(scene_h)
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setZValue(-100)

    def set_page_pixmap(self, pixmap, scene_w, scene_h):
        self._pixmap = pixmap
        if abs(self._scene_w - scene_w) > 1e-4 or abs(self._scene_h - scene_h) > 1e-4:
            self._scene_w = float(scene_w)
            self._scene_h = float(scene_h)
            self.setRect(0.0, 0.0, self._scene_w, self._scene_h)
        self.update()

    def paint(self, painter, option, widget=None):
        if self._pixmap is None or self._pixmap.isNull():
            return

        wt = painter.worldTransform()
        if abs(wt.m12()) < 1e-6 and abs(wt.m21()) < 1e-6:
            p0 = wt.map(QPointF(0.0, 0.0))
            p1 = wt.map(QPointF(self._scene_w, self._scene_h))

            dpr = 1.0
            if widget is not None:
                dpr = max(1.0, float(widget.devicePixelRatioF()))
            elif painter.device() is not None:
                dpr = max(1.0, float(painter.device().devicePixelRatioF()))

            dest_phys_w = max(1, int(round((p1.x() - p0.x()) * dpr)))
            dest_phys_h = max(1, int(round((p1.y() - p0.y()) * dpr)))

            if (
                abs(self._pixmap.width() - dest_phys_w) <= 2
                and abs(self._pixmap.height() - dest_phys_h) <= 2
            ):
                aligned_phys_x = round(p0.x() * dpr)
                aligned_phys_y = round(p0.y() * dpr)

                painter.save()
                painter.resetTransform()
                self._pixmap.setDevicePixelRatio(dpr)
                painter.drawPixmap(
                    QPointF(aligned_phys_x / dpr, aligned_phys_y / dpr),
                    self._pixmap,
                )
                painter.restore()
                return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self._pixmap.setDevicePixelRatio(1.0)
        painter.drawPixmap(
            QRectF(0.0, 0.0, self._scene_w, self._scene_h),
            self._pixmap,
            QRectF(0.0, 0.0, float(self._pixmap.width()), float(self._pixmap.height())),
        )
        painter.restore()


class PDFViewer(QGraphicsView):

    statusUpdated = pyqtSignal()
    pagesUpdated = pyqtSignal(object)
    pageSelectionChanged = pyqtSignal(int)
    filesDroppedOnViewer = pyqtSignal(list)
    fontSizeChanged = pyqtSignal(float)
    drawWidthChanged = pyqtSignal(float)
    colorChanged = pyqtSignal(tuple)
    calcSelectionChanged = pyqtSignal()
    escapePressed = pyqtSignal()
    searchStatusChanged = pyqtSignal(int, int)
    batchStatusMessage = pyqtSignal(str)
    scrollModeChanged = pyqtSignal(bool)
    _cached_font_family = None

    def __init__(self, parent=None):
        super().__init__(parent)

        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)

        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAcceptDrops(True)

        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        if PDFViewer._cached_font_family is None:
            PDFViewer._cached_font_family = get_preview_font_family()

        self.preview_font_family = PDFViewer._cached_font_family

        self.current_pdf = None
        self.current_page_idx = 0
        self.current_file = None
        self.current_pdf_was_encrypted = False

        self.mode = "view"
        self.current_color = tuple(DEFAULT_COLOR)
        self.pen_eraser_active = False
        self.current_draw_width = float(DRAW_WIDTH)
        self.current_font_size = float(TEXT_FONT_SIZE)

        self.drawing = False
        self._panning = False
        self._pan_button = None
        self._pan_start_pos = QPointF()
        self._space_pressed = False
        self._last_wheel_page_turn_time = 0.0

        # ページスクロール表示（通常画面で全ページを縦に並べてスクロールする表示）。
        # 閲覧専用。注釈の表示・編集はしない（ダブルクリックで1ページ表示に戻る）。
        self.scroll_mode = False
        self._scroll_zoom = 1.0
        self._scroll_page_rects: Dict[int, QRectF] = {}
        self._scroll_items: Dict[int, object] = {}
        self._scroll_rendered: set = set()
        self._scroll_pixmap_cache: Dict[Tuple[int, int], QPixmap] = {}
        self._scroll_signature = None
        self._scroll_updating_page = False
        self._scroll_press_pos = None
        self.verticalScrollBar().valueChanged.connect(self._on_scroll_bar_changed)

        self.page_annotations = {}
        self.history = HistoryManager()

        self.current_path = None
        self.current_path_item = None
        self.current_stroke = []

        self.drag_start_pos = None
        self.drag_rect_item = None

        self.text_guide_group = None
        self.hover_highlight_item = None
        self.calc_highlight_group = None
        self.calc_selected_items = []
        self._calc_dragging = False
        self._calc_drag_start_pos = QPointF()
        self._calc_drag_rect_item = None
        self.search_highlight_group = None
        self.search_results = []
        self.current_search_idx = -1
        self.current_search_query = ""
        self._last_hover_scene_pos = None

        self._dragging_text_ann_idx = None
        self._dragging_text_item = None
        self._text_drag_start_view_pos = QPointF()
        self._text_drag_start_scene_pos = QPointF()
        self._text_drag_start_item_pos = QPointF()
        self._text_drag_moved = False

        self.render_scale = RENDER_SCALE
        self.page_rect = None
        self._page_fitz_rect = fitz.Rect(0, 0, 1, 1)
        self._page_crop_rect = fitz.Rect(0, 0, 1, 1)
        self._page_rotation = 0
        self.pixmap_width = 0
        self.pixmap_height = 0

        self._base_pixmap_item = None
        self._cached_base_pixmap = None
        self._cached_page_idx = -1
        self._rendered_target_phys_width = 0
        self._rendered_target_phys_height = 0
        self._clean_redacted_doc = None

        self._zoom_render_timer = QTimer(self)
        self._zoom_render_timer.setSingleShot(True)
        self._zoom_render_timer.setInterval(45)
        self._zoom_render_timer.timeout.connect(self._on_zoom_render_timeout)

        self._cached_pdf_tokens = []
        self._cached_tokens_key = None
        self._pdf_tokens_cache: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
        self.region_ocr_controller = None
        # ページ一覧で選択中のページ番号を返すコールバック（MainWindow が設定）。
        # 未設定の場合は今のページだけが選択対象になる。
        self.selected_pages_provider = None
        # 表示だけの折りたたみ帯（ページ番号 -> (向き, a0, a1) 表示座標系）。
        # 向き "h" は横帯 (y0, y1)、向き "v" は縦帯 (x0, x1)。
        # PDFファイルには一切書き込まない。表示だけの状態。
        self._fold_bands: Dict[int, Tuple[str, float, float]] = {}
        # テキスト選択（ドラッグで文字を選び、コピーボタンを出す）の一時状態。
        # 単語は raw 座標のタプル (x0, y0, x1, y1, text, block, line, word)。
        self._text_sel_words: List[Tuple] = []
        self._text_sel_text = ""
        self._text_sel_highlight_group = None
        self._copy_popup = None

    @property
    def current_annotations(self):
        return self.page_annotations.setdefault(self.current_page_idx, [])

    @property
    def is_modified(self):
        return self.history.is_modified

    def _close_clean_redacted_doc(self):
        if self._clean_redacted_doc is not None:
            try:
                with FITZ_LOCK:
                    self._clean_redacted_doc.close()
            except Exception:
                logger.debug("Failed to close clean redacted doc", exc_info=True)
            self._clean_redacted_doc = None

    def notify_pages_updated(self, page_indices=None):
        if self.current_search_query:
            self._refresh_active_search()
        self.pagesUpdated.emit(page_indices)
        self.statusUpdated.emit()

    def cancel_active_interaction(self):
        if self.current_path_item is not None:
            if self.current_path_item.scene() is self.scene:
                self.scene.removeItem(self.current_path_item)
            self.current_path_item = None
        self.current_path = None
        self.current_stroke = []

        if self.drag_rect_item is not None:
            if self.drag_rect_item.scene() is self.scene:
                self.scene.removeItem(self.drag_rect_item)
            self.drag_rect_item = None
        self.drag_start_pos = None
        self.drawing = False

        if self._calc_drag_rect_item is not None:
            if self._calc_drag_rect_item.scene() is self.scene:
                self.scene.removeItem(self._calc_drag_rect_item)
            self._calc_drag_rect_item = None
        self._calc_dragging = False

        if self._dragging_text_item is not None and self._text_drag_moved:
            self._dragging_text_item.setPos(self._text_drag_start_item_pos)
        self._reset_text_drag_state()
        self._clear_interactive_guides()
        self.clear_text_selection()
        if self.region_ocr_controller is not None:
            self.region_ocr_controller.cancel_drag()

    # ==========================================
    # 表示だけの折りたたみ（帯を非表示にして詰めて表示）
    # 横帯は上下を詰め、縦帯は左右を詰める。PDFファイルには一切書き込まない。表示だけの状態。
    # ==========================================

    def _fold_band_for_page(self, page_idx=None):
        """指定ページの折りたたみ帯（(向き, a0, a1) 表示座標系）を返す。なければ None。

        ページ形状が変わって帯が無効になっていたら破棄する。
        """
        idx = self.current_page_idx if page_idx is None else int(page_idx)
        band = self._fold_bands.get(idx)
        if band is None or self.page_rect is None:
            return None
        orientation, a0, a1 = band[0], float(band[1]), float(band[2])
        if orientation == FOLD_VERTICAL:
            err = validate_fold_band_axis(
                self.page_rect.x(), self.page_rect.width(), a0, a1, "幅"
            )
        else:
            err = validate_fold_band(
                self.page_rect.y(), self.page_rect.height(), a0, a1
            )
        if err is not None:
            self._fold_bands.pop(idx, None)
            return None
        return (orientation, a0, a1)

    def _fold_display_height(self):
        """折りたたみ後の表示上のページ高さ（表示座標系の単位）を返す。"""
        if self.page_rect is None:
            return 0.0
        band = self._fold_band_for_page()
        if band is not None and band[0] == FOLD_HORIZONTAL:
            return fold_display_height(self.page_rect.height(), (band[1], band[2]))
        return float(self.page_rect.height())

    def _fold_display_width(self):
        """折りたたみ後の表示上のページ幅（表示座標系の単位）を返す。"""
        if self.page_rect is None:
            return 0.0
        band = self._fold_band_for_page()
        if band is not None and band[0] == FOLD_VERTICAL:
            return fold_display_width(self.page_rect.width(), (band[1], band[2]))
        return float(self.page_rect.width())

    def _display_scale(self):
        """表示座標系の長さをシーン座標に換算する倍率（= render_scale 相当）。"""
        display_w = self._fold_display_width()
        if display_w <= 0 or self.pixmap_width <= 0:
            return 0.0
        return self.pixmap_width / display_w

    def has_fold_band(self, page_idx=None):
        return self._fold_band_for_page(page_idx) is not None

    def set_fold_band_for_current_page(self, a0, a1, orientation=FOLD_HORIZONTAL):
        """今のページに折りたたみ帯を設定する。

        向き "h" の場合は a0=y0, a1=y1（横帯）、向き "v" の場合は
        a0=x0, a1=x1（縦帯）。成功したら None、無効なら理由の文言を返す。
        設定時は表示を折りたたみ後の状態で描き直す。
        """
        if self.page_rect is None or not self.current_pdf:
            return "ページが開かれていません。"
        if orientation == FOLD_VERTICAL:
            band = clamp_fold_band_to_axis(
                self.page_rect.x(), self.page_rect.width(), a0, a1
            )
            err = validate_fold_band_axis(
                self.page_rect.x(), self.page_rect.width(), band[0], band[1], "幅"
            )
        else:
            orientation = FOLD_HORIZONTAL
            band = clamp_fold_band_to_page(
                self.page_rect.y(), self.page_rect.height(), a0, a1
            )
            err = validate_fold_band(
                self.page_rect.y(), self.page_rect.height(), band[0], band[1]
            )
        if err is not None:
            return err
        self._fold_bands[self.current_page_idx] = (orientation, band[0], band[1])
        self.render_page(fit_view=False, rebuild_base=True)
        return None

    def clear_fold_band(self, page_idx=None, rerender=True):
        """指定ページの折りたたみ帯を解除する。帯があったら True を返す。"""
        idx = self.current_page_idx if page_idx is None else int(page_idx)
        if idx not in self._fold_bands:
            return False
        del self._fold_bands[idx]
        if rerender and idx == self.current_page_idx:
            self.render_page(fit_view=False, rebuild_base=True)
        return True

    def _view_rect_intersects_fold(self, raw_rect):
        """PDF座標の矩形が今のページの折りたたみ帯と重なるか判定する。"""
        band = self._fold_band_for_page()
        if band is None:
            return False
        view_rect = raw_rect_to_view_rect_from_geom(
            self._page_crop_rect,
            self._page_fitz_rect,
            self._page_rotation,
            fitz.Rect(raw_rect),
        )
        return view_rect_intersects_band_axis(
            (view_rect.x0, view_rect.y0, view_rect.x1, view_rect.y1),
            (band[1], band[2]),
            band[0],
        )

    def _view_point_in_fold(self, raw_point):
        """PDF座標の点が今のページの折りたたみ帯の中にあるか判定する。"""
        band = self._fold_band_for_page()
        if band is None:
            return False
        view_pt = raw_pdf_to_view_point_from_geom(
            self._page_crop_rect,
            self._page_fitz_rect,
            self._page_rotation,
            fitz.Point(raw_point),
        )
        if band[0] == FOLD_VERTICAL:
            return view_coord_in_band(view_pt.x, (band[1], band[2]))
        return view_coord_in_band(view_pt.y, (band[1], band[2]))

    def _refresh_hover_from_global_cursor(self):
        vp_pos = self.viewport().mapFromGlobal(QCursor.pos())
        if self.viewport().rect().contains(vp_pos):
            self._last_hover_scene_pos = self.mapToScene(vp_pos)
            self._update_hover_and_text_guide(self._last_hover_scene_pos)
        else:
            self._last_hover_scene_pos = None
            self._clear_interactive_guides()

    def set_pen_eraser_active(self, active: bool):
        new_val = bool(active)
        if self.pen_eraser_active == new_val:
            return
        self.pen_eraser_active = new_val
        self.colorChanged.emit(self.current_color)
        if (
            self.mode == "draw"
            and self._last_hover_scene_pos is not None
            and not self.drawing
        ):
            self._update_hover_and_text_guide(self._last_hover_scene_pos)

    def set_current_color(self, rgb_tuple):
        if not rgb_tuple or len(rgb_tuple) < 3:
            return
        new_color = (
            max(0.0, min(1.0, float(rgb_tuple[0]))),
            max(0.0, min(1.0, float(rgb_tuple[1]))),
            max(0.0, min(1.0, float(rgb_tuple[2]))),
        )
        was_eraser = self.pen_eraser_active
        self.pen_eraser_active = False
        if self.current_color == new_color and not was_eraser:
            return
        self.current_color = new_color
        self.colorChanged.emit(new_color)
        if (
            self.mode in ("type", "draw")
            and self._last_hover_scene_pos is not None
            and self._dragging_text_ann_idx is None
            and not self.drawing
        ):
            self._update_hover_and_text_guide(self._last_hover_scene_pos)

    def set_draw_width(self, width):
        clamped = round(max(MIN_DRAW_WIDTH, min(MAX_DRAW_WIDTH, float(width))), 1)
        if abs(self.current_draw_width - clamped) < 1e-4:
            return
        self.current_draw_width = clamped
        self.drawWidthChanged.emit(clamped)
        if (
            self.mode == "draw"
            and self._last_hover_scene_pos is not None
            and not self.drawing
        ):
            self._update_hover_and_text_guide(self._last_hover_scene_pos)

    def set_font_size(self, size):
        clamped = round(max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, float(size))), 1)
        if abs(self.current_font_size - clamped) < 1e-4:
            return
        self.current_font_size = clamped
        self.fontSizeChanged.emit(clamped)
        if self.mode == "type" and self._last_hover_scene_pos is not None and self._dragging_text_ann_idx is None:
            self._update_hover_and_text_guide(self._last_hover_scene_pos)

    def reset_history(self):
        self.history.clear()
        self.page_annotations.clear()
        self.calc_selected_items.clear()
        self.calcSelectionChanged.emit()
        self.clear_search_results()
        self._close_clean_redacted_doc()
        self._cached_base_pixmap = None
        self._cached_page_idx = -1
        self._rendered_target_phys_width = 0
        self._rendered_target_phys_height = 0
        self._cached_pdf_tokens = []
        self._cached_tokens_key = None
        self._pdf_tokens_cache.clear()
        self.clear_text_selection()
        if self.region_ocr_controller is not None and hasattr(self.region_ocr_controller, "reset"):
            self.region_ocr_controller.reset()

    def clear_search_results(self):
        self.search_results.clear()
        self.current_search_idx = -1
        self.current_search_query = ""
        if self.search_highlight_group is not None:
            if self.search_highlight_group.scene() is self.scene:
                self.scene.removeItem(self.search_highlight_group)
            self.search_highlight_group = None
        self.searchStatusChanged.emit(0, 0)

    def _collect_search_matches(self, clean_q):
        with FITZ_LOCK:
            return self._collect_search_matches_locked(clean_q)

    def _collect_search_matches_locked(self, clean_q):
        if not clean_q or not self.current_pdf:
            return []

        norm_q = unicodedata.normalize("NFKC", clean_q).casefold()
        results = []

        for p_idx in range(len(self.current_pdf)):
            page = self.current_pdf[p_idx]
            effective_crop = get_effective_cropbox(page)
            ann_list = self.page_annotations.get(p_idx, [])

            erased_rects = collect_text_only_redact_raw_rects(ann_list)
            for ann in ann_list:
                if ann["type"] in ("erase", "box_erase"):
                    erased_rects.extend(annotation_to_erase_rects(ann))

            page_matches_raw = []

            try:
                hit_rects = page.search_for(clean_q)
            except Exception:
                hit_rects = []

            # search_for の矩形は get_text と同じ「未回転・cropbox基準」の
            # 座標で返る。raw 座標へは cropbox の原点を足すだけでよい
            # （view_rect_to_raw_rect を使うと回転ページで二重に回転してしまう）。
            for vr in hit_rects:
                raw_r = fitz.Rect(
                    vr.x0 + effective_crop.x0,
                    vr.y0 + effective_crop.y0,
                    vr.x1 + effective_crop.x0,
                    vr.y1 + effective_crop.y0,
                )
                center = fitz.Point((raw_r.x0 + raw_r.x1) / 2.0, (raw_r.y0 + raw_r.y1) / 2.0)
                if not effective_crop.contains(center):
                    continue
                if is_rect_substantially_erased(raw_r, erased_rects):
                    continue
                clipped_r = raw_r & effective_crop
                if not clipped_r.is_empty:
                    page_matches_raw.append(clipped_r)

            try:
                raw_dict = page.get_text("rawdict")
            except Exception:
                raw_dict = {}

            for block in raw_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    line_chars = []
                    for span in line.get("spans", []):
                        for ch in span.get("chars", []):
                            c_str = ch.get("c", "")
                            if c_str:
                                line_chars.append((c_str, fitz.Rect(ch.get("bbox", (0, 0, 0, 0)))))
                    if not line_chars:
                        continue

                    norm_Pieces = []
                    char_map = []
                    for idx_c, (c_str, _) in enumerate(line_chars):
                        nc = unicodedata.normalize("NFKC", c_str).casefold()
                        if nc:
                            norm_Pieces.append(nc)
                            char_map.extend([idx_c] * len(nc))

                    norm_line = "".join(norm_Pieces)
                    if not norm_line or norm_q not in norm_line:
                        continue

                    start_pos = 0
                    q_len = len(norm_q)
                    while True:
                        found = norm_line.find(norm_q, start_pos)
                        if found == -1:
                            break
                        c_start = char_map[found]
                        c_end = char_map[min(len(char_map) - 1, found + q_len - 1)]
                        vr = fitz.Rect(line_chars[c_start][1])
                        for k in range(c_start + 1, c_end + 1):
                            vr |= line_chars[k][1]

                        # 文字矩形も未回転・cropbox基準の座標なので、
                        # search_for と同じく原点を足して raw 座標にする
                        raw_r = fitz.Rect(
                            vr.x0 + effective_crop.x0,
                            vr.y0 + effective_crop.y0,
                            vr.x1 + effective_crop.x0,
                            vr.y1 + effective_crop.y0,
                        )
                        center = fitz.Point((raw_r.x0 + raw_r.x1) / 2.0, (raw_r.y0 + raw_r.y1) / 2.0)
                        if effective_crop.contains(center) and not is_rect_substantially_erased(raw_r, erased_rects):
                            clipped_r = raw_r & effective_crop
                            if not clipped_r.is_empty and not any(
                                existing.intersects(clipped_r) for existing in page_matches_raw
                            ):
                                page_matches_raw.append(clipped_r)
                        start_pos = found + max(1, q_len)

            for ann_idx, ann in enumerate(ann_list):
                if ann.get("type") != "text":
                    continue
                later_erases = get_later_user_erase_rects(ann_list, ann_idx, ann)
                bbox = estimate_text_bbox_in_pdf(ann)
                if not effective_crop.intersects(bbox):
                    continue
                if is_rect_substantially_erased(bbox, later_erases):
                    continue
                ann_text = str(ann.get("text", ""))
                norm_ann = unicodedata.normalize("NFKC", ann_text).casefold()
                if norm_q in norm_ann:
                    clipped_bbox = bbox & effective_crop
                    if not clipped_bbox.is_empty:
                        page_matches_raw.append(clipped_bbox)

            def _view_sort_key(r_raw):
                vr_sort = raw_rect_to_view_rect(page, r_raw)
                return (round(vr_sort.y0 / 4.0), round(vr_sort.x0, 1))

            page_matches_raw.sort(key=_view_sort_key)
            for r in page_matches_raw:
                results.append({
                    "page_idx": p_idx,
                    "raw_rect": (r.x0, r.y0, r.x1, r.y1),
                })

        return results

    def _refresh_active_search(self):
        if not self.current_search_query or not self.current_pdf:
            return
        results = self._collect_search_matches(self.current_search_query)
        self.search_results = results
        if not results:
            self.current_search_idx = -1
            self._update_search_highlight_items()
            self.searchStatusChanged.emit(0, 0)
            return
        if self.current_search_idx < 0:
            self.current_search_idx = 0
        else:
            self.current_search_idx = min(self.current_search_idx, len(results) - 1)
        self._update_search_highlight_items()
        self.searchStatusChanged.emit(self.current_search_idx + 1, len(results))

    def perform_text_search(self, query):
        clean_q = (query or "").strip()
        if not clean_q or not self.current_pdf:
            self.clear_search_results()
            return 0

        self.current_search_query = clean_q
        results = self._collect_search_matches(clean_q)
        self.search_results = results

        if not results:
            self.current_search_idx = -1
            self._update_search_highlight_items()
            self.searchStatusChanged.emit(0, 0)
            return 0

        first_idx = 0
        for i, item in enumerate(results):
            if item["page_idx"] >= self.current_page_idx:
                first_idx = i
                break

        self.go_to_search_result(first_idx)
        return len(results)

    def find_next_match(self, query=None):
        if query is not None and query.strip() != self.current_search_query:
            return self.perform_text_search(query)
        if not self.search_results:
            if query:
                return self.perform_text_search(query)
            return 0
        next_idx = (self.current_search_idx + 1) % len(self.search_results)
        self.go_to_search_result(next_idx)
        return len(self.search_results)

    def find_prev_match(self, query=None):
        if query is not None and query.strip() != self.current_search_query:
            return self.perform_text_search(query)
        if not self.search_results:
            if query:
                return self.perform_text_search(query)
            return 0
        prev_idx = (self.current_search_idx - 1) % len(self.search_results)
        self.go_to_search_result(prev_idx)
        return len(self.search_results)

    def go_to_search_result(self, result_idx):
        if not (0 <= result_idx < len(self.search_results)) or not self.current_pdf:
            return

        self.current_search_idx = result_idx
        target = self.search_results[result_idx]
        target_page = target["page_idx"]

        if target_page != self.current_page_idx:
            self.load_pdf(self.current_pdf, target_page, fit_view=False)
        else:
            self._update_search_highlight_items()

        r = fitz.Rect(target["raw_rect"])
        x0, y0 = self.pdf_to_scene(fitz.Point(r.x0, r.y0))
        x1, y1 = self.pdf_to_scene(fitz.Point(r.x1, r.y1))
        s_rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized()
        self.centerOn(s_rect.center())

        self.searchStatusChanged.emit(result_idx + 1, len(self.search_results))

    def _update_search_highlight_items(self):
        if self.search_highlight_group is not None:
            if self.search_highlight_group.scene() is self.scene:
                self.scene.removeItem(self.search_highlight_group)
            self.search_highlight_group = None

        if not self.current_pdf or not self.search_results:
            return

        group = QGraphicsItemGroup()
        group.setZValue(960)
        has_item = False

        for i, entry in enumerate(self.search_results):
            if entry["page_idx"] != self.current_page_idx:
                continue
            r = fitz.Rect(entry["raw_rect"])
            if self._view_rect_intersects_fold(r):
                continue
            x0, y0 = self.pdf_to_scene(fitz.Point(r.x0, r.y0))
            x1, y1 = self.pdf_to_scene(fitz.Point(r.x1, r.y1))
            s_rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized().adjusted(-3, -2, 3, 2)

            rect_item = QGraphicsRectItem(s_rect)
            if i == self.current_search_idx:
                rect_item.setPen(QPen(QColor(220, 20, 90, 250), 2.8, Qt.PenStyle.SolidLine))
                rect_item.setBrush(QBrush(QColor(255, 80, 140, 85)))
            else:
                rect_item.setPen(QPen(QColor(0, 150, 190, 220), 1.8, Qt.PenStyle.SolidLine))
                rect_item.setBrush(QBrush(QColor(0, 210, 255, 55)))
            group.addToGroup(rect_item)
            has_item = True

        if has_item:
            self.scene.addItem(group)
            self.search_highlight_group = group

    # ==========================================
    # テキスト選択 → コピーボタン（表示だけの一時選択。PDFは変えない）
    # ==========================================

    def _get_page_word_rects_raw(self, page_idx=None):
        """今のページの単語矩形を raw 座標のタプル一覧で返す。

        PyMuPDF の get_text("words") は未回転・cropbox 基準の座標を返すため、
        cropbox の原点を足して raw（未回転・mediabox 基準）座標に変換する。
        各要素は (x0, y0, x1, y1, text, block_no, line_no, word_no)。
        """
        if not self.current_pdf:
            return []
        target_idx = self.current_page_idx if page_idx is None else int(page_idx)
        if not (0 <= target_idx < len(self.current_pdf)):
            return []
        with FITZ_LOCK:
            page = self.current_pdf[target_idx]
            crop = get_effective_cropbox(page)
            try:
                words = page.get_text("words") or []
            except Exception:
                return []
        out = []
        for w in words:
            if len(w) < 5:
                continue
            text = str(w[4]).strip()
            if not text:
                continue
            try:
                block_no = int(w[5]) if len(w) > 5 else 0
                line_no = int(w[6]) if len(w) > 6 else 0
                word_no = int(w[7]) if len(w) > 7 else 0
            except (TypeError, ValueError):
                block_no, line_no, word_no = 0, 0, 0
            out.append((
                float(w[0]) + crop.x0,
                float(w[1]) + crop.y0,
                float(w[2]) + crop.x0,
                float(w[3]) + crop.y0,
                text,
                block_no,
                line_no,
                word_no,
            ))
        return out

    def _get_annotation_word_rects_raw(self, page_idx=None):
        """今のページのテキスト注釈を、ドラッグ選択用の単語タプルで返す。

        置換で入れたテキストはライブ文書の単語一覧に現れないため、
        注釈の推定BBoxを1単語として扱う。座標は _get_page_word_rects_raw と
        同じ raw 座標系。5要素タプル (x0, y0, x1, y1, text) で返し、
        select_words_in_rect の並び順は座標ベースになる。
        後からユーザー消去で隠された注釈は対象外。
        """
        if not self.current_pdf:
            return []
        target_idx = self.current_page_idx if page_idx is None else int(page_idx)
        ann_list = self.page_annotations.get(target_idx) or []
        out = []
        for ann_idx, ann in enumerate(ann_list):
            if ann.get("type") != "text":
                continue
            text = str(ann.get("text", ""))
            if not text.strip():
                continue
            bbox = estimate_text_bbox_in_pdf(ann)
            if bbox.is_empty:
                continue
            later_erases = get_later_user_erase_rects(ann_list, ann_idx, ann)
            if is_rect_substantially_erased(bbox, later_erases):
                continue
            out.append((bbox.x0, bbox.y0, bbox.x1, bbox.y1, text))
        return out

    def _finish_text_selection(self, raw_rect, scene_rect):
        """ドラッグ確定後のテキスト選択処理。単語を集めてハイライト＋コピーポップアップを出す。

        消去済み領域（白塗り墨消し・テキスト消去）の単語は対象外にする。
        なお単語矩形は get_text("words") が未回転・cropbox 基準で返すため、
        _get_page_word_rects_raw の cropbox 原点加算が全回転角で正しい
        （view_rect_to_raw_rect で変換すると回転時にズレるので使わない）。
        """
        words = self._get_page_word_rects_raw()
        erased_rects = collect_all_erased_raw_rects(self.current_annotations)
        if erased_rects:
            words = [
                w
                for w in words
                if not is_rect_substantially_erased(
                    fitz.Rect(w[0], w[1], w[2], w[3]), erased_rects
                )
            ]
        # 置換で入れたテキスト注釈の文字も選択対象にする（ライブ文書の
        # 単語一覧には現れないため）。注釈の推定BBoxは元の断片の墨消し
        # 矩形と重なるので、上の消去済みフィルタにはかけない。
        # 5要素タプルで足すと並び順は座標ベースのフォールバックになる。
        words = words + self._get_annotation_word_rects_raw()
        selected = select_words_in_rect(
            words, (raw_rect.x0, raw_rect.y0, raw_rect.x1, raw_rect.y1)
        )
        text = words_to_text(selected)
        if not text:
            self.clear_text_selection()
            self._show_status_message("選択範囲にコピーできる文字が見つかりませんでした")
            return
        self._text_sel_words = selected
        self._text_sel_text = text
        self._update_text_sel_highlight_items()
        self._show_copy_popup(scene_rect)
        self._show_status_message(
            f"📋 {len(selected)}個の単語を選択しました（{len(text)}文字）。"
            "コピーボタンを押すとコピーして閉じます"
        )

    def _update_text_sel_highlight_items(self):
        if self._text_sel_highlight_group is not None:
            if self._text_sel_highlight_group.scene() is self.scene:
                self.scene.removeItem(self._text_sel_highlight_group)
            self._text_sel_highlight_group = None

        if not self.current_pdf or not self._text_sel_words:
            return

        group = QGraphicsItemGroup()
        group.setZValue(955)
        for w in self._text_sel_words:
            x0, y0 = self.pdf_to_scene(fitz.Point(w[0], w[1]))
            x1, y1 = self.pdf_to_scene(fitz.Point(w[2], w[3]))
            s_rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized().adjusted(-1.5, -1.5, 1.5, 1.5)
            rect_item = QGraphicsRectItem(s_rect)
            rect_item.setPen(QPen(QColor(0, 120, 215, 200), 1.2, Qt.PenStyle.SolidLine))
            rect_item.setBrush(QBrush(QColor(0, 120, 215, 55)))
            group.addToGroup(rect_item)

        self.scene.addItem(group)
        self._text_sel_highlight_group = group

    def _show_copy_popup(self, scene_rect):
        """選択範囲の右下の近くにフローティングの「📋 コピー」ボタンを出す。"""
        self._hide_copy_popup()
        popup = _TextCopyPopup(
            self.viewport(), self._copy_selected_text, self._on_copy_popup_closed
        )
        popup.adjustSize()
        anchor_view = self.mapFromScene(scene_rect.bottomRight())
        pw, ph = popup.width(), popup.height()
        vp_rect = self.viewport().rect()
        x = min(max(anchor_view.x() + 8, 4), max(4, vp_rect.width() - pw - 4))
        y = min(max(anchor_view.y() + 8, 4), max(4, vp_rect.height() - ph - 4))
        popup.move(self.viewport().mapToGlobal(QPoint(x, y)))
        self._copy_popup = popup
        popup.show()
        popup.setFocus()

    def _hide_copy_popup(self):
        if self._copy_popup is not None:
            popup, self._copy_popup = self._copy_popup, None
            popup.close()

    def _on_copy_popup_closed(self):
        """ポップアップが閉じたら（コピー・Esc・外側クリック）選択も解除する。"""
        self._copy_popup = None
        self._clear_text_selection_data()

    def _clear_text_selection_data(self):
        self._text_sel_words = []
        self._text_sel_text = ""
        if self._text_sel_highlight_group is not None:
            if self._text_sel_highlight_group.scene() is self.scene:
                self.scene.removeItem(self._text_sel_highlight_group)
            self._text_sel_highlight_group = None

    def clear_text_selection(self):
        """テキスト選択のポップアップとハイライトを解除する。"""
        self._hide_copy_popup()
        self._clear_text_selection_data()

    def _copy_selected_text(self):
        """選択中のテキストをクリップボードにコピーする（ポップアップは呼び出し側で閉じる）。"""
        text = self._text_sel_text
        if not text:
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        self._show_status_message(f"📋 テキストをコピーしました（{len(text)}文字）")

    def clear_calc_selection(self):
        if not self.calc_selected_items and self.calc_highlight_group is None:
            return
        self.calc_selected_items.clear()
        self._update_calc_highlight_items()
        self.calcSelectionChanged.emit()

    def _sync_calc_selection_with_annotations(self):
        if not self.calc_selected_items:
            return
        if not self.current_pdf:
            self.calc_selected_items.clear()
            self.calcSelectionChanged.emit()
            return

        num_pages = len(self.current_pdf)
        updated_items = []
        changed = False

        for entry in self.calc_selected_items:
            p_idx = entry.get("page_idx", -1)
            if not (0 <= p_idx < num_pages):
                changed = True
                continue

            with FITZ_LOCK:
                page = self.current_pdf[p_idx]
                effective_crop = get_effective_cropbox(page)
            ann_list = self.page_annotations.get(p_idx, [])
            item_id = entry["id"]
            kind = item_id[0]

            if kind == "ann":
                ann_idx = item_id[2]
                ann = ann_list[ann_idx] if (0 <= ann_idx < len(ann_list)) else None
                if (
                    ann is None
                    or ann.get("type") != "text"
                    or (
                        entry.get("ann_pos") is not None
                        and tuple(ann.get("pos", ())) != tuple(entry["ann_pos"])
                        and ann.get("text", "") != entry.get("raw_text", "")
                    )
                ):
                    relocated_idx = None
                    target_pos = entry.get("ann_pos")
                    target_txt = entry.get("raw_text", "")
                    for cand_idx, cand in enumerate(ann_list):
                        if cand.get("type") != "text":
                            continue
                        if target_pos is not None and tuple(cand.get("pos", ())) == tuple(target_pos) and cand.get("text", "") == target_txt:
                            relocated_idx = cand_idx
                            break
                    if relocated_idx is None and target_pos is not None:
                        for cand_idx, cand in enumerate(ann_list):
                            if cand.get("type") == "text" and tuple(cand.get("pos", ())) == tuple(target_pos):
                                relocated_idx = cand_idx
                                break
                    if relocated_idx is None:
                        changed = True
                        continue
                    ann_idx = relocated_idx
                    ann = ann_list[ann_idx]
                    entry = dict(entry)
                    entry["id"] = ("ann", p_idx, ann_idx)
                    changed = True
                later_erases = get_later_user_erase_rects(ann_list, ann_idx, ann)
                bbox = estimate_text_bbox_in_pdf(ann)
                if not effective_crop.intersects(bbox):
                    changed = True
                    continue
                if is_rect_substantially_erased(bbox, later_erases):
                    changed = True
                    continue
                raw_text = ann.get("text", "")
                val = extract_number_from_text(raw_text)
                if val is None:
                    changed = True
                    continue
                new_rect = (bbox.x0, bbox.y0, bbox.x1, bbox.y1)
                new_pos = tuple(ann.get("pos", ()))
                if (
                    entry["value"] != val
                    or entry["raw_text"] != raw_text
                    or entry["raw_rect"] != new_rect
                    or entry.get("ann_pos") != new_pos
                ):
                    entry = dict(entry)
                    entry["value"] = val
                    entry["raw_text"] = raw_text
                    entry["raw_rect"] = new_rect
                    entry["ann_pos"] = new_pos
                    changed = True
                updated_items.append(entry)

            elif kind == "pdf":
                r = fitz.Rect(entry["raw_rect"])
                r.normalize()
                center = fitz.Point((r.x0 + r.x1) / 2.0, (r.y0 + r.y1) / 2.0)
                if not effective_crop.contains(center):
                    changed = True
                    continue
                erased_rects = collect_text_only_redact_raw_rects(ann_list)
                for ann in ann_list:
                    if ann["type"] in ("erase", "box_erase"):
                        erased_rects.extend(annotation_to_erase_rects(ann))
                if any(er.contains(center) for er in erased_rects):
                    changed = True
                    continue
                updated_items.append(entry)

        if changed or len(updated_items) != len(self.calc_selected_items):
            self.calc_selected_items = updated_items
            self.calcSelectionChanged.emit()

    def _update_calc_highlight_items(self):
        if self.calc_highlight_group is not None:
            if self.calc_highlight_group.scene() is self.scene:
                self.scene.removeItem(self.calc_highlight_group)
            self.calc_highlight_group = None

        if not self.current_pdf or not self.calc_selected_items:
            return

        page_items = [
            entry
            for entry in self.calc_selected_items
            if entry["page_idx"] == self.current_page_idx
        ]
        if not page_items:
            return

        group = QGraphicsItemGroup()
        group.setZValue(950)

        for entry in page_items:
            r = fitz.Rect(entry["raw_rect"])
            if self._view_rect_intersects_fold(r):
                continue
            x0, y0 = self.pdf_to_scene(fitz.Point(r.x0, r.y0))
            x1, y1 = self.pdf_to_scene(fitz.Point(r.x1, r.y1))
            s_rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized().adjusted(-3, -2, 3, 2)

            rect_item = QGraphicsRectItem(s_rect)
            rect_item.setPen(QPen(QColor(230, 105, 0, 245), 2.4, Qt.PenStyle.SolidLine))
            rect_item.setBrush(QBrush(QColor(255, 165, 0, 65)))
            group.addToGroup(rect_item)

        self.scene.addItem(group)
        self.calc_highlight_group = group

    def _toggle_calc_item_at_scene_pos(self, scene_pos):
        if not self.current_pdf or scene_pos is None:
            return False

        hit_item, hit_ann_idx = self._find_text_item_at_scene_pos(scene_pos)
        if hit_item is not None and hit_ann_idx is not None:
            if 0 <= hit_ann_idx < len(self.current_annotations):
                ann = self.current_annotations[hit_ann_idx]
                raw_text = ann.get("text", "")
                val = extract_number_from_text(raw_text)
                if val is None:
                    win = self.window()
                    if isinstance(win, QMainWindow):
                        win.statusBar().showMessage(f"数値として認識できませんでした: 「{raw_text}」")
                    return True

                item_id = ("ann", self.current_page_idx, hit_ann_idx)
                for i, existing in enumerate(self.calc_selected_items):
                    if existing["id"] == item_id:
                        self.calc_selected_items.pop(i)
                        self._update_calc_highlight_items()
                        self.calcSelectionChanged.emit()
                        return True

                bbox = estimate_text_bbox_in_pdf(ann)
                self.calc_selected_items.append({
                    "id": item_id,
                    "page_idx": self.current_page_idx,
                    "value": val,
                    "raw_text": raw_text,
                    "raw_rect": (bbox.x0, bbox.y0, bbox.x1, bbox.y1),
                    "ann_pos": tuple(ann.get("pos", ())),
                })
                self._update_calc_highlight_items()
                self.calcSelectionChanged.emit()
                return True

        pdf_token = self._find_pdf_text_token_at_scene_pos(scene_pos)
        if pdf_token is not None:
            raw_text = pdf_token.get("text", "")
            val = extract_number_from_text(raw_text)
            if val is None:
                win = self.window()
                if isinstance(win, QMainWindow):
                    win.statusBar().showMessage(f"数値として認識できませんでした: 「{raw_text}」")
                return True

            rect_key = tuple(round(v, 2) for v in pdf_token["raw_rect"])
            item_id = ("pdf", self.current_page_idx, rect_key, raw_text)
            for i, existing in enumerate(self.calc_selected_items):
                if existing["id"] == item_id:
                    self.calc_selected_items.pop(i)
                    self._update_calc_highlight_items()
                    self.calcSelectionChanged.emit()
                    return True

            self.calc_selected_items.append({
                "id": item_id,
                "page_idx": self.current_page_idx,
                "value": val,
                "raw_text": raw_text,
                "raw_rect": pdf_token["raw_rect"],
            })
            self._update_calc_highlight_items()
            self.calcSelectionChanged.emit()
            return True

        return False

    @staticmethod
    def _rect_matches_calc_selection(item_rect: fitz.Rect, sel_rect: fitz.Rect) -> bool:
        ir = fitz.Rect(item_rect)
        ir.normalize()
        if not ir.intersects(sel_rect):
            return False
        center = fitz.Point((ir.x0 + ir.x1) * 0.5, (ir.y0 + ir.y1) * 0.5)
        if sel_rect.contains(center):
            return True
        inter = ir & sel_rect
        item_area = max(1e-4, ir.width * ir.height)
        return (inter.width * inter.height) >= item_area * 0.35

    def _select_calc_items_in_scene_rect(self, scene_rect: QRectF) -> int:
        if not self.current_pdf or scene_rect is None or scene_rect.isEmpty():
            return 0

        p0 = self.scene_to_pdf(scene_rect.topLeft())
        p1 = self.scene_to_pdf(scene_rect.bottomRight())
        target_raw_rect = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
        target_raw_rect.normalize()
        if target_raw_rect.is_empty:
            return 0

        with FITZ_LOCK:
            page = self.current_pdf[self.current_page_idx]
            effective_crop = get_effective_cropbox(page)

        existing_ids = {entry["id"] for entry in self.calc_selected_items}
        candidates = []

        ann_list = self.current_annotations
        for ann_idx, ann in enumerate(ann_list):
            if ann.get("type") != "text":
                continue
            later_erases = get_later_user_erase_rects(ann_list, ann_idx, ann)
            bbox = estimate_text_bbox_in_pdf(ann)
            if not effective_crop.intersects(bbox):
                continue
            if is_rect_substantially_erased(bbox, later_erases):
                continue
            if not self._rect_matches_calc_selection(bbox, target_raw_rect):
                continue
            raw_text = ann.get("text", "")
            val = extract_number_from_text(raw_text)
            if val is None:
                continue
            item_id = ("ann", self.current_page_idx, ann_idx)
            if item_id in existing_ids:
                continue
            existing_ids.add(item_id)
            sx, sy = self.pdf_to_scene(fitz.Point(bbox.x0, bbox.y0))
            candidates.append(
                (
                    round(sy / 4.0),
                    sx,
                    {
                        "id": item_id,
                        "page_idx": self.current_page_idx,
                        "value": val,
                        "raw_text": raw_text,
                        "raw_rect": (bbox.x0, bbox.y0, bbox.x1, bbox.y1),
                        "ann_pos": tuple(ann.get("pos", ())),
                    },
                )
            )

        erased_rects = collect_all_erased_raw_rects(ann_list)
        for tok in self._get_page_pdf_text_tokens():
            # 注釈由来のトークンは上の注釈ループで処理済みなので二重に数えない
            if tok.get("ann_idx") is not None:
                continue
            r = fitz.Rect(tok["raw_rect"])
            r.normalize()
            center = fitz.Point((r.x0 + r.x1) * 0.5, (r.y0 + r.y1) * 0.5)
            if not effective_crop.contains(center):
                continue
            if is_rect_substantially_erased(r, erased_rects):
                continue
            if not self._rect_matches_calc_selection(r, target_raw_rect):
                continue
            raw_text = tok.get("text", "")
            val = extract_number_from_text(raw_text)
            if val is None:
                continue
            rect_key = tuple(round(v, 2) for v in tok["raw_rect"])
            item_id = ("pdf", self.current_page_idx, rect_key, raw_text)
            if item_id in existing_ids:
                continue
            existing_ids.add(item_id)
            sx, sy = self.pdf_to_scene(fitz.Point(r.x0, r.y0))
            candidates.append(
                (
                    round(sy / 4.0),
                    sx,
                    {
                        "id": item_id,
                        "page_idx": self.current_page_idx,
                        "value": val,
                        "raw_text": raw_text,
                        "raw_rect": tok["raw_rect"],
                    },
                )
            )

        if not candidates:
            self._show_status_message("選択範囲内に新しく集計できる数字が見つかりませんでした")
            return 0

        candidates.sort(key=lambda item: (item[0], item[1]))
        for _, _, entry in candidates:
            self.calc_selected_items.append(entry)

        self._update_calc_highlight_items()
        self.calcSelectionChanged.emit()
        return len(candidates)

    def load_pdf(self, doc, page_idx=0, fit_view=True):
        # スクロール表示中は、同じ文書のページ移動はスクロールだけですませる。
        if self.scroll_mode and doc is self.current_pdf and len(doc) > 0:
            target = max(0, min(page_idx, len(doc) - 1))
            # ページ構成が変わっていたら（ページの削除・回転などを load_pdf で
            # 通知してくるコマンド系）、古い並びのままにせず作り直す。
            if self._scroll_structure_signature() != self._scroll_signature:
                self._scroll_pixmap_cache.clear()
                self.current_page_idx = target
                self._render_scroll_view()
                self.pageSelectionChanged.emit(target)
                self.statusUpdated.emit()
                return
            self._scroll_to_page(target)
            if target != self.current_page_idx:
                self.current_page_idx = target
                self.pageSelectionChanged.emit(target)
                self.statusUpdated.emit()
            return
        # 別の文書を開いたら折りたたみ状態はリセットする。
        # 同じ文書内のページ移動では状態を残す。
        if doc is not self.current_pdf:
            self._fold_bands.clear()
            self._scroll_pixmap_cache.clear()
        self.cancel_active_interaction()
        self.current_pdf = doc
        self._close_clean_redacted_doc()
        self._cached_base_pixmap = None
        self._cached_page_idx = -1
        self._rendered_target_phys_width = 0
        self._rendered_target_phys_height = 0
        self._cached_pdf_tokens = []
        self._cached_tokens_key = None
        self._pdf_tokens_cache.clear()

        if len(doc) == 0:
            self.current_page_idx = 0
            self.scene.clear()
            self._base_pixmap_item = None
            self.text_guide_group = None
            self.hover_highlight_item = None
            self.calc_highlight_group = None
            self.search_highlight_group = None
            self.page_rect = None
            self.pageSelectionChanged.emit(self.current_page_idx)
            self.statusUpdated.emit()
            return

        self.current_page_idx = max(0, min(page_idx, len(doc) - 1))
        self.render_page(fit_view=fit_view, rebuild_base=True)
        self.pageSelectionChanged.emit(self.current_page_idx)

    def go_prev_page(self):
        if self.current_pdf and self.current_page_idx > 0:
            self.load_pdf(self.current_pdf, self.current_page_idx - 1, fit_view=True)
            return True
        return False

    def go_next_page(self):
        if self.current_pdf and self.current_page_idx < len(self.current_pdf) - 1:
            self.load_pdf(self.current_pdf, self.current_page_idx + 1, fit_view=True)
            return True
        return False

    # ==========================================
    # ページスクロール表示（ベータ版）
    # 通常画面で全ページを縦に並べ、スクロールして読む表示。閲覧専用で、
    # 注釈の表示・編集はしない。ダブルクリックで1ページ表示に戻って編集する。
    # ==========================================
    SCROLL_PAGE_GAP = 16.0
    SCROLL_MIN_ZOOM = 0.5
    SCROLL_MAX_ZOOM = 3.0

    def set_scroll_mode(self, on: bool):
        """スクロール表示のON/OFFを切り替える。OFFにすると1ページ表示に戻る。"""
        on = bool(on)
        if on and (not self.current_pdf or len(self.current_pdf) == 0):
            self._show_status_message("スクロール表示するPDFが開かれていません。先にPDFを開いてください。")
            self.scrollModeChanged.emit(False)
            return
        if on == self.scroll_mode:
            return
        self.cancel_active_interaction()
        self.scroll_mode = on
        if on:
            self._scroll_zoom = 1.0
            self._scroll_pixmap_cache.clear()
            self._render_scroll_view()
            self._show_status_message(
                "スクロール表示：ホイールでページをスクロールします。"
                "ダブルクリックで1ページ表示に戻って編集できます。"
            )
        else:
            self.load_pdf(self.current_pdf, self.current_page_idx, fit_view=True)
        self.scrollModeChanged.emit(on)

    def _scroll_display_size(self, page_idx: int) -> Tuple[float, float]:
        """ページの表示サイズ（PDFポイント単位）を返す。
        page.rect は回転を反映した大きさ（90度回転なら幅と高さが入れ替わった
        大きさ）なので、そのまま使う。通常表示（render_page）と同じ扱い。"""
        with FITZ_LOCK:
            page = self.current_pdf[page_idx]
            rect = page.rect
        return max(1.0, float(rect.width)), max(1.0, float(rect.height))

    def _scroll_structure_signature(self):
        """ページ構成（ページ数・各ページの大きさと回転）の署名を返す。
        ページの追加・削除・回転・トリミングで変わる。注釈の変更では変わらない。"""
        if not self.current_pdf:
            return None
        with FITZ_LOCK:
            return tuple(
                (
                    round(float(page.rect.width), 2),
                    round(float(page.rect.height), 2),
                    page.rotation % 360,
                )
                for page in self.current_pdf
            )

    def _scroll_fit_scale(self) -> float:
        """画面幅に合わせる倍率（ズーム倍率込み）を返す。"""
        if not self.current_pdf or len(self.current_pdf) == 0:
            return 1.0
        max_w = max(self._scroll_display_size(i)[0] for i in range(len(self.current_pdf)))
        avail = max(50.0, float(self.viewport().width()) - 8.0)
        return max(0.05, avail / max_w) * self._scroll_zoom

    def _render_scroll_view(self):
        """スクロール表示のシーン全体を作り直す（レイアウト＋見えているページの描画）。"""
        if not self.current_pdf or len(self.current_pdf) == 0:
            return
        self._scroll_signature = self._scroll_structure_signature()
        self.drawing = False
        self._reset_text_drag_state()
        self.current_path = None
        self.current_path_item = None
        self.current_stroke = []
        self.drag_rect_item = None
        self._calc_dragging = False
        self._calc_drag_rect_item = None
        self.text_guide_group = None
        self.hover_highlight_item = None
        self.calc_highlight_group = None
        self.search_highlight_group = None
        self._hide_copy_popup()
        self._text_sel_words = []
        self._text_sel_text = ""
        self._text_sel_highlight_group = None
        self._base_pixmap_item = None
        if self.region_ocr_controller is not None:
            self.region_ocr_controller.on_scene_about_to_clear()
        self.scene.clear()

        scale = self._scroll_fit_scale()
        total_w = 0.0
        self._scroll_page_rects = {}
        self._scroll_items = {}
        self._scroll_rendered = set()
        rects: Dict[int, QRectF] = {}
        y = 0.0
        for i in range(len(self.current_pdf)):
            w, h = self._scroll_display_size(i)
            rw, rh = w * scale, h * scale
            total_w = max(total_w, rw)
            rects[i] = (rw, rh, y)
            y += rh + self.SCROLL_PAGE_GAP
        total_h = max(1.0, y - self.SCROLL_PAGE_GAP)
        for i, (rw, rh, top) in rects.items():
            x = (total_w - rw) / 2.0
            rect = QRectF(x, top, rw, rh)
            self._scroll_page_rects[i] = rect
            ph = QGraphicsRectItem(rect)
            ph.setPen(QPen(QColor(200, 190, 170)))
            ph.setBrush(QBrush(QColor(245, 242, 235)))
            self.scene.addItem(ph)
            self._scroll_items[i] = ph
        self.scene.setSceneRect(QRectF(0, 0, total_w, total_h))
        self.resetTransform()
        self._render_visible_scroll_pages()
        self._scroll_to_page(self.current_page_idx)
        self.statusUpdated.emit()

    def _render_visible_scroll_pages(self):
        """画面に見えているページ（＋上下1ページ分）だけ描画する。"""
        if not self.scroll_mode or not self._scroll_page_rects:
            return
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        margin = max(200.0, self.viewport().height())
        expanded = visible.adjusted(0, -margin, 0, margin)
        for i, rect in self._scroll_page_rects.items():
            if i in self._scroll_rendered:
                continue
            if rect.intersects(expanded):
                self._render_scroll_page(i)

    def _render_scroll_page(self, page_idx: int):
        rect = self._scroll_page_rects.get(page_idx)
        if rect is None:
            return
        dpr = max(1.0, float(self.viewport().devicePixelRatioF()))
        target_w = max(64, int(round(rect.width() * dpr)))
        cache_key = (page_idx, target_w)
        pixmap = self._scroll_pixmap_cache.get(cache_key)
        if pixmap is None:
            pixmap = self._render_scroll_page_pixmap(page_idx, target_w)
            if pixmap is None:
                return
            self._scroll_pixmap_cache[cache_key] = pixmap
        item = CrispPagePixmapItem(pixmap, rect.width(), rect.height())
        item.setPos(rect.topLeft())
        old = self._scroll_items.get(page_idx)
        if old is not None and old.scene() is self.scene:
            self.scene.removeItem(old)
        self.scene.addItem(item)
        self._scroll_items[page_idx] = item
        self._scroll_rendered.add(page_idx)

    def _render_scroll_page_pixmap(self, page_idx: int, target_w: int):
        """1ページ分の画像を作る。墨消し（テキストのみ）済みのページは消した状態で描く。"""
        with FITZ_LOCK:
            disp_w, _disp_h = self._scroll_display_size(page_idx)
            scale = target_w / disp_w
            source_page = self.current_pdf[page_idx]
            temp_doc = None
            redact_rects, keep_image_rects = split_text_only_redact_raw_rects(
                self.page_annotations.get(page_idx, [])
            )
            if redact_rects or keep_image_rects:
                temp_doc = fitz.open()
                temp_doc.insert_pdf(self.current_pdf, from_page=page_idx, to_page=page_idx)
                temp_page = temp_doc[0]
                saved_rot = temp_page.rotation % 360
                saved_crop = get_effective_cropbox(temp_page)
                temp_page.set_rotation(0)
                temp_page.set_cropbox(temp_page.mediabox)
                PDFDocumentManager.apply_text_only_redactions_to_page(
                    temp_page, redact_rects, keep_image_rects
                )
                if temp_page.rotation != saved_rot:
                    temp_page.set_rotation(saved_rot)
                if saved_crop != temp_page.mediabox:
                    temp_page.set_cropbox(saved_crop)
                source_page = temp_page
            try:
                pix = source_page.get_pixmap(
                    matrix=fitz.Matrix(scale, scale),
                    colorspace=fitz.csRGB,
                    alpha=False,
                )
                qimg = QImage(
                    pix.samples,
                    pix.width,
                    pix.height,
                    pix.stride,
                    QImage.Format.Format_RGB888,
                ).copy()
            finally:
                if temp_doc is not None:
                    temp_doc.close()
        return QPixmap.fromImage(qimg)

    def _on_scroll_bar_changed(self):
        if not self.scroll_mode:
            return
        self._render_visible_scroll_pages()
        self._update_scroll_current_page()

    def _scroll_page_at_scene_pos(self, pos: QPointF):
        """シーン座標がどのページの上か（隙間の場合は一番近いページ）を返す。"""
        if not self._scroll_page_rects:
            return None
        if pos.y() < 0:
            return 0
        last = len(self.current_pdf) - 1 if self.current_pdf else 0
        for i in sorted(self._scroll_page_rects):
            rect = self._scroll_page_rects[i]
            if rect.contains(pos):
                return i
            if pos.y() < rect.bottom():
                return i
        return last

    def _update_scroll_current_page(self):
        """画面の中央にあるページを現在のページにする。"""
        if self._scroll_updating_page or not self.scroll_mode:
            return
        center = self.mapToScene(self.viewport().rect().center())
        idx = self._scroll_page_at_scene_pos(center)
        if idx is not None and idx != self.current_page_idx:
            self._scroll_updating_page = True
            try:
                self.current_page_idx = idx
                self.pageSelectionChanged.emit(idx)
                self.statusUpdated.emit()
            finally:
                self._scroll_updating_page = False

    def _scroll_to_page(self, page_idx: int):
        """指定ページの上端が画面の上に来るようスクロールする。"""
        rect = self._scroll_page_rects.get(page_idx)
        if rect is None:
            return False
        self._render_visible_scroll_pages()
        self.verticalScrollBar().setValue(int(round(rect.top())))
        self._update_scroll_current_page()
        return True

    def rotate_current_page(self, delta_angle):
        if not self.current_pdf or not (0 <= self.current_page_idx < len(self.current_pdf)):
            self._show_status_message("回転するPDFが開かれていません。先にPDFを開いてください。")
            return

        self.cancel_active_interaction()
        # 回転すると表示座標系が変わるため折りたたみ帯は解除する。
        self._fold_bands.pop(self.current_page_idx, None)
        with FITZ_LOCK:
            page = self.current_pdf[self.current_page_idx]
            old_rot = page.rotation
        new_rot = (old_rot + delta_angle) % 360
        if old_rot == new_rot:
            return

        cmd = RotatePageCommand(self.current_page_idx, old_rot, new_rot)
        cmd.redo(self)
        self.history.push(cmd)

    def _clamp_to_scene(self, pos):
        rect = self.scene.sceneRect()
        if rect.isEmpty():
            return pos
        x = max(rect.left(), min(pos.x(), rect.right()))
        y = max(rect.top(), min(pos.y(), rect.bottom()))
        return QPointF(x, y)

    def scene_to_pdf(self, pos):
        if (
            not self.current_pdf
            or self.page_rect is None
            or self.pixmap_width == 0
            or self.pixmap_height == 0
        ):
            return fitz.Point(0, 0)

        sx = self._fold_display_width() / self.pixmap_width
        sy = self._fold_display_height() / self.pixmap_height

        vx = self.page_rect.x() + pos.x() * sx
        vy = self.page_rect.y() + pos.y() * sy

        # 折りたたみ中は、詰めた後の表示座標を元の表示座標に戻す。
        band = self._fold_band_for_page()
        if band is not None:
            if band[0] == FOLD_VERTICAL:
                vx = folded_x_to_view_x(vx, (band[1], band[2]))
            else:
                vy = folded_y_to_view_y(vy, (band[1], band[2]))

        return view_point_to_raw_pdf_from_geom(
            self._page_crop_rect,
            self._page_fitz_rect,
            self._page_rotation,
            fitz.Point(vx, vy),
        )

    def pdf_to_scene(self, point):
        if (
            not self.current_pdf
            or self.page_rect is None
            or self.page_rect.width() == 0
            or self.page_rect.height() == 0
        ):
            return (0.0, 0.0)

        view_pt = raw_pdf_to_view_point_from_geom(
            self._page_crop_rect,
            self._page_fitz_rect,
            self._page_rotation,
            point,
        )

        # 折りたたみ中は、帯の先の表示座標を帯の分だけ詰める。
        band = self._fold_band_for_page()
        vx = view_pt.x
        vy = view_pt.y
        if band is not None:
            if band[0] == FOLD_VERTICAL:
                vx = view_x_to_folded_x(vx, (band[1], band[2]))
            else:
                vy = view_y_to_folded_y(vy, (band[1], band[2]))

        sx = self.pixmap_width / self._fold_display_width()
        sy = self.pixmap_height / self._fold_display_height()

        return (
            (vx - self.page_rect.x()) * sx,
            (vy - self.page_rect.y()) * sy,
        )

    def _compute_text_pdf_positions(self, scene_pos, fontsize, ann_rot=None):
        if not self.current_pdf or self.page_rect is None or self.page_rect.width() == 0:
            return (0.0, 0.0), (0.0, 0.0)

        cur_page_rot = self._page_rotation
        if ann_rot is None:
            ann_rot = cur_page_rot
        ann_rot = ann_rot % 360

        scale_factor = self._display_scale()
        dy = fontsize * scale_factor * 0.88
        rel_angle_rad = math.radians((cur_page_rot - ann_rot) % 360)

        baseline_scene_pos = QPointF(
            scene_pos.x() - dy * math.sin(rel_angle_rad),
            scene_pos.y() + dy * math.cos(rel_angle_rad),
        )

        pdf_pos = self.scene_to_pdf(scene_pos)
        pdf_baseline_pos = self.scene_to_pdf(baseline_scene_pos)
        return (pdf_pos.x, pdf_pos.y), (pdf_baseline_pos.x, pdf_baseline_pos.y)

    def _get_page_pdf_text_tokens(
        self, page_idx: Optional[int] = None, doc: Optional[fitz.Document] = None
    ) -> List[Dict[str, Any]]:
        target_doc = self.current_pdf if doc is None else doc
        target_idx = self.current_page_idx if page_idx is None else int(page_idx)
        if not target_doc or not (0 <= target_idx < len(target_doc)):
            return []

        with FITZ_LOCK:
            page = target_doc[target_idx]
            key = (
                id(target_doc),
                target_idx,
                page.xref,
                page.rotation % 360,
                tuple(page.cropbox),
            )
            if key in self._pdf_tokens_cache:
                return self._pdf_tokens_cache[key]

            tokens: List[Dict[str, Any]] = []
            try:
                raw_dict = page.get_text("rawdict")
            except Exception:
                raw_dict = {}

            try:
                word_tuples = page.get_text("words") or []
            except Exception:
                word_tuples = []

            word_boxes: List[Tuple[fitz.Rect, int]] = [
                (fitz.Rect(w[0], w[1], w[2], w[3]), idx_w)
                for idx_w, w in enumerate(word_tuples)
                if len(w) >= 5
            ]

            def find_word_id(char_rect: fitz.Rect) -> Optional[int]:
                pt = fitz.Point((char_rect.x0 + char_rect.x1) * 0.5, (char_rect.y0 + char_rect.y1) * 0.5)
                for w_rect, w_id in word_boxes:
                    if w_rect.contains(pt):
                        return w_id
                return None

            for block in raw_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    line_dir = line.get("dir", (1.0, 0.0))
                    dir_len = math.hypot(line_dir[0], line_dir[1])
                    dx, dy = (line_dir[0] / dir_len, line_dir[1] / dir_len) if dir_len > 1e-4 else (1.0, 0.0)

                    line_chars: List[Tuple[Dict[str, Any], float, RGBTuple, int]] = []
                    for span_idx, span in enumerate(line.get("spans", [])):
                        span_size = float(span.get("size", TEXT_FONT_SIZE))
                        span_color = srgb_int_to_rgb_tuple(span.get("color", 0))
                        for ch in span.get("chars", []):
                            line_chars.append((ch, span_size, span_color, span_idx))

                    if not line_chars:
                        continue

                    chunks = []
                    cur_chunk = []
                    prev_proj_max = None
                    prev_origin_perp = None
                    prev_size = None
                    prev_color = None
                    prev_word_id = None
                    prev_span_idx = None
                    prev_char_str = ""

                    for ch, ch_size, ch_color, span_idx in line_chars:
                        c_str = ch.get("c", "")
                        ch_bbox = fitz.Rect(ch.get("bbox", (0, 0, 0, 0)))
                        if not c_str or c_str.isspace():
                            if cur_chunk:
                                chunks.append(cur_chunk)
                                cur_chunk = []
                            prev_proj_max = None
                            prev_origin_perp = None
                            prev_word_id = None
                            prev_span_idx = None
                            prev_char_str = ""
                            continue

                        is_combining = (
                            unicodedata.category(c_str[0]) in ("Mn", "Mc", "Me")
                            or c_str in ("\u3099", "\u309a", "\u309b", "\u309c", "゛", "゜")
                        )

                        corners = [
                            (ch_bbox.x0, ch_bbox.y0),
                            (ch_bbox.x1, ch_bbox.y0),
                            (ch_bbox.x0, ch_bbox.y1),
                            (ch_bbox.x1, ch_bbox.y1),
                        ]
                        projs = [px * dx + py * dy for px, py in corners]
                        perps = [-px * dy + py * dx for px, py in corners]
                        cur_proj_min = min(projs)
                        cur_proj_max = max(projs)
                        char_perp_h = max(perps) - min(perps)

                        origin = ch.get("origin")
                        cur_origin_perp = (
                            (-float(origin[0]) * dy + float(origin[1]) * dx)
                            if origin
                            else max(perps)
                        )
                        eff_size = max(ch_size, char_perp_h * 0.85) if ch_size < MIN_FONT_SIZE else ch_size
                        cur_word_id = find_word_id(ch_bbox)

                        if prev_proj_max is not None and not is_combining:
                            gap_along = cur_proj_min - prev_proj_max
                            gap_perp = abs(cur_origin_perp - prev_origin_perp) if prev_origin_perp is not None else 0.0
                            color_diff = ch_color != prev_color
                            size_diff = abs(eff_size - prev_size) > 2.0 if prev_size is not None else False

                            same_word_box = (
                                cur_word_id is not None
                                and prev_word_id is not None
                                and cur_word_id == prev_word_id
                            )
                            is_numeric_chain = (
                                (prev_char_str in "0123456789,.$¥+-▲△(" or c_str in "0123456789,.%円)")
                                and (prev_char_str.isdigit() or c_str.isdigit())
                            )
                            if same_word_box or is_numeric_chain:
                                max_gap_ratio = 0.78
                            elif span_idx == prev_span_idx:
                                max_gap_ratio = 0.55
                            else:
                                max_gap_ratio = 0.42

                            if (
                                gap_along > eff_size * max_gap_ratio
                                or gap_along < -eff_size * 0.65
                                or gap_perp > eff_size * 0.50
                                or color_diff
                                or size_diff
                            ):
                                if cur_chunk:
                                    chunks.append(cur_chunk)
                                    cur_chunk = []

                        cur_chunk.append((ch, eff_size, ch_color))
                        prev_proj_max = (
                            max(prev_proj_max, cur_proj_max)
                            if (is_combining and prev_proj_max is not None)
                            else cur_proj_max
                        )
                        if not is_combining:
                            prev_origin_perp = cur_origin_perp
                            prev_size = eff_size
                            prev_color = ch_color
                            prev_word_id = cur_word_id
                            prev_span_idx = span_idx
                            prev_char_str = c_str

                    if cur_chunk:
                        chunks.append(cur_chunk)

                    # 注釈の rotation はページ回転を含めない
                    # （core.geometry.text_annotation_rotation 参照）
                    token_rot = text_annotation_rotation(dx, dy)

                    for chunk in chunks:
                        raw_text_str = "".join(item[0].get("c", "") for item in chunk).strip()
                        text_str = unicodedata.normalize("NFC", raw_text_str)
                        if not text_str:
                            continue

                        fontsize = max(item[1] for item in chunk)
                        color = chunk[0][2]

                        union_rect = fitz.Rect(chunk[0][0]["bbox"])
                        for item in chunk[1:]:
                            union_rect |= fitz.Rect(item[0]["bbox"])

                        # rawdict の bbox / origin は「未回転・cropbox基準」の
                        # 座標系で返る（表示座標ではない）。raw 座標へは
                        # cropbox の原点を足すだけでよい。表示座標として
                        # view→raw 変換をかけると、回転ページで二重に
                        # 回転して位置がずれてしまう。
                        _crop = get_effective_cropbox(page)
                        _ox, _oy = _crop.x0, _crop.y0

                        first_origin = chunk[0][0].get("origin")
                        if first_origin:
                            base_pt = fitz.Point(first_origin[0], first_origin[1])
                        else:
                            first_bbox = fitz.Rect(chunk[0][0]["bbox"])
                            base_pt = fitz.Point(
                                first_bbox.x0 - dy * fontsize * 0.88,
                                first_bbox.y0 + dx * fontsize * 0.88,
                            )

                        top_pt = fitz.Point(
                            base_pt.x + dy * fontsize * 0.88,
                            base_pt.y - dx * fontsize * 0.88,
                        )

                        raw_rect = fitz.Rect(
                            union_rect.x0 + _ox,
                            union_rect.y0 + _oy,
                            union_rect.x1 + _ox,
                            union_rect.y1 + _oy,
                        )
                        raw_base = fitz.Point(base_pt.x + _ox, base_pt.y + _oy)
                        raw_top = fitz.Point(top_pt.x + _ox, top_pt.y + _oy)

                        tokens.append({
                            "text": text_str,
                            "fontsize": round(max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, fontsize)), 1),
                            "color": color,
                            "raw_rect": (raw_rect.x0, raw_rect.y0, raw_rect.x1, raw_rect.y1),
                            "pos": (raw_top.x, raw_top.y),
                            "baseline_pos": (raw_base.x, raw_base.y),
                            "rotation": token_rot,
                        })

            # 置換で入れたテキスト注釈はライブ文書の文字レイヤーに存在しない
            # ため、このままではテキスト編集コマンドで二度と選べなくなる。
            # 注釈由来のトークンを末尾に足して公開する（元の断片トークンは
            # 各利用側の消去済みチェックで除外されるので二重にはならない）。
            # 別文書が明示された場合（見開きリーダーの別文書など）は、
            # その文書に対応する注釈が無いので足さない。
            if doc is None:
                ann_tokens = self._annotation_text_tokens(target_idx)
                # 注釈の推定BBoxと重なる元トークンは、注釈側を優先するため
                # 除外する（中心が消去矩形の外にある元断片が、置換テキストの
                # 場所で先にヒットしてしまうのを防ぐ）。完全に含まれる・
                # 中心が入る・面積の半分以上が重なる場合に対象とする。
                if ann_tokens:
                    ann_boxes = [fitz.Rect(t["raw_rect"]) for t in ann_tokens]

                    def _covered_by_ann(tok) -> bool:
                        tr = fitz.Rect(tok["raw_rect"])
                        tr.normalize()
                        center = fitz.Point(
                            (tr.x0 + tr.x1) * 0.5, (tr.y0 + tr.y1) * 0.5
                        )
                        for ab in ann_boxes:
                            if ab.contains(center):
                                return True
                            inter = tr & ab
                            area = tr.width * tr.height
                            if area > 0 and (
                                inter.width * inter.height
                            ) >= area * 0.5:
                                return True
                        return False

                    tokens = [t for t in tokens if not _covered_by_ann(t)]
                tokens.extend(ann_tokens)

            self._cached_tokens_key = key
            self._cached_pdf_tokens = tokens
            if len(self._pdf_tokens_cache) >= 16:
                oldest_key = next(iter(self._pdf_tokens_cache))
                self._pdf_tokens_cache.pop(oldest_key, None)
            self._pdf_tokens_cache[key] = tokens
            return tokens

    def _annotation_text_tokens(self, page_idx: int) -> List[Dict[str, Any]]:
        """指定ページのテキスト注釈を、PDF内文字トークンと同じ形式で返す。

        「PDFに反映」やトークン編集で入れた置換テキストは、ライブ文書の
        文字レイヤーではなく注釈として存在するため、get_text からは拾えない。
        テキスト編集コマンドで再び選べるよう、注釈からトークンを合成する。
        各トークンには ann_idx（注釈のインデックス）を含め、呼び出し側が
        新しい置換を重ねず、注釈そのものを編集・削除できるようにする。
        後からユーザー消去で隠された注釈は、find_text_annotation_at_point と
        同じ規則で対象外にする。
        """
        ann_list = self.page_annotations.get(page_idx) or []
        tokens: List[Dict[str, Any]] = []
        for ann_idx, ann in enumerate(ann_list):
            if ann.get("type") != "text":
                continue
            if not str(ann.get("text", "")).strip():
                continue
            bbox = estimate_text_bbox_in_pdf(ann)
            if bbox.is_empty:
                continue
            later_erases = get_later_user_erase_rects(ann_list, ann_idx, ann)
            if is_rect_substantially_erased(bbox, later_erases):
                continue
            tokens.append({
                "text": str(ann.get("text", "")),
                "fontsize": float(ann.get("fontsize", TEXT_FONT_SIZE)),
                "color": tuple(ann.get("color", (0.0, 0.0, 0.0))),
                "raw_rect": (bbox.x0, bbox.y0, bbox.x1, bbox.y1),
                "pos": tuple(ann.get("pos", (bbox.x0, bbox.y0))),
                "baseline_pos": tuple(
                    ann.get("baseline_pos", (bbox.x0, bbox.y1))
                ),
                "rotation": int(ann.get("rotation", 0)) % 360,
                "ann_idx": ann_idx,
            })
        return tokens

    def _find_pdf_text_token_at_scene_pos(self, scene_pos):
        if not self.current_pdf or scene_pos is None:
            return None
        raw_pt = self.scene_to_pdf(scene_pos)
        return find_pdf_token_at_point(
            self._get_page_pdf_text_tokens(),
            self.current_annotations,
            raw_pt,
            padding=1.5,
        )

    def _is_highlight_annotation_visible(self, ann_list, idx, ann):
        return is_highlight_annotation_visible(ann_list, idx, ann)

    def _find_highlight_at_scene_pos(self, scene_pos):
        if not self.current_pdf or scene_pos is None:
            return None, None
        raw_pt = self.scene_to_pdf(scene_pos)
        return find_highlight_annotation_at_point(self.current_annotations, raw_pt, padding=1.5)

    def _find_highlights_in_scene_rect(self, scene_rect):
        if not self.current_pdf or scene_rect is None or scene_rect.isEmpty():
            return []
        p0 = self.scene_to_pdf(scene_rect.topLeft())
        p1 = self.scene_to_pdf(scene_rect.bottomRight())
        return find_highlights_in_rect(
            self.current_annotations, fitz.Rect(p0.x, p0.y, p1.x, p1.y)
        )

    def remove_highlight_annotation_at(self, ann_idx):
        ann_list = self.current_annotations
        if not (0 <= ann_idx < len(ann_list)) or ann_list[ann_idx].get("type") != "highlight":
            return False

        before_annotations = copy.deepcopy(ann_list)
        removed = ann_list.pop(ann_idx)
        adjust_updated_after_on_remove(ann_list, ann_idx)
        self.history.push(RemoveAnnotationCommand(self.current_page_idx, ann_idx, removed, before_annotations=before_annotations))
        self.render_page(fit_view=False, rebuild_base=False)
        self.notify_pages_updated([self.current_page_idx])

        win = self.window()
        if isinstance(win, QMainWindow):
            win.statusBar().showMessage("🖍✕ マーカーを1件消去しました（下の文字や罫線はそのまま保持されています）")
        return True

    def remove_highlight_annotations_in_rect(self, scene_rect):
        matched = self._find_highlights_in_scene_rect(scene_rect)
        if not matched:
            return 0

        if len(matched) == 1:
            idx, _ = matched[0]
            self.remove_highlight_annotation_at(idx)
            return 1

        ann_list = self.current_annotations
        before_annotations = copy.deepcopy(ann_list)
        for idx, _ in reversed(matched):
            if 0 <= idx < len(ann_list):
                ann_list.pop(idx)
                adjust_updated_after_on_remove(ann_list, idx)

        self.history.push(RemoveMultipleAnnotationsCommand(self.current_page_idx, matched, before_annotations=before_annotations))
        self.render_page(fit_view=False, rebuild_base=False)
        self.notify_pages_updated([self.current_page_idx])

        win = self.window()
        if isinstance(win, QMainWindow):
            win.statusBar().showMessage(
                f"🖍✕ 選択範囲内のマーカーを {len(matched)} 件消去しました（下の文字や罫線はそのまま保持されています）"
            )
        return len(matched)

    def _get_visible_draw_segments(self, ann_list, idx, ann):
        return get_visible_draw_segments(ann_list, idx, ann)

    def _find_draw_at_scene_pos(self, scene_pos):
        if not self.current_pdf or scene_pos is None:
            return None, None, None
        raw_pt = self.scene_to_pdf(scene_pos)
        return find_draw_annotation_at_point(
            self.current_annotations, raw_pt, default_width=DRAW_WIDTH
        )

    def _find_draws_along_pdf_stroke(self, eraser_pts):
        if not self.current_pdf or not eraser_pts:
            return []
        return find_draws_along_eraser_stroke(
            self.current_annotations, eraser_pts, default_width=DRAW_WIDTH
        )

    def remove_draw_annotation_at(self, ann_idx):
        ann_list = self.current_annotations
        if not (0 <= ann_idx < len(ann_list)) or ann_list[ann_idx].get("type") != "draw":
            return False
        before_annotations = copy.deepcopy(ann_list)
        removed = ann_list.pop(ann_idx)
        adjust_updated_after_on_remove(ann_list, ann_idx)
        self.history.push(RemoveAnnotationCommand(self.current_page_idx, ann_idx, removed, before_annotations=before_annotations))
        self.render_page(fit_view=False, rebuild_base=False)
        self.notify_pages_updated([self.current_page_idx])
        win = self.window()
        if isinstance(win, QMainWindow):
            win.statusBar().showMessage("🧽 ペンで描いた線を1件消去しました（下の文字や罫線はそのまま保持されています）")
        return True

    def remove_draw_annotations_along_stroke(self, eraser_pts):
        matched = self._find_draws_along_pdf_stroke(eraser_pts)
        if not matched:
            return 0
        if len(matched) == 1:
            idx, _ = matched[0]
            self.remove_draw_annotation_at(idx)
            return 1
        ann_list = self.current_annotations
        before_annotations = copy.deepcopy(ann_list)
        for idx, _ in reversed(matched):
            if 0 <= idx < len(ann_list):
                ann_list.pop(idx)
                adjust_updated_after_on_remove(ann_list, idx)
        self.history.push(RemoveMultipleAnnotationsCommand(self.current_page_idx, matched, before_annotations=before_annotations))
        self.render_page(fit_view=False, rebuild_base=False)
        self.notify_pages_updated([self.current_page_idx])
        win = self.window()
        if isinstance(win, QMainWindow):
            win.statusBar().showMessage(
                f"🧽 ペンで描いた線を {len(matched)} 件消去しました（下の文字や罫線はそのまま保持されています）"
            )
        return len(matched)

    def _reset_text_drag_state(self):
        self._dragging_text_ann_idx = None
        self._dragging_text_item = None
        self._text_drag_moved = False

    def _clear_interactive_guides(self):
        if self.text_guide_group is not None:
            if self.text_guide_group.scene() is self.scene:
                self.scene.removeItem(self.text_guide_group)
            self.text_guide_group = None

        if self.hover_highlight_item is not None:
            if self.hover_highlight_item.scene() is self.scene:
                self.scene.removeItem(self.hover_highlight_item)
            self.hover_highlight_item = None

    def _find_text_item_at_scene_pos(self, scene_pos):
        if scene_pos is None:
            return None, None

        # 1. シーン標準のヒット判定
        for item in self.scene.items(scene_pos):
            if isinstance(item, QGraphicsTextItem):
                ann_idx = item.data(Qt.ItemDataRole.UserRole)
                if ann_idx is not None:
                    return item, int(ann_idx)

        # 2. シーン内の全テキストアイテムに対する直接座標変換判定（空QTextDocument問題を回避）
        best_item = None
        best_idx = None
        for item in self.scene.items():
            if isinstance(item, QGraphicsTextItem):
                ann_idx = item.data(Qt.ItemDataRole.UserRole)
                if ann_idx is not None:
                    local_pt = item.mapFromScene(scene_pos)
                    if item.boundingRect().adjusted(-6.0, -6.0, 6.0, 6.0).contains(local_pt):
                        if best_idx is None or int(ann_idx) > best_idx:
                            best_item = item
                            best_idx = int(ann_idx)
        if best_idx is not None:
            return best_item, best_idx

        # 3. PDF座標の推定バウンディングボックスによるフォールバック判定
        if self.current_pdf:
            raw_pt = self.scene_to_pdf(scene_pos)
            idx, _ = find_text_annotation_at_point(
                self.current_annotations, raw_pt, estimate_text_bbox_in_pdf, min_pad=3.5
            )
            if idx is not None:
                for item in self.scene.items():
                    if isinstance(item, QGraphicsTextItem) and item.data(Qt.ItemDataRole.UserRole) == idx:
                        return item, idx
                return None, idx

        return None, None

    def remove_texts_in_scene_rect(self, scene_rect) -> int:
        if not self.current_pdf or scene_rect is None or scene_rect.isEmpty():
            return 0

        p0 = self.scene_to_pdf(scene_rect.topLeft())
        p1 = self.scene_to_pdf(scene_rect.bottomRight())
        target_raw_rect = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
        target_raw_rect.normalize()

        ann_list = self.current_annotations
        matched_ann_indices = set()

        for item in self.scene.items(scene_rect, Qt.ItemSelectionMode.IntersectsItemBoundingRect):
            if isinstance(item, QGraphicsTextItem):
                ann_idx = item.data(Qt.ItemDataRole.UserRole)
                if ann_idx is not None and 0 <= int(ann_idx) < len(ann_list):
                    if ann_list[int(ann_idx)].get("type") == "text":
                        matched_ann_indices.add(int(ann_idx))

        ann_hits, matched_tokens = find_text_targets_in_rect(
            ann_list,
            self._get_page_pdf_text_tokens(),
            target_raw_rect,
            estimate_text_bbox_in_pdf,
        )
        matched_ann_indices.update(ann_hits)

        if not matched_ann_indices and not matched_tokens:
            return 0

        old_ann_list = copy.deepcopy(ann_list)
        new_ann_list = copy.deepcopy(ann_list)
        total_count = 0
        rebuild_base = False

        for idx in sorted(matched_ann_indices, reverse=True):
            action, _, redact_ann = plan_text_annotation_removal(new_ann_list, idx)
            if action == "replace_with_redact" and redact_ann is not None:
                new_ann_list[idx] = redact_ann
                rebuild_base = True
                total_count += 1
            elif action == "pop":
                new_ann_list.pop(idx)
                adjust_updated_after_on_remove(new_ann_list, idx)
                total_count += 1

        for tok in matched_tokens:
            new_ann_list.append({
                "type": "text_redact",
                "rect": tok["raw_rect"],
            })
            rebuild_base = True
            total_count += 1

        if total_count > 0:
            self.page_annotations[self.current_page_idx] = new_ann_list
            self.history.push(
                ReplacePageAnnotationsCommand(
                    self.current_page_idx, old_ann_list, new_ann_list
                )
            )
            self.render_page(fit_view=False, rebuild_base=rebuild_base)
            self.notify_pages_updated([self.current_page_idx])
            win = self.window()
            if isinstance(win, QMainWindow):
                win.statusBar().showMessage(f"🧹 選択範囲内のテキストを {total_count} 件消去しました")
        return total_count

    def _update_drag_text_guide(self, text_item, ann):
        self._clear_interactive_guides()
        if text_item is None or self.page_rect is None or self.page_rect.width() == 0:
            return

        scale_factor = self._display_scale()
        fontsize = float(ann.get("fontsize", self.current_font_size))

        poly = text_item.mapToScene(text_item.boundingRect())
        path = QPainterPath()
        path.addPolygon(poly)
        path.closeSubpath()

        highlight = QGraphicsPathItem(path)
        highlight.setPen(QPen(QColor(0, 110, 230, 230), 2.0, Qt.PenStyle.DashLine))
        highlight.setBrush(QBrush(QColor(0, 140, 255, 30)))
        highlight.setZValue(1000)
        self.scene.addItem(highlight)
        self.hover_highlight_item = highlight

        group = QGraphicsItemGroup()
        group.setZValue(999)
        b_rect = text_item.boundingRect()
        lines = str(ann.get("text", "")).split("\n")
        for line_idx in range(max(1, len(lines))):
            line_dy = fontsize * scale_factor * (0.88 + line_idx * TEXT_LINE_SPACING)
            p_left = text_item.mapToScene(QPointF(b_rect.left() - 10, line_dy))
            p_right = text_item.mapToScene(QPointF(b_rect.right() + 10, line_dy))
            base_line = QGraphicsLineItem(p_left.x(), p_left.y(), p_right.x(), p_right.y())
            if line_idx == 0:
                base_line.setPen(QPen(QColor(220, 20, 20, 220), 2.0, Qt.PenStyle.SolidLine))
            else:
                base_line.setPen(QPen(QColor(220, 20, 20, 160), 1.5, Qt.PenStyle.DashLine))
            group.addToGroup(base_line)
        self.scene.addItem(group)
        self.text_guide_group = group

    def _update_hover_and_text_guide(self, scene_pos, ctrl_pressed=None):
        self._clear_interactive_guides()

        modifiers = QApplication.keyboardModifiers()
        if ctrl_pressed is None:
            ctrl_pressed = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift_pressed = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        if (
            not self.current_pdf
            or self.page_rect is None
            or self.page_rect.width() == 0
            or scene_pos is None
            or not self.scene.sceneRect().contains(scene_pos)
        ):
            return

        if ctrl_pressed:
            hit_item, ann_idx = self._find_text_item_at_scene_pos(scene_pos)
            if hit_item is not None and ann_idx is not None:
                poly = hit_item.mapToScene(hit_item.boundingRect())
                path = QPainterPath()
                path.addPolygon(poly)
                path.closeSubpath()
                highlight = QGraphicsPathItem(path)
                highlight.setPen(QPen(QColor(230, 105, 0, 240), 2.2, Qt.PenStyle.DashLine))
                highlight.setBrush(QBrush(QColor(255, 165, 0, 45)))
                highlight.setZValue(1000)
                self.scene.addItem(highlight)
                self.hover_highlight_item = highlight
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                return

            pdf_token = self._find_pdf_text_token_at_scene_pos(scene_pos)
            if pdf_token is not None:
                r = fitz.Rect(pdf_token["raw_rect"])
                x0, y0 = self.pdf_to_scene(fitz.Point(r.x0, r.y0))
                x1, y1 = self.pdf_to_scene(fitz.Point(r.x1, r.y1))
                s_rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized().adjusted(-3, -2, 3, 2)
                path = QPainterPath()
                path.addRect(s_rect)
                highlight = QGraphicsPathItem(path)
                highlight.setPen(QPen(QColor(230, 105, 0, 240), 2.2, Qt.PenStyle.DashLine))
                highlight.setBrush(QBrush(QColor(255, 165, 0, 45)))
                highlight.setZValue(1000)
                self.scene.addItem(highlight)
                self.hover_highlight_item = highlight
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                return

        if self.mode not in ("type", "text_erase", "draw", "highlight_erase", "highlight"):
            self._restore_mode_cursor()
            return

        if self.mode == "highlight_erase" or (self.mode == "highlight" and shift_pressed):
            hl_idx, hl_ann = self._find_highlight_at_scene_pos(scene_pos)
            if hl_idx is not None and hl_ann is not None:
                r = fitz.Rect(hl_ann["rect"])
                r.normalize()
                x0, y0 = self.pdf_to_scene(fitz.Point(r.x0, r.y0))
                x1, y1 = self.pdf_to_scene(fitz.Point(r.x1, r.y1))
                s_rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized().adjusted(-2, -2, 2, 2)

                path = QPainterPath()
                path.addRect(s_rect)
                highlight = QGraphicsPathItem(path)
                highlight.setPen(QPen(QColor(220, 20, 20, 240), 2.5, Qt.PenStyle.DashLine))
                highlight.setBrush(QBrush(QColor(255, 60, 60, 50)))
                highlight.setZValue(1000)
                self.scene.addItem(highlight)
                self.hover_highlight_item = highlight
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                return

            self._restore_mode_cursor()
            return

        if self.mode == "highlight":
            self._restore_mode_cursor()
            return

        if self.mode in ("type", "text_erase"):
            hit_item, ann_idx = self._find_text_item_at_scene_pos(scene_pos)
            if ann_idx is not None and 0 <= ann_idx < len(self.current_annotations):
                path = QPainterPath()
                if hit_item is not None:
                    poly = hit_item.mapToScene(hit_item.boundingRect())
                    path.addPolygon(poly)
                    path.closeSubpath()
                else:
                    bbox = estimate_text_bbox_in_pdf(self.current_annotations[ann_idx])
                    x0, y0 = self.pdf_to_scene(fitz.Point(bbox.x0, bbox.y0))
                    x1, y1 = self.pdf_to_scene(fitz.Point(bbox.x1, bbox.y1))
                    s_rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized().adjusted(-3, -2, 3, 2)
                    path.addRect(s_rect)

                highlight = QGraphicsPathItem(path)
                if self.mode == "text_erase":
                    pen = QPen(QColor(220, 20, 20, 230), 2.5, Qt.PenStyle.SolidLine)
                    brush = QBrush(QColor(255, 80, 80, 55))
                    self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                else:
                    pen = QPen(QColor(0, 110, 220, 230), 2.0, Qt.PenStyle.DashLine)
                    brush = QBrush(QColor(0, 140, 255, 35))
                    self.viewport().setCursor(Qt.CursorShape.SizeAllCursor)

                highlight.setPen(pen)
                highlight.setBrush(brush)
                highlight.setZValue(1000)
                self.scene.addItem(highlight)
                self.hover_highlight_item = highlight
                return

            pdf_token = self._find_pdf_text_token_at_scene_pos(scene_pos)
            if pdf_token is not None:
                r = fitz.Rect(pdf_token["raw_rect"])
                x0, y0 = self.pdf_to_scene(fitz.Point(r.x0, r.y0))
                x1, y1 = self.pdf_to_scene(fitz.Point(r.x1, r.y1))
                s_rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized().adjusted(-3, -2, 3, 2)

                path = QPainterPath()
                path.addRect(s_rect)

                highlight = QGraphicsPathItem(path)
                if self.mode == "text_erase":
                    pen = QPen(QColor(220, 20, 20, 230), 2.5, Qt.PenStyle.SolidLine)
                    brush = QBrush(QColor(255, 80, 80, 55))
                else:
                    pen = QPen(QColor(0, 165, 80, 235), 2.2, Qt.PenStyle.DashLine)
                    brush = QBrush(QColor(0, 200, 90, 38))

                highlight.setPen(pen)
                highlight.setBrush(brush)
                highlight.setZValue(1000)
                self.scene.addItem(highlight)
                self.hover_highlight_item = highlight
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                return

        self._restore_mode_cursor()

        scale_factor = self._display_scale()

        if self.mode == "draw":
            if self.pen_eraser_active or shift_pressed:
                draw_idx, draw_ann, draw_segs = self._find_draw_at_scene_pos(scene_pos)
                if draw_idx is not None and draw_segs:
                    path = QPainterPath()
                    for seg in draw_segs:
                        if not seg:
                            continue
                        x0, y0 = self.pdf_to_scene(fitz.Point(*seg[0]))
                        path.moveTo(x0, y0)
                        for px, py in seg[1:]:
                            x, y = self.pdf_to_scene(fitz.Point(px, py))
                            path.lineTo(x, y)
                    stroker = QPainterPathStroker()
                    w_px = max(6.0, float(draw_ann.get("width", DRAW_WIDTH)) * scale_factor + 6.0)
                    stroker.setWidth(w_px)
                    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
                    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    hl_path = stroker.createStroke(path)
                    highlight = QGraphicsPathItem(hl_path)
                    highlight.setPen(QPen(QColor(220, 20, 20, 240), 2.0, Qt.PenStyle.DashLine))
                    highlight.setBrush(QBrush(QColor(255, 70, 70, 55)))
                    highlight.setZValue(1000)
                    self.scene.addItem(highlight)
                    self.hover_highlight_item = highlight
                    self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                    return

            if self.pen_eraser_active:
                diameter = max(10.0, 10.0 * scale_factor)
                radius = diameter / 2.0
                group = QGraphicsItemGroup()
                group.setZValue(999)
                circle = QGraphicsEllipseItem(
                    scene_pos.x() - radius,
                    scene_pos.y() - radius,
                    diameter,
                    diameter,
                )
                circle.setPen(QPen(QColor(220, 20, 20, 230), 1.8, Qt.PenStyle.DashLine))
                circle.setBrush(QBrush(QColor(255, 80, 80, 45)))
                group.addToGroup(circle)
                self.scene.addItem(group)
                self.text_guide_group = group
                return

            diameter = max(3.0, self.current_draw_width * scale_factor)
            radius = diameter / 2.0
            group = QGraphicsItemGroup()
            group.setZValue(999)

            circle = QGraphicsEllipseItem(
                scene_pos.x() - radius,
                scene_pos.y() - radius,
                diameter,
                diameter,
            )
            circle.setPen(QPen(rgb_tuple_to_qcolor(self.current_color, 230), 1.5))
            circle.setBrush(QBrush(rgb_tuple_to_qcolor(self.current_color, 95)))
            group.addToGroup(circle)

            self.scene.addItem(group)
            self.text_guide_group = group
            return

        if self.mode == "type":
            fontsize = self.current_font_size
            font_px = max(1, int(round(fontsize * scale_factor)))
            baseline_offset = fontsize * scale_factor * 0.88
            guide_width = font_px * 6.5

            x = scene_pos.x()
            y_top = scene_pos.y()
            y_base = y_top + baseline_offset
            y_bottom = y_top + font_px * 1.05

            group = QGraphicsItemGroup()
            group.setZValue(999)

            box_item = QGraphicsRectItem(QRectF(x, y_top, guide_width, y_bottom - y_top))
            box_item.setPen(QPen(QColor(0, 120, 215, 130), 1.0, Qt.PenStyle.DotLine))
            box_item.setBrush(QBrush(QColor(0, 140, 255, 18)))
            group.addToGroup(box_item)

            base_line = QGraphicsLineItem(x - 6, y_base, x + guide_width + 10, y_base)
            base_line.setPen(QPen(QColor(0, 100, 220, 220), 2.0, Qt.PenStyle.SolidLine))
            group.addToGroup(base_line)

            cursor_line = QGraphicsLineItem(x, y_top - 3, x, y_bottom + 3)
            cursor_line.setPen(QPen(rgb_tuple_to_qcolor(self.current_color, 230), 2.2, Qt.PenStyle.SolidLine))
            group.addToGroup(cursor_line)

            ghost_text = PreciseTextAnnotationItem(
                f"あいうABC ({format_pt(fontsize)}pt)",
                fontsize * scale_factor,
                rgb_tuple_to_qcolor(self.current_color, 150),
                self.preview_font_family,
            )
            ghost_text.setPos(x, y_top)
            group.addToGroup(ghost_text)

            self.scene.addItem(group)
            self.text_guide_group = group

    def _prepare_clean_page_for_rendering(self):
        with FITZ_LOCK:
            self._prepare_clean_page_for_rendering_locked()

    def _prepare_clean_page_for_rendering_locked(self):
        page = self.current_pdf[self.current_page_idx]
        self._page_fitz_rect = fitz.Rect(page.rect)
        self._page_crop_rect = get_effective_cropbox(page)
        self._page_rotation = page.rotation % 360
        self.page_rect = QRectF(
            page.rect.x0,
            page.rect.y0,
            page.rect.width,
            page.rect.height,
        )
        self.pixmap_width = max(
            1, int(round(self._fold_display_width() * self.render_scale))
        )
        # 折りたたみ中は帯の高さ分だけ表示が短くなる。
        self.pixmap_height = max(
            1, int(round(self._fold_display_height() * self.render_scale))
        )

        self._close_clean_redacted_doc()
        text_redact_rects, keep_image_rects = split_text_only_redact_raw_rects(
            self.current_annotations
        )
        if text_redact_rects or keep_image_rects:
            temp_doc = fitz.open()
            temp_doc.insert_pdf(
                self.current_pdf,
                from_page=self.current_page_idx,
                to_page=self.current_page_idx,
            )
            temp_page = temp_doc[0]
            saved_rot = temp_page.rotation % 360
            saved_crop = get_effective_cropbox(temp_page)
            temp_page.set_rotation(0)
            temp_page.set_cropbox(temp_page.mediabox)

            PDFDocumentManager.apply_text_only_redactions_to_page(
                temp_page, text_redact_rects, keep_image_rects
            )

            if temp_page.rotation != saved_rot:
                temp_page.set_rotation(saved_rot)
            if saved_crop != temp_page.mediabox:
                temp_page.set_cropbox(saved_crop)

            self._clean_redacted_doc = temp_doc

        self._cached_page_idx = self.current_page_idx
        self._rendered_target_phys_width = 0
        self._rendered_target_phys_height = 0

    def _compute_target_physical_size(self):
        view_scale_x = max(0.05, abs(self.transform().m11()))
        view_scale_y = max(0.05, abs(self.transform().m22()))
        dpr = max(1.0, float(self.viewport().devicePixelRatioF()))

        target_phys_w = max(64, int(round(self.pixmap_width * view_scale_x * dpr)))
        target_phys_h = max(64, int(round(self.pixmap_height * view_scale_y * dpr)))
        return target_phys_w, target_phys_h

    def _render_optimal_pixmap_for_current_view(self):
        if (
            not self.current_pdf
            or self.page_rect is None
            or self.page_rect.width() <= 0
            or self.page_rect.height() <= 0
        ):
            return

        target_phys_w, target_phys_h = self._compute_target_physical_size()

        # 折りたたみ中は、表示の幅・高さ（帯を除いた大きさ）に対する倍率で
        # ページ全体を描画し、後で帯の行・列を切り取って詰める。
        display_w = self._fold_display_width()
        display_h = self._fold_display_height()
        exact_scale_x, exact_scale_y = clamp_render_scales(
            self.page_rect.width(),
            self.page_rect.height(),
            target_phys_w / display_w,
            target_phys_h / display_h,
        )

        with FITZ_LOCK:
            source_page = (
                self._clean_redacted_doc[0]
                if self._clean_redacted_doc is not None
                else self.current_pdf[self.current_page_idx]
            )
            pix = source_page.get_pixmap(
                matrix=fitz.Matrix(exact_scale_x, exact_scale_y),
                colorspace=fitz.csRGB,
                alpha=False,
            )

            qimg = QImage(
                pix.samples,
                pix.width,
                pix.height,
                pix.stride,
                QImage.Format.Format_RGB888,
            ).copy()

            band = self._fold_band_for_page()
            if band is not None and band[0] == FOLD_VERTICAL:
                left_col, right_col = fold_band_pixel_cols(
                    self.page_rect.x(), (band[1], band[2]), exact_scale_x
                )
                left_col = max(0, min(left_col, pix.width))
                right_col = max(left_col, min(right_col, pix.width))
                folded_w = pix.width - (right_col - left_col)
                if 0 < right_col - left_col < pix.width and folded_w > 0:
                    folded = QImage(
                        folded_w, pix.height, QImage.Format.Format_RGB888
                    )
                    painter = QPainter(folded)
                    # 帯より左の部分
                    painter.drawImage(0, 0, qimg, 0, 0, left_col, pix.height)
                    # 帯より右の部分を隙間なく左に詰める
                    painter.drawImage(
                        left_col,
                        0,
                        qimg,
                        right_col,
                        0,
                        pix.width - right_col,
                        pix.height,
                    )
                    painter.end()
                    qimg = folded
            elif band is not None:
                top_row, bottom_row = fold_band_pixel_rows(
                    self.page_rect.y(), (band[1], band[2]), exact_scale_y
                )
                top_row = max(0, min(top_row, pix.height))
                bottom_row = max(top_row, min(bottom_row, pix.height))
                folded_h = pix.height - (bottom_row - top_row)
                if 0 < bottom_row - top_row < pix.height and folded_h > 0:
                    folded = QImage(
                        pix.width, folded_h, QImage.Format.Format_RGB888
                    )
                    painter = QPainter(folded)
                    # 帯より上の部分
                    painter.drawImage(0, 0, qimg, 0, 0, pix.width, top_row)
                    # 帯より下の部分を隙間なく上に詰める
                    painter.drawImage(
                        0,
                        top_row,
                        qimg,
                        0,
                        bottom_row,
                        pix.width,
                        pix.height - bottom_row,
                    )
                    painter.end()
                    qimg = folded

        self._cached_base_pixmap = QPixmap.fromImage(qimg)
        self._rendered_target_phys_width = target_phys_w
        self._rendered_target_phys_height = target_phys_h

        if self._base_pixmap_item is not None and self._base_pixmap_item.scene() is self.scene:
            self._base_pixmap_item.set_page_pixmap(
                self._cached_base_pixmap,
                self.pixmap_width,
                self.pixmap_height,
            )
            self.viewport().update()

    def _show_status_message(self, message):
        win = self.window()
        if isinstance(win, QMainWindow):
            win.statusBar().showMessage(message)

    def _schedule_zoom_rerender(self):
        if self.current_pdf:
            self._zoom_render_timer.start()

    def _on_zoom_render_timeout(self):
        # 文書が閉じられた後にタイマーが発火することがある（テスト等）。
        # 閉じた文書に len() を呼ぶと ValueError になり、Qt スロットからの
        # 例外でプロセスが abort するため、is_closed で先に弾く。
        doc = self.current_pdf
        if doc is None or getattr(doc, "is_closed", False):
            return
        if self.scroll_mode:
            # 画面サイズが変わったら幅に合わせて並べ直す（45msの遅延つき）。
            self._render_scroll_view()
            return
        if self.page_rect is None or self.page_rect.width() <= 0:
            return
        current_phys_w, current_phys_h = self._compute_target_physical_size()

        if (
            abs(current_phys_w - self._rendered_target_phys_width) > 1
            or abs(current_phys_h - self._rendered_target_phys_height) > 1
        ):
            self._render_optimal_pixmap_for_current_view()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_zoom_rerender()

    def render_page(self, fit_view=False, rebuild_base=False):
        # 保留中のズーム再描画タイマーを止める（監査 #6）。
        # 止めないと fit_view 直後にタイマーが遅れて発火し、同じページを2回描く。
        if hasattr(self, "_zoom_render_timer"):
            self._zoom_render_timer.stop()

        # スクロール表示中は1ページ描画ではなくスクロール表示を作り直す。
        # ここは内容の変更（コマンドの実行・Undo/Redoなど）で呼ばれる入口なので、
        # 古い画像キャッシュを捨てて現在の内容で描き直す（墨消しのUndo/Redoが
        # 表示に反映されない事故を防ぐ）。
        if self.scroll_mode:
            self._scroll_pixmap_cache.clear()
            self._render_scroll_view()
            return
        if not self.current_pdf:
            return

        if self.current_page_idx >= len(self.current_pdf):
            return

        h_val = self.horizontalScrollBar().value()
        v_val = self.verticalScrollBar().value()

        self.drawing = False
        self._reset_text_drag_state()
        self.current_path = None
        self.current_path_item = None
        self.current_stroke = []
        self.drag_rect_item = None
        self._calc_dragging = False
        self._calc_drag_rect_item = None
        self.text_guide_group = None
        self.hover_highlight_item = None
        self.calc_highlight_group = None
        self.search_highlight_group = None
        self._hide_copy_popup()
        self._text_sel_words = []
        self._text_sel_text = ""
        self._text_sel_highlight_group = None
        self._base_pixmap_item = None
        if self.region_ocr_controller is not None:
            self.region_ocr_controller.on_scene_about_to_clear()
        self.scene.clear()

        need_prepare = (
            rebuild_base
            or self._cached_base_pixmap is None
            or self._cached_page_idx != self.current_page_idx
        )
        if need_prepare:
            self._prepare_clean_page_for_rendering()

        self.scene.setSceneRect(QRectF(0, 0, self.pixmap_width, self.pixmap_height))

        if fit_view:
            self.fitInView(
                self.scene.sceneRect(),
                Qt.AspectRatioMode.KeepAspectRatio,
            )

        target_w, target_h = self._compute_target_physical_size()
        if (
            need_prepare
            or fit_view
            or self._cached_base_pixmap is None
            or abs(target_w - self._rendered_target_phys_width) > 1
            or abs(target_h - self._rendered_target_phys_height) > 1
        ):
            self._render_optimal_pixmap_for_current_view()

        pixmap_item = CrispPagePixmapItem(
            self._cached_base_pixmap,
            self.pixmap_width,
            self.pixmap_height,
        )
        self.scene.addItem(pixmap_item)
        self._base_pixmap_item = pixmap_item

        self.draw_pending_annotations()
        self._sync_calc_selection_with_annotations()
        self._update_calc_highlight_items()
        self._update_search_highlight_items()

        if not fit_view:
            self.horizontalScrollBar().setValue(h_val)
            self.verticalScrollBar().setValue(v_val)

        if self._last_hover_scene_pos is not None:
            self._update_hover_and_text_guide(self._last_hover_scene_pos)

    def _build_erase_annotation_preview_path(self, ann, scale_factor):
        if ann["type"] == "box_erase":
            r = fitz.Rect(ann["rect"])
            r.normalize()
            x0, y0 = self.pdf_to_scene(fitz.Point(r.x0, r.y0))
            x1, y1 = self.pdf_to_scene(fitz.Point(r.x1, r.y1))
            path = QPainterPath()
            path.addRect(QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized())
            return path

        points = ann.get("points", [])
        if not points:
            return QPainterPath()

        width = ERASE_WIDTH * scale_factor
        half = width / 2.0
        scene_pts = [self.pdf_to_scene(fitz.Point(px, py)) for px, py in points]

        x0, y0 = scene_pts[0]
        max_dist = max((math.hypot(px - x0, py - y0) for px, py in scene_pts), default=0.0)
        if max_dist < 0.5:
            path = QPainterPath()
            path.addRect(QRectF(x0 - half, y0 - half, width, width))
            return path

        raw_path = QPainterPath()
        raw_path.moveTo(x0, y0)
        for px, py in scene_pts[1:]:
            raw_path.lineTo(px, py)

        stroker = QPainterPathStroker()
        stroker.setWidth(width)
        stroker.setCapStyle(Qt.PenCapStyle.SquareCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.BevelJoin)
        return stroker.createStroke(raw_path)

    def draw_pending_annotations(self):
        if self.page_rect is None or self.page_rect.width() == 0:
            return

        scale_factor = self._display_scale()
        cur_page_rot = self._page_rotation
        ann_list = self.current_annotations

        for idx, ann in enumerate(ann_list):
            if ann["type"] == "text_redact":
                continue

            later_erases = get_later_user_erase_rects(ann_list, idx, ann)

            if ann["type"] == "highlight":
                r = fitz.Rect(ann["rect"])
                r.normalize()
                if is_rect_substantially_erased(r, later_erases):
                    continue
                # 折りたたみ帯と重なる注釈は、折りたたみ中は表示しない。
                # データは残っており、解除すれば再び表示される。
                if self._view_rect_intersects_fold(r):
                    continue
                x0, y0 = self.pdf_to_scene(fitz.Point(r.x0, r.y0))
                x1, y1 = self.pdf_to_scene(fitz.Point(r.x1, r.y1))
                qrect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized()
                opacity = float(ann.get("opacity", HIGHLIGHT_OPACITY))
                alpha = max(20, min(200, int(round(opacity * 255))))
                hl_qcolor = rgb_tuple_to_qcolor(ann.get("color", (0.98, 0.90, 0.05)), alpha)

                item = QGraphicsRectItem(qrect)
                item.setPen(QPen(Qt.PenStyle.NoPen))
                item.setBrush(QBrush(hl_qcolor))
                self.scene.addItem(item)

            elif ann["type"] == "draw":
                segments = clip_stroke_by_rects(ann["points"], later_erases)
                points = ann.get("points", [])
                if points:
                    xs = [float(p[0]) for p in points]
                    ys = [float(p[1]) for p in points]
                    stroke_rect = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
                    if self._view_rect_intersects_fold(stroke_rect):
                        continue
                stroke_color = rgb_tuple_to_qcolor(ann.get("color", DEFAULT_COLOR))
                stroke_width = float(ann.get("width", DRAW_WIDTH)) * scale_factor
                pen = QPen(stroke_color, stroke_width)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

                for seg in segments:
                    if len(seg) < 2:
                        continue
                    path = QPainterPath()
                    x0, y0 = self.pdf_to_scene(fitz.Point(*seg[0]))
                    path.moveTo(x0, y0)
                    has_len = False
                    for px, py in seg[1:]:
                        x, y = self.pdf_to_scene(fitz.Point(px, py))
                        if math.hypot(x - x0, y - y0) >= 0.1:
                            has_len = True
                        path.lineTo(x, y)
                    if not has_len:
                        path.lineTo(x0 + 0.1, y0 + 0.1)

                    item = QGraphicsPathItem(path)
                    item.setPen(pen)
                    self.scene.addItem(item)

            elif ann["type"] in ("erase", "box_erase"):
                if ann["type"] == "box_erase":
                    if self._view_rect_intersects_fold(fitz.Rect(ann["rect"])):
                        continue
                else:
                    e_points = ann.get("points", [])
                    if e_points:
                        e_xs = [float(p[0]) for p in e_points]
                        e_ys = [float(p[1]) for p in e_points]
                        if self._view_rect_intersects_fold(
                            fitz.Rect(min(e_xs), min(e_ys), max(e_xs), max(e_ys))
                        ):
                            continue
                path = self._build_erase_annotation_preview_path(ann, scale_factor)
                if path.isEmpty():
                    continue

                item = QGraphicsPathItem(path)
                pen = QPen(QColor(220, 0, 0, 200), 1.5, Qt.PenStyle.DashLine)
                brush = QBrush(QColor(255, 255, 255, 240))
                item.setPen(pen)
                item.setBrush(brush)
                self.scene.addItem(item)

            elif ann["type"] == "text":
                # 不可視テキスト（OCRレイヤーの置換）は画面に字形を描かない。
                # 文字データとしてのみ存在し、編集は文字データ画面から行う。
                if ann.get("invisible"):
                    continue
                text_bbox = estimate_text_bbox_in_pdf(ann)
                if is_rect_substantially_erased(text_bbox, later_erases):
                    continue
                if self._view_point_in_fold(fitz.Point(*ann["pos"])):
                    continue

                x, y = self.pdf_to_scene(fitz.Point(*ann["pos"]))
                fontsize = float(ann.get("fontsize", TEXT_FONT_SIZE))
                text_color = rgb_tuple_to_qcolor(ann.get("color", (0.0, 0.0, 0.0)))
                ann_rot = ann.get("rotation", cur_page_rot)

                item = PreciseTextAnnotationItem(
                    ann["text"],
                    fontsize * scale_factor,
                    text_color,
                    self.preview_font_family,
                )
                item.setPos(x, y)
                item.setRotation((cur_page_rot - ann_rot) % 360)
                item.setData(Qt.ItemDataRole.UserRole, idx)
                self.scene.addItem(item)

    def remove_text_annotation_at(self, ann_idx):
        ann_list = self.current_annotations
        action, old_ann, new_ann = plan_text_annotation_removal(ann_list, ann_idx)
        if action == "noop" or old_ann is None:
            return False

        if action == "replace_with_redact" and new_ann is not None:
            ann_list[ann_idx] = new_ann
            self.history.push(
                EditAnnotationCommand(self.current_page_idx, ann_idx, old_ann, new_ann)
            )
            self.render_page(fit_view=False, rebuild_base=True)
        else:
            before_annotations = copy.deepcopy(ann_list)
            removed = ann_list.pop(ann_idx)
            adjust_updated_after_on_remove(ann_list, ann_idx)
            self.history.push(RemoveAnnotationCommand(self.current_page_idx, ann_idx, removed, before_annotations=before_annotations))
            self.render_page(fit_view=False, rebuild_base=False)

        self.notify_pages_updated([self.current_page_idx])
        win = self.window()
        if isinstance(win, QMainWindow):
            win.statusBar().showMessage("🧹 テキストを消去しました")
        return True

    def redact_existing_pdf_token(self, pdf_token):
        if not pdf_token:
            return False
        ann_idx = pdf_token.get("ann_idx")
        if ann_idx is not None:
            # 置換テキスト注釈由来のトークンの削除は、注釈の削除として扱う
            # （plan_text_annotation_removal 経由で消去の引き継ぎも行われる）。
            return self.remove_text_annotation_at(int(ann_idx))
        annot = {
            "type": "text_redact",
            "rect": pdf_token["raw_rect"],
        }
        self.current_annotations.append(annot)
        self.history.push(AddAnnotationCommand(self.current_page_idx, annot))
        self.render_page(fit_view=False, rebuild_base=True)
        self.notify_pages_updated([self.current_page_idx])
        return True

    def _token_is_scan_overlay(self, pdf_token):
        """PDF内文字トークンがスキャン画像の上に乗ったOCR文字か判定する。

        OCR文字の置換は、画像の上に字形を表示せず、文字データだけを
        差し替える（不可視テキストにする）ために使う。
        """
        if not pdf_token or not self.current_pdf:
            return False
        if not (0 <= self.current_page_idx < len(self.current_pdf)):
            return False
        page = self.current_pdf[self.current_page_idx]
        return spans_are_scan_overlay(page, [pdf_token["raw_rect"]])

    def quick_recolor_existing_pdf_token(self, pdf_token, new_color):
        if not pdf_token:
            return False
        ann_idx = pdf_token.get("ann_idx")
        if ann_idx is not None:
            # 置換テキスト注釈由来のトークンは、注釈そのものの色を変える
            # （新しい置換注釈を重ねて作らない）。
            ann_list = self.current_annotations
            if not (0 <= int(ann_idx) < len(ann_list)):
                return False
            old_ann = copy.deepcopy(ann_list[int(ann_idx)])
            if old_ann.get("type") != "text":
                return False
            if tuple(old_ann.get("color", ())) == tuple(new_color):
                return True
            new_ann = copy.deepcopy(old_ann)
            new_ann["color"] = tuple(new_color)
            new_ann["updated_after"] = len(ann_list)
            ann_list[int(ann_idx)] = new_ann
            self.history.push(
                EditAnnotationCommand(
                    self.current_page_idx, int(ann_idx), old_ann, new_ann
                )
            )
            self.render_page(fit_view=False, rebuild_base=False)
            self.notify_pages_updated([self.current_page_idx])
            return True
        annot = build_pdf_token_replacement_annotation(
            pdf_token,
            new_color=new_color,
            updated_after=len(self.current_annotations),
            invisible=self._token_is_scan_overlay(pdf_token),
        )
        self.current_annotations.append(annot)
        self.history.push(AddAnnotationCommand(self.current_page_idx, annot))
        self.render_page(fit_view=False, rebuild_base=True)
        self.notify_pages_updated([self.current_page_idx])
        return True

    def edit_existing_pdf_token(self, pdf_token):
        if not pdf_token or self.page_rect is None or self.page_rect.width() == 0:
            return

        # 置換テキスト注釈由来のトークンは、元PDF文字ではなく注釈そのものを
        # 編集する（新しい置換注釈を重ねて作らない）。
        ann_idx = pdf_token.get("ann_idx")
        if ann_idx is not None:
            self.edit_or_delete_text_annotation_at(int(ann_idx))
            return

        self._clear_interactive_guides()

        dlg = TextEditDeleteDialog(
            pdf_token["text"],
            pdf_token["fontsize"],
            pdf_token["color"],
            is_pdf_origin=True,
            parent=self.window(),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._refresh_hover_from_global_cursor()
            return

        if dlg.result_action == TextEditDeleteDialog.ACTION_DELETE:
            self.redact_existing_pdf_token(pdf_token)
            self._refresh_hover_from_global_cursor()
            return

        if dlg.result_action == TextEditDeleteDialog.ACTION_UPDATE:
            new_text = dlg.get_clean_text()
            new_fontsize = dlg.get_fontsize()
            new_color = dlg.get_color()
            if not new_text:
                self.redact_existing_pdf_token(pdf_token)
                self._refresh_hover_from_global_cursor()
                return

            self.set_font_size(new_fontsize)
            self.set_current_color(new_color)

            if abs(new_fontsize - float(pdf_token["fontsize"])) < 1e-4:
                new_pos = pdf_token["pos"]
                new_base_pos = pdf_token["baseline_pos"]
            else:
                bx, by = self.pdf_to_scene(fitz.Point(*pdf_token["baseline_pos"]))
                scale_factor = self._display_scale()
                dy = new_fontsize * scale_factor * 0.88
                cur_page_rot = self._page_rotation
                token_rot = int(pdf_token["rotation"]) % 360
                rel_angle_rad = math.radians((cur_page_rot - token_rot) % 360)
                top_scene_pos = QPointF(
                    bx + dy * math.sin(rel_angle_rad),
                    by - dy * math.cos(rel_angle_rad),
                )
                new_pos, _ = self._compute_text_pdf_positions(
                    top_scene_pos,
                    new_fontsize,
                    token_rot,
                )
                new_base_pos = pdf_token["baseline_pos"]

            annot = {
                "type": "text",
                "text": new_text,
                "pos": new_pos,
                "baseline_pos": new_base_pos,
                "fontsize": new_fontsize,
                "color": new_color,
                "rotation": pdf_token["rotation"],
                "redact_rect": pdf_token["raw_rect"],
                "updated_after": len(self.current_annotations),
            }
            if self._token_is_scan_overlay(pdf_token):
                annot["invisible"] = True
            self.current_annotations.append(annot)
            self.history.push(AddAnnotationCommand(self.current_page_idx, annot))
            self.render_page(fit_view=False, rebuild_base=True)
            self.notify_pages_updated([self.current_page_idx])

        self._refresh_hover_from_global_cursor()

    def edit_or_delete_text_annotation_at(self, ann_idx):
        ann_list = self.current_annotations
        if not (0 <= ann_idx < len(ann_list)) or ann_list[ann_idx].get("type") != "text":
            return

        self._clear_interactive_guides()

        old_ann = copy.deepcopy(ann_list[ann_idx])
        old_fontsize = float(old_ann.get("fontsize", self.current_font_size))
        old_color = tuple(old_ann.get("color", (0.0, 0.0, 0.0)))

        dlg = TextEditDeleteDialog(
            old_ann.get("text", ""),
            old_fontsize,
            old_color,
            is_pdf_origin=bool(old_ann.get("redact_rect")),
            parent=self.window(),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._refresh_hover_from_global_cursor()
            return

        if dlg.result_action == TextEditDeleteDialog.ACTION_DELETE:
            self.remove_text_annotation_at(ann_idx)
        elif dlg.result_action == TextEditDeleteDialog.ACTION_UPDATE:
            new_text = dlg.get_clean_text()
            new_fontsize = dlg.get_fontsize()
            new_color = dlg.get_color()
            if not new_text:
                self.remove_text_annotation_at(ann_idx)
                self._refresh_hover_from_global_cursor()
                return

            self.set_font_size(new_fontsize)
            self.set_current_color(new_color)

            if (
                new_text != old_ann.get("text", "")
                or abs(new_fontsize - old_fontsize) > 1e-4
                or new_color != old_color
            ):
                new_ann = copy.deepcopy(old_ann)
                new_ann["text"] = new_text
                new_ann["fontsize"] = new_fontsize
                new_ann["color"] = new_color
                new_ann["updated_after"] = len(ann_list)

                if abs(new_fontsize - old_fontsize) > 1e-4:
                    ann_rot = int(
                        old_ann.get("rotation", self._page_rotation)
                    ) % 360
                    if "baseline_pos" in old_ann:
                        bx, by = self.pdf_to_scene(fitz.Point(*old_ann["baseline_pos"]))
                        scale_factor = self._display_scale()
                        dy = new_fontsize * scale_factor * 0.88
                        cur_page_rot = self._page_rotation
                        rel_angle_rad = math.radians((cur_page_rot - ann_rot) % 360)
                        top_scene_pos = QPointF(
                            bx + dy * math.sin(rel_angle_rad),
                            by - dy * math.cos(rel_angle_rad),
                        )
                        new_pos, _ = self._compute_text_pdf_positions(
                            top_scene_pos,
                            new_fontsize,
                            ann_rot,
                        )
                        new_ann["pos"] = new_pos
                        new_ann["baseline_pos"] = old_ann["baseline_pos"]
                    else:
                        sx, sy = self.pdf_to_scene(fitz.Point(*old_ann["pos"]))
                        new_pos, new_base_pos = self._compute_text_pdf_positions(
                            QPointF(sx, sy),
                            new_fontsize,
                            ann_rot,
                        )
                        new_ann["pos"] = new_pos
                        new_ann["baseline_pos"] = new_base_pos

                ann_list[ann_idx] = new_ann
                self.history.push(
                    EditAnnotationCommand(self.current_page_idx, ann_idx, old_ann, new_ann)
                )
                self.render_page(fit_view=False, rebuild_base=False)
                self.notify_pages_updated([self.current_page_idx])

        self._refresh_hover_from_global_cursor()

    def prompt_new_text_at_scene_pos(self, pos):
        if not self.current_pdf or self.page_rect is None or self.page_rect.width() == 0:
            return

        self._update_hover_and_text_guide(pos)

        dlg = TextEditDeleteDialog(
            "",
            self.current_font_size,
            self.current_color,
            is_pdf_origin=False,
            is_new=True,
            parent=self.window(),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._refresh_hover_from_global_cursor()
            return

        if dlg.result_action == TextEditDeleteDialog.ACTION_UPDATE:
            clean_text = dlg.get_clean_text()
            if not clean_text:
                self._refresh_hover_from_global_cursor()
                return

            fontsize = dlg.get_fontsize()
            text_color = dlg.get_color()
            self.set_font_size(fontsize)
            self.set_current_color(text_color)

            page_rot = self._page_rotation
            pdf_pos, pdf_baseline_pos = self._compute_text_pdf_positions(
                pos,
                fontsize,
                page_rot,
            )

            annot = {
                "type": "text",
                "text": clean_text,
                "pos": pdf_pos,
                "baseline_pos": pdf_baseline_pos,
                "fontsize": fontsize,
                "color": text_color,
                "rotation": page_rot,
                "updated_after": len(self.current_annotations),
            }
            self.current_annotations.append(annot)
            self.history.push(AddAnnotationCommand(self.current_page_idx, annot))
            self.render_page(fit_view=False, rebuild_base=False)
            self.notify_pages_updated([self.current_page_idx])

        self._refresh_hover_from_global_cursor()

    def leaveEvent(self, event):
        self._last_hover_scene_pos = None
        if self._dragging_text_ann_idx is None:
            self._clear_interactive_guides()
        super().leaveEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [
                url.toLocalFile()
                for url in event.mimeData().urls()
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf")
            ]
            if paths:
                event.acceptProposedAction()
                QTimer.singleShot(0, lambda p=paths: self.filesDroppedOnViewer.emit(p))
                return
        super().dropEvent(event)

    def focusOutEvent(self, event):
        if self._space_pressed or self._panning:
            self._space_pressed = False
            self._panning = False
            self._pan_button = None
            self._restore_mode_cursor()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            # ドラッグ中のテキスト移動・描画・範囲選択もキャンセルする
            # （監査 #5: 呼ばないとドラッグ状態のままマウスを離した時に確定してしまう）。
            self.cancel_active_interaction()
            if self.search_results:
                self.clear_search_results()
            self.clear_calc_selection()
            self.clear_text_selection()
            self.escapePressed.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Control, Qt.Key.Key_Shift) and self._last_hover_scene_pos is not None:
            self._update_hover_and_text_guide(self._last_hover_scene_pos)
        if event.key() == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self._space_pressed = True
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        if event.key() == Qt.Key.Key_PageUp:
            self.go_prev_page()
            event.accept()
            return
        if event.key() == Qt.Key.Key_PageDown:
            self.go_next_page()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key.Key_Control, Qt.Key.Key_Shift) and self._last_hover_scene_pos is not None:
            self._update_hover_and_text_guide(self._last_hover_scene_pos)
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pressed = False
            self._panning = False
            self._pan_button = None
            self._restore_mode_cursor()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _restore_mode_cursor(self):
        if self.mode == "view":
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        elif self.mode == "text_select":
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        elif self.mode in ("crop", "box_erase", "highlight", "type", "draw", "region_ocr", "fold"):
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        elif self.mode in ("text_erase", "highlight_erase"):
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def wheelEvent(self, event):
        if self.drawing or self._calc_dragging or self._dragging_text_ann_idx is not None:
            event.accept()
            return
        if self.scroll_mode:
            # スクロール表示ではホイールはそのままスクロール（ページ送りはしない）。
            # Ctrl+ホイールはスクロール表示の拡大・縮小。
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                angle = event.angleDelta().y()
                if angle > 0:
                    self._scroll_zoom = min(self.SCROLL_MAX_ZOOM, self._scroll_zoom * 1.2)
                elif angle < 0:
                    self._scroll_zoom = max(self.SCROLL_MIN_ZOOM, self._scroll_zoom / 1.2)
                else:
                    event.accept()
                    return
                self._render_scroll_view()
                event.accept()
                return
            super().wheelEvent(event)
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            angle = event.angleDelta().y()
            current_scale = self.transform().m11()

            if angle > 0 and current_scale < 10.0:
                factor = 1.2
            elif angle < 0 and current_scale > 0.1:
                factor = 1 / 1.2
            else:
                factor = 1.0

            if factor != 1.0:
                self.scale(factor, factor)
                self._schedule_zoom_rerender()
            event.accept()
            return

        if not self.current_pdf:
            super().wheelEvent(event)
            return

        angle_y = event.angleDelta().y()
        if angle_y == 0:
            super().wheelEvent(event)
            return

        v_bar = self.verticalScrollBar()
        has_vertical_scroll = (v_bar.maximum() - v_bar.minimum()) > 3

        if has_vertical_scroll:
            if angle_y < 0 and v_bar.value() < v_bar.maximum():
                super().wheelEvent(event)
                return
            if angle_y > 0 and v_bar.value() > v_bar.minimum():
                super().wheelEvent(event)
                return

        now = time.monotonic()
        if now - self._last_wheel_page_turn_time < WHEEL_PAGE_TURN_COOLDOWN:
            event.accept()
            return

        turned = False
        if angle_y < 0:
            turned = self.go_next_page()
        elif angle_y > 0:
            turned = self.go_prev_page()

        if turned:
            self._last_wheel_page_turn_time = now
        event.accept()

    def zoom_in(self):
        if self.scroll_mode:
            self._scroll_zoom = min(self.SCROLL_MAX_ZOOM, self._scroll_zoom * 1.25)
            self._render_scroll_view()
            return
        if not self.current_pdf:
            self._show_status_message("拡大するPDFが開かれていません。先にPDFを開いてください。")
            return
        if self.transform().m11() < 10.0:
            self.scale(1.25, 1.25)
            self._schedule_zoom_rerender()

    def zoom_out(self):
        if self.scroll_mode:
            self._scroll_zoom = max(self.SCROLL_MIN_ZOOM, self._scroll_zoom / 1.25)
            self._render_scroll_view()
            return
        if not self.current_pdf:
            self._show_status_message("縮小するPDFが開かれていません。先にPDFを開いてください。")
            return
        if self.transform().m11() > 0.1:
            self.scale(0.8, 0.8)
            self._schedule_zoom_rerender()

    def fit_page(self):
        if self.scroll_mode:
            self._scroll_zoom = 1.0
            self._render_scroll_view()
            return
        if not self.current_pdf:
            self._show_status_message("表示するPDFが開かれていません。先にPDFを開いてください。")
            return
        if not self.scene.items():
            return
        self.fitInView(
            self.scene.sceneRect(),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        self._render_optimal_pixmap_for_current_view()

    def mouseDoubleClickEvent(self, event):
        if self.scroll_mode:
            # スクロール表示でダブルクリックしたページを1ページ表示で開いて編集できるようにする。
            scene_pos = self.mapToScene(event.position().toPoint())
            idx = self._scroll_page_at_scene_pos(scene_pos)
            if idx is not None:
                self.current_page_idx = idx
            self.set_scroll_mode(False)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if not self.current_pdf:
            return

        if self.scroll_mode:
            # スクロール表示では編集操作はしない。左ドラッグで画面を動かし、
            # クリックでページを選び、ダブルクリックで1ページ表示に戻る。
            if event.button() == Qt.MouseButton.LeftButton:
                self._scroll_press_pos = event.position()
                if self.mode != "view":
                    self._show_status_message(
                        "スクロール表示では編集できません。"
                        "ダブルクリックで1ページ表示に切り替えてください。"
                    )
            super().mousePressEvent(event)
            return

        raw_pos = self.mapToScene(event.position().toPoint())

        if (
            event.button() == Qt.MouseButton.LeftButton
            and (event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        ):
            if self.drawing or self._dragging_text_ann_idx is not None:
                event.accept()
                return
            pos = self._clamp_to_scene(raw_pos)
            self._clear_interactive_guides()
            self._calc_dragging = True
            self._calc_drag_start_pos = pos
            self._calc_drag_rect_item = QGraphicsRectItem(QRectF(pos, pos))
            self._calc_drag_rect_item.setPen(
                QPen(QColor(230, 105, 0, 240), 2.0, Qt.PenStyle.DashLine)
            )
            self._calc_drag_rect_item.setBrush(QBrush(QColor(255, 165, 0, 45)))
            self._calc_drag_rect_item.setZValue(1050)
            self.scene.addItem(self._calc_drag_rect_item)
            event.accept()
            return

        if (
            event.button() == Qt.MouseButton.RightButton
            and self.mode == "draw"
        ):
            draw_idx, _, _ = self._find_draw_at_scene_pos(raw_pos)
            if draw_idx is not None:
                self.remove_draw_annotation_at(draw_idx)
                event.accept()
                return

        if (
            event.button() == Qt.MouseButton.RightButton
            and self.mode in ("highlight", "highlight_erase")
        ):
            hl_idx, _ = self._find_highlight_at_scene_pos(raw_pos)
            if hl_idx is not None:
                self.remove_highlight_annotation_at(hl_idx)
                event.accept()
                return

        if (
            event.button() == Qt.MouseButton.RightButton
            and self.mode in ("type", "text_erase")
        ):
            _, hit_ann_idx = self._find_text_item_at_scene_pos(raw_pos)
            if hit_ann_idx is not None:
                self.remove_text_annotation_at(hit_ann_idx)
                event.accept()
                return

            pdf_token = self._find_pdf_text_token_at_scene_pos(raw_pos)
            if pdf_token is not None:
                self.redact_existing_pdf_token(pdf_token)
                event.accept()
                return

        if (
            event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton)
            or (event.button() == Qt.MouseButton.LeftButton and self._space_pressed)
        ):
            if self.drawing or self._dragging_text_ann_idx is not None:
                event.accept()
                return
            self._panning = True
            self._pan_button = event.button()
            self._pan_start_pos = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        if self.mode == "draw" and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            draw_idx, _, _ = self._find_draw_at_scene_pos(raw_pos)
            if draw_idx is not None:
                self.remove_draw_annotation_at(draw_idx)
                event.accept()
                return

        if self.mode == "highlight" and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            hl_idx, _ = self._find_highlight_at_scene_pos(raw_pos)
            if hl_idx is not None:
                self.remove_highlight_annotation_at(hl_idx)
                event.accept()
                return

        if self.mode == "text_erase":
            _, hit_ann_idx = self._find_text_item_at_scene_pos(raw_pos)
            if hit_ann_idx is not None:
                self.remove_text_annotation_at(hit_ann_idx)
                event.accept()
                return
            pdf_token = self._find_pdf_text_token_at_scene_pos(raw_pos)
            if pdf_token is not None:
                self.redact_existing_pdf_token(pdf_token)
                event.accept()
                return
            # クリック位置の直下に文字がない場合はドラッグ範囲選択を開始
            pos = self._clamp_to_scene(raw_pos)
            self._clear_interactive_guides()
            self.drawing = True
            self.drag_start_pos = pos
            self.drag_rect_item = QGraphicsRectItem(QRectF(pos, pos))
            self.drag_rect_item.setPen(
                QPen(QColor(220, 20, 20, 230), 2.0, Qt.PenStyle.DashLine)
            )
            self.drag_rect_item.setBrush(QBrush(QColor(255, 80, 80, 45)))
            self.scene.addItem(self.drag_rect_item)
            event.accept()
            return

        if self.mode == "fold":
            # 帯がすでにある状態でドラッグを始める場合は、いったん解除して
            # 折りたたんでいないページで新しい帯を指定し直してもらう。
            if self.has_fold_band():
                self.clear_fold_band()
                self._show_status_message(
                    "折りたたみを解除しました。もう一度ドラッグすると新しい帯を指定できます。"
                )
                event.accept()
                return

        if self.mode in ("crop", "box_erase", "highlight", "highlight_erase", "fold", "text_select"):
            pos = self._clamp_to_scene(raw_pos)
        else:
            if not self.scene.sceneRect().contains(raw_pos):
                return
            pos = raw_pos

        if self.mode == "type":
            hit_item, hit_ann_idx = self._find_text_item_at_scene_pos(pos)
            if hit_item is not None and hit_ann_idx is not None:
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.remove_text_annotation_at(hit_ann_idx)
                    return
                if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                    old_ann = copy.deepcopy(self.current_annotations[hit_ann_idx])
                    if old_ann.get("color") != self.current_color:
                        new_ann = copy.deepcopy(old_ann)
                        new_ann["color"] = self.current_color
                        new_ann["updated_after"] = len(self.current_annotations)
                        self.current_annotations[hit_ann_idx] = new_ann
                        self.history.push(
                            EditAnnotationCommand(self.current_page_idx, hit_ann_idx, old_ann, new_ann)
                        )
                        self.render_page(fit_view=False, rebuild_base=False)
                        self.notify_pages_updated([self.current_page_idx])
                    return

                self._dragging_text_ann_idx = hit_ann_idx
                self._dragging_text_item = hit_item
                self._text_drag_start_view_pos = event.position()
                self._text_drag_start_scene_pos = pos
                self._text_drag_start_item_pos = hit_item.pos()
                self._text_drag_moved = False
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                if 0 <= hit_ann_idx < len(self.current_annotations):
                    self._update_drag_text_guide(hit_item, self.current_annotations[hit_ann_idx])
                event.accept()
                return

            if hit_ann_idx is not None:
                # 座標判定でのみヒットした注釈（不可視テキストなど、画面に
                # 字形アイテムが無いもの）。ドラッグ移動はできないため、
                # クリックで編集ダイアログを開く（Shift+クリックで削除）。
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.remove_text_annotation_at(hit_ann_idx)
                    event.accept()
                    return
                event.accept()
                QTimer.singleShot(
                    0,
                    lambda idx=hit_ann_idx: self.edit_or_delete_text_annotation_at(idx),
                )
                return

            pdf_token = self._find_pdf_text_token_at_scene_pos(pos)
            if pdf_token is not None:
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.redact_existing_pdf_token(pdf_token)
                    return
                if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                    self.quick_recolor_existing_pdf_token(pdf_token, self.current_color)
                    return

                event.accept()
                QTimer.singleShot(0, lambda tok=copy.deepcopy(pdf_token): self.edit_existing_pdf_token(tok))
                return

            event.accept()
            QTimer.singleShot(0, lambda p=QPointF(pos): self.prompt_new_text_at_scene_pos(p))
            return

        if self.mode in ("crop", "box_erase", "highlight", "highlight_erase", "fold", "text_select"):
            self._clear_interactive_guides()
            if self.mode == "text_select":
                self.clear_text_selection()
            self.drawing = True
            self.drag_start_pos = pos

            self.drag_rect_item = QGraphicsRectItem(QRectF(pos, pos))
            if self.mode == "crop":
                self.drag_rect_item.setPen(
                    QPen(QColor("blue"), 2, Qt.PenStyle.DashLine)
                )
                self.drag_rect_item.setBrush(QBrush(QColor(0, 0, 255, 35)))
            elif self.mode == "fold":
                self.drag_rect_item.setPen(
                    QPen(QColor(128, 0, 160), 2, Qt.PenStyle.DashLine)
                )
                self.drag_rect_item.setBrush(QBrush(QColor(128, 0, 160, 45)))
            elif self.mode == "highlight":
                alpha = int(round(HIGHLIGHT_OPACITY * 255))
                self.drag_rect_item.setPen(
                    QPen(rgb_tuple_to_qcolor(self.current_color, 200), 1.5, Qt.PenStyle.DashLine)
                )
                self.drag_rect_item.setBrush(QBrush(rgb_tuple_to_qcolor(self.current_color, alpha)))
            elif self.mode == "highlight_erase":
                self.drag_rect_item.setPen(
                    QPen(QColor(220, 20, 20, 230), 2.0, Qt.PenStyle.DashLine)
                )
                self.drag_rect_item.setBrush(QBrush(QColor(255, 80, 80, 45)))
            elif self.mode == "text_select":
                self.drag_rect_item.setPen(
                    QPen(QColor(0, 120, 215, 230), 1.8, Qt.PenStyle.DashLine)
                )
                self.drag_rect_item.setBrush(QBrush(QColor(0, 120, 215, 45)))
            else:
                self.drag_rect_item.setPen(
                    QPen(QColor(220, 0, 0, 200), 1.5, Qt.PenStyle.DashLine)
                )
                self.drag_rect_item.setBrush(QBrush(QColor(255, 255, 255, 230)))

            self.scene.addItem(self.drag_rect_item)
            return

        if self.mode in ("draw", "erase"):
            self._clear_interactive_guides()
            self.drawing = True

            pdf_pos = self.scene_to_pdf(pos)
            self.current_stroke = [(pdf_pos.x, pdf_pos.y)]

            self.current_path = QPainterPath()
            self.current_path.moveTo(pos)
            self.current_path_item = QGraphicsPathItem(self.current_path)

            scale_factor = self._display_scale()

            if self.mode == "draw" and self.pen_eraser_active:
                width = max(6.0, 10.0 * scale_factor)
                pen = QPen(QColor(220, 30, 30, 210), width, Qt.PenStyle.DashLine)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                self.current_path_item.setPen(pen)
            elif self.mode == "draw":
                width = self.current_draw_width * scale_factor
                pen = QPen(rgb_tuple_to_qcolor(self.current_color), width)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                self.current_path_item.setPen(pen)
            else:
                width = ERASE_WIDTH * scale_factor
                pen = QPen(QColor(255, 255, 255, 230), width)
                pen.setCapStyle(Qt.PenCapStyle.SquareCap)
                pen.setJoinStyle(Qt.PenJoinStyle.BevelJoin)
                self.current_path_item.setPen(pen)

            self.scene.addItem(self.current_path_item)
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.position() - self._pan_start_pos
            self._pan_start_pos = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            event.accept()
            return

        raw_pos = self.mapToScene(event.position().toPoint())
        self._last_hover_scene_pos = raw_pos

        if self._calc_dragging and self._calc_drag_rect_item is not None:
            clamped_pos = self._clamp_to_scene(raw_pos)
            rect = QRectF(self._calc_drag_start_pos, clamped_pos).normalized()
            self._calc_drag_rect_item.setRect(rect)
            event.accept()
            return

        if (
            self._dragging_text_ann_idx is not None
            and self._dragging_text_item is not None
        ):
            view_dist = (event.position() - self._text_drag_start_view_pos).manhattanLength()
            if view_dist >= 4 or self._text_drag_moved:
                self._text_drag_moved = True
                delta_scene = raw_pos - self._text_drag_start_scene_pos
                new_item_pos = self._clamp_to_scene(self._text_drag_start_item_pos + delta_scene)
                self._dragging_text_item.setPos(new_item_pos)

                ann_idx = self._dragging_text_ann_idx
                if 0 <= ann_idx < len(self.current_annotations):
                    self._update_drag_text_guide(
                        self._dragging_text_item,
                        self.current_annotations[ann_idx],
                    )
            event.accept()
            return

        if (
            self.drawing
            and self.mode in ("crop", "box_erase", "highlight", "highlight_erase", "text_erase", "fold", "text_select")
            and self.drag_rect_item is not None
        ):
            clamped_pos = self._clamp_to_scene(raw_pos)
            if self.mode == "fold":
                # ドラッグの形で向きを決める。縦長なら縦帯（ページ全高）、
                # 横長・ほぼ正方形なら横帯（ページ全幅）のプレビューを表示する。
                drag_w = abs(clamped_pos.x() - self.drag_start_pos.x())
                drag_h = abs(clamped_pos.y() - self.drag_start_pos.y())
                if decide_fold_orientation(drag_w, drag_h) == FOLD_VERTICAL:
                    rect = QRectF(
                        min(self.drag_start_pos.x(), clamped_pos.x()),
                        0.0,
                        drag_w,
                        float(self.pixmap_height),
                    )
                else:
                    rect = QRectF(
                        0.0,
                        min(self.drag_start_pos.y(), clamped_pos.y()),
                        float(self.pixmap_width),
                        drag_h,
                    )
            else:
                rect = QRectF(self.drag_start_pos, clamped_pos).normalized()
            self.drag_rect_item.setRect(rect)

        elif self.drawing and self.mode in ("draw", "erase") and self.current_path_item is not None:
            clamped_pos = self._clamp_to_scene(raw_pos)
            pdf_pos = self.scene_to_pdf(clamped_pos)
            if self.current_stroke:
                lx, ly = self.current_stroke[-1]
                if math.hypot(pdf_pos.x - lx, pdf_pos.y - ly) < 0.4:
                    return
            self.current_stroke.append((pdf_pos.x, pdf_pos.y))

            self.current_path.lineTo(clamped_pos)
            self.current_path_item.setPath(self.current_path)
        else:
            ctrl_held = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if ctrl_held or self.mode in ("type", "text_erase", "draw", "highlight_erase", "highlight"):
                self._update_hover_and_text_guide(raw_pos, ctrl_pressed=ctrl_held)
            super().mouseMoveEvent(event)

    # ==========================================
    # 複数ページへの一括適用（トリミング・範囲墨消し）
    # ==========================================

    def _selected_page_rows(self):
        """ページ一覧で選択中のページ番号（0始まり）を返す。"""
        if self.selected_pages_provider is not None:
            try:
                rows = [int(r) for r in self.selected_pages_provider()]
            except Exception:
                logger.debug("selected_pages_provider failed", exc_info=True)
                rows = []
            return rows
        return []

    def _page_geometries(self):
        """全ページの形状 (mediabox x0, y0, x1, y1, 回転角) の一覧を返す。"""
        geometries = []
        if not self.current_pdf:
            return geometries
        with FITZ_LOCK:
            for page in self.current_pdf:
                mb = page.mediabox
                geometries.append(
                    (float(mb.x0), float(mb.y0), float(mb.x1), float(mb.y1), int(page.rotation) % 360)
                )
        return geometries

    def _ask_batch_scope(self, label, selected_count, page_count):
        """矩形確定後に適用範囲を選ぶ小さなダイアログを出す。

        戻り値は "current" / "selected" / "all" のいずれか。
        キャンセルされた場合は None を返す。
        """
        msg = QMessageBox(self)
        msg.setWindowTitle("適用範囲の選択")
        msg.setText(f"{label}をどのページに適用しますか？")
        btn_current = msg.addButton("このページだけ", QMessageBox.ButtonRole.AcceptRole)
        btn_selected = msg.addButton(
            f"選択中のページ（{selected_count}ページ）",
            QMessageBox.ButtonRole.AcceptRole,
        )
        btn_all = msg.addButton("すべてのページ", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(btn_selected if selected_count > 1 else btn_current)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is btn_current:
            return SCOPE_CURRENT
        if clicked is btn_selected:
            return SCOPE_SELECTED
        if clicked is btn_all:
            return SCOPE_ALL
        return None

    def _resolve_batch_targets(self, label):
        """適用範囲ダイアログを出し、(適用ページ一覧, スキップ一覧) を返す。

        1ページしかない文書ではダイアログを出さず今のページだけを返す。
        キャンセルされた場合は (None, None) を返す。
        """
        doc = self.current_pdf
        if not doc:
            return None, None
        page_count = len(doc)
        if page_count <= 1:
            return [self.current_page_idx], []
        selected_rows = self._selected_page_rows()
        scope = self._ask_batch_scope(label, len(selected_rows) or 1, page_count)
        if scope is None:
            return None, None
        targets = resolve_target_pages(
            scope, self.current_page_idx, selected_rows, page_count
        )
        geometries = self._page_geometries()
        applicable, skipped = partition_compatible_pages(
            geometries, self.current_page_idx, targets
        )
        return applicable, skipped

    def _apply_crop_to_pages(self, raw_crop):
        """トリミング矩形（PDF座標）を選択範囲のページへ適用する。

        戻り値は適用したページ数。キャンセル時は None。
        """
        doc = self.current_pdf
        if not doc:
            return None
        applicable, skipped = self._resolve_batch_targets("トリミング")
        if applicable is None:
            return None
        # トリミングでページ形状が変わるため、対象ページの折りたたみ帯は解除する。
        for idx in applicable:
            self._fold_bands.pop(idx, None)
        if len(applicable) == 1 and applicable[0] == self.current_page_idx:
            with FITZ_LOCK:
                old_cropbox = fitz.Rect(doc[self.current_page_idx].cropbox)
                doc[self.current_page_idx].set_cropbox(raw_crop)
            self.history.push(
                CropPageCommand(self.current_page_idx, old_cropbox, raw_crop)
            )
            self.render_page(fit_view=True, rebuild_base=True)
            self.notify_pages_updated([self.current_page_idx])
            return 1
        entries = []
        with FITZ_LOCK:
            for idx in applicable:
                entries.append(
                    (idx, fitz.Rect(doc[idx].cropbox), fitz.Rect(raw_crop))
                )
                doc[idx].set_cropbox(raw_crop)
        self.history.push(CropPagesCommand(entries))
        self.render_page(fit_view=True, rebuild_base=True)
        self.notify_pages_updated(applicable)
        self.batchStatusMessage.emit(
            format_batch_result("トリミング", len(applicable), len(skipped))
        )
        return len(applicable)

    def _apply_box_erase_to_pages(self, raw_rect):
        """範囲墨消し矩形（PDF座標）を選択範囲のページへ適用する。

        戻り値は適用したページ数。キャンセル時は None。
        複数ページへの墨消しは元に戻せない削除のため、適用前に確認する。
        """
        applicable, skipped = self._resolve_batch_targets("範囲墨消し")
        if applicable is None:
            return None
        annot = {
            "type": "box_erase",
            "rect": (raw_rect.x0, raw_rect.y0, raw_rect.x1, raw_rect.y1),
        }
        if len(applicable) == 1 and applicable[0] == self.current_page_idx:
            self.current_annotations.append(annot)
            self.history.push(AddAnnotationCommand(self.current_page_idx, annot))
            self.render_page(fit_view=False, rebuild_base=False)
            self.notify_pages_updated([self.current_page_idx])
            return 1
        reply = QMessageBox.question(
            self,
            "確認",
            f"{len(applicable)}ページに墨消しを適用します。元に戻せません。よろしいですか？",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return None
        for idx in applicable:
            self.page_annotations.setdefault(idx, []).append(copy.deepcopy(annot))
        self.history.push(
            AddAnnotationsMultiPageCommand([(idx, annot) for idx in applicable])
        )
        self.render_page(fit_view=False, rebuild_base=False)
        self.notify_pages_updated(applicable)
        self.batchStatusMessage.emit(
            format_batch_result("範囲墨消し", len(applicable), len(skipped))
        )
        return len(applicable)

    def mouseReleaseEvent(self, event):
        if (
            self.scroll_mode
            and event.button() == Qt.MouseButton.LeftButton
            and self._scroll_press_pos is not None
        ):
            # スクロール表示でのクリック（ドラッグでなければ）でページを選ぶ。
            moved = (event.position() - self._scroll_press_pos).manhattanLength()
            self._scroll_press_pos = None
            if moved < 6.0:
                scene_pos = self.mapToScene(event.position().toPoint())
                idx = self._scroll_page_at_scene_pos(scene_pos)
                if idx is not None and idx != self.current_page_idx:
                    self.current_page_idx = idx
                    self.pageSelectionChanged.emit(idx)
                    self.statusUpdated.emit()
            super().mouseReleaseEvent(event)
            event.accept()
            return

        if self._calc_dragging and event.button() == Qt.MouseButton.LeftButton:
            self._calc_dragging = False
            start_pos = self._calc_drag_start_pos
            rect = QRectF()
            if self._calc_drag_rect_item is not None:
                rect = self._calc_drag_rect_item.rect()
                if self._calc_drag_rect_item.scene() is self.scene:
                    self.scene.removeItem(self._calc_drag_rect_item)
                self._calc_drag_rect_item = None

            if rect.width() < 5.0 and rect.height() < 5.0:
                self._toggle_calc_item_at_scene_pos(start_pos)
            else:
                self._select_calc_items_in_scene_rect(rect)
            event.accept()
            return

        if self._panning and (
            event.button() == self._pan_button
            or self._pan_button is None
        ):
            self._panning = False
            self._pan_button = None
            if self._space_pressed:
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self._restore_mode_cursor()
                if (
                    self.mode in ("type", "text_erase", "draw", "highlight_erase", "highlight")
                    and self._last_hover_scene_pos is not None
                ):
                    self._update_hover_and_text_guide(self._last_hover_scene_pos)
            event.accept()
            return

        if (
            self._dragging_text_ann_idx is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            ann_idx = self._dragging_text_ann_idx
            drag_item = self._dragging_text_item
            was_moved = self._text_drag_moved
            self._reset_text_drag_state()

            if was_moved and drag_item is not None and 0 <= ann_idx < len(self.current_annotations):
                old_ann = copy.deepcopy(self.current_annotations[ann_idx])
                new_ann = copy.deepcopy(old_ann)
                fontsize = float(old_ann.get("fontsize", self.current_font_size))
                ann_rot = old_ann.get("rotation", self._page_rotation)

                new_pos, new_base_pos = self._compute_text_pdf_positions(
                    drag_item.pos(),
                    fontsize,
                    ann_rot,
                )
                new_ann["pos"] = new_pos
                new_ann["baseline_pos"] = new_base_pos
                new_ann["updated_after"] = len(self.current_annotations)

                self.current_annotations[ann_idx] = new_ann
                self.history.push(
                    EditAnnotationCommand(self.current_page_idx, ann_idx, old_ann, new_ann)
                )
                self.render_page(fit_view=False, rebuild_base=False)
                self.notify_pages_updated([self.current_page_idx])
            elif not was_moved and 0 <= ann_idx < len(self.current_annotations):
                self._restore_mode_cursor()
                QTimer.singleShot(0, lambda idx=ann_idx: self.edit_or_delete_text_annotation_at(idx))

            event.accept()
            return

        if self.drawing and event.button() == Qt.MouseButton.LeftButton:
            self.drawing = False
            # 破線の選択枠はここで必ずシーンから外す（監査 #4）。
            # 従来は render_page() 頼みだったため、適用ダイアログをキャンセルした
            # 場合など render_page() が呼ばれない経路で枠が残留した。
            if self.drag_rect_item is not None and self.drag_rect_item.scene() is self.scene:
                self.scene.removeItem(self.drag_rect_item)

            if self.mode == "crop" and self.drag_rect_item is not None:
                rect = self.drag_rect_item.rect()
                self.drag_rect_item = None

                if rect.width() > 10 and rect.height() > 10:
                    p0 = self.scene_to_pdf(rect.topLeft())
                    p1 = self.scene_to_pdf(rect.bottomRight())

                    with FITZ_LOCK:
                        page = self.current_pdf[self.current_page_idx]
                        raw_crop = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
                        raw_crop.normalize()
                        raw_crop &= page.mediabox

                    if not raw_crop.is_empty and raw_crop.width > 5 and raw_crop.height > 5:
                        if self._apply_crop_to_pages(raw_crop) is None:
                            self.render_page(fit_view=False, rebuild_base=False)
                        return

                self.render_page(fit_view=False, rebuild_base=False)

            elif self.mode == "highlight" and self.drag_rect_item is not None:
                rect = self.drag_rect_item.rect()
                self.drag_rect_item = None

                if rect.width() > 3 and rect.height() > 3:
                    p0 = self.scene_to_pdf(rect.topLeft())
                    p1 = self.scene_to_pdf(rect.bottomRight())
                    raw_rect = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
                    raw_rect.normalize()

                    annot = {
                        "type": "highlight",
                        "rect": (raw_rect.x0, raw_rect.y0, raw_rect.x1, raw_rect.y1),
                        "color": self.current_color,
                        "opacity": HIGHLIGHT_OPACITY,
                    }
                    self.current_annotations.append(annot)
                    self.history.push(AddAnnotationCommand(self.current_page_idx, annot))

                self.render_page(fit_view=False, rebuild_base=False)
                self.notify_pages_updated([self.current_page_idx])

            elif self.mode == "highlight_erase" and self.drag_rect_item is not None:
                rect = self.drag_rect_item.rect()
                if self.drag_rect_item.scene() is self.scene:
                    self.scene.removeItem(self.drag_rect_item)
                self.drag_rect_item = None

                if rect.width() <= 5 and rect.height() <= 5:
                    hl_idx, _ = self._find_highlight_at_scene_pos(self.drag_start_pos)
                    if hl_idx is not None:
                        self.remove_highlight_annotation_at(hl_idx)
                    elif self._last_hover_scene_pos is not None:
                        self._update_hover_and_text_guide(self._last_hover_scene_pos)
                else:
                    removed_count = self.remove_highlight_annotations_in_rect(rect)
                    if removed_count == 0 and self._last_hover_scene_pos is not None:
                        self._update_hover_and_text_guide(self._last_hover_scene_pos)

            elif self.mode == "text_erase" and self.drag_rect_item is not None:
                rect = self.drag_rect_item.rect()
                if self.drag_rect_item.scene() is self.scene:
                    self.scene.removeItem(self.drag_rect_item)
                self.drag_rect_item = None

                if rect.width() > 4 and rect.height() > 4:
                    removed_count = self.remove_texts_in_scene_rect(rect)
                    if removed_count == 0 and self._last_hover_scene_pos is not None:
                        self._update_hover_and_text_guide(self._last_hover_scene_pos)
                elif self._last_hover_scene_pos is not None:
                    self._update_hover_and_text_guide(self._last_hover_scene_pos)

            elif self.mode == "box_erase" and self.drag_rect_item is not None:
                rect = self.drag_rect_item.rect()
                self.drag_rect_item = None

                if rect.width() > 3 and rect.height() > 3:
                    p0 = self.scene_to_pdf(rect.topLeft())
                    p1 = self.scene_to_pdf(rect.bottomRight())
                    raw_rect = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
                    raw_rect.normalize()

                    if self._apply_box_erase_to_pages(raw_rect) is None:
                        self.render_page(fit_view=False, rebuild_base=False)
                        self.notify_pages_updated([self.current_page_idx])
                else:
                    self.render_page(fit_view=False, rebuild_base=False)
                    self.notify_pages_updated([self.current_page_idx])

            elif self.mode == "fold" and self.drag_rect_item is not None:
                rect = self.drag_rect_item.rect()
                self.drag_rect_item = None

                if rect.width() > 3 or rect.height() > 3:
                    p0 = self.scene_to_pdf(rect.topLeft())
                    p1 = self.scene_to_pdf(rect.bottomRight())
                    raw_rect = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
                    raw_rect.normalize()
                    view_rect = raw_rect_to_view_rect_from_geom(
                        self._page_crop_rect,
                        self._page_fitz_rect,
                        self._page_rotation,
                        raw_rect,
                    )
                    # ドラッグの形で向きを決める。縦長なら縦帯、横長・
                    # ほぼ正方形なら横帯（従来動作）。
                    orientation = decide_fold_orientation(
                        view_rect.width, view_rect.height
                    )
                    if orientation == FOLD_VERTICAL:
                        err = self.set_fold_band_for_current_page(
                            view_rect.x0, view_rect.x1, FOLD_VERTICAL
                        )
                        band_size = view_rect.x1 - view_rect.x0
                        band_label = "縦帯"
                        unit = "幅"
                    else:
                        err = self.set_fold_band_for_current_page(
                            view_rect.y0, view_rect.y1, FOLD_HORIZONTAL
                        )
                        band_size = view_rect.y1 - view_rect.y0
                        band_label = "横帯"
                        unit = "高さ"
                    if err is not None:
                        self._show_status_message(err)
                        self.render_page(fit_view=False, rebuild_base=False)
                    else:
                        self._show_status_message(
                            f"折りたたみ表示にしました（{band_label}・{format_pt(band_size)}pt分の{unit}を非表示）。"
                            "解除するにはもう一度「折りたたみ」ボタンを押してください。"
                        )
                else:
                    self.render_page(fit_view=False, rebuild_base=False)

            elif self.mode == "text_select" and self.drag_rect_item is not None:
                rect = self.drag_rect_item.rect()
                if self.drag_rect_item.scene() is self.scene:
                    self.scene.removeItem(self.drag_rect_item)
                self.drag_rect_item = None

                if rect.width() > 3 and rect.height() > 3:
                    p0 = self.scene_to_pdf(rect.topLeft())
                    p1 = self.scene_to_pdf(rect.bottomRight())
                    raw_rect = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
                    raw_rect.normalize()
                    self._finish_text_selection(raw_rect, rect)
                else:
                    self.clear_text_selection()

            elif self.mode in ("draw", "erase") and self.current_path_item is not None:
                raw_pos = self._clamp_to_scene(self.mapToScene(event.position().toPoint()))
                end_pdf_pos = self.scene_to_pdf(raw_pos)
                if self.current_stroke:
                    lx, ly = self.current_stroke[-1]
                    if math.hypot(end_pdf_pos.x - lx, end_pdf_pos.y - ly) >= 0.1:
                        self.current_stroke.append((end_pdf_pos.x, end_pdf_pos.y))
                if len(self.current_stroke) == 1:
                    self.current_stroke.append(self.current_stroke[0])

                if self.mode == "draw" and self.pen_eraser_active:
                    stroke_pts = list(self.current_stroke)
                    self.current_path = None
                    if self.current_path_item.scene() is self.scene:
                        self.scene.removeItem(self.current_path_item)
                    self.current_path_item = None
                    self.current_stroke = []
                    removed_cnt = self.remove_draw_annotations_along_stroke(stroke_pts)
                    if removed_cnt == 0 and self._last_hover_scene_pos is not None:
                        self._update_hover_and_text_guide(self._last_hover_scene_pos)
                else:
                    if self.mode == "draw":
                        annot = {
                            "type": "draw",
                            "points": self.current_stroke,
                            "color": self.current_color,
                            "width": self.current_draw_width,
                        }
                    else:
                        annot = {
                            "type": "erase",
                            "points": self.current_stroke,
                        }

                    self.current_annotations.append(annot)
                    self.history.push(AddAnnotationCommand(self.current_page_idx, annot))

                    self.current_path = None
                    self.current_path_item = None
                    self.render_page(fit_view=False, rebuild_base=False)
                    self.notify_pages_updated([self.current_page_idx])
        else:
            super().mouseReleaseEvent(event)

    def undo(self):
        self.history.undo(self)
        self.statusUpdated.emit()

    def redo(self):
        self.history.redo(self)
        self.statusUpdated.emit()
