# -*- coding: utf-8 -*-
"""全画面見開きリーダー (FullscreenSpreadReader) - 見開き閲覧・直接注釈編集"""
import copy
import math
from typing import Any, Dict, List, Optional, Tuple
import fitz

from PyQt6.QtWidgets import (
    QWidget,
    QLabel,
    QPushButton,
    QHBoxLayout,
    QVBoxLayout,
    QComboBox,
    QDoubleSpinBox,
    QColorDialog,
    QDialog,
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QGraphicsOpacityEffect,
)
from PyQt6.QtGui import (
    QPainter,
    QPainterPath,
    QPen,
    QBrush,
    QColor,
    QPixmap,
    QIcon,
    QImage,
    QMouseEvent,
    QWheelEvent,
    QKeyEvent,
)
from PyQt6.QtCore import (
    Qt,
    QRectF,
    QPointF,
    QTimer,
    pyqtSignal,
)

from core import (
    COLOR_PRESETS,
    ERASE_WIDTH,
    HIGHLIGHT_OPACITY,
    TEXT_LINE_SPACING,
    adjust_updated_after_on_remove,
    build_pdf_token_replacement_annotation,
    build_text_annotation,
    plan_text_annotation_removal,
    annotation_to_erase_rects,
    clip_stroke_by_rects,
    collect_all_erased_raw_rects,
    collect_text_only_redact_raw_rects,
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
    point_to_segment_distance as _point_to_segment_dist,
)
from ui import (
    compute_scaled_baseline_top_pos,
    create_color_swatch_icon as _create_swatch_icon,
    create_pen_eraser_icon as _create_pen_eraser_icon,
    qcolor_to_rgb_tuple as _qcolor_to_rgb,
    rgb_tuple_to_qcolor as _rgb_to_qcolor,
    sync_color_combo,
)
from core.geometry import (
    estimate_text_bbox_in_pdf,
    raw_pdf_to_view_point_from_geom,
    view_point_to_raw_pdf_from_geom,
)


from ui.spread_engine import SpreadCoordMapper, SpreadItem, SpreadLayoutEngine


