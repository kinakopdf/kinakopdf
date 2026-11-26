# -*- coding: utf-8 -*-
"""
スキャン用紙の色抜き（カラー網掛け・色枠線・点々消去）＆ NDLOCR-Lite プレビューダイアログ
（QThreadPool による完全非同期ワーカースレッド実行・高精細プレビュー・全画面表示対応版）
"""
import threading
from typing import Dict, List, Optional

import fitz
from PyQt6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core.commands import ReplacePagesContentCommand
from core.geometry import FITZ_LOCK
from core.ocr_cleaner import (
    apply_cleaned_image_and_ocr_to_page,
    clean_scanned_qimage,
    render_page_to_qimage,
)
from core.ocr_engine import run_ocr_batch_on_qimages, run_ocr_on_qimage

PREVIEW_MAX_RENDER_PIXELS = 36_000_000


class _OcrTestSignals(QObject):
    # chunks, err_msg, cancelled
    finished = pyqtSignal(list, str, bool)


class _OcrTestWorker(QRunnable):
    """プレビュー画像に対する単ページOCRテストを非同期実行するワーカー"""

    def __init__(
        self,
        cleaned_img: QImage,
        fix_numeric: bool,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.cleaned_img = cleaned_img.copy()
        self.fix_numeric = fix_numeric
        self.cancel_event = cancel_event
        self.signals = _OcrTestSignals()

    def run(self):
        try:
            chunks = run_ocr_on_qimage(
                self.cleaned_img,
                fix_numeric=self.fix_numeric,
                cancel_event=self.cancel_event,
            )
            if self.cancel_event.is_set():
                self.signals.finished.emit([], "", True)
                return
            self.signals.finished.emit(chunks, "", False)
        except Exception as e:
            if self.cancel_event.is_set():
                self.signals.finished.emit([], "", True)
            else:
                self.signals.finished.emit([], str(e), False)


class _BatchApplySignals(QObject):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(object, list, int, str)


class _BatchApplyWorker(QRunnable):
    """対象ページ群の色抜き・NDLOCR-Lite・PDFページ構築をバックグラウンドで実行するワーカー"""

    def __init__(
        self,
        source_pdf_bytes: bytes,
        target_indices: List[int],
        total_doc_pages: int,
        clean_params: dict,
        need_visual_clean: bool,
        need_ocr: bool,
        fix_num: bool,
        render_dpi: int,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.source_pdf_bytes = source_pdf_bytes
        self.target_indices = list(target_indices)
        self.total_doc_pages = total_doc_pages
        self.clean_params = dict(clean_params)
        self.need_visual_clean = need_visual_clean
        self.need_ocr = need_ocr
        self.fix_num = fix_num
        self.render_dpi = max(200, int(render_dpi))
        self.cancel_event = cancel_event
        self.signals = _BatchApplySignals()

    BATCH_SIZE = 3

    def run(self):
        with FITZ_LOCK:
            src_doc = fitz.open(stream=self.source_pdf_bytes, filetype="pdf")
            after_doc = fitz.open()

        processed_indices: List[int] = []
        total_ocr_words = 0
        num_targets = len(self.target_indices)

        def _cancel_and_finish():
            with FITZ_LOCK:
                after_doc.close()
                src_doc.close()
            self.signals.finished.emit(None, [], 0, "CANCELED")

        try:
            for batch_start in range(0, num_targets, self.BATCH_SIZE):
                batch_end = min(num_targets, batch_start + self.BATCH_SIZE)
                if self.cancel_event.is_set():
                    _cancel_and_finish()
                    return

                cleaned_images: List[QImage] = []
                for step_i in range(batch_start, batch_end):
                    if self.cancel_event.is_set():
                        break
                    p_idx = self.target_indices[step_i]
                    self.signals.progress.emit(
                        step_i,
                        f"{p_idx + 1} / {self.total_doc_pages} ページ目の色抜き画像を作成中...",
                    )
                    page_qimg = render_page_to_qimage(
                        src_doc[step_i],
                        dpi=self.render_dpi,
                        max_pixels=PREVIEW_MAX_RENDER_PIXELS,
                    )
                    if self.cancel_event.is_set():
                        break
                    cleaned_images.append(clean_scanned_qimage(page_qimg, **self.clean_params))
                    del page_qimg
                if self.cancel_event.is_set():
                    _cancel_and_finish()
                    return

                ocr_results_list: List[Optional[List[Dict]]] = [None] * len(cleaned_images)
                if self.need_ocr and cleaned_images:
                    first_note = ""
                    try:
                        from core.ocr_engine import engine_ready
                        if not engine_ready():
                            first_note = "（初回はOCRの準備に少し時間がかかります）"
                    except Exception:
                        pass
                    self.signals.progress.emit(
                        batch_start,
                        f"文字認識を実行中... ({batch_start + 1} / {num_targets}){first_note}",
                    )

                    def _on_ocr_page_done(done_i: int, _bs=batch_start):
                        if self.cancel_event.is_set():
                            return
                        next_i = min(num_targets, _bs + done_i + 1)
                        self.signals.progress.emit(
                            min(num_targets - 1, next_i - 1),
                            f"文字認識を実行中... ({next_i} / {num_targets})",
                        )

                    batch_chunks = run_ocr_batch_on_qimages(
                        cleaned_images,
                        fix_numeric=self.fix_num,
                        cancel_event=self.cancel_event,
                        progress_callback=_on_ocr_page_done,
                    )
                    if self.cancel_event.is_set():
                        _cancel_and_finish()
                        return
                    for i, ch_list in enumerate(batch_chunks):
                        ocr_results_list[i] = ch_list
                        total_ocr_words += len(ch_list)

                for j, step_i in enumerate(range(batch_start, batch_end)):
                    if self.cancel_event.is_set():
                        break
                    p_idx = self.target_indices[step_i]
                    with FITZ_LOCK:
                        single_doc = fitz.open()
                        single_doc.insert_pdf(src_doc, from_page=step_i, to_page=step_i)
                    try:
                        c_img = cleaned_images[j]
                        apply_cleaned_image_and_ocr_to_page(
                            doc=single_doc,
                            page_idx=0,
                            cleaned_qimg=c_img if self.need_visual_clean else None,
                            ocr_chunks=ocr_results_list[j],
                            ocr_img_size=(c_img.width(), c_img.height()),
                            apply_cleaned_visual=self.need_visual_clean,
                        )
                        with FITZ_LOCK:
                            after_doc.insert_pdf(single_doc, from_page=0, to_page=0)
                        processed_indices.append(p_idx)
                    finally:
                        with FITZ_LOCK:
                            single_doc.close()
                    cleaned_images[j] = None
                cleaned_images.clear()
                if self.cancel_event.is_set():
                    _cancel_and_finish()
                    return

            with FITZ_LOCK:
                src_doc.close()

            self.signals.finished.emit(after_doc, processed_indices, total_ocr_words, "")

        except Exception as e:
            with FITZ_LOCK:
                after_doc.close()
                src_doc.close()
            self.signals.finished.emit(None, [], 0, str(e))


class _PreviewGraphicsView(QGraphicsView):
    """マウスホイール拡大縮小とドラッグ移動に対応した高精細比較プレビュービュー"""

    zoomModifiedByUser = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setStyleSheet("background-color: #2b2d30; border: 1px solid #b0b5bd; border-radius: 4px;")

    def wheelEvent(self, event):
        angle = event.angleDelta().y()
        cur_scale = self.transform().m11()
        if angle > 0 and cur_scale < 24.0:
            self.scale(1.25, 1.25)
            self.zoomModifiedByUser.emit()
        elif angle < 0 and cur_scale > 0.10:
            self.scale(1.0 / 1.25, 1.0 / 1.25)
            self.zoomModifiedByUser.emit()
        event.accept()


class ScanCleanOcrDialog(QDialog):
    """スキャン書類の色抜き・点々除去・NDLOCR-Liteダイアログ"""

    PRESETS = [
        (
            "標準：カラー背景・色枠線を消去（黄・赤・青などの網掛け）",
            {"strength": 20, "white_th": 185, "black_th": 135, "despeckle": 1, "binarize": True, "keep_color_text": True},
        ),
        (
            "強力：細かい点々（網点・地紋・裏写り）も強力除去",
            {"strength": 24, "white_th": 170, "black_th": 125, "despeckle": 2, "binarize": True, "keep_color_text": False},
        ),
        (
            "色文字保持：薄い背景色と点々のみ消去（濃い赤文字・青文字は残す）",
            {"strength": 18, "white_th": 185, "black_th": 135, "despeckle": 1, "binarize": True, "keep_color_text": True},
        ),
        (
            "滑らかグレー：二値化せず自然なグレースケール文字で色だけ消去",
            {"strength": 20, "white_th": 190, "black_th": 120, "despeckle": 1, "binarize": False, "keep_color_text": False},
        ),
    ]

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self.viewer = viewer
        self.doc: fitz.Document = viewer.current_pdf
        self.page_idx: int = viewer.current_page_idx

        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setWindowTitle("🧼 スキャン用紙の色抜き（カラー背景・点々消去）＆ OCR（国会図書館 NDLOCR-Lite）")
        self.resize(1280, 820)
        self.setStyleSheet(
            "QDialog { background-color: #f6f7f9; color: #1a1a1a; }"
            "QGroupBox { font-weight: bold; border: 1px solid #c8ccd4; border-radius: 6px; "
            "  margin-top: 8px; padding-top: 10px; background-color: #ffffff; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
            "QLabel { color: #1a1a1a; }"
            "QComboBox, QSpinBox, QPlainTextEdit { background-color: #ffffff; color: #111111; "
            "  border: 1px solid #b0b5bd; border-radius: 4px; padding: 4px; }"
            "QPushButton { background-color: #ffffff; color: #1a1a1a; border: 1px solid #a0a5ad; "
            "  border-radius: 5px; padding: 5px 10px; font-weight: bold; }"
            "QPushButton:hover { background-color: #eef2f7; }"
        )

        self._orig_qimg: Optional[QImage] = None
        self._cleaned_qimg: Optional[QImage] = None
        self._last_ocr_chunks: List[Dict] = []
        self._showing_original: bool = False
        self._initial_fit_done: bool = False
        self._auto_fit_mode: bool = True
        self._is_canceled_or_closed: bool = False
        self._render_dpi: int = 360

        self._test_cancel_event: Optional[threading.Event] = None
        self._batch_cancel_event: Optional[threading.Event] = None
        self._progress_dlg: Optional[QProgressDialog] = None
        self._active_worker_ref = None
        self._active_batch_worker_ref = None

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(70)
        self._preview_timer.timeout.connect(self._update_cleaned_preview)

        self._build_ui()

        self._shortcut_fullscreen = QShortcut(QKeySequence("F11"), self)
        self._shortcut_fullscreen.activated.connect(self._toggle_fullscreen)

        self._load_source_page_image()

    def _build_ui(self):
        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(12)

        left_layout = QVBoxLayout()
        top_bar = QHBoxLayout()
        top_bar.setSpacing(6)

        self.lbl_preview_info = QLabel(f"📄 プレビュー（{self.page_idx + 1} / {len(self.doc)}P）")
        self.lbl_preview_info.setStyleSheet("font-weight: bold; font-size: 13px;")
        top_bar.addWidget(self.lbl_preview_info)

        top_bar.addWidget(QLabel("画質:"))
        self.combo_dpi = QComboBox()
        self.combo_dpi.addItem("高精細 (360 DPI)", 360)
        self.combo_dpi.addItem("超高精細・細字用 (450 DPI)", 450)
        self.combo_dpi.addItem("標準・高速 (260 DPI)", 260)
        self.combo_dpi.setCurrentIndex(0)
        self.combo_dpi.setToolTip("プレビューおよびOCR実行時の画像解像度を切り替えます")
        self.combo_dpi.currentIndexChanged.connect(self._on_dpi_changed)
        top_bar.addWidget(self.combo_dpi)

        top_bar.addStretch()

        self.btn_hold_orig = QPushButton("👁 押している間だけ元画像")
        self.btn_hold_orig.setStyleSheet("background-color: #fff4ce; border-color: #d9a300;")
        self.btn_hold_orig.pressed.connect(self._on_show_orig_pressed)
        self.btn_hold_orig.released.connect(self._on_show_orig_released)
        top_bar.addWidget(self.btn_hold_orig)

        btn_zoom_out = QPushButton("🔎－")
        btn_zoom_out.setToolTip("縮小")
        btn_zoom_out.clicked.connect(self._zoom_out_preview)
        top_bar.addWidget(btn_zoom_out)

        btn_zoom_in = QPushButton("🔍＋")
        btn_zoom_in.setToolTip("拡大")
        btn_zoom_in.clicked.connect(self._zoom_in_preview)
        top_bar.addWidget(btn_zoom_in)

        btn_actual = QPushButton("100%等倍")
        btn_actual.setToolTip("ピクセル等倍（くっきり100%表示）に拡大します")
        btn_actual.clicked.connect(self._zoom_actual_preview)
        top_bar.addWidget(btn_actual)

        btn_fit = QPushButton("全体表示")
        btn_fit.setToolTip("ページ全体が画面に収まるように表示します")
        btn_fit.clicked.connect(self._fit_preview)
        top_bar.addWidget(btn_fit)

        self.btn_toggle_panel = QPushButton("📐 設定を隠す")
        self.btn_toggle_panel.setCheckable(True)
        self.btn_toggle_panel.setToolTip("右側の設定パネルを折りたたんでプレビューを画面いっぱいに広げます")
        self.btn_toggle_panel.toggled.connect(self._on_toggle_panel)
        top_bar.addWidget(self.btn_toggle_panel)

        self.btn_fullscreen = QPushButton("⛶ 全画面 (F11)")
        self.btn_fullscreen.setCheckable(True)
        self.btn_fullscreen.setStyleSheet(
            "QPushButton { background-color: #e7f5ff; color: #1864ab; border-color: #74c0fc; }"
            "QPushButton:checked { background-color: #1971c2; color: #ffffff; border-color: #145a9e; }"
        )
        self.btn_fullscreen.setToolTip("ウィンドウを全画面表示に切り替えます (F11 / Escで戻る)")
        self.btn_fullscreen.clicked.connect(self._toggle_fullscreen)
        top_bar.addWidget(self.btn_fullscreen)

        left_layout.addLayout(top_bar)

        self.preview_scene = QGraphicsScene(self)
        self.preview_view = _PreviewGraphicsView(self)
        self.preview_view.setScene(self.preview_scene)
        self.preview_view.zoomModifiedByUser.connect(self._on_user_zoomed)
        self._pixmap_item: Optional[QGraphicsPixmapItem] = None
        self._ocr_box_items: List[QGraphicsRectItem] = []
        left_layout.addWidget(self.preview_view, stretch=1)

        hint_lbl = QLabel(
            "💡 ヒント: マウスホイールで拡大・ドラッグで移動できます。「⛶ 全画面 (F11)」や「100%等倍」で細部まで確認できます。"
        )
        hint_lbl.setStyleSheet("color: #555; font-size: 11px;")
        left_layout.addWidget(hint_lbl)
        root_layout.addLayout(left_layout, stretch=7)

        self.right_panel = QWidget(self)
        self.right_panel.setFixedWidth(410)
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        grp_clean = QGroupBox("1. 色抜き・点々（網点）除去の設定")
        clean_layout = QVBoxLayout(grp_clean)

        clean_layout.addWidget(QLabel("用紙タイプ（プリセット）:"))
        self.combo_preset = QComboBox()
        for title, _ in self.PRESETS:
            self.combo_preset.addItem(title)
        self.combo_preset.currentIndexChanged.connect(self._on_preset_changed)
        clean_layout.addWidget(self.combo_preset)

        row_str = QHBoxLayout()
        row_str.addWidget(QLabel("カラー消去強度 (黄・赤・青):"))
        self.lbl_strength_val = QLabel("2.0")
        row_str.addStretch()
        row_str.addWidget(self.lbl_strength_val)
        clean_layout.addLayout(row_str)

        self.slider_strength = QSlider(Qt.Orientation.Horizontal)
        self.slider_strength.setRange(0, 35)
        self.slider_strength.setValue(20)
        self.slider_strength.valueChanged.connect(self._on_param_changed)
        clean_layout.addWidget(self.slider_strength)

        row_wth = QHBoxLayout()
        row_wth.addWidget(QLabel("背景の白飛ばし強さ (薄い影・地紋):"))
        self.lbl_wth_val = QLabel("185")
        row_wth.addStretch()
        row_wth.addWidget(self.lbl_wth_val)
        clean_layout.addLayout(row_wth)

        self.slider_white_th = QSlider(Qt.Orientation.Horizontal)
        self.slider_white_th.setRange(110, 240)
        self.slider_white_th.setValue(185)
        self.slider_white_th.valueChanged.connect(self._on_param_changed)
        clean_layout.addWidget(self.slider_white_th)

        row_bth = QHBoxLayout()
        row_bth.addWidget(QLabel("文字の濃さ・くっきり度:"))
        self.lbl_bth_val = QLabel("135")
        row_bth.addStretch()
        row_bth.addWidget(self.lbl_bth_val)
        clean_layout.addLayout(row_bth)

        self.slider_black_th = QSlider(Qt.Orientation.Horizontal)
        self.slider_black_th.setRange(50, 210)
        self.slider_black_th.setValue(135)
        self.slider_black_th.valueChanged.connect(self._on_param_changed)
        clean_layout.addWidget(self.slider_black_th)

        row_desp = QHBoxLayout()
        row_desp.addWidget(QLabel("うっすらした点々（網点）除去:"))
        self.combo_despeckle = QComboBox()
        self.combo_despeckle.addItem("なし (0)", 0)
        self.combo_despeckle.addItem("標準：孤立した細かい点々を除去 (1)", 1)
        self.combo_despeckle.addItem("強力：網点・ドット地紋を強力除去 (2)", 2)
        self.combo_despeckle.setCurrentIndex(1)
        self.combo_despeckle.currentIndexChanged.connect(self._on_param_changed)
        row_desp.addWidget(self.combo_despeckle, stretch=1)
        clean_layout.addLayout(row_desp)

        self.chk_binarize = QCheckBox("白黒二値化する（OCR精度が最も高くなります）")
        self.chk_binarize.setChecked(True)
        self.chk_binarize.toggled.connect(self._on_param_changed)
        clean_layout.addWidget(self.chk_binarize)

        self.chk_keep_color_text = QCheckBox("濃い赤文字・青文字（マイナス表記等）を残す")
        self.chk_keep_color_text.setChecked(True)
        self.chk_keep_color_text.toggled.connect(self._on_param_changed)
        clean_layout.addWidget(self.chk_keep_color_text)

        right_layout.addWidget(grp_clean)

        grp_ocr = QGroupBox("2. OCR（国会図書館 NDLOCR-Lite）＆ ページ反映オプション")
        ocr_layout = QVBoxLayout(grp_ocr)

        ocr_layout.addWidget(QLabel("実行内容:"))
        self.combo_action_mode = QComboBox()
        self.combo_action_mode.addItem(
            "🧼 色抜き（白黒クリーンアップ）画像のみページに反映する（標準）", "clean_only"
        )
        self.combo_action_mode.addItem(
            "✨ 色抜き画像＋透明OCR文字も埋め込む", "both"
        )
        self.combo_action_mode.addItem(
            "🔎 ページの見た目（カラー）は変えず、透明OCR文字だけ埋め込む", "ocr_only"
        )
        ocr_layout.addWidget(self.combo_action_mode)

        self.chk_fix_num = QCheckBox("金額・数値のOCR誤認識（O→0, I/l→1）を自動補正する")
        self.chk_fix_num.setChecked(True)
        ocr_layout.addWidget(self.chk_fix_num)

        row_target = QHBoxLayout()
        row_target.addWidget(QLabel("対象ページ:"))
        self.combo_scope = QComboBox()
        self.combo_scope.addItem(f"現在のページのみ ({self.page_idx + 1}ページ目)", "current")
        if len(self.doc) > 1:
            self.combo_scope.addItem(f"すべてのページ一括 (全 {len(self.doc)} ページ)", "all")
        row_target.addWidget(self.combo_scope, stretch=1)
        ocr_layout.addLayout(row_target)

        self.btn_test_ocr = QPushButton("🔍 このページでOCRをテスト実行して認識文字を確認")
        self.btn_test_ocr.setStyleSheet(
            "background-color: #e8f2fc; color: #0b4f9c; border: 1px solid #7ab0f5;"
        )
        self.btn_test_ocr.clicked.connect(self._on_test_ocr_clicked)
        ocr_layout.addWidget(self.btn_test_ocr)

        self.txt_ocr_preview = QPlainTextEdit()
        self.txt_ocr_preview.setPlaceholderText(
            "上の「OCRをテスト実行」を押すと、色抜き後の画像から読み取られた文字・金額の一覧がここに表示され、プレビュー上に緑色の認識枠が表示されます。"
        )
        self.txt_ocr_preview.setMaximumHeight(115)
        self.txt_ocr_preview.setReadOnly(True)
        ocr_layout.addWidget(self.txt_ocr_preview)

        right_layout.addWidget(grp_ocr)
        right_layout.addStretch()

        btn_row = QHBoxLayout()
        self.btn_cancel = QPushButton("キャンセル")
        self.btn_cancel.clicked.connect(self._on_cancel_button_clicked)

        self.btn_apply = QPushButton("✔ PDFページに適用する")
        self.btn_apply.setStyleSheet(
            "QPushButton { background-color: #1971c2; color: #ffffff; border: 1px solid #145a9e; "
            "padding: 8px 18px; font-size: 13px; font-weight: bold; }"
            "QPushButton:hover { background-color: #1864ab; }"
        )
        self.btn_apply.clicked.connect(self._on_apply_clicked)

        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_apply, stretch=1)
        right_layout.addLayout(btn_row)

        root_layout.addWidget(self.right_panel)

    def _on_dpi_changed(self, _idx: int):
        new_dpi = int(self.combo_dpi.currentData() or 360)
        if new_dpi == self._render_dpi:
            return
        self._render_dpi = new_dpi
        self._clear_ocr_overlay_boxes()
        self._initial_fit_done = False
        self._load_source_page_image()

    def _on_toggle_panel(self, hidden: bool):
        self.right_panel.setVisible(not hidden)
        self.btn_toggle_panel.setText("📐 設定を表示" if hidden else "📐 設定を隠す")
        if self._auto_fit_mode:
            QTimer.singleShot(15, self._fit_preview)

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.btn_fullscreen.blockSignals(True)
            self.btn_fullscreen.setChecked(False)
            self.btn_fullscreen.setText("⛶ 全画面 (F11)")
            self.btn_fullscreen.blockSignals(False)
        else:
            self.showFullScreen()
            self.btn_fullscreen.blockSignals(True)
            self.btn_fullscreen.setChecked(True)
            self.btn_fullscreen.setText("🗗 通常画面に戻す (F11)")
            self.btn_fullscreen.blockSignals(False)

        if self._auto_fit_mode:
            QTimer.singleShot(20, self._fit_preview)

    def _on_user_zoomed(self):
        self._auto_fit_mode = False

    def _zoom_in_preview(self):
        if self.preview_view.transform().m11() < 24.0:
            self._auto_fit_mode = False
            self.preview_view.scale(1.3, 1.3)

    def _zoom_out_preview(self):
        if self.preview_view.transform().m11() > 0.10:
            self._auto_fit_mode = False
            self.preview_view.scale(1.0 / 1.3, 1.0 / 1.3)

    def _zoom_actual_preview(self):
        self._auto_fit_mode = False
        self.preview_view.resetTransform()
        dpr = max(1.0, float(self.preview_view.devicePixelRatioF()))
        if dpr > 1.0:
            self.preview_view.scale(1.0 / dpr, 1.0 / dpr)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._auto_fit_mode and self._initial_fit_done:
            QTimer.singleShot(0, self._fit_preview)

    def _cancel_running_tasks(self):
        self._is_canceled_or_closed = True
        if self._test_cancel_event is not None:
            self._test_cancel_event.set()
        if self._batch_cancel_event is not None:
            self._batch_cancel_event.set()
        self._test_cancel_event = None
        if getattr(self, "btn_test_ocr", None) is not None:
            self.btn_test_ocr.setText("🔍 このページでOCRをテスト実行して認識文字を確認")

    def _on_cancel_button_clicked(self):
        self._cancel_running_tasks()
        super().reject()

    def reject(self):
        if self.isFullScreen():
            self._toggle_fullscreen()
            return
        self._cancel_running_tasks()
        super().reject()

    def closeEvent(self, event):
        self._cancel_running_tasks()
        self._close_progress_dialog_safely()
        super().closeEvent(event)

    def _load_source_page_image(self):
        if not self.doc or not (0 <= self.page_idx < len(self.doc)):
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._orig_qimg = render_page_to_qimage(
                self.doc[self.page_idx],
                dpi=self._render_dpi,
                max_pixels=PREVIEW_MAX_RENDER_PIXELS,
            )
            self._update_cleaned_preview()
        finally:
            QApplication.restoreOverrideCursor()

    def _on_preset_changed(self, idx: int):
        if not (0 <= idx < len(self.PRESETS)):
            return
        cfg = self.PRESETS[idx][1]
        for w in (
            self.slider_strength,
            self.slider_white_th,
            self.slider_black_th,
            self.combo_despeckle,
            self.chk_binarize,
            self.chk_keep_color_text,
        ):
            w.blockSignals(True)

        self.slider_strength.setValue(int(cfg["strength"]))
        self.slider_white_th.setValue(int(cfg["white_th"]))
        self.slider_black_th.setValue(int(cfg["black_th"]))
        desp_idx = self.combo_despeckle.findData(int(cfg["despeckle"]))
        if desp_idx >= 0:
            self.combo_despeckle.setCurrentIndex(desp_idx)
        self.chk_binarize.setChecked(bool(cfg["binarize"]))
        self.chk_keep_color_text.setChecked(bool(cfg["keep_color_text"]))

        for w in (
            self.slider_strength,
            self.slider_white_th,
            self.slider_black_th,
            self.combo_despeckle,
            self.chk_binarize,
            self.chk_keep_color_text,
        ):
            w.blockSignals(False)

        self._on_param_changed()

    def _on_param_changed(self, *_args):
        strength = self.slider_strength.value() / 10.0
        w_th = self.slider_white_th.value()
        b_th = min(w_th - 10, self.slider_black_th.value())
        if b_th != self.slider_black_th.value():
            self.slider_black_th.blockSignals(True)
            self.slider_black_th.setValue(b_th)
            self.slider_black_th.blockSignals(False)

        self.lbl_strength_val.setText(f"{strength:.1f}")
        self.lbl_wth_val.setText(str(w_th))
        self.lbl_bth_val.setText(str(b_th))
        self._preview_timer.start()

    def _get_clean_params(self) -> dict:
        return {
            "color_drop_strength": self.slider_strength.value() / 10.0,
            "white_threshold": self.slider_white_th.value(),
            "black_threshold": self.slider_black_th.value(),
            "despeckle_level": int(self.combo_despeckle.currentData() or 0),
            "binarize": self.chk_binarize.isChecked(),
            "keep_dark_color_text": self.chk_keep_color_text.isChecked(),
        }

    def _update_cleaned_preview(self):
        if self._orig_qimg is None:
            return
        params = self._get_clean_params()
        self._cleaned_qimg = clean_scanned_qimage(self._orig_qimg, **params)
        self._refresh_displayed_pixmap()

    def _refresh_displayed_pixmap(self):
        target_img = self._orig_qimg if self._showing_original else self._cleaned_qimg
        if target_img is None:
            return
        pix = QPixmap.fromImage(target_img)
        if self._pixmap_item is None or self._pixmap_item.scene() is not self.preview_scene:
            self.preview_scene.clear()
            self._ocr_box_items.clear()
            self._pixmap_item = self.preview_scene.addPixmap(pix)
            self._pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            self.preview_scene.setSceneRect(QRectF(0, 0, pix.width(), pix.height()))
        else:
            self._pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            self._pixmap_item.setPixmap(pix)

        if not self._initial_fit_done and pix.width() > 0:
            self._initial_fit_done = True
            QTimer.singleShot(0, self._fit_preview)

    def _fit_preview(self):
        if not self.preview_scene.sceneRect().isEmpty():
            self._auto_fit_mode = True
            self.preview_view.fitInView(
                self.preview_scene.sceneRect(),
                Qt.AspectRatioMode.KeepAspectRatio,
            )

    _refresh_Displayed_pixmap = _refresh_displayed_pixmap

    def _on_show_orig_pressed(self):
        self._showing_original = True
        self._refresh_displayed_pixmap()

    def _on_show_orig_released(self):
        self._showing_original = False
        self._refresh_displayed_pixmap()

    def _clear_ocr_overlay_boxes(self):
        for item in self._ocr_box_items:
            if item.scene() is self.preview_scene:
                self.preview_scene.removeItem(item)
        self._ocr_box_items.clear()

    def _on_test_ocr_clicked(self):
        if self._cleaned_qimg is None:
            return
        if self._test_cancel_event is not None and not self._test_cancel_event.is_set():
            self._test_cancel_event.set()
            self.btn_test_ocr.setText("🔍 このページでOCRをテスト実行して認識文字を確認")
            self.txt_ocr_preview.setPlainText("（OCRテストを中止しました）")
            return

        self._clear_ocr_overlay_boxes()
        self._test_cancel_event = threading.Event()
        self.btn_test_ocr.setText("⏳ OCR解析中... (クリックで中止)")
        self.txt_ocr_preview.setPlainText("NDLOCR-Liteで文字を認識しています...")

        worker = _OcrTestWorker(
            self._cleaned_qimg,
            fix_numeric=self.chk_fix_num.isChecked(),
            cancel_event=self._test_cancel_event,
        )
        worker.signals.finished.connect(
            self._on_test_ocr_finished, Qt.ConnectionType.QueuedConnection
        )
        self._active_worker_ref = worker
        QThreadPool.globalInstance().start(worker)

    def _on_test_ocr_finished(self, chunks: list, err_msg: str, cancelled: bool = False):
        active = getattr(self, "_active_worker_ref", None)
        if active is not None and self.sender() is not getattr(active, "signals", None):
            return
        self._test_cancel_event = None
        self.btn_test_ocr.setText("🔍 このページでOCRをテスト実行して認識文字を確認")

        if cancelled:
            self.txt_ocr_preview.setPlainText("（OCRテストを中止しました）")
            return

        if err_msg:
            QMessageBox.warning(self, "OCRエラー", err_msg)
            return

        self._last_ocr_chunks = chunks
        if not chunks:
            self.txt_ocr_preview.setPlainText(
                "（文字が検出されませんでした。スライダーで文字の濃さを調整してみてください）"
            )
            return

        pen = QPen(QColor(0, 175, 80, 230), 2.0)
        brush = QBrush(QColor(0, 210, 95, 38))
        lines_out = []
        for ch in chunks:
            x0, y0, x1, y1 = ch["bbox"]
            r_item = QGraphicsRectItem(QRectF(x0, y0, max(2.0, x1 - x0), max(2.0, y1 - y0)))
            r_item.setPen(pen)
            r_item.setBrush(brush)
            r_item.setZValue(10)
            self.preview_scene.addItem(r_item)
            self._ocr_box_items.append(r_item)
            lines_out.append(ch["text"])

        self.txt_ocr_preview.setPlainText(
            f"【検出チャンク数: {len(chunks)}件】\n" + " / ".join(lines_out)
        )

    def _on_apply_clicked(self):
        if not self.doc:
            return

        self._cancel_running_tasks()
        self._close_progress_dialog_safely()

        action_mode = self.combo_action_mode.currentData()
        scope = self.combo_scope.currentData()
        self._pending_need_ocr = action_mode in ("both", "ocr_only")
        need_visual_clean = action_mode in ("both", "clean_only")
        params = self._get_clean_params()
        fix_num = self.chk_fix_num.isChecked()

        if scope == "all":
            target_indices = list(range(len(self.doc)))
        else:
            target_indices = [self.page_idx]

        with FITZ_LOCK:
            sub_doc = fitz.open()
            for p_idx in target_indices:
                sub_doc.insert_pdf(self.doc, from_page=p_idx, to_page=p_idx)
            source_pdf_bytes = sub_doc.tobytes(garbage=1, deflate=True)
            sub_doc.close()

        self._is_canceled_or_closed = False
        self._batch_cancel_event = threading.Event()
        self.btn_apply.setEnabled(False)

        progress_max = max(2, len(target_indices) + 1)
        dlg = QProgressDialog(
            "スキャン色抜き・文字認識を実行中...",
            "中止",
            0,
            progress_max,
            self,
        )
        dlg.setWindowTitle("処理中")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.canceled.connect(self._on_batch_canceled)
        self._progress_dlg = dlg
        dlg.show()

        worker = _BatchApplyWorker(
            source_pdf_bytes=source_pdf_bytes,
            target_indices=target_indices,
            total_doc_pages=len(self.doc),
            clean_params=params,
            need_visual_clean=need_visual_clean,
            need_ocr=self._pending_need_ocr,
            fix_num=fix_num,
            render_dpi=self._render_dpi,
            cancel_event=self._batch_cancel_event,
        )
        worker.signals.progress.connect(
            self._on_batch_progress, Qt.ConnectionType.QueuedConnection
        )
        worker.signals.finished.connect(
            self._on_batch_finished, Qt.ConnectionType.QueuedConnection
        )
        self._active_batch_worker_ref = worker
        QThreadPool.globalInstance().start(worker)

    def _on_batch_progress(self, step_i: int, label_text: str):
        active_batch = getattr(self, "_active_batch_worker_ref", None)
        if active_batch is not None and self.sender() is not getattr(active_batch, "signals", None):
            return
        if getattr(self, "_is_canceled_or_closed", False):
            return
        dlg = getattr(self, "_progress_dlg", None)
        if dlg is None:
            return
        try:
            dlg.setLabelText(label_text)
            dlg = getattr(self, "_progress_dlg", None)
            if dlg is not None and not getattr(self, "_is_canceled_or_closed", False):
                dlg.setValue(min(step_i, max(0, dlg.maximum() - 1)))
        except (RuntimeError, AttributeError):
            pass

    def _on_batch_canceled(self):
        self._is_canceled_or_closed = True
        if self._batch_cancel_event is not None:
            self._batch_cancel_event.set()

    def _close_progress_dialog_safely(self):
        dlg = getattr(self, "_progress_dlg", None)
        self._progress_dlg = None
        if dlg is not None:
            try:
                dlg.canceled.disconnect(self._on_batch_canceled)
            except Exception:
                pass
            try:
                dlg.hide()
                dlg.close()
                dlg.deleteLater()
            except (RuntimeError, AttributeError):
                pass

    def _on_batch_finished(
        self,
        after_doc: Optional[fitz.Document],
        processed_indices: list,
        total_ocr_words: int,
        err_msg: str,
    ):
        active_batch = getattr(self, "_active_batch_worker_ref", None)
        if active_batch is not None and self.sender() is not getattr(active_batch, "signals", None):
            if after_doc is not None:
                with FITZ_LOCK:
                    after_doc.close()
            return
        self._active_batch_worker_ref = None
        self._batch_cancel_event = None
        self.btn_apply.setEnabled(True)
        self._close_progress_dialog_safely()

        if err_msg == "CANCELED" or getattr(self, "_is_canceled_or_closed", False):
            if after_doc is not None:
                with FITZ_LOCK:
                    after_doc.close()
            return

        if err_msg:
            if after_doc is not None:
                with FITZ_LOCK:
                    after_doc.close()
            QMessageBox.critical(self, "処理エラー", f"処理中にエラーが発生しました:\n{err_msg}")
            return

        if after_doc is None or not processed_indices:
            if after_doc is not None:
                with FITZ_LOCK:
                    after_doc.close()
            return

        cmd = ReplacePagesContentCommand(
            doc=self.doc,
            page_indices=processed_indices,
            new_pages_doc=after_doc,
            page_annotations=self.viewer.page_annotations,
            prev_page_idx=self.viewer.current_page_idx,
            take_ownership=True,
        )
        cmd.redo(self.viewer)
        self.viewer.history.push(cmd)

        win = self.viewer.window()
        if hasattr(win, "statusBar"):
            if getattr(self, "_pending_need_ocr", False):
                win.statusBar().showMessage(
                    f"🧼 色抜き＆OCR完了: {len(processed_indices)}ページ処理（認識ブロック: {total_ocr_words}件 — Ctrl+Fで検索・Ctrl+クリックで電卓合計が可能です）"
                )
            else:
                win.statusBar().showMessage(
                    f"🧼 色抜きクリーンアップ完了: {len(processed_indices)}ページ処理（Ctrl+Zで元に戻せます）"
                )

        self.accept()