class FullscreenSpreadReader(QWidget):
    closed = pyqtSignal()

    def __init__(
        self,
        doc: fitz.Document,
        start_page: int = 0,
        file_manifest: Optional[List[Tuple[str, int, int]]] = None,
        right_to_left: bool = False,
        cover_single: bool = True,
        annotations: Optional[dict] = None,
        draw_annot_callback=None,
        redact_page_callback=None,
        font_family: str = "Sans Serif",
        own_doc: bool = False,
        fitz_lock=None,
        viewer=None,
        editor_callbacks: Optional[Dict[str, Any]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.doc = doc
        self.file_manifest = file_manifest or []
        self.right_to_left = right_to_left
        self.cover_single = cover_single
        self.viewer = viewer
        self.annotations = (
            viewer.page_annotations
            if (viewer is not None and annotations is None)
            else (annotations if annotations is not None else {})
        )
        self.draw_annot_callback = draw_annot_callback
        self.redact_page_callback = redact_page_callback
        self.font_family = font_family
        self.own_doc = own_doc
        self.fitz_lock = fitz_lock
        self.editor_callbacks = editor_callbacks or {}

        self.spreads: List[SpreadItem] = []
        self.current_spread_idx = 0
        self._cached_draw_items: List[Dict[str, Any]] = []

        # 編集モード状態
        self.mode = "view"
        self.current_color: Tuple[float, float, float] = (
            tuple(viewer.current_color) if viewer is not None else (0.98, 0.90, 0.05)
        )
        self._pen_eraser_active: bool = (
            bool(getattr(viewer, "pen_eraser_active", False)) if viewer is not None else False
        )
        self.current_draw_width: float = (
            float(viewer.current_draw_width) if viewer is not None else 1.5
        )
        self.current_font_size: float = (
            float(viewer.current_font_size) if viewer is not None else 14.0
        )
        self._toolbar_pinned = False

        # ドラッグ描画・プレビュー用状態
        self._drawing = False
        self._active_page_info: Optional[Dict[str, Any]] = None
        self._drag_start_pt = QPointF()
        self._drag_current_pt = QPointF()
        self._stroke_screen_pts: List[QPointF] = []
        self._stroke_pdf_pts: List[Tuple[float, float]] = []
        self._hover_screen_rect: Optional[QRectF] = None
        self._hover_screen_color: QColor = QColor(0, 180, 90, 220)

        # スタンドアロン（フォルダ一括読込）時の簡易Undo/Redoスタックと編集状態追跡
        self._local_undo_stack: List[Tuple[int, List[dict]]] = []
        self._local_redo_stack: List[Tuple[int, List[dict]]] = []
        self._has_edited = False
        self._last_edited_page_idx: Optional[int] = None
        self._last_spread_size: Tuple[int, int] = (0, 0)

        # ズーム＆パン（拡大・スクロール）およびテキストドラッグ移動用状態
        self._zoom: float = 1.0
        self._rendered_zoom: float = 1.0
        self._pan_offset = QPointF(0.0, 0.0)
        self._panning = False
        self._pan_button = None
        self._pan_start_pt = QPointF()
        self._pan_start_offset = QPointF()
        self._space_pressed = False
        self._view_press_pt: Optional[QPointF] = None
        self._right_press_pt: Optional[QPointF] = None
        self._dragging_text_info: Optional[Dict[str, Any]] = None

        self._zoom_rerender_timer = QTimer(self)
        self._zoom_rerender_timer.setSingleShot(True)
        self._zoom_rerender_timer.setInterval(120)
        self._zoom_rerender_timer.timeout.connect(self._on_zoom_rerender_timeout)

        # 子ダイアログ（テキスト入力等）へ黒背景が継承されないようオブジェクト名でセレクタを限定
        self.setObjectName("fullscreenSpreadReader")
        self.setStyleSheet("QWidget#fullscreenSpreadReader { background-color: #0d0d0d; }")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setMouseTracking(True)

        # 1. 下部 HUD + ページ移動・編集ツール呼び出しボタン
        self.hud_container = QWidget(self)
        self.hud_container.setStyleSheet(
            "QWidget#hudBox { background-color: rgba(20, 20, 20, 225); border: 1px solid rgba(255,255,255,50); border-radius: 10px; }"
        )
        self.hud_container.setObjectName("hudBox")
        hud_layout = QHBoxLayout(self.hud_container)
        hud_layout.setContentsMargins(10, 5, 10, 5)
        hud_layout.setSpacing(8)

        hud_btn_style = (
            "QPushButton { background-color: #2c2c34; color: #f0f0f0; border: 1px solid #555562; "
            "border-radius: 5px; padding: 2px 8px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #3e3e4a; border-color: #808095; }"
        )
        self.btn_hud_prev = QPushButton("◀", self.hud_container)
        self.btn_hud_prev.setToolTip("前の見開きへ (左矢印 / ホイール上)")
        self.btn_hud_prev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hud_prev.setStyleSheet(hud_btn_style)
        self.btn_hud_prev.clicked.connect(self.prev_spread)
        hud_layout.addWidget(self.btn_hud_prev)

        self.hud_label = QLabel(self.hud_container)
        self.hud_label.setStyleSheet(
            "background: transparent; color: #f0f0f0; font-weight: bold; font-size: 13px;"
        )
        hud_layout.addWidget(self.hud_label)

        self.btn_hud_next = QPushButton("▶", self.hud_container)
        self.btn_hud_next.setToolTip("次の見開きへ (右矢印 / ホイール下)")
        self.btn_hud_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hud_next.setStyleSheet(hud_btn_style)
        self.btn_hud_next.clicked.connect(self.next_spread)
        hud_layout.addWidget(self.btn_hud_next)

        self.btn_hud_jump = QPushButton("🔢 ページ移動 (G)", self.hud_container)
        self.btn_hud_jump.setToolTip("ページ番号を指定して移動します (G)")
        self.btn_hud_jump.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hud_jump.setStyleSheet(hud_btn_style)
        self.btn_hud_jump.clicked.connect(self._prompt_page_jump)
        hud_layout.addWidget(self.btn_hud_jump)

        # 表紙の有無を選択するボタン（[C] キーと同じ動作。押すたびに切替）
        self.btn_hud_cover = QPushButton("📕 表紙", self.hud_container)
        self.btn_hud_cover.setToolTip("表紙を1ページ目だけ単独で表示する / しない（Cキーでも切替）")
        self.btn_hud_cover.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hud_cover.setCheckable(True)
        self.btn_hud_cover.setChecked(self.cover_single)
        self.btn_hud_cover.setStyleSheet(
            hud_btn_style
            + "QPushButton:checked { background-color: #0e639c; color: #ffffff; border-color: #3794ff; }"
        )
        self.btn_hud_cover.clicked.connect(self._toggle_cover_mode)
        hud_layout.addWidget(self.btn_hud_cover)

        self.btn_hud_edit = QPushButton("✏ マーカー・編集ツール (E)", self.hud_container)
        self.btn_hud_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hud_edit.setStyleSheet(
            "QPushButton { background-color: #2b579a; color: #ffffff; border: 1px solid #4a89dc; "
            "border-radius: 6px; padding: 3px 10px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #3b69aa; border-color: #7ab0ff; }"
        )
        self.btn_hud_edit.clicked.connect(self._toggle_toolbar_pinned)
        hud_layout.addWidget(self.btn_hud_edit)

        self.hud_opacity = QGraphicsOpacityEffect(self.hud_container)
        self.hud_container.setGraphicsEffect(self.hud_opacity)

        self.hud_timer = QTimer(self)
        self.hud_timer.setSingleShot(True)
        self.hud_timer.setInterval(2600)
        self.hud_timer.timeout.connect(self._hide_hud)

        # 2. 右上カーソルホバーで出現する「✕」閉じるボタン
        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(46, 46)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setToolTip("見開き全画面を閉じる (Esc / F5)")
        self.close_btn.setStyleSheet(
            "QPushButton { background-color: rgba(28, 28, 30, 225); color: #ffffff; "
            "border: 1.5px solid rgba(255, 255, 255, 130); border-radius: 23px; "
            "font-size: 20px; font-weight: bold; }"
            "QPushButton:hover { background-color: #e81123; color: #ffffff; border-color: #ff8080; }"
        )
        self.close_btn.clicked.connect(self.close)
        self.close_btn.hide()

        self.close_hide_timer = QTimer(self)
        self.close_hide_timer.setSingleShot(True)
        self.close_hide_timer.setInterval(900)
        self.close_hide_timer.timeout.connect(self._check_hide_top_overlays)

        # 3. 上部フローティング編集ツールバー
        self._init_floating_edit_bar()

        # ウィンドウ設定
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)

        # 見開き解析
        self._build_spreads()
        target = max(0, min(start_page, len(self.doc) - 1)) if len(self.doc) > 0 else 0
        for idx, sp in enumerate(self.spreads):
            if target in sp.page_indices:
                self.current_spread_idx = idx
                break

        self.showFullScreen()
        QTimer.singleShot(0, self._ensure_initial_render)

    def _init_floating_edit_bar(self):
        self.edit_bar = QWidget(self)
        self.edit_bar.setObjectName("floatingEditBar")
        self.edit_bar.setStyleSheet(
            "QWidget#floatingEditBar { background-color: rgba(24, 24, 28, 240); "
            "border: 1px solid rgba(255, 255, 255, 70); border-radius: 10px; }"
            "QLabel { color: #e8e8e8; font-size: 12px; font-weight: bold; background: transparent; }"
            "QPushButton { background-color: #2d2d34; color: #f0f0f0; border: 1px solid #50505c; "
            "border-radius: 6px; padding: 4px 9px; font-size: 12px; font-weight: bold; }"
            "QPushButton:hover { background-color: #3e3e4a; border-color: #75758a; }"
            "QPushButton:checked { background-color: #0e639c; color: #ffffff; border: 1px solid #3794ff; }"
            "QComboBox, QDoubleSpinBox { background-color: #25252b; color: #f0f0f0; "
            "border: 1px solid #555562; border-radius: 5px; padding: 2px 5px; font-size: 12px; }"
            "QComboBox QAbstractItemView { background-color: #25252b; color: #f0f0f0; "
            "selection-background-color: #0e639c; selection-color: #ffffff; }"
        )
        outer_layout = QVBoxLayout(self.edit_bar)
        outer_layout.setContentsMargins(10, 6, 10, 6)
        outer_layout.setSpacing(4)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(5)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(6)

        self.mode_buttons: Dict[str, QPushButton] = {}
        mode_specs = [
            ("✋ 閲覧 (V)", "view", "ページめくり閲覧モード (V)"),
            ("🖍 マーカー (M)", "highlight", "ドラッグでマーカーを引きます / マーカー上で右クリックで消去 (M)"),
            ("🖍✕ マーカー消去", "highlight_erase", "クリックまたはドラッグでマーカーのみ消去します"),
            ("✏ ペン (P)", "draw", "ドラッグで自由曲線を描きます / 色から『ペン消しゴム』も選べます (P)"),
            ("⌨ テキスト (T)", "type", "PDF内の文字をクリックで修正・削除、空白クリックで文字追加 (T)"),
            ("🧹 文字消去", "text_erase", "クリックした文字データを消去します"),
            ("🔲 範囲墨消し", "box_erase", "ドラッグした矩形範囲を白塗りで消去します"),
            ("🧽 消しゴム", "erase", "ドラッグした軌跡を白塗りで消去します"),
        ]
        for label, m_key, tip in mode_specs:
            btn = QPushButton(label, self.edit_bar)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tip)
            if m_key == "view":
                btn.setChecked(True)
            btn.clicked.connect(lambda checked=False, k=m_key: self.set_edit_mode(k))
            row1.addWidget(btn)
            self.mode_buttons[m_key] = btn
        row1.addStretch()

        row2.addWidget(QLabel("🎨色:", self.edit_bar))
        self.combo_color = QComboBox(self.edit_bar)
        self.combo_color.setMinimumWidth(145)
        for name, rgb in COLOR_PRESETS:
            self.combo_color.addItem(_create_swatch_icon(rgb), name, rgb)
        self.combo_color.addItem(
            _create_pen_eraser_icon(14), "🧽 ペン消しゴム（ペンのみ消去）", "pen_eraser"
        )
        self.combo_color.addItem("カスタム...", "custom")
        self._sync_color_combo(self.current_color)
        self.combo_color.activated.connect(self._on_color_selected)
        row2.addWidget(self.combo_color)

        row2.addWidget(QLabel("✏太さ:", self.edit_bar))
        self.spin_width = QDoubleSpinBox(self.edit_bar)
        self.spin_width.setRange(0.5, 30.0)
        self.spin_width.setSingleStep(0.5)
        self.spin_width.setDecimals(1)
        self.spin_width.setSuffix("pt")
        self.spin_width.setValue(self.current_draw_width)
        self.spin_width.valueChanged.connect(self._on_width_changed)
        row2.addWidget(self.spin_width)

        row2.addWidget(QLabel("⌨文字:", self.edit_bar))
        self.spin_fontsize = QDoubleSpinBox(self.edit_bar)
        self.spin_fontsize.setRange(5.0, 96.0)
        self.spin_fontsize.setSingleStep(0.5)
        self.spin_fontsize.setDecimals(1)
        self.spin_fontsize.setSuffix("pt")
        self.spin_fontsize.setValue(self.current_font_size)
        self.spin_fontsize.valueChanged.connect(self._on_fontsize_changed)
        row2.addWidget(self.spin_fontsize)

        btn_undo = QPushButton("↶ 戻す", self.edit_bar)
        btn_undo.setToolTip("元に戻す (Ctrl+Z)")
        btn_undo.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_undo.clicked.connect(self._perform_undo)
        row2.addWidget(btn_undo)

        btn_redo = QPushButton("↷ やり直し", self.edit_bar)
        btn_redo.setToolTip("やり直し (Ctrl+Y)")
        btn_redo.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_redo.clicked.connect(self._perform_redo)
        row2.addWidget(btn_redo)

        btn_zoom_out = QPushButton("🔎-", self.edit_bar)
        btn_zoom_out.setToolTip("縮小 (Ctrl+ホイール下 / -)")
        btn_zoom_out.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_zoom_out.clicked.connect(self.zoom_out)
        row2.addWidget(btn_zoom_out)

        self.btn_zoom_reset = QPushButton("100%", self.edit_bar)
        self.btn_zoom_reset.setToolTip("表示倍率を100%に戻す (0)")
        self.btn_zoom_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_zoom_reset.clicked.connect(self.reset_zoom)
        row2.addWidget(self.btn_zoom_reset)

        btn_zoom_in = QPushButton("🔍+", self.edit_bar)
        btn_zoom_in.setToolTip("拡大 (Ctrl+ホイール上 / +)")
        btn_zoom_in.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_zoom_in.clicked.connect(self.zoom_in)
        row2.addWidget(btn_zoom_in)

        btn_save = QPushButton("💾 保存", self.edit_bar)
        btn_save.setToolTip("上書き保存 / 別名保存 (Ctrl+S)")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.clicked.connect(self._trigger_save)
        row2.addWidget(btn_save)

        if self.viewer is not None or "adopt_doc_to_main" in self.editor_callbacks:
            btn_to_main = QPushButton("🛠 メイン画面で編集", self.edit_bar)
            btn_to_main.setToolTip("全画面を閉じて現在のページ・ツールをメインエディタで開きます")
            btn_to_main.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_to_main.setStyleSheet(
                "QPushButton { background-color: #1f6f43; color: #ffffff; border: 1px solid #3ca569; }"
                "QPushButton:hover { background-color: #288a54; }"
            )
            btn_to_main.clicked.connect(self._switch_to_main_editor)
            row2.addWidget(btn_to_main)

        self.btn_pin_bar = QPushButton("📌 固定", self.edit_bar)
        self.btn_pin_bar.setCheckable(True)
        self.btn_pin_bar.setToolTip("ツールバーを常時表示に固定します")
        self.btn_pin_bar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pin_bar.toggled.connect(self._on_pin_toggled)
        row2.addWidget(self.btn_pin_bar)
        row2.addStretch()

        outer_layout.addLayout(row1)
        outer_layout.addLayout(row2)

        self.edit_bar.adjustSize()
        self.edit_bar.hide()

    def _position_overlays(self):
        # 右上の✕ボタン配置
        self.close_btn.move(max(10, self.width() - self.close_btn.width() - 16), 12)
        self.close_btn.raise_()

        # 上部編集ツールバー配置（右上の✕ボタンと重ならないよう中央〜左寄りに調整）
        self.edit_bar.adjustSize()
        bar_w = self.edit_bar.width()
        max_right = self.width() - self.close_btn.width() - 28
        bar_x = max(12, min((self.width() - bar_w) // 2, max_right - bar_w))
        self.edit_bar.move(bar_x, 12)
        self.edit_bar.raise_()

        # 下部HUD配置
        self.hud_container.adjustSize()
        hx = (self.width() - self.hud_container.width()) // 2
        hy = self.height() - self.hud_container.height() - 22
        self.hud_container.move(max(10, hx), max(10, hy))
        self.hud_container.raise_()

    def _toggle_toolbar_pinned(self):
        self._toolbar_pinned = not self._toolbar_pinned
        self.btn_pin_bar.blockSignals(True)
        self.btn_pin_bar.setChecked(self._toolbar_pinned)
        self.btn_pin_bar.blockSignals(False)
        if self._toolbar_pinned:
            self._position_overlays()
            self.edit_bar.show()
            self.close_btn.show()
            self._show_hud("編集ツールバーを表示しました（上部でマーカーやペン・文字編集を選択できます）")
        else:
            if self.mode == "view":
                self.edit_bar.hide()
            self._show_hud("編集ツールバーの固定を解除しました")

    def _on_pin_toggled(self, checked: bool):
        self._toolbar_pinned = bool(checked)
        if not self._toolbar_pinned and self.mode == "view":
            self.close_hide_timer.start()

    def _is_pos_on_overlay_ui(self, pos: QPointF) -> bool:
        pt = pos.toPoint()
        if self.close_btn.isVisible() and self.close_btn.geometry().contains(pt):
            return True
        if self.edit_bar.isVisible() and self.edit_bar.geometry().contains(pt):
            return True
        if (
            self.hud_container.isVisible()
            and self.hud_opacity.opacity() > 0.05
            and self.hud_container.geometry().contains(pt)
        ):
            return True
        return False

    def _cancel_active_drawing(self):
        need_update = False
        if self._drawing:
            self._drawing = False
            self._active_page_info = None
            self._stroke_pdf_pts.clear()
            self._stroke_screen_pts.clear()
            need_update = True
        if self._dragging_text_info is not None:
            self._dragging_text_info = None
            need_update = True
        if self._panning:
            self._panning = False
            self._pan_button = None
            need_update = True
        self._view_press_pt = None
        self._right_press_pt = None
        if need_update:
            self.update()

    def _check_hide_top_overlays(self):
        if self._drawing:
            return
        cursor_pos = self.mapFromGlobal(self.cursor().pos())
        in_top_right = cursor_pos.x() >= self.width() - 170 and cursor_pos.y() <= 110
        in_top_bar = cursor_pos.y() <= 95 or (
            self.edit_bar.isVisible() and self.edit_bar.geometry().adjusted(-12, -12, 12, 18).contains(cursor_pos)
        )
        if self.combo_color.view().isVisible():
            return

        if not in_top_right and not in_top_bar and not self._toolbar_pinned and self.mode == "view":
            self.close_btn.hide()
            self.edit_bar.hide()

    def set_edit_mode(self, mode: str):
        self._cancel_active_drawing()
        if mode != "draw" and self._pen_eraser_active:
            self._pen_eraser_active = False
            if self.viewer is not None and hasattr(self.viewer, "set_pen_eraser_active"):
                self.viewer.set_pen_eraser_active(False)
            self._sync_color_combo(self.current_color)

        self.mode = mode
        for k, btn in self.mode_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(k == mode)
            btn.blockSignals(False)

        self._hover_screen_rect = None
        if mode == "view":
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._show_hud("✋ 閲覧モード（クリック/ホイールでページ移動）")
            if not self._toolbar_pinned:
                self.close_hide_timer.start()
        else:
            # マーカー選択時に色が赤や黒のままならマーカー向きの黄色に自動プリセット
            if mode == "highlight" and self.current_color in ((0.90, 0.05, 0.05), (0.0, 0.0, 0.0)):
                self.current_color = (0.98, 0.90, 0.05)
                self._sync_color_combo(self.current_color)
                if self.viewer is not None:
                    self.viewer.set_current_color(self.current_color)
            # テキスト選択時にマーカー用の黄色のままなら見やすい黒に自動プリセット
            elif mode == "type" and all(abs(a - b) < 0.04 for a, b in zip(self.current_color, (0.98, 0.90, 0.05))):
                self.current_color = (0.0, 0.0, 0.0)
                self._sync_color_combo(self.current_color)
                if self.viewer is not None:
                    self.viewer.set_current_color(self.current_color)

            if mode in ("highlight", "draw", "box_erase", "type"):
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.setCursor(Qt.CursorShape.PointingHandCursor)

            self._position_overlays()
            self.edit_bar.show()
            self.close_btn.show()
            mode_names = {
                "highlight": "🖍 マーカーモード：ページ上をドラッグしてラインを引きます（右クリックでマーカー消去）",
                "highlight_erase": "🖍✕ マーカー消去：マーカーをクリックまたはドラッグして消去します",
                "draw": (
                    "🧽 ペン消しゴム：ペンで描いた線をクリックまたはドラッグして消去します"
                    if self._pen_eraser_active
                    else "✏ ペンモード：ドラッグで描き込み（色から『ペン消しゴム』選択または右クリックでペン消去）"
                ),
                "type": "⌨ テキストモード：文字をクリックで編集・削除 / 空白クリックで文字追加",
                "text_erase": "🧹 文字消去モード：消したい文字をクリックして削除します",
                "box_erase": "🔲 範囲墨消しモード：ドラッグした範囲を白塗りで消去します",
                "erase": "🧽 消しゴムモード：ドラッグした軌跡を白塗りで消去します",
            }
            self._show_hud(mode_names.get(mode, f"編集モード: {mode}"))
        self.update()

    def _sync_color_combo(self, rgb_tuple):
        sync_color_combo(
            self.combo_color, rgb_tuple, pen_eraser_active=self._pen_eraser_active
        )

    def _on_color_selected(self, index: int):
        data = self.combo_color.itemData(index)
        if data == "pen_eraser":
            self._pen_eraser_active = True
            if self.viewer is not None and hasattr(self.viewer, "set_pen_eraser_active"):
                self.viewer.set_pen_eraser_active(True)
            if self.mode != "draw":
                self.set_edit_mode("draw")
            else:
                self._show_hud("🧽 ペン消しゴム：ペンで描いた線をクリックまたはドラッグして消去します（文字や罫線は消えません）")
            return

        if data == "custom":
            chosen = QColorDialog.getColor(_rgb_to_qcolor(self.current_color), self, "色を選択")
            if chosen.isValid():
                self.current_color = _qcolor_to_rgb(chosen)
                self._pen_eraser_active = False
            self._sync_color_combo(self.current_color)
        elif isinstance(data, tuple):
            self._pen_eraser_active = False
            self.current_color = data

        if self.viewer is not None:
            if hasattr(self.viewer, "set_pen_eraser_active"):
                self.viewer.set_pen_eraser_active(self._pen_eraser_active)
            if not self._pen_eraser_active:
                self.viewer.set_current_color(self.current_color)

    def _on_width_changed(self, val: float):
        self.current_draw_width = round(float(val), 1)
        if self.viewer is not None:
            self.viewer.set_draw_width(self.current_draw_width)

    def _on_fontsize_changed(self, val: float):
        self.current_font_size = round(float(val), 1)
        if self.viewer is not None:
            self.viewer.set_font_size(self.current_font_size)

    def _switch_to_main_editor(self):
        self._cancel_active_drawing()
        target_mode = self.mode if self.mode != "view" else "highlight"
        if self.viewer is not None:
            main_win = self.viewer.window()
            if hasattr(main_win, "set_mode"):
                main_win.set_mode(target_mode)
            self.close()
        elif self.doc is not None and "adopt_doc_to_main" in self.editor_callbacks:
            cur_page = (
                self._last_edited_page_idx
                if (self._last_edited_page_idx is not None and self.spreads and self._last_edited_page_idx in self.spreads[self.current_spread_idx].page_indices)
                else (self.spreads[self.current_spread_idx].page_indices[0] if self.spreads else 0)
            )
            adopted = self.editor_callbacks["adopt_doc_to_main"](
                self.doc, self.annotations, cur_page, target_mode, self._has_edited
            )
            if adopted:
                self.own_doc = False
                self._has_edited = False
                self.close()

    def _trigger_save(self) -> bool:
        self._cancel_active_drawing()
        if self.viewer is not None:
            main_win = self.viewer.window()
            if hasattr(main_win, "save_file"):
                main_win.save_file()
                if not self.viewer.is_modified:
                    self._has_edited = False
                self._show_hud("💾 保存処理を実行しました")
                return not self.viewer.is_modified
            return False
        elif self.doc is not None and "save_safely" in self.editor_callbacks:
            filepath, _ = QFileDialog.getSaveFileName(
                self, "結合PDFを別名保存", "", "PDF Files (*.pdf)"
            )
            if not filepath:
                return False
            if not filepath.lower().endswith(".pdf"):
                filepath += ".pdf"
            save_fn = self.editor_callbacks["save_safely"]
            was_encrypted = bool(
                getattr(self.doc, "is_encrypted", False)
                or getattr(self.doc, "needs_pass", False)
                or (getattr(self.doc, "metadata", None) or {}).get("encryption")
            )
            if self.viewer is not None:
                was_encrypted = was_encrypted or bool(
                    getattr(self.viewer, "current_pdf_was_encrypted", False)
                )
            allow_strip = False
            if was_encrypted:
                reply = QMessageBox.question(
                    self,
                    "パスワード保護解除の確認",
                    "元のPDFにはパスワード保護が設定されていました。\n"
                    "保存するとパスワード保護が解除された状態で保存されます。続行しますか？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return False
                allow_strip = True
            try:
                _, err = save_fn(
                    source_doc=self.doc,
                    page_annotations=self.annotations,
                    filepath=filepath,
                    current_file=None,
                    allow_strip_encryption=allow_strip,
                )
            except TypeError:
                # Core without the allow_strip_encryption guard yet.
                _, err = save_fn(
                    source_doc=self.doc,
                    page_annotations=self.annotations,
                    filepath=filepath,
                    current_file=None,
                )
            if err is not None:
                QMessageBox.critical(self, "保存エラー", f"保存に失敗しました:\n{err}")
                return False
            self._has_edited = False
            self._show_hud(f"💾 保存完了: {filepath}")
            return True
        return False

    def _ensure_initial_render(self):
        if not self._cached_draw_items:
            self._update_current_spread_cache()
            self.update()
        self._position_overlays()
        self._show_hud("右上へ移動: [✕ 閉じる] | 上端へ移動または[E]: 編集ツール | [M] マーカー | [R] 右開き | [C] 表紙")

    def _is_page_landscape(self, page_idx: int) -> bool:
        return SpreadLayoutEngine.is_page_landscape(self.doc, page_idx, self.fitz_lock)

    def _build_spreads(self):
        self.spreads = SpreadLayoutEngine.build_spreads(
            self.doc, cover_single=self.cover_single, fitz_lock=self.fitz_lock
        )

    def _prompt_page_jump(self):
        if not self.doc or len(self.doc) == 0:
            return
        self._cancel_active_drawing()
        cur_p = self.spreads[self.current_spread_idx].page_indices[0] + 1 if self.spreads else 1
        page_num, ok = QInputDialog.getInt(
            self,
            "ページ移動",
            f"移動先のページ番号を入力 (1 〜 {len(self.doc)}):",
            value=cur_p,
            min=1,
            max=len(self.doc),
        )
        if ok:
            self.set_current_page(page_num - 1)

    def zoom_in(self):
        center = QPointF(self.width() * 0.5, self.height() * 0.5)
        self._set_zoom(self._zoom * 1.25, center)

    def zoom_out(self):
        center = QPointF(self.width() * 0.5, self.height() * 0.5)
        self._set_zoom(self._zoom / 1.25, center)

    def reset_zoom(self):
        if abs(self._zoom - 1.0) < 1e-3 and self._pan_offset.manhattanLength() < 1.0:
            return
        self._zoom = 1.0
        self._pan_offset = QPointF(0.0, 0.0)
        self.btn_zoom_reset.setText("100%")
        self._apply_zoom_pan_to_entries()
        self._zoom_rerender_timer.start()
        self._show_hud("表示倍率: 100%")
        self.update()

    def _set_zoom(self, new_zoom: float, anchor_pt: Optional[QPointF] = None):
        clamped = max(1.0, min(4.0, float(new_zoom)))
        if abs(clamped - 1.0) < 0.03:
            clamped = 1.0
        if abs(clamped - self._zoom) < 1e-4:
            return

        old_zoom = self._zoom
        if clamped == 1.0:
            self._zoom = 1.0
            self._pan_offset = QPointF(0.0, 0.0)
        else:
            if anchor_pt is None:
                anchor_pt = QPointF(self.width() * 0.5, self.height() * 0.5)
            cx = self.width() * 0.5
            cy = self.height() * 0.5
            ratio = clamped / old_zoom
            rel_x = anchor_pt.x() - cx - self._pan_offset.x()
            rel_y = anchor_pt.y() - cy - self._pan_offset.y()
            self._pan_offset = QPointF(
                anchor_pt.x() - cx - rel_x * ratio,
                anchor_pt.y() - cy - rel_y * ratio,
            )
            self._zoom = clamped

        self.btn_zoom_reset.setText(f"{int(round(self._zoom * 100))}%")
        self._hover_screen_rect = None
        self._apply_zoom_pan_to_entries()
        self._zoom_rerender_timer.start()
        self._show_hud(f"🔍 表示倍率: {int(round(self._zoom * 100))}% （右ドラッグでスクロール / [0]で100%に戻す）")
        self.update()

    def _on_zoom_rerender_timeout(self):
        target_rz = min(2.5, max(1.0, self._zoom))
        if abs(target_rz - self._rendered_zoom) >= 0.15:
            self._update_current_spread_cache()
            self.update()

    def _apply_zoom_pan_to_entries(self):
        self._pan_offset = SpreadLayoutEngine.apply_zoom_pan_to_entries(
            self._cached_draw_items,
            float(self.width()),
            float(self.height()),
            self._zoom,
            self._pan_offset,
        )

    def set_current_page(self, page_idx: int):
        self._cancel_active_drawing()
        self._pan_offset = QPointF(0.0, 0.0)
        self._hover_screen_rect = None
        target = max(0, min(page_idx, len(self.doc) - 1))
        for idx, sp in enumerate(self.spreads):
            if target in sp.page_indices:
                self.current_spread_idx = idx
                break
        self._update_current_spread_cache()
        self._show_hud()
        self.update()

    def next_spread(self):
        self._cancel_active_drawing()
        if self.current_spread_idx < len(self.spreads) - 1:
            self.current_spread_idx += 1
            self._pan_offset = QPointF(0.0, 0.0)
            self._hover_screen_rect = None
            self._update_current_spread_cache()
            self._show_hud()
            self.update()

    def prev_spread(self):
        self._cancel_active_drawing()
        if self.current_spread_idx > 0:
            self.current_spread_idx -= 1
            self._pan_offset = QPointF(0.0, 0.0)
            self._hover_screen_rect = None
            self._update_current_spread_cache()
            self._show_hud()
            self.update()

    def _toggle_reading_direction(self):
        self._cancel_active_drawing()
        self.right_to_left = not self.right_to_left
        self._update_current_spread_cache()
        self._show_hud(f"綴じ方向(R): {'[右開き]' if self.right_to_left else '[左開き]'}")
        self.update()

    def _toggle_cover_mode(self):
        self._cancel_active_drawing()
        cur_page = self.spreads[self.current_spread_idx].page_indices[0] if self.spreads else 0
        self.cover_single = not self.cover_single
        if getattr(self, "btn_hud_cover", None) is not None:
            self.btn_hud_cover.setChecked(self.cover_single)
        self._build_spreads()
        for idx, sp in enumerate(self.spreads):
            if cur_page in sp.page_indices:
                self.current_spread_idx = idx
                break
        self._update_current_spread_cache()
        self._show_hud(f"表紙モード(C): {'[表紙単ページ]' if self.cover_single else '[1P目から見開き]'}")
        self.update()

    def _show_hud(self, extra_text=""):
        if not self.spreads or not hasattr(self, "hud_label"):
            return
        sp = self.spreads[self.current_spread_idx]
        p_nums = [idx + 1 for idx in sp.page_indices]
        p_str = f"P.{p_nums[0]}" if len(p_nums) == 1 else f"P.{p_nums[0]}-{p_nums[1]}"

        file_info = ""
        first_idx = sp.page_indices[0]
        for name, start, end in self.file_manifest:
            if start <= first_idx <= end:
                file_info = f" | {name}"
                break

        progress = int(round((p_nums[-1] / len(self.doc)) * 100))
        hud_text = f"{p_str} / {len(self.doc)} ({progress}%){file_info}"
        if extra_text:
            hud_text += f"  —  {extra_text}"

        self.hud_label.setText(hud_text)
        self._position_overlays()

        self.hud_opacity.setOpacity(1.0)
        self.hud_container.show()
        self.hud_timer.start()

    def _hide_hud(self):
        if hasattr(self, "hud_opacity"):
            cursor_pos = self.mapFromGlobal(self.cursor().pos())
            if self.hud_container.geometry().contains(cursor_pos):
                self.hud_timer.start()
                return
            self.hud_opacity.setOpacity(0.0)

    def _render_page_entry(self, page_idx: int, target_w: float, target_h: float) -> Dict[str, Any]:
        return SpreadLayoutEngine.render_page_entry(
            doc=self.doc,
            page_idx=page_idx,
            target_w=target_w,
            target_h=target_h,
            zoom=self._zoom,
            dpr=float(self.devicePixelRatioF()),
            ann_list=self.annotations.get(page_idx, []),
            font_family=self.font_family,
            draw_annot_callback=self.draw_annot_callback,
            redact_page_callback=self.redact_page_callback,
            fitz_lock=self.fitz_lock,
        )

    def _update_current_spread_cache(self, only_page_idx: Optional[int] = None):
        if not self.spreads or not self.doc or self.width() <= 0 or self.height() <= 0:
            self._cached_draw_items = []
            return

        sp = self.spreads[self.current_spread_idx]
        screen_w = float(self.width())
        screen_h = float(self.height())
        cur_size = (self.width(), self.height())
        can_reuse = (
            only_page_idx is not None
            and self._last_spread_size == cur_size
            and len(self._cached_draw_items) == len(sp.page_indices)
        )

        if not sp.is_spread:
            entry = self._render_page_entry(sp.page_indices[0], screen_w, screen_h)
            img = entry["img"]
            items: List[Dict[str, Any]] = []
            if not img.isNull():
                pw = img.width() / img.devicePixelRatio()
                ph = img.height() / img.devicePixelRatio()
                x = (screen_w - pw) / 2.0
                y = (screen_h - ph) / 2.0
                entry["base_rect"] = QRectF(x, y, pw, ph)
                entry["rect"] = QRectF(x, y, pw, ph)
                items.append(entry)
            self._cached_draw_items = items
        else:
            p_left_idx, p_right_idx = sp.page_indices[0], sp.page_indices[1]
            if self.right_to_left:
                p_left_idx, p_right_idx = p_right_idx, p_left_idx

            half_w = screen_w / 2.0
            if can_reuse and self._cached_draw_items[0]["page_idx"] == p_left_idx and p_left_idx != only_page_idx:
                entry_l = self._cached_draw_items[0]
            else:
                entry_l = self._render_page_entry(p_left_idx, half_w, screen_h)

            if can_reuse and self._cached_draw_items[1]["page_idx"] == p_right_idx and p_right_idx != only_page_idx:
                entry_r = self._cached_draw_items[1]
            else:
                entry_r = self._render_page_entry(p_right_idx, half_w, screen_h)

            img_l, img_r = entry_l["img"], entry_r["img"]
            items = []
            if not img_l.isNull() and not img_r.isNull():
                pw_l = img_l.width() / img_l.devicePixelRatio()
                ph_l = img_l.height() / img_l.devicePixelRatio()
                pw_r = img_r.width() / img_r.devicePixelRatio()
                ph_r = img_r.height() / img_r.devicePixelRatio()

                entry_l["base_rect"] = QRectF(half_w - pw_l, (screen_h - ph_l) / 2.0, pw_l, ph_l)
                entry_r["base_rect"] = QRectF(half_w, (screen_h - ph_r) / 2.0, pw_r, ph_r)
                entry_l["rect"] = QRectF(entry_l["base_rect"])
                entry_r["rect"] = QRectF(entry_r["base_rect"])
                items.append(entry_l)
                items.append(entry_r)
            self._cached_draw_items = items

        self._rendered_zoom = min(2.5, max(1.0, self._zoom))
        self._last_spread_size = cur_size
        self._apply_zoom_pan_to_entries()

    # ==========================================
    # 見開き画面 ⇔ PDF未回転MediaBox座標変換
    # ==========================================

    def _find_page_entry_at(self, pos: QPointF) -> Optional[Dict[str, Any]]:
        for entry in self._cached_draw_items:
            if entry["rect"].contains(pos):
                return entry
        return None

    _clamp_point_to_rect = staticmethod(SpreadCoordMapper.clamp_point_to_rect)
    _screen_to_raw_pdf = staticmethod(SpreadCoordMapper.screen_to_raw_pdf)
    _raw_pdf_to_screen = staticmethod(SpreadCoordMapper.raw_pdf_to_screen)
    _raw_rect_to_screen_rect = staticmethod(SpreadCoordMapper.raw_rect_to_screen_rect)

    # ==========================================
    # 注釈の追加・削除・テキスト編集・Undo/Redo
    # ==========================================

    def _record_and_apply_add(self, page_idx: int, annot: dict):
        self._has_edited = True
        self._last_edited_page_idx = page_idx
        if self.viewer is not None and "AddAnnotationCommand" in self.editor_callbacks:
            cmd_cls = self.editor_callbacks["AddAnnotationCommand"]
            self.annotations.setdefault(page_idx, []).append(annot)
            self.viewer.history.push(cmd_cls(page_idx, annot))
            self._sync_main_viewer_after_edit(page_idx)
        else:
            prev_list = copy.deepcopy(self.annotations.get(page_idx, []))
            self._local_undo_stack.append((page_idx, prev_list))
            self._local_redo_stack.clear()
            self.annotations.setdefault(page_idx, []).append(annot)

        self._update_current_spread_cache(only_page_idx=page_idx)
        self.update()

    def _record_and_apply_remove(self, page_idx: int, ann_idx: int):
        ann_list = self.annotations.get(page_idx, [])
        if not (0 <= ann_idx < len(ann_list)):
            return
        self._has_edited = True
        self._last_edited_page_idx = page_idx
        if self.viewer is not None and "RemoveAnnotationCommand" in self.editor_callbacks:
            before_annotations = copy.deepcopy(ann_list)
            removed = ann_list.pop(ann_idx)
            if "adjust_updated_after_on_remove" in self.editor_callbacks:
                self.editor_callbacks["adjust_updated_after_on_remove"](ann_list, ann_idx)
            cmd_cls = self.editor_callbacks["RemoveAnnotationCommand"]
            self.viewer.history.push(cmd_cls(page_idx, ann_idx, removed, before_annotations=before_annotations))
            self._sync_main_viewer_after_edit(page_idx)
        else:
            prev_list = copy.deepcopy(ann_list)
            self._local_undo_stack.append((page_idx, prev_list))
            self._local_redo_stack.clear()
            ann_list.pop(ann_idx)
            if "adjust_updated_after_on_remove" in self.editor_callbacks:
                self.editor_callbacks["adjust_updated_after_on_remove"](ann_list, ann_idx)

        self._update_current_spread_cache(only_page_idx=page_idx)
        self.update()

    def _record_and_apply_remove_multiple(self, page_idx: int, indices: List[int]):
        ann_list = self.annotations.get(page_idx, [])
        valid_indices = sorted({i for i in indices if 0 <= i < len(ann_list)})
        if not valid_indices:
            return
        if len(valid_indices) == 1:
            self._record_and_apply_remove(page_idx, valid_indices[0])
            return

        self._has_edited = True
        self._last_edited_page_idx = page_idx
        if self.viewer is not None and "RemoveMultipleAnnotationsCommand" in self.editor_callbacks:
            before_annotations = copy.deepcopy(ann_list)
            removed_pairs = [(i, copy.deepcopy(ann_list[i])) for i in valid_indices]
            for i in reversed(valid_indices):
                ann_list.pop(i)
                if "adjust_updated_after_on_remove" in self.editor_callbacks:
                    self.editor_callbacks["adjust_updated_after_on_remove"](ann_list, i)
            cmd_cls = self.editor_callbacks["RemoveMultipleAnnotationsCommand"]
            self.viewer.history.push(cmd_cls(page_idx, removed_pairs, before_annotations=before_annotations))
            self._sync_main_viewer_after_edit(page_idx)
        else:
            prev_list = copy.deepcopy(ann_list)
            self._local_undo_stack.append((page_idx, prev_list))
            self._local_redo_stack.clear()
            for i in reversed(valid_indices):
                ann_list.pop(i)
                if "adjust_updated_after_on_remove" in self.editor_callbacks:
                    self.editor_callbacks["adjust_updated_after_on_remove"](ann_list, i)

        self._update_current_spread_cache(only_page_idx=page_idx)
        self.update()

    def _record_and_apply_edit(self, page_idx: int, ann_idx: int, old_ann: dict, new_ann: dict):
        ann_list = self.annotations.get(page_idx, [])
        if not (0 <= ann_idx < len(ann_list)):
            return
        self._has_edited = True
        self._last_edited_page_idx = page_idx
        if self.viewer is not None and "EditAnnotationCommand" in self.editor_callbacks:
            ann_list[ann_idx] = new_ann
            cmd_cls = self.editor_callbacks["EditAnnotationCommand"]
            self.viewer.history.push(cmd_cls(page_idx, ann_idx, old_ann, new_ann))
            self._sync_main_viewer_after_edit(page_idx)
        else:
            prev_list = copy.deepcopy(ann_list)
            self._local_undo_stack.append((page_idx, prev_list))
            self._local_redo_stack.clear()
            ann_list[ann_idx] = new_ann

        self._update_current_spread_cache(only_page_idx=page_idx)
        self.update()

    def _sync_main_viewer_after_edit(self, page_idx: int):
        if self.viewer is None:
            return
        self.viewer.notify_pages_updated([page_idx])

    def _perform_undo(self):
        self._cancel_active_drawing()
        self._hover_screen_rect = None
        if self.viewer is not None:
            self.viewer.undo()
            self._has_edited = True
            target_p = self.viewer.current_page_idx
            self._last_edited_page_idx = target_p
            if self.spreads and target_p not in self.spreads[self.current_spread_idx].page_indices:
                self.set_current_page(target_p)
            else:
                self._update_current_spread_cache()
                self.update()
            self._show_hud("↶ 操作を元に戻しました")
        elif self._local_undo_stack:
            page_idx, prev_list = self._local_undo_stack.pop()
            cur_list = copy.deepcopy(self.annotations.get(page_idx, []))
            self._local_redo_stack.append((page_idx, cur_list))
            self.annotations[page_idx] = prev_list
            self._has_edited = bool(self._local_undo_stack)
            self._last_edited_page_idx = page_idx
            if self.spreads and page_idx not in self.spreads[self.current_spread_idx].page_indices:
                self.set_current_page(page_idx)
            else:
                self._update_current_spread_cache(only_page_idx=page_idx)
                self.update()
            self._show_hud("↶ 操作を元に戻しました")

    def _perform_redo(self):
        self._cancel_active_drawing()
        self._hover_screen_rect = None
        if self.viewer is not None:
            self.viewer.redo()
            self._has_edited = True
            target_p = self.viewer.current_page_idx
            self._last_edited_page_idx = target_p
            if self.spreads and target_p not in self.spreads[self.current_spread_idx].page_indices:
                self.set_current_page(target_p)
            else:
                self._update_current_spread_cache()
                self.update()
            self._show_hud("↷ 操作をやり直しました")
        elif self._local_redo_stack:
            page_idx, next_list = self._local_redo_stack.pop()
            cur_list = copy.deepcopy(self.annotations.get(page_idx, []))
            self._local_undo_stack.append((page_idx, cur_list))
            self.annotations[page_idx] = next_list
            self._has_edited = True
            self._last_edited_page_idx = page_idx
            if self.spreads and page_idx not in self.spreads[self.current_spread_idx].page_indices:
                self.set_current_page(page_idx)
            else:
                self._update_current_spread_cache(only_page_idx=page_idx)
                self.update()
            self._show_hud("↷ 操作をやり直しました")

    def _get_visible_draw_segments(self, ann_list: list, idx: int, ann: dict) -> List[List[Tuple[float, float]]]:
        return get_visible_draw_segments(ann_list, idx, ann)

    def _find_draw_at(self, raw_pt: fitz.Point, page_idx: int) -> Tuple[Optional[int], Optional[dict], Optional[fitz.Rect]]:
        ann_list = self.annotations.get(page_idx, [])
        idx, ann, segments = find_draw_annotation_at_point(ann_list, raw_pt, default_width=1.5)
        if idx is not None and ann is not None and segments:
            all_pts = [pt for seg in segments for pt in seg]
            xs = [p[0] for p in all_pts]
            ys = [p[1] for p in all_pts]
            w_pad = max(2.0, float(ann.get("width", 1.5)) * 0.5 + 2.0)
            bbox = fitz.Rect(min(xs) - w_pad, min(ys) - w_pad, max(xs) + w_pad, max(ys) + w_pad)
            return idx, ann, bbox
        return None, None, None

    def _find_draws_along_stroke(self, eraser_pts: List[Tuple[float, float]], page_idx: int) -> List[int]:
        ann_list = self.annotations.get(page_idx, [])
        matched = find_draws_along_eraser_stroke(ann_list, eraser_pts, default_width=1.5)
        return [idx for idx, _ in matched]

    def _find_highlight_at(self, raw_pt: fitz.Point, page_idx: int) -> Tuple[Optional[int], Optional[dict]]:
        ann_list = self.annotations.get(page_idx, [])
        return find_highlight_annotation_at_point(ann_list, raw_pt, padding=2.0)

    def _compute_text_raw_positions(
        self, screen_pos: QPointF, fontsize: float, entry: Dict[str, Any], ann_rot: Optional[int] = None
    ) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        return SpreadCoordMapper.compute_text_raw_positions(screen_pos, fontsize, entry, ann_rot)

    def _find_text_annot_at(self, raw_pt: fitz.Point, page_idx: int) -> Tuple[Optional[int], Optional[dict]]:
        ann_list = self.annotations.get(page_idx, [])
        return find_text_annotation_at_point(
            ann_list, raw_pt, estimate_text_bbox_in_pdf, min_pad=4.0
        )

    def _record_and_apply_replace_ann_list(self, page_idx: int, old_list: list, new_list: list):
        self._has_edited = True
        self._last_edited_page_idx = page_idx
        if self.viewer is not None and "ReplacePageAnnotationsCommand" in self.editor_callbacks:
            cmd_cls = self.editor_callbacks["ReplacePageAnnotationsCommand"]
            self.annotations[page_idx] = copy.deepcopy(new_list)
            self.viewer.history.push(cmd_cls(page_idx, old_list, new_list))
            self._sync_main_viewer_after_edit(page_idx)
        else:
            self._local_undo_stack.append((page_idx, copy.deepcopy(old_list)))
            self._local_redo_stack.clear()
            self.annotations[page_idx] = copy.deepcopy(new_list)

        self._update_current_spread_cache(only_page_idx=page_idx)
        self.update()

    def _erase_texts_in_pdf_rect(self, target_r: fitz.Rect, page_idx: int) -> int:
        ann_list = self.annotations.get(page_idx, [])
        tokens = self._get_tokens_for_page(page_idx)
        matched_ann_indices, matched_tokens = find_text_targets_in_rect(
            ann_list, tokens or [], target_r, estimate_text_bbox_in_pdf
        )
        if not matched_ann_indices and not matched_tokens:
            return 0

        old_list = copy.deepcopy(ann_list)
        new_list = copy.deepcopy(ann_list)
        count = 0

        for idx in reversed(matched_ann_indices):
            action, _, redact_ann = plan_text_annotation_removal(new_list, idx)
            if action == "replace_with_redact" and redact_ann is not None:
                new_list[idx] = redact_ann
                count += 1
            elif action == "pop":
                new_list.pop(idx)
                adjust_updated_after_on_remove(new_list, idx)
                count += 1

        for tok in matched_tokens:
            new_list.append({"type": "text_redact", "rect": tok["raw_rect"]})
            count += 1

        if count > 0:
            self._record_and_apply_replace_ann_list(page_idx, old_list, new_list)
        return count

    def _get_tokens_for_page(self, page_idx: int) -> List[dict]:
        get_tokens_fn = self.editor_callbacks.get("get_pdf_text_tokens")
        if get_tokens_fn is not None:
            try:
                return get_tokens_fn(page_idx=page_idx, doc=self.doc) or []
            except TypeError:
                try:
                    return get_tokens_fn(page_idx=page_idx) or []
                except Exception:
                    logger.debug("Failed to get PDF text tokens via callback", exc_info=True)
                    return []
        if self.viewer is not None and hasattr(self.viewer, "_get_page_pdf_text_tokens"):
            try:
                return self.viewer._get_page_pdf_text_tokens(page_idx=page_idx, doc=self.doc) or []
            except TypeError:
                return self.viewer._get_page_pdf_text_tokens(page_idx=page_idx) or []
        return []

    def _find_pdf_token_at(self, raw_pt: fitz.Point, page_idx: int) -> Optional[dict]:
        tokens = self._get_tokens_for_page(page_idx)
        ann_list = self.annotations.get(page_idx, [])
        return find_pdf_token_at_point(tokens, ann_list, raw_pt, padding=1.5)

    def _handle_text_click_on_spread(self, pos: QPointF, entry: Dict[str, Any]):
        dlg_cls = self.editor_callbacks.get("TextEditDeleteDialog")
        if dlg_cls is None:
            return
        page_idx = entry["page_idx"]
        page_rot = entry["page_rot"]
        raw_pt = self._screen_to_raw_pdf(pos, entry)

        # 1. 既存の追加テキスト注釈の編集・削除
        ann_idx, ann = self._find_text_annot_at(raw_pt, page_idx)
        if ann_idx is not None and ann is not None:
            old_ann = copy.deepcopy(ann)
            old_fs = float(old_ann.get("fontsize", self.current_font_size))
            old_col = tuple(old_ann.get("color", (0.0, 0.0, 0.0)))
            dlg = dlg_cls(
                old_ann.get("text", ""),
                old_fs,
                old_col,
                is_pdf_origin=bool(old_ann.get("redact_rect")),
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            if dlg.result_action == dlg_cls.ACTION_DELETE or not dlg.get_clean_text():
                if old_ann.get("redact_rect"):
                    new_ann = {"type": "text_redact", "rect": old_ann["redact_rect"]}
                    self._record_and_apply_edit(page_idx, ann_idx, old_ann, new_ann)
                else:
                    self._record_and_apply_remove(page_idx, ann_idx)
                self._show_hud("🗑 文字を削除しました")
                return
            if dlg.result_action == dlg_cls.ACTION_UPDATE:
                new_text = dlg.get_clean_text()
                new_fs = dlg.get_fontsize()
                new_col = dlg.get_color()
                self.current_font_size = new_fs
                self.current_color = new_col
                self._pen_eraser_active = False
                self._sync_color_combo(new_col)
                self.spin_fontsize.blockSignals(True)
                self.spin_fontsize.setValue(new_fs)
                self.spin_fontsize.blockSignals(False)
                if self.viewer is not None:
                    self.viewer.set_font_size(new_fs)
                    self.viewer.set_current_color(new_col)

                new_ann = copy.deepcopy(old_ann)
                new_ann["text"] = new_text
                new_ann["fontsize"] = new_fs
                new_ann["color"] = new_col
                new_ann["updated_after"] = len(self.annotations.get(page_idx, []))
                if abs(new_fs - old_fs) > 1e-4 and "baseline_pos" in old_ann and "pos" in old_ann:
                    new_ann["pos"] = compute_scaled_baseline_top_pos(
                        old_ann["pos"], old_ann["baseline_pos"], new_fs
                    )
                self._record_and_apply_edit(page_idx, ann_idx, old_ann, new_ann)
                self._show_hud("✔ 文字を更新しました")
            return

        # 2. 元のPDF内の文字トークンの編集・削除
        tok = self._find_pdf_token_at(raw_pt, page_idx)
        if tok is not None:
            dlg = dlg_cls(
                tok["text"],
                tok["fontsize"],
                tok["color"],
                is_pdf_origin=True,
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            if dlg.result_action == dlg_cls.ACTION_DELETE or not dlg.get_clean_text():
                annot = {"type": "text_redact", "rect": tok["raw_rect"]}
                self._record_and_apply_add(page_idx, annot)
                self._show_hud("🗑 PDF内の文字を消去しました")
                return
            if dlg.result_action == dlg_cls.ACTION_UPDATE:
                new_text = dlg.get_clean_text()
                new_fs = dlg.get_fontsize()
                new_col = dlg.get_color()
                self.current_font_size = new_fs
                self.current_color = new_col
                self._pen_eraser_active = False
                self._sync_color_combo(new_col)
                self.spin_fontsize.blockSignals(True)
                self.spin_fontsize.setValue(new_fs)
                self.spin_fontsize.blockSignals(False)
                if self.viewer is not None:
                    self.viewer.set_font_size(new_fs)
                    self.viewer.set_current_color(new_col)

                new_pos = tok["pos"]
                old_tok_fs = float(tok.get("fontsize", new_fs))
                if abs(new_fs - old_tok_fs) > 1e-4 and "baseline_pos" in tok and "pos" in tok:
                    new_pos = compute_scaled_baseline_top_pos(
                        tok["pos"], tok["baseline_pos"], new_fs
                    )

                annot = build_pdf_token_replacement_annotation(
                    tok,
                    new_text=new_text,
                    new_fontsize=new_fs,
                    new_color=new_col,
                    new_pos=new_pos,
                    updated_after=len(self.annotations.get(page_idx, [])),
                )
                self._record_and_apply_add(page_idx, annot)
                self._show_hud("✔ PDF内の文字を書き換えました")
            return

        # 3. 空白位置への新規テキスト追加
        dlg = dlg_cls(
            "",
            self.current_font_size,
            self.current_color,
            is_pdf_origin=False,
            is_new=True,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_action != dlg_cls.ACTION_UPDATE:
            return
        clean_text = dlg.get_clean_text()
        if not clean_text:
            return
        fs = dlg.get_fontsize()
        col = dlg.get_color()
        self.current_font_size = fs
        self.current_color = col
        self._pen_eraser_active = False
        self._sync_color_combo(col)
        self.spin_fontsize.blockSignals(True)
        self.spin_fontsize.setValue(fs)
        self.spin_fontsize.blockSignals(False)
        if self.viewer is not None:
            self.viewer.set_font_size(fs)
            self.viewer.set_current_color(col)

        pdf_pos, pdf_base_pos = self._compute_text_raw_positions(pos, fs, entry, page_rot)
        annot = build_text_annotation(
            text=clean_text,
            pos=pdf_pos,
            baseline_pos=pdf_base_pos,
            fontsize=fs,
            color=col,
            rotation=page_rot,
            updated_after=len(self.annotations.get(page_idx, [])),
        )
        self._record_and_apply_add(page_idx, annot)
        self._show_hud("✔ 新規テキストを追加しました")

    # ==========================================
    # イベントハンドラ（描画・マウス・キー）
    # ==========================================

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_current_spread_cache()
        self._position_overlays()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(13, 13, 13))
        if not self._cached_draw_items:
            painter.end()
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        for entry in self._cached_draw_items:
            painter.drawImage(entry["rect"], entry["img"])

        # テキストドラッグ移動中のプレビュー枠
        if self._dragging_text_info is not None and self._dragging_text_info.get("moved"):
            d_rect: QRectF = self._dragging_text_info["current_screen_rect"]
            painter.setPen(QPen(QColor(0, 120, 230, 240), 2.2, Qt.PenStyle.DashLine))
            painter.setBrush(QBrush(QColor(0, 140, 255, 40)))
            painter.drawRect(d_rect)
        # ホバー対象（マーカー・ペン消去対象や編集対象文字）のハイライト枠
        elif self._hover_screen_rect is not None and not self._drawing:
            painter.setPen(QPen(self._hover_screen_color, 2.2, Qt.PenStyle.DashLine))
            fill_col = QColor(self._hover_screen_color)
            fill_col.setAlpha(45)
            painter.setBrush(QBrush(fill_col))
            painter.drawRect(self._hover_screen_rect)

        # ドラッグ中のリアルタイムプレビュー描画
        if self._drawing and self._active_page_info is not None:
            r_scr: QRectF = self._active_page_info["rect"]
            pr: fitz.Rect = self._active_page_info["page_rect"]
            scale_px = r_scr.width() / max(1.0, pr.width)

            if self.mode == "highlight":
                qrect = QRectF(self._drag_start_pt, self._drag_current_pt).normalized()
                alpha = int(round(HIGHLIGHT_OPACITY * 255))
                painter.setPen(QPen(_rgb_to_qcolor(self.current_color, 210), 1.5, Qt.PenStyle.DashLine))
                painter.setBrush(QBrush(_rgb_to_qcolor(self.current_color, alpha)))
                painter.drawRect(qrect)
            elif self.mode in ("highlight_erase", "text_erase"):
                qrect = QRectF(self._drag_start_pt, self._drag_current_pt).normalized()
                painter.setPen(QPen(QColor(220, 20, 20, 230), 2.0, Qt.PenStyle.DashLine))
                painter.setBrush(QBrush(QColor(255, 80, 80, 45)))
                painter.drawRect(qrect)
            elif self.mode == "box_erase":
                qrect = QRectF(self._drag_start_pt, self._drag_current_pt).normalized()
                painter.setPen(QPen(QColor(220, 0, 0, 200), 1.5, Qt.PenStyle.DashLine))
                painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
                painter.drawRect(qrect)
            elif self.mode in ("draw", "erase") and len(self._stroke_screen_pts) >= 2:
                path = QPainterPath()
                path.moveTo(self._stroke_screen_pts[0])
                for pt in self._stroke_screen_pts[1:]:
                    path.lineTo(pt)
                if self.mode == "draw" and self._pen_eraser_active:
                    pen = QPen(QColor(220, 30, 30, 220), max(5.0, 10.0 * scale_px), Qt.PenStyle.DashLine)
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                elif self.mode == "draw":
                    pen = QPen(_rgb_to_qcolor(self.current_color, 255), max(1.5, self.current_draw_width * scale_px))
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                else:
                    pen = QPen(QColor(255, 255, 255, 235), max(3.0, ERASE_WIDTH * scale_px))
                    pen.setCapStyle(Qt.PenCapStyle.SquareCap)
                    pen.setJoinStyle(Qt.PenJoinStyle.BevelJoin)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)

        painter.end()

    def mousePressEvent(self, event: QMouseEvent):
        pos = event.position()
        if self._is_pos_on_overlay_ui(pos):
            return

        # 中ボタン または Space+左ドラッグ時はパン移動開始
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and self._space_pressed
        ):
            self._panning = True
            self._pan_button = event.button()
            self._pan_start_pt = pos
            self._pan_start_offset = QPointF(self._pan_offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        # 閲覧モード時はクリックでページめくり（拡大時はドラッグでパン移動も可）
        if self.mode == "view":
            if event.button() == Qt.MouseButton.LeftButton:
                if self._zoom > 1.01:
                    self._panning = True
                    self._pan_button = Qt.MouseButton.LeftButton
                    self._pan_start_pt = pos
                    self._pan_start_offset = QPointF(self._pan_offset)
                    self._view_press_pt = pos
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                else:
                    self.next_spread()
            elif event.button() == Qt.MouseButton.RightButton:
                if self._zoom > 1.01:
                    self._panning = True
                    self._pan_button = Qt.MouseButton.RightButton
                    self._pan_start_pt = pos
                    self._pan_start_offset = QPointF(self._pan_offset)
                    self._right_press_pt = pos
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                else:
                    self.prev_spread()
            return

        entry = self._find_page_entry_at(pos)

        # 右ボタン：対象物の上なら即座消去、それ以外（または拡大中ドラッグ）ならパン移動
        if event.button() == Qt.MouseButton.RightButton:
            if entry is not None:
                page_idx = entry["page_idx"]
                raw_pt = self._screen_to_raw_pdf(pos, entry)
                if self.mode == "draw":
                    d_idx, _, _ = self._find_draw_at(raw_pt, page_idx)
                    if d_idx is not None:
                        self._record_and_apply_remove(page_idx, d_idx)
                        self._show_hud("🧽 ペンで描いた線を1件消去しました")
                        return
                elif self.mode in ("highlight", "highlight_erase"):
                    hl_idx, _ = self._find_highlight_at(raw_pt, page_idx)
                    if hl_idx is not None:
                        self._record_and_apply_remove(page_idx, hl_idx)
                        self._show_hud("🖍✕ マーカーを1件消去しました")
                        return
                elif self.mode in ("type", "text_erase"):
                    ann_idx, old_ann = self._find_text_annot_at(raw_pt, page_idx)
                    if ann_idx is not None and old_ann is not None:
                        if old_ann.get("redact_rect"):
                            self._record_and_apply_edit(
                                page_idx, ann_idx, old_ann, {"type": "text_redact", "rect": old_ann["redact_rect"]}
                            )
                        else:
                            self._record_and_apply_remove(page_idx, ann_idx)
                        self._show_hud("🗑 文字を削除しました")
                        return
                    tok = self._find_pdf_token_at(raw_pt, page_idx)
                    if tok is not None:
                        self._record_and_apply_add(page_idx, {"type": "text_redact", "rect": tok["raw_rect"]})
                        self._show_hud("🗑 PDF内の文字を消去しました")
                        return
            if self._zoom > 1.01:
                self._panning = True
                self._pan_button = Qt.MouseButton.RightButton
                self._pan_start_pt = pos
                self._pan_start_offset = QPointF(self._pan_offset)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if event.button() != Qt.MouseButton.LeftButton or entry is None:
            return

        page_idx = entry["page_idx"]
        raw_pt = self._screen_to_raw_pdf(pos, entry)

        # Shift+左クリックでペン線またはマーカー消去
        if self.mode == "draw" and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            d_idx, _, _ = self._find_draw_at(raw_pt, page_idx)
            if d_idx is not None:
                self._record_and_apply_remove(page_idx, d_idx)
                self._show_hud("🧽 ペンで描いた線を1件消去しました")
                return

        if self.mode == "highlight" and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            hl_idx, _ = self._find_highlight_at(raw_pt, page_idx)
            if hl_idx is not None:
                self._record_and_apply_remove(page_idx, hl_idx)
                self._show_hud("🖍✕ マーカーを1件消去しました")
                return

        if self.mode == "text_erase":
            ann_idx, old_ann = self._find_text_annot_at(raw_pt, page_idx)
            if ann_idx is not None and old_ann is not None:
                if old_ann.get("redact_rect"):
                    self._record_and_apply_edit(
                        page_idx, ann_idx, old_ann, {"type": "text_redact", "rect": old_ann["redact_rect"]}
                    )
                else:
                    self._record_and_apply_remove(page_idx, ann_idx)
                self._hover_screen_rect = None
                self._show_hud("🧹 追加した文字を消去しました")
                return
            tok = self._find_pdf_token_at(raw_pt, page_idx)
            if tok is not None:
                self._record_and_apply_add(page_idx, {"type": "text_redact", "rect": tok["raw_rect"]})
                self._hover_screen_rect = None
                self._show_hud("🧹 PDF内の文字を消去しました")
                return
            # クリック位置の直下に文字がない場合はドラッグ範囲選択を開始
            self._drawing = True
            self._active_page_info = entry
            self._drag_start_pt = pos
            self._drag_current_pt = pos
            self.update()
            return

        if self.mode == "type":
            ann_idx, t_ann = self._find_text_annot_at(raw_pt, page_idx)
            if ann_idx is not None and t_ann is not None:
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    if t_ann.get("redact_rect"):
                        self._record_and_apply_edit(
                            page_idx, ann_idx, t_ann, {"type": "text_redact", "rect": t_ann["redact_rect"]}
                        )
                    else:
                        self._record_and_apply_remove(page_idx, ann_idx)
                    self._show_hud("🗑 文字を削除しました")
                    return
                if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                    old_ann = copy.deepcopy(t_ann)
                    new_ann = copy.deepcopy(t_ann)
                    new_ann["color"] = self.current_color
                    new_ann["updated_after"] = len(self.annotations.get(page_idx, []))
                    self._record_and_apply_edit(page_idx, ann_idx, old_ann, new_ann)
                    self._show_hud("🎨 文字色を変更しました")
                    return
                est_fn = self.editor_callbacks.get("estimate_text_bbox_in_pdf")
                scr_rect = (
                    self._raw_rect_to_screen_rect(est_fn(t_ann), entry)
                    if est_fn is not None
                    else QRectF(pos.x() - 30, pos.y() - 12, 60, 24)
                )
                self._dragging_text_info = {
                    "page_idx": page_idx,
                    "ann_idx": ann_idx,
                    "ann": copy.deepcopy(t_ann),
                    "entry": entry,
                    "start_pt": pos,
                    "orig_screen_rect": scr_rect,
                    "current_screen_rect": QRectF(scr_rect),
                    "moved": False,
                }
                return

            tok = self._find_pdf_token_at(raw_pt, page_idx)
            if tok is not None:
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._record_and_apply_add(page_idx, {"type": "text_redact", "rect": tok["raw_rect"]})
                    self._show_hud("🗑 PDF内の文字を消去しました")
                    return
                if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                    annot = {
                        "type": "text",
                        "text": tok["text"],
                        "pos": tok["pos"],
                        "baseline_pos": tok["baseline_pos"],
                        "fontsize": tok["fontsize"],
                        "color": self.current_color,
                        "rotation": tok["rotation"],
                        "redact_rect": tok["raw_rect"],
                        "updated_after": len(self.annotations.get(page_idx, [])),
                    }
                    self._record_and_apply_add(page_idx, annot)
                    self._show_hud("🎨 PDF内の文字色を変更しました")
                    return

            QTimer.singleShot(0, lambda p=QPointF(pos), e=entry: self._handle_text_click_on_spread(p, e))
            return

        if self.mode in ("highlight", "highlight_erase", "box_erase"):
            self._drawing = True
            self._active_page_info = entry
            self._drag_start_pt = pos
            self._drag_current_pt = pos
            self.update()
            return

        if self.mode in ("draw", "erase"):
            self._drawing = True
            self._active_page_info = entry
            self._stroke_screen_pts = [pos, pos]
            self._stroke_pdf_pts = [(raw_pt.x, raw_pt.y)]
            self.update()
            return

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = event.position()

        # 右上エリアにカーソルを持ってきたら「✕」閉じるボタンを即座に表示
        in_top_right = pos.x() >= self.width() - 170 and pos.y() <= 110
        in_top_edge = pos.y() <= 55

        if in_top_right:
            self._position_overlays()
            self.close_btn.show()
            self.close_btn.raise_()

        if in_top_edge:
            self._position_overlays()
            self.edit_bar.show()
            self.close_btn.show()

        if not self._toolbar_pinned and self.mode == "view":
            self.close_hide_timer.start()

        self._show_hud()

        if self._panning:
            delta = pos - self._pan_start_pt
            self._pan_offset = self._pan_start_offset + delta
            self._apply_zoom_pan_to_entries()
            self.update()
            return

        if self._dragging_text_info is not None:
            start_pt: QPointF = self._dragging_text_info["start_pt"]
            if (pos - start_pt).manhattanLength() >= 4.0 or self._dragging_text_info["moved"]:
                self._dragging_text_info["moved"] = True
                delta = pos - start_pt
                orig_r: QRectF = self._dragging_text_info["orig_screen_rect"]
                self._dragging_text_info["current_screen_rect"] = orig_r.translated(delta)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
            return

        if self._drawing and self._active_page_info is not None:
            clamped = self._clamp_point_to_rect(pos, self._active_page_info["rect"])
            if self.mode in ("highlight", "highlight_erase", "box_erase", "text_erase"):
                self._drag_current_pt = clamped
                self.update()
            elif self.mode in ("draw", "erase"):
                raw_pt = self._screen_to_raw_pdf(clamped, self._active_page_info)
                if self._stroke_pdf_pts:
                    lx, ly = self._stroke_pdf_pts[-1]
                    if math.hypot(raw_pt.x - lx, raw_pt.y - ly) >= 0.35:
                        self._stroke_pdf_pts.append((raw_pt.x, raw_pt.y))
                        self._stroke_screen_pts.append(clamped)
                        self.update()
            return

        # 編集モード時のホバーガイド更新
        prev_hover = self._hover_screen_rect
        self._hover_screen_rect = None
        shift_held = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if (
            self.mode in ("highlight_erase", "type", "text_erase")
            or (self.mode == "highlight" and shift_held)
            or (self.mode == "draw" and (self._pen_eraser_active or shift_held))
        ):
            entry = self._find_page_entry_at(pos)
            if entry is not None:
                page_idx = entry["page_idx"]
                raw_pt = self._screen_to_raw_pdf(pos, entry)
                if self.mode == "draw":
                    _, _, d_bbox = self._find_draw_at(raw_pt, page_idx)
                    if d_bbox is not None:
                        self._hover_screen_rect = self._raw_rect_to_screen_rect(d_bbox, entry).adjusted(-3, -3, 3, 3)
                        self._hover_screen_color = QColor(220, 20, 20, 240)
                elif self.mode in ("highlight_erase", "highlight"):
                    _, hl_ann = self._find_highlight_at(raw_pt, page_idx)
                    if hl_ann is not None:
                        self._hover_screen_rect = self._raw_rect_to_screen_rect(hl_ann["rect"], entry).adjusted(-2, -2, 2, 2)
                        self._hover_screen_color = QColor(220, 20, 20, 240)
                elif self.mode in ("type", "text_erase"):
                    _, t_ann = self._find_text_annot_at(raw_pt, page_idx)
                    est_fn = self.editor_callbacks.get("estimate_text_bbox_in_pdf")
                    if t_ann is not None and est_fn is not None:
                        bbox = est_fn(t_ann)
                        self._hover_screen_rect = self._raw_rect_to_screen_rect(bbox, entry).adjusted(-2, -2, 2, 2)
                        self._hover_screen_color = QColor(220, 20, 20, 240) if self.mode == "text_erase" else QColor(0, 120, 230, 240)
                    else:
                        tok = self._find_pdf_token_at(raw_pt, page_idx)
                        if tok is not None:
                            self._hover_screen_rect = self._raw_rect_to_screen_rect(tok["raw_rect"], entry).adjusted(-2, -2, 2, 2)
                            self._hover_screen_color = QColor(220, 20, 20, 240) if self.mode == "text_erase" else QColor(0, 180, 90, 240)

        if prev_hover != self._hover_screen_rect:
            self.update()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        pos = event.position()

        if self._panning and (event.button() == self._pan_button or self._pan_button is None):
            was_view_left = self._view_press_pt is not None and (pos - self._view_press_pt).manhattanLength() < 5.0
            was_view_right = self._right_press_pt is not None and (pos - self._right_press_pt).manhattanLength() < 5.0
            self._panning = False
            self._pan_button = None
            self._view_press_pt = None
            self._right_press_pt = None
            if self._space_pressed:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            elif self.mode == "view":
                self.setCursor(Qt.CursorShape.ArrowCursor)
                if was_view_left:
                    self.next_spread()
                elif was_view_right:
                    self.prev_spread()
            elif self.mode in ("highlight", "draw", "box_erase", "type"):
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            return

        if self._dragging_text_info is not None and event.button() == Qt.MouseButton.LeftButton:
            info = self._dragging_text_info
            self._dragging_text_info = None
            self.setCursor(Qt.CursorShape.CrossCursor)
            page_idx = info["page_idx"]
            ann_idx = info["ann_idx"]
            old_ann = info["ann"]
            entry = info["entry"]
            if info["moved"]:
                delta = pos - info["start_pt"]
                old_top_scr = self._raw_pdf_to_screen(old_ann["pos"], entry)
                new_top_raw = self._screen_to_raw_pdf(old_top_scr + delta, entry)
                new_ann = copy.deepcopy(old_ann)
                new_ann["pos"] = (new_top_raw.x, new_top_raw.y)
                if "baseline_pos" in old_ann:
                    old_base_scr = self._raw_pdf_to_screen(old_ann["baseline_pos"], entry)
                    new_base_raw = self._screen_to_raw_pdf(old_base_scr + delta, entry)
                    new_ann["baseline_pos"] = (new_base_raw.x, new_base_raw.y)
                new_ann["updated_after"] = len(self.annotations.get(page_idx, []))
                self._record_and_apply_edit(page_idx, ann_idx, old_ann, new_ann)
                self._show_hud("✔ 文字の位置を移動しました")
            else:
                QTimer.singleShot(0, lambda p=QPointF(info["start_pt"]), e=entry: self._handle_text_click_on_spread(p, e))
            return

        if not self._drawing or event.button() != Qt.MouseButton.LeftButton or self._active_page_info is None:
            super().mouseReleaseEvent(event)
            return

        entry = self._active_page_info
        page_idx = entry["page_idx"]
        clamped = self._clamp_point_to_rect(pos, entry["rect"])
        self._drawing = False
        self._active_page_info = None

        if self.mode == "highlight":
            qrect = QRectF(self._drag_start_pt, clamped).normalized()
            if qrect.width() > 3 and qrect.height() > 3:
                p0 = self._screen_to_raw_pdf(qrect.topLeft(), entry)
                p1 = self._screen_to_raw_pdf(qrect.bottomRight(), entry)
                r = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
                r.normalize()
                annot = {
                    "type": "highlight",
                    "rect": (r.x0, r.y0, r.x1, r.y1),
                    "color": self.current_color,
                    "opacity": HIGHLIGHT_OPACITY,
                }
                self._record_and_apply_add(page_idx, annot)
            else:
                self.update()

        elif self.mode == "highlight_erase":
            qrect = QRectF(self._drag_start_pt, clamped).normalized()
            if qrect.width() <= 5 and qrect.height() <= 5:
                raw_pt = self._screen_to_raw_pdf(self._drag_start_pt, entry)
                hl_idx, _ = self._find_highlight_at(raw_pt, page_idx)
                if hl_idx is not None:
                    self._record_and_apply_remove(page_idx, hl_idx)
                    self._show_hud("🖍✕ マーカーを1件消去しました")
                else:
                    self.update()
            else:
                p0 = self._screen_to_raw_pdf(qrect.topLeft(), entry)
                p1 = self._screen_to_raw_pdf(qrect.bottomRight(), entry)
                target_r = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
                target_r.normalize()
                ann_list = self.annotations.get(page_idx, [])
                matched_indices = [
                    idx for idx, _ in find_highlights_in_rect(ann_list, target_r)
                ]
                if matched_indices:
                    self._record_and_apply_remove_multiple(page_idx, matched_indices)
                    self._show_hud(f"🖍✕ マーカーを {len(matched_indices)} 件消去しました")
                else:
                    self.update()

        elif self.mode == "text_erase":
            qrect = QRectF(self._drag_start_pt, clamped).normalized()
            if qrect.width() > 4 and qrect.height() > 4:
                p0 = self._screen_to_raw_pdf(qrect.topLeft(), entry)
                p1 = self._screen_to_raw_pdf(qrect.bottomRight(), entry)
                target_r = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
                target_r.normalize()
                erased_cnt = self._erase_texts_in_pdf_rect(target_r, page_idx)
                if erased_cnt > 0:
                    self._hover_screen_rect = None
                    self._show_hud(f"🧹 範囲内の文字を {erased_cnt} 件消去しました")
                else:
                    self.update()
            else:
                self.update()

        elif self.mode == "box_erase":
            qrect = QRectF(self._drag_start_pt, clamped).normalized()
            if qrect.width() > 3 and qrect.height() > 3:
                p0 = self._screen_to_raw_pdf(qrect.topLeft(), entry)
                p1 = self._screen_to_raw_pdf(qrect.bottomRight(), entry)
                r = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
                r.normalize()
                annot = {
                    "type": "box_erase",
                    "rect": (r.x0, r.y0, r.x1, r.y1),
                }
                self._record_and_apply_add(page_idx, annot)
            else:
                self.update()

        elif self.mode in ("draw", "erase"):
            end_raw = self._screen_to_raw_pdf(clamped, entry)
            if self._stroke_pdf_pts:
                lx, ly = self._stroke_pdf_pts[-1]
                if math.hypot(end_raw.x - lx, end_raw.y - ly) >= 0.1:
                    self._stroke_pdf_pts.append((end_raw.x, end_raw.y))
            if len(self._stroke_pdf_pts) == 1:
                self._stroke_pdf_pts.append(self._stroke_pdf_pts[0])

            stroke_pts = list(self._stroke_pdf_pts)
            self._stroke_pdf_pts.clear()
            self._stroke_screen_pts.clear()

            if self.mode == "draw" and self._pen_eraser_active:
                matched_indices = self._find_draws_along_stroke(stroke_pts, page_idx)
                if matched_indices:
                    self._hover_screen_rect = None
                    self._record_and_apply_remove_multiple(page_idx, matched_indices)
                    self._show_hud(f"🧽 ペンで描いた線を {len(matched_indices)} 件消去しました")
                else:
                    self.update()
                return

            if self.mode == "draw":
                annot = {
                    "type": "draw",
                    "points": stroke_pts,
                    "color": self.current_color,
                    "width": self.current_draw_width,
                }
            else:
                annot = {
                    "type": "erase",
                    "points": stroke_pts,
                }
            self._record_and_apply_add(page_idx, annot)

    def wheelEvent(self, event: QWheelEvent):
        if self._drawing or self._is_pos_on_overlay_ui(event.position()):
            return
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if delta > 0:
                self._set_zoom(self._zoom * 1.2, event.position())
            elif delta < 0:
                self._set_zoom(self._zoom / 1.2, event.position())
            return
        if delta < 0:
            self.next_spread()
        elif delta > 0:
            self.prev_spread()

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        mods = event.modifiers()

        if key == Qt.Key.Key_Space and (self._zoom > 1.01 or self.mode != "view"):
            if not event.isAutoRepeat():
                self._space_pressed = True
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            return

        if mods & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_Z:
                self._perform_undo()
                return
            if key == Qt.Key.Key_Y:
                self._perform_redo()
                return
            if key == Qt.Key.Key_S:
                self._trigger_save()
                return
            if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Semicolon):
                self.zoom_in()
                return
            if key == Qt.Key.Key_Minus:
                self.zoom_out()
                return
            if key == Qt.Key.Key_0:
                self.reset_zoom()
                return

        if key == Qt.Key.Key_Escape and (self._drawing or self._dragging_text_info is not None):
            self._cancel_active_drawing()
            self._show_hud("操作をキャンセルしました")
            return

        if key in (Qt.Key.Key_Escape, Qt.Key.Key_F11, Qt.Key.Key_F5):
            if key == Qt.Key.Key_Escape and self.mode != "view":
                self.set_edit_mode("view")
                return
            if key == Qt.Key.Key_Escape and self._zoom > 1.01:
                self.reset_zoom()
                return
            self.close()
        elif key == Qt.Key.Key_E:
            self._toggle_toolbar_pinned()
        elif key == Qt.Key.Key_G:
            self._prompt_page_jump()
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_in()
        elif key == Qt.Key.Key_Minus:
            self.zoom_out()
        elif key == Qt.Key.Key_0:
            self.reset_zoom()
        elif key == Qt.Key.Key_M:
            self.set_edit_mode("highlight" if self.mode != "highlight" else "view")
        elif key == Qt.Key.Key_P:
            self.set_edit_mode("draw" if self.mode != "draw" else "view")
        elif key == Qt.Key.Key_T:
            self.set_edit_mode("type" if self.mode != "type" else "view")
        elif key == Qt.Key.Key_V:
            self.set_edit_mode("view")
        elif key == Qt.Key.Key_Right:
            if self.right_to_left:
                self.prev_spread()
            else:
                self.next_spread()
        elif key == Qt.Key.Key_Left:
            if self.right_to_left:
                self.next_spread()
            else:
                self.prev_spread()
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_PageDown, Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.next_spread()
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_PageUp, Qt.Key.Key_Backspace):
            self.prev_spread()
        elif key == Qt.Key.Key_R:
            self._toggle_reading_direction()
        elif key == Qt.Key.Key_C:
            self._toggle_cover_mode()
        elif key == Qt.Key.Key_Home:
            if self.spreads:
                self._cancel_active_drawing()
                self._pan_offset = QPointF(0.0, 0.0)
                self._hover_screen_rect = None
                self.current_spread_idx = 0
                self._update_current_spread_cache()
                self._show_hud()
                self.update()
        elif key == Qt.Key.Key_End:
            if self.spreads:
                self._cancel_active_drawing()
                self._pan_offset = QPointF(0.0, 0.0)
                self._hover_screen_rect = None
                self.current_spread_idx = len(self.spreads) - 1
                self._update_current_spread_cache()
                self._show_hud()
                self.update()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat() and self._space_pressed:
            self._space_pressed = False
            self._panning = False
            self._pan_button = None
            if self.mode == "view":
                self.setCursor(Qt.CursorShape.ArrowCursor)
            elif self.mode in ("highlight", "draw", "box_erase", "type"):
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            return
        super().keyReleaseEvent(event)

    def leaveEvent(self, event):
        self._hover_screen_rect = None
        if not self._toolbar_pinned and self.mode == "view":
            self.close_hide_timer.start()
        self.update()
        super().leaveEvent(event)

    def closeEvent(self, event):
        self._cancel_active_drawing()
        if getattr(self, "own_doc", False) and self._has_edited and self.doc is not None:
            reply = QMessageBox.question(
                self,
                "未保存の編集",
                "見開き画面で追加・編集した内容がまだ保存されていません。\n保存してから閉じますか？",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.StandardButton.Save:
                if not self._trigger_save():
                    event.ignore()
                    return

        self.closed.emit()
        if getattr(self, "own_doc", False) and self.doc is not None:
            try:
                if self.fitz_lock is not None:
                    with self.fitz_lock:
                        self.doc.close()
                else:
                    self.doc.close()
            except Exception:
                logger.debug("Spread reader operation skipped on exception", exc_info=True)
            self.doc = None
        super().closeEvent(event)
