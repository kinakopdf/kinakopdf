# -*- coding: utf-8 -*-
"""メインウィンドウ (MainWindow) - ツールバー・文字検索・電卓・印刷・見開きリーダー連携"""
import copy
import os
import sys
from pathlib import Path

import fitz
from PyQt6.QtCore import QRectF, QSettings, Qt, QThreadPool, QTimer
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QImage,
    QKeySequence,
    QPageLayout,
    QPainter,
    QShortcut,
)
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGraphicsView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QToolBar,
    QToolButton,
)

from core import (
    COLOR_PRESETS,
    adjust_updated_after_on_remove,
    annotation_to_erase_rects,
    build_text_annotation,
    clip_stroke_by_rects,
    collect_text_only_redact_raw_rects,
    get_later_user_erase_rects,
    is_rect_substantially_erased,
    logger,
)
from ui import (
    create_color_swatch_icon,
    create_pen_eraser_icon,
    qcolor_to_rgb_tuple,
    rgb_tuple_to_qcolor,
    sync_color_combo,
)
from core.commands import (
    AddAnnotationCommand,
    EditAnnotationCommand,
    RemoveAnnotationCommand,
    RemoveMultipleAnnotationsCommand,
    ReplacePageAnnotationsCommand,
)
from core.geometry import (
    APP_NAME,
    DRAW_WIDTH,
    FITZ_LOCK,
    MAX_DRAW_WIDTH,
    MAX_FONT_SIZE,
    MAX_RECENT_FILES,
    MIN_DRAW_WIDTH,
    MIN_FONT_SIZE,
    TEXT_FONT_SIZE,
    estimate_text_bbox_in_pdf,
    format_number_value,
    register_as_windows_pdf_handler,
)
from core.pdf_engine import PDFDocumentManager
from core.pdf_loader import load_folder_as_merged_document
from core.text_tools import (
    extract_page_spans,
    extract_page_texts,
    fit_fontsize_to_width,
    get_page_view_rects,
    render_page_png_bytes,
    save_image_only_pdf,
    spans_are_scan_overlay,
)
from ui.dialogs import TextEditDeleteDialog
from ui.page_manager import PageManager
from ui.pdf_viewer import PDFViewer
from ui.region_ocr_ext import DirectViewerRegionOCRController
from ui.reader_store import ReaderStore
from ui.spread_reader import FullscreenSpreadReader


class MainWindow(QMainWindow):

    _open_windows = []

    def __init__(self):
        super().__init__()
        MainWindow._open_windows.append(self)

        self.setWindowTitle(APP_NAME)
        app_inst = QApplication.instance()
        if app_inst is not None and not app_inst.windowIcon().isNull():
            self.setWindowIcon(app_inst.windowIcon())
        self._initial_positioned = False
        self._active_reader = None
        self._active_reader_closed_handler = None
        # 全画面見開きの表紙の有無・綴じ方向（保存された設定を引き継ぐ）
        self._reader_store = ReaderStore()
        _reader_prefs = self._reader_store.get_prefs()
        self._reader_cover_single = bool(_reader_prefs.get("cover_single", True))
        self._reader_right_to_left = bool(_reader_prefs.get("right_to_left", False))
        # 履歴から開いたときだけ使う、見開きのレジューム先 (パス, ページ)
        self._pending_reader_resume = None
        # 現在の文書が複数ファイルの一括読み込み（フォルダ結合）由来か。
        # True の間は元のファイル群への上書きを防ぐため、保存は別名保存のみ。
        self._current_doc_from_bulk = False
        # 文字データダイアログの作業状態（ページごとの編集中テキストと
        # くっつけプレビュー）。ダイアログを閉じても同じ文書の間は保持し、
        # 文書が差し替わったら破棄する。
        self._text_data_session = {"pages": {}, "all": None, "join": {}}
        self._text_data_session_doc = None
        # 直近の「PDFに反映」が不可視（OCRレイヤー型）だったか。
        # 文字データ画面が完了メッセージの出し分けに使う。
        self._last_text_data_apply_invisible = False

        self.viewer = PDFViewer()
        self.setCentralWidget(self.viewer)

        self.page_manager = PageManager(self)
        self.addDockWidget(
            Qt.DockWidgetArea.LeftDockWidgetArea,
            self.page_manager,
        )

        self.viewer.pagesUpdated.connect(self.page_manager.update_thumbnails)
        self.viewer.pageSelectionChanged.connect(self.page_manager.sync_selection)
        self.viewer.scrollModeChanged.connect(self._sync_scroll_action)
        self.viewer.batchStatusMessage.connect(self.statusBar().showMessage)
        self.viewer.selected_pages_provider = (
            lambda: sorted(
                {
                    self.page_manager.list_widget.row(item)
                    for item in self.page_manager.list_widget.selectedItems()
                }
            )
        )
        self.viewer.statusUpdated.connect(self.update_window_title)
        self.viewer.filesDroppedOnViewer.connect(self.on_files_dropped_on_viewer)
        self.viewer.fontSizeChanged.connect(self._sync_font_spinbox)
        self.viewer.drawWidthChanged.connect(self._sync_draw_width_spinbox)
        self.viewer.colorChanged.connect(self._sync_color_combobox)
        self.viewer.calcSelectionChanged.connect(self._on_calc_selection_changed)
        self.viewer.searchStatusChanged.connect(self._on_search_status_changed)
        self.viewer.escapePressed.connect(self._on_escape_pressed)

        self.init_toolbar()
        self._direct_region_ocr_ctrl = DirectViewerRegionOCRController(self, self.viewer)
        self.viewer.region_ocr_controller = self._direct_region_ocr_ctrl
        self._apply_initial_geometry()

        self.statusBar().showMessage(
            "PDFを開いてください（Ctrl+Fで文字検索 / Ctrl+クリック・ドラッグで表内の数字を合計 / ホイールでページ送り）"
        )

    def _get_titlebar_margin(self):
        title_h = self.style().pixelMetric(QStyle.PixelMetric.PM_TitleBarHeight)
        frame_w = self.style().pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth)
        return max(36, title_h + frame_w * 2 + 8)

    def _apply_initial_geometry(self, desired_w=1380, desired_h=880, offset_from=None):
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            self.resize(desired_w, desired_h)
            return

        avail = screen.availableGeometry()
        top_margin = self._get_titlebar_margin()
        side_margin = 16

        max_w = max(640, avail.width() - side_margin * 2)
        max_h = max(480, avail.height() - top_margin - side_margin)

        w = min(desired_w, max_w)
        h = min(desired_h, max_h)

        if offset_from is not None:
            x = offset_from.x() + 40
            y = offset_from.y() + 40
            if x + w > avail.right() - side_margin:
                x = avail.left() + side_margin
            if y + h > avail.bottom() - side_margin:
                y = avail.top() + top_margin
        else:
            x = avail.left() + (avail.width() - w) // 2
            y = avail.top() + max(top_margin, (avail.height() - h) // 2)

        x = max(avail.left() + side_margin // 2, min(x, avail.right() - w))
        y = max(avail.top() + top_margin, min(y, avail.bottom() - h))

        self.setGeometry(x, y, w, h)
        self._initial_positioned = True

    def _ensure_within_screen(self):
        if self.isMaximized() or self.isFullScreen():
            return

        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return

        avail = screen.availableGeometry()
        frame = self.frameGeometry()
        geo = self.geometry()

        actual_top_border = max(geo.top() - frame.top(), self._get_titlebar_margin())

        max_client_w = max(640, avail.width() - 16)
        max_client_h = max(480, avail.height() - actual_top_border - 8)
        new_w = min(geo.width(), max_client_w)
        new_h = min(geo.height(), max_client_h)
        if new_w != geo.width() or new_h != geo.height():
            self.resize(new_w, new_h)
            geo = self.geometry()

        min_client_y = avail.top() + actual_top_border
        max_client_y = max(min_client_y, avail.bottom() - new_h)

        min_client_x = avail.left()
        max_client_x = max(min_client_x, avail.right() - new_w)

        new_x = max(min_client_x, min(geo.x(), max_client_x))
        new_y = max(min_client_y, min(geo.y(), max_client_y))

        if new_x != geo.x() or new_y != geo.y():
            self.move(new_x, new_y)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._ensure_within_screen)

    def init_toolbar(self):
        top_toolbar = QToolBar("ファイル・表示ツールバー")
        top_toolbar.setMovable(False)
        self.addToolBar(top_toolbar)

        act_open = self.add_action(top_toolbar, "📁 開く", self.open_file)
        act_open.setShortcut(QKeySequence.StandardKey.Open)

        self.btn_recent_files = QToolButton(self)
        self.btn_recent_files.setText("🕘 最近のファイル")
        self.btn_recent_files.setToolTip("最近開いたPDFファイル（最大5件）から選択して開きます")
        self.btn_recent_files.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.recent_files_menu = QMenu(self)
        self.btn_recent_files.setMenu(self.recent_files_menu)
        top_toolbar.addWidget(self.btn_recent_files)
        self._update_recent_files_menu()

        act_save = self.add_action(top_toolbar, "💾 保存", self.save_file)
        act_save.setShortcut(QKeySequence.StandardKey.Save)

        act_save_as = self.add_action(top_toolbar, "💾 別名保存", self.save_as)
        act_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)

        act_print = self.add_action(top_toolbar, "🖨 印刷", self.print_file)
        act_print.setShortcut(QKeySequence.StandardKey.Print)
        act_print.setToolTip("現在編集中のPDFを印刷します (Ctrl+P)")

        act_excel = self.add_action(top_toolbar, "📊 Excel出力", self.export_to_excel)
        act_excel.setToolTip("PDF内の表や文字データを追加ライブラリなしでExcel(.xlsx)に変換して保存します")

        self.add_action(top_toolbar, "🗔 新規", self.spawn_new_window)

        act_read = self.add_action(top_toolbar, "📖 見開き", self.open_fullscreen_reader)
        act_read.setShortcut(QKeySequence("F5"))
        act_read.setToolTip("現在のPDFを全画面見開きモードで閲覧します (F5)")

        act_folder = self.add_action(top_toolbar, "📚 フォルダ読込", self.open_pdf_folder)
        act_folder.setToolTip("フォルダ内の複数PDFをまとめて1つの本として読み込みます")

        act_ocr = self.add_action(top_toolbar, "🧼 色抜き・OCR", self.open_scan_clean_ocr_dialog)
        act_ocr.setShortcut(QKeySequence("Ctrl+Shift+O"))
        act_ocr.setToolTip("スキャンした明細から黄・赤・青の背景や点々を消し、Windows標準OCRで文字認識します (Ctrl+Shift+O)")

        self.act_text_data = self.add_action(top_toolbar, "📝 文字データ", self.open_text_data_dialog)
        self.act_text_data.setToolTip("PDFに埋め込まれている文字データをそのまま表示します（AIや検索が読む中身の確認・手直し・コピー用）")
        self.act_text_data.setEnabled(False)

        top_toolbar.addSeparator()

        self.add_action(top_toolbar, "◀ 前へ", self.viewer.go_prev_page)
        self.lbl_page_info = QLabel(" 0 / 0 ")
        self.lbl_page_info.setStyleSheet("font-weight: bold; padding: 0 4px;")
        top_toolbar.addWidget(self.lbl_page_info)
        self.add_action(top_toolbar, "次へ ▶", self.viewer.go_next_page)

        top_toolbar.addSeparator()

        act_rot_left = self.add_action(
            top_toolbar,
            "↺ 左90°回転",
            lambda: self.viewer.rotate_current_page(-90),
        )
        act_rot_left.setShortcut(QKeySequence("Ctrl+L"))

        act_rot_right = self.add_action(
            top_toolbar,
            "↻ 右90°回転",
            lambda: self.viewer.rotate_current_page(90),
        )
        act_rot_right.setShortcut(QKeySequence("Ctrl+R"))

        top_toolbar.addSeparator()

        act_undo = self.add_action(top_toolbar, "↶ 元に戻す", self.viewer.undo)
        act_undo.setShortcut(QKeySequence.StandardKey.Undo)

        act_redo = self.add_action(top_toolbar, "↷ やり直し", self.viewer.redo)
        act_redo.setShortcut(QKeySequence.StandardKey.Redo)

        top_toolbar.addSeparator()

        self.add_action(top_toolbar, "🔍 拡大", self.viewer.zoom_in)
        self.add_action(top_toolbar, "🔎 縮小", self.viewer.zoom_out)
        self.add_action(top_toolbar, "全体表示", self.viewer.fit_page)

        self.act_scroll_mode = self.add_action(top_toolbar, "📜 スクロール表示", self.toggle_scroll_mode)
        self.act_scroll_mode.setCheckable(True)
        self.act_scroll_mode.setToolTip("通常画面でページを上下スクロールして表示します（ダブルクリックで1ページ表示に戻ります）")

        top_toolbar.addSeparator()

        lbl_search = QLabel(" 🔎 検索:")
        lbl_search.setStyleSheet("font-weight: bold; padding-left: 4px;")
        top_toolbar.addWidget(lbl_search)

        self.edit_search = QLineEdit(self)
        self.edit_search.setPlaceholderText("文字を検索 (Ctrl+F)")
        self.edit_search.setMinimumWidth(110)
        self.edit_search.setMaximumWidth(160)
        self.edit_search.returnPressed.connect(self._on_search_return_pressed)
        top_toolbar.addWidget(self.edit_search)

        self.btn_search_prev = QPushButton("▲", self)
        self.btn_search_prev.setToolTip("前の一致箇所へ移動 (Shift+F3)")
        self.btn_search_prev.setFixedWidth(28)
        self.btn_search_prev.clicked.connect(self._on_search_prev_clicked)
        top_toolbar.addWidget(self.btn_search_prev)

        self.btn_search_next = QPushButton("▼", self)
        self.btn_search_next.setToolTip("次の一致箇所へ移動 (Enter / F3)")
        self.btn_search_next.setFixedWidth(28)
        self.btn_search_next.clicked.connect(self._on_search_next_clicked)
        top_toolbar.addWidget(self.btn_search_next)

        self.lbl_search_count = QLabel(" 0 / 0 ")
        self.lbl_search_count.setStyleSheet("padding: 0 4px; color: #333;")
        top_toolbar.addWidget(self.lbl_search_count)

        self.btn_search_clear = QPushButton("✖", self)
        self.btn_search_clear.setToolTip("検索ハイライトをクリア")
        self.btn_search_clear.setFixedWidth(28)
        self.btn_search_clear.clicked.connect(self._on_search_clear_clicked)
        top_toolbar.addWidget(self.btn_search_clear)

        self.shortcut_find = QShortcut(QKeySequence.StandardKey.Find, self)
        self.shortcut_find.activated.connect(self._focus_search_box)

        self.shortcut_find_next = QShortcut(QKeySequence("F3"), self)
        self.shortcut_find_next.activated.connect(self._on_search_next_clicked)

        self.shortcut_find_prev = QShortcut(QKeySequence("Shift+F3"), self)
        self.shortcut_find_prev.activated.connect(self._on_search_prev_clicked)

        self.shortcut_escape = QShortcut(QKeySequence("Escape"), self)
        self.shortcut_escape.setContext(Qt.ShortcutContext.WindowShortcut)
        self.shortcut_escape.activated.connect(self._on_escape_pressed)

        if sys.platform == "win32":
            top_toolbar.addSeparator()
            act_default_app = self.add_action(
                top_toolbar,
                "⚙ 既定アプリに設定",
                self.setup_as_default_pdf_app,
            )
            act_default_app.setToolTip("Windowsの既定のPDFアプリに登録します")

        self.addToolBarBreak()

        edit_toolbar = QToolBar("編集・スタイルツールバー")
        edit_toolbar.setMovable(False)
        self.addToolBar(edit_toolbar)

        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)

        modes = [
            ("✋ 閲覧", "view"),
            ("📋 テキスト選択", "text_select"),
            ("✂ トリミング", "crop"),
            ("🗜 折りたたみ", "fold"),
            ("✏ ペン", "draw"),
            ("🖍 マーカー", "highlight"),
            ("🖍✕ マーカー消去", "highlight_erase"),
            ("🔲 範囲墨消し", "box_erase"),
            ("🧽 消しゴム", "erase"),
            ("⌨ テキスト編集", "type"),
            ("🧹 テキスト消去", "text_erase"),
            ("🎯 範囲OCR", "region_ocr"),
        ]

        for name, mode in modes:
            action = QAction(name, self)
            action.setCheckable(True)
            action.setData(mode)
            if mode == "view":
                action.setChecked(True)
            elif mode == "text_select":
                action.setToolTip(
                    "ドラッグで文字を選択すると、近くにコピーボタンが表示されます。"
                    "ボタンを押すと選択中のテキストをコピーしてポップアップが閉じます"
                    "（Escキー・他の場所のクリックでも閉じます）"
                )
            elif mode == "fold":
                action.setToolTip(
                    "折りたたみたい場所を左上から右下にドラッグしてください。"
                    "横長のドラッグで横帯、縦長のドラッグで縦帯を非表示にして詰めて表示します"
                    "（表示だけ・PDFは変わりません。もう一度押すと解除します）"
                )
            elif mode == "region_ocr":
                action.setShortcut(QKeySequence("Ctrl+Alt+O"))
                action.setToolTip(
                    "ドラッグで囲んだ範囲だけをピンポイントで高精度OCRし、検索・電卓・文字編集を可能にします (Ctrl+Alt+O)"
                )
            action.triggered.connect(lambda checked=False, m=mode: self.set_mode(m))
            self.mode_group.addAction(action)
            edit_toolbar.addAction(action)

        edit_toolbar.addSeparator()

        lbl_color = QLabel(" 🎨 色:")
        lbl_color.setStyleSheet("padding-left: 4px; font-weight: bold;")
        edit_toolbar.addWidget(lbl_color)

        self.combo_color = QComboBox()
        self.combo_color.setMinimumWidth(145)
        for name, rgb in COLOR_PRESETS:
            self.combo_color.addItem(create_color_swatch_icon(rgb), name, rgb)
        self.combo_color.addItem(
            create_pen_eraser_icon(16), "🧽 ペン消しゴム（ペンのみ消去）", "pen_eraser"
        )
        self.combo_color.addItem("カスタム色...", "custom")
        self.combo_color.activated.connect(self._on_toolbar_color_activated)
        edit_toolbar.addWidget(self.combo_color)

        edit_toolbar.addSeparator()

        lbl_pen_width = QLabel(" ✏ ペンの太さ:")
        lbl_pen_width.setStyleSheet("padding-left: 4px;")
        edit_toolbar.addWidget(lbl_pen_width)

        self.spin_draw_width = QDoubleSpinBox()
        self.spin_draw_width.setRange(MIN_DRAW_WIDTH, MAX_DRAW_WIDTH)
        self.spin_draw_width.setSingleStep(0.5)
        self.spin_draw_width.setDecimals(1)
        self.spin_draw_width.setValue(DRAW_WIDTH)
        self.spin_draw_width.setSuffix(" pt")
        self.spin_draw_width.valueChanged.connect(self.viewer.set_draw_width)
        edit_toolbar.addWidget(self.spin_draw_width)

        edit_toolbar.addSeparator()

        lbl_font = QLabel(" ⌨ 文字サイズ:")
        lbl_font.setStyleSheet("padding-left: 4px;")
        edit_toolbar.addWidget(lbl_font)

        self.spin_font_size = QDoubleSpinBox()
        self.spin_font_size.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self.spin_font_size.setDecimals(1)
        self.spin_font_size.setSingleStep(0.5)
        self.spin_font_size.setValue(TEXT_FONT_SIZE)
        self.spin_font_size.setSuffix(" pt")
        self.spin_font_size.valueChanged.connect(self.viewer.set_font_size)
        edit_toolbar.addWidget(self.spin_font_size)

        edit_toolbar.addSeparator()

        self.lbl_calc_sum = QLabel(" 🧮 合計: 0 (0個) ")
        self.lbl_calc_sum.setToolTip("Ctrlキーを押しながらPDF内の数字をクリック、またはドラッグで範囲選択すると合計します")
        self.lbl_calc_sum.setStyleSheet(
            "background-color: #fff8e6; color: #b25000; border: 1px solid #f0b464; "
            "border-radius: 4px; font-weight: bold; padding: 3px 8px; margin-left: 4px;"
        )
        edit_toolbar.addWidget(self.lbl_calc_sum)

        self.btn_copy_sum = QPushButton("📋 合計コピー")
        self.btn_copy_sum.clicked.connect(self._copy_calc_sum_to_clipboard)
        self.btn_copy_sum.setEnabled(False)
        edit_toolbar.addWidget(self.btn_copy_sum)

        self.btn_clear_sum = QPushButton("✖ クリア")
        self.btn_clear_sum.clicked.connect(self.viewer.clear_calc_selection)
        self.btn_clear_sum.setEnabled(False)
        edit_toolbar.addWidget(self.btn_clear_sum)

    def _focus_search_box(self):
        self.edit_search.setFocus()
        self.edit_search.selectAll()

    def _on_search_return_pressed(self):
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            self._on_search_prev_clicked()
        else:
            self._on_search_next_clicked()

    def _on_search_next_clicked(self):
        query = self.edit_search.text().strip()
        if not query:
            self.viewer.clear_search_results()
            return
        total = self.viewer.find_next_match(query)
        if total == 0:
            self.statusBar().showMessage(f"「{query}」は見つかりませんでした")

    def _on_search_prev_clicked(self):
        query = self.edit_search.text().strip()
        if not query:
            self.viewer.clear_search_results()
            return
        total = self.viewer.find_prev_match(query)
        if total == 0:
            self.statusBar().showMessage(f"「{query}」は見つかりませんでした")

    def _on_escape_pressed(self):
        """Esc: clear the search box, search highlights and calculator selection."""
        self.edit_search.clear()
        self.viewer.clear_search_results()
        self.viewer.clear_calc_selection()
        self.viewer.clear_text_selection()

    def _on_search_clear_clicked(self):
        self.edit_search.clear()
        self.viewer.clear_search_results()
        self.viewer.setFocus()

    def _on_search_status_changed(self, current_1based, total):
        self.lbl_search_count.setText(f" {current_1based} / {total} ")
        if total > 0:
            self.statusBar().showMessage(
                f"🔎 検索結果: {current_1based} / {total} 件 （{self.viewer.current_page_idx + 1}ページ目）"
            )

    def setup_as_default_pdf_app(self):
        try:
            open_cmd = register_as_windows_pdf_handler()
            msg = (
                f"Windowsに「{APP_NAME}」をPDF対応アプリとして登録しました！\n\n"
                f"【実行コマンド】\n{open_cmd}\n\n"
                "続けて Windows の「既定のアプリ」画面を開きますか？\n"
                "（開いた画面の検索欄に「.pdf」と入力し、現在のアプリをクリックして\n"
                f"「{APP_NAME}」→「既定値に設定」を選ぶと完了します）"
            )
            reply = QMessageBox.question(
                self,
                "既定のPDFソフトへの登録完了",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes and sys.platform == "win32":
                os.startfile("ms-settings:defaultapps")
        except Exception as e:
            QMessageBox.warning(self, "登録エラー", f"Windowsへの関連付け登録に失敗しました。\n{e}")

    def _get_settings(self):
        return QSettings("SecureLocalPDFEditor", "SecureLocalPDFEditor")

    def _get_recent_files(self):
        settings = self._get_settings()
        files = settings.value("recent_files", [])
        if isinstance(files, str):
            files = [files] if files else []
        elif not isinstance(files, list):
            files = []
        files = [str(f) for f in files if f][:MAX_RECENT_FILES]
        existing = [f for f in files if os.path.isfile(f)]
        if len(existing) != len(files):
            settings.setValue("recent_files", existing)
            settings.sync()
        return existing

    def _add_recent_file(self, filepath):
        if not filepath:
            return
        norm_path = os.path.abspath(filepath)
        files = self._get_recent_files()
        files = [
            f for f in files
            if os.path.normcase(os.path.abspath(f)) != os.path.normcase(norm_path)
        ]
        files.insert(0, norm_path)
        files = files[:MAX_RECENT_FILES]

        settings = self._get_settings()
        settings.setValue("recent_files", files)
        settings.sync()

        for win in MainWindow._open_windows:
            win._update_recent_files_menu()

    def _clear_recent_files(self):
        settings = self._get_settings()
        settings.setValue("recent_files", [])
        settings.sync()
        for win in MainWindow._open_windows:
            win._update_recent_files_menu()

    def _update_recent_files_menu(self):
        if not hasattr(self, "recent_files_menu"):
            return
        self.recent_files_menu.clear()
        files = self._get_recent_files()

        if not files:
            empty_act = self.recent_files_menu.addAction("（履歴はありません）")
            empty_act.setEnabled(False)
            return

        for idx, fp in enumerate(files, start=1):
            p = Path(fp)
            label = f"{idx}. {p.name}  ({p.parent})"
            act = self.recent_files_menu.addAction(label)
            act.setToolTip(fp)
            act.triggered.connect(lambda checked=False, path=fp: self._open_recent_file(path))

        self.recent_files_menu.addSeparator()
        clear_act = self.recent_files_menu.addAction("🗑 最近開いたファイルの履歴をクリア")
        clear_act.triggered.connect(self._clear_recent_files)

    def _open_recent_file(self, filepath):
        if not os.path.isfile(filepath):
            QMessageBox.warning(
                self,
                "ファイルが見つかりません",
                f"指定されたファイルが見つかりませんでした。履歴から削除します。\n{filepath}",
            )
            files = [
                f for f in self._get_recent_files()
                if os.path.normcase(os.path.abspath(f)) != os.path.normcase(os.path.abspath(filepath))
            ]
            settings = self._get_settings()
            settings.setValue("recent_files", files)
            settings.sync()
            for win in MainWindow._open_windows:
                win._update_recent_files_menu()
            return

        if self.viewer.is_modified:
            reply = QMessageBox.question(
                self,
                "確認",
                "未保存の編集があります。破棄して選択したPDFを開きますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.open_file_from_path(filepath)

    def _on_calc_selection_changed(self):
        items = self.viewer.calc_selected_items
        count = len(items)
        if count == 0:
            self.lbl_calc_sum.setText(" 🧮 合計: 0 (0個) ")
            self.btn_copy_sum.setEnabled(False)
            self.btn_clear_sum.setEnabled(False)
            self.statusBar().showMessage("数字の選択をクリアしました")
            return

        total = sum(item["value"] for item in items)
        avg = total / count
        total_str = format_number_value(total)
        avg_str = format_number_value(avg)

        self.lbl_calc_sum.setText(f" 🧮 合計: {total_str} ({count}個) ")
        self.btn_copy_sum.setEnabled(True)
        self.btn_clear_sum.setEnabled(True)

        formula_parts = [format_number_value(item["value"]) for item in items[:10]]
        formula_str = " + ".join(formula_parts).replace("+ -", "- ")
        if count > 10:
            formula_str += f" ... (他{count - 10}個)"

        self.statusBar().showMessage(
            f"🧮 合計: {total_str} （選択: {count}個 / 平均: {avg_str}） | 内訳: {formula_str}"
        )

    def _copy_calc_sum_to_clipboard(self):
        items = self.viewer.calc_selected_items
        if not items:
            return
        total = sum(item["value"] for item in items)
        if abs(total - round(total)) < 1e-7:
            raw_str = str(int(round(total)))
        else:
            raw_str = f"{total:.4f}".rstrip("0").rstrip(".")

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(raw_str)
            self.statusBar().showMessage(
                f"📋 合計値 「{raw_str}」 ({format_number_value(total)}) をクリップボードにコピーしました"
            )

    def _on_toolbar_color_activated(self, index):
        data = self.combo_color.itemData(index)
        if data == "pen_eraser":
            self.viewer.set_pen_eraser_active(True)
            if self.viewer.mode != "draw":
                self.set_mode("draw")
            else:
                self.statusBar().showMessage(
                    "🧽 ペン消しゴムモード：ペンで描いた線をクリックまたはドラッグして消去します（文字や罫線は消えません）"
                )
            return
        if data == "custom":
            initial = rgb_tuple_to_qcolor(self.viewer.current_color)
            chosen = QColorDialog.getColor(initial, self, "ペン・テキスト・マーカーの色を選択")
            if chosen.isValid():
                rgb = qcolor_to_rgb_tuple(chosen)
                self.combo_color.setItemIcon(index, create_color_swatch_icon(rgb))
                self.combo_color.setItemText(index, f"カスタム ({chosen.name()})")
                self.viewer.set_pen_eraser_active(False)
                self.viewer.set_current_color(rgb)
            else:
                self._sync_color_combobox(self.viewer.current_color)
        elif isinstance(data, tuple):
            self.viewer.set_pen_eraser_active(False)
            self.viewer.set_current_color(data)

    def _sync_color_combobox(self, rgb_tuple):
        sync_color_combo(
            self.combo_color,
            rgb_tuple,
            pen_eraser_active=bool(getattr(self.viewer, "pen_eraser_active", False)),
        )

    def _sync_draw_width_spinbox(self, width):
        if abs(self.spin_draw_width.value() - width) > 1e-4:
            self.spin_draw_width.blockSignals(True)
            self.spin_draw_width.setValue(width)
            self.spin_draw_width.blockSignals(False)

    def _sync_font_spinbox(self, size):
        if abs(self.spin_font_size.value() - size) > 1e-4:
            self.spin_font_size.blockSignals(True)
            self.spin_font_size.setValue(size)
            self.spin_font_size.blockSignals(False)

    def add_action(self, toolbar, name, callback):
        action = QAction(name, self)
        action.triggered.connect(callback)
        toolbar.addAction(action)
        return action

    def _sync_text_data_action(self):
        """「📝 文字データ」ボタンの有効/無効を、文書を開いているかどうかで切り替える"""
        if not hasattr(self, "act_text_data"):
            return
        doc = self.viewer.current_pdf
        has_doc = False
        if doc is not None:
            try:
                has_doc = len(doc) > 0
            except Exception:
                has_doc = False
        self.act_text_data.setEnabled(has_doc)

    def update_window_title(self):
        self._sync_text_data_action()
        if not self.viewer.current_pdf:
            self.setWindowTitle(APP_NAME)
            return
        name = Path(self.viewer.current_file).name if self.viewer.current_file else "(新規結合ドキュメント)"
        mark = " *" if self.viewer.is_modified else ""
        self.setWindowTitle(f"{APP_NAME} - {name}{mark}")

    def update_page_indicator(self):
        doc = self.viewer.current_pdf
        if not doc or len(doc) == 0:
            self.lbl_page_info.setText(" 0 / 0 ")
        else:
            self.lbl_page_info.setText(f" {self.viewer.current_page_idx + 1} / {len(doc)} ")
        self.update_window_title()

    def spawn_new_window(self, filepath=None):
        new_win = MainWindow()
        new_win._apply_initial_geometry(offset_from=self)
        new_win.show()
        if isinstance(filepath, (str, os.PathLike)) and filepath and os.path.isfile(filepath):
            QTimer.singleShot(0, lambda p=str(filepath): new_win.open_file_from_path(p))
        return new_win

    def _sync_mode_action_checked(self, target_mode: str):
        if not hasattr(self, "mode_group"):
            return
        for act in self.mode_group.actions():
            if act.data() == target_mode:
                if not act.isChecked():
                    act.setChecked(True)
                break

    def toggle_scroll_mode(self):
        """ツールバーの「📜 スクロール表示」ボタンで通常画面の表示を切り替える"""
        self.viewer.set_scroll_mode(self.act_scroll_mode.isChecked())

    def _sync_scroll_action(self, on: bool):
        if hasattr(self, "act_scroll_mode"):
            self.act_scroll_mode.blockSignals(True)
            self.act_scroll_mode.setChecked(on)
            self.act_scroll_mode.blockSignals(False)

    def set_mode(self, mode):
        # スクロール表示中に編集モードを選んだら、1ページ表示に戻って編集できるようにする。
        if mode != "view" and self.viewer.scroll_mode:
            self.viewer.set_scroll_mode(False)
        # 折りたたみモードのボタンを押したとき、すでに折りたたみ帯がある
        # ページでは、ボタンが折りたたみ表示のON/OFF切り替えになる。
        if mode == "fold" and self.viewer.has_fold_band():
            self.viewer.clear_fold_band()
            self.viewer.mode = "view"
            self._sync_mode_action_checked("view")
            self.viewer.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            self.statusBar().showMessage("折りたたみを解除しました")
            return
        self._sync_mode_action_checked(mode)
        self.viewer.cancel_active_interaction()
        if mode != "draw" and getattr(self.viewer, "pen_eraser_active", False):
            self.viewer.set_pen_eraser_active(False)
            self._sync_color_combobox(self.viewer.current_color)
        self.viewer.mode = mode

        if mode == "view":
            self.viewer.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            msg = "閲覧モード（ホイールでページ送り / Ctrl+ホイールで拡大縮小）"
        elif mode == "text_select":
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.IBeamCursor)
            msg = (
                "テキスト選択モード：ドラッグで文字を選択すると、近くにコピーボタンが出ます。"
                "ボタンを押すとコピーして閉じます（Esc・他の場所のクリックで解除）"
            )
        elif mode == "highlight":
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.CrossCursor)
            if self.viewer._last_hover_scene_pos is not None:
                self.viewer._update_hover_and_text_guide(self.viewer._last_hover_scene_pos)
            msg = (
                "マーカーモード：ドラッグした範囲に半透明の色を塗ります"
                "（引いたマーカー上で右クリック または Shift+左クリックでマーカーのみ消去可能）"
            )
        elif mode == "highlight_erase":
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            if self.viewer._last_hover_scene_pos is not None:
                self.viewer._update_hover_and_text_guide(self.viewer._last_hover_scene_pos)
            msg = (
                "マーカー消去モード：下の文字や罫線を残したまま、自分で引いたマーカーだけを"
                "クリック（またはドラッグで範囲選択）して消去します"
            )
        elif mode == "crop":
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.CrossCursor)
            msg = "トリミングモード：残したい範囲をドラッグして指定します（右ドラッグで画面スクロール）"
        elif mode == "fold":
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.CrossCursor)
            msg = (
                "折りたたみモード：折りたたみたい場所を左上から右下にドラッグしてください"
                "（横長のドラッグで横帯、縦長のドラッグで縦帯。帯の分だけ詰めて表示されます。"
                "PDF自体は変わりません。折りたたみ中は、もう一度ボタンを押すと解除します）"
            )
        elif mode == "box_erase":
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.CrossCursor)
            msg = "範囲墨消しモード：消去したい範囲をドラッグして白塗り墨消しします（右ドラッグで画面スクロール）"
        elif mode == "draw":
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.CrossCursor)
            if self.viewer._last_hover_scene_pos is not None:
                self.viewer._update_hover_and_text_guide(self.viewer._last_hover_scene_pos)
            msg = "ペンモード：ツールバーで「色」と「ペンの太さ」を変更できます（右ドラッグで画面スクロール）"
        elif mode == "type":
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.CrossCursor)
            if self.viewer._last_hover_scene_pos is not None:
                self.viewer._update_hover_and_text_guide(self.viewer._last_hover_scene_pos)
            msg = (
                "テキストモード：PDF内の数字・文字（緑枠）をクリックで書き換え・色変更 / "
                "Alt+クリックで即座に選択色へ変更 / 空白クリックで新規文字追加"
            )
        elif mode == "text_erase":
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            if self.viewer._last_hover_scene_pos is not None:
                self.viewer._update_hover_and_text_guide(self.viewer._last_hover_scene_pos)
            msg = "テキスト消去モード：元からPDFにある数字・文字や追加した文字をクリックまたは範囲選択で削除できます"
        elif mode == "erase":
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            msg = "消しゴムモード：ドラッグした軌跡を白塗りで消去します（右ドラッグまたはSpace+ドラッグで画面スクロール）"
        else:
            self.viewer.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewer.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            msg = "編集モード（右ドラッグまたはSpace+ドラッグでいつでも画面スクロール可能）"

        self.statusBar().showMessage(msg)

    def on_files_dropped_on_viewer(self, paths):
        if not paths:
            return
        if not self.viewer.current_pdf:
            self.open_file_from_path(paths[0])
            if len(paths) > 1 and self.viewer.current_pdf is not None:
                self.page_manager.on_files_dropped(len(self.viewer.current_pdf), paths[1:])
        else:
            self.page_manager.on_files_dropped(self.viewer.current_page_idx + 1, paths)

    def open_pdf_with_password(self, filepath):
        disk_doc = None
        try:
            raw_bytes = Path(filepath).read_bytes()
            with FITZ_LOCK:
                disk_doc = fitz.open(stream=raw_bytes, filetype="pdf")
                needs_pass = disk_doc.needs_pass
                is_enc_doc = PDFDocumentManager._is_encrypted_document(disk_doc)
            self._last_opened_was_encrypted = bool(needs_pass or is_enc_doc)
            if needs_pass:
                password, ok = QInputDialog.getText(
                    self,
                    "パスワード",
                    f"「{Path(filepath).name}」のパスワードを入力:",
                    QLineEdit.EchoMode.Password,
                )
                if not ok:
                    with FITZ_LOCK:
                        disk_doc.close()
                    return None
                with FITZ_LOCK:
                    auth_ok = bool(disk_doc.authenticate(password))
                if not auth_ok:
                    with FITZ_LOCK:
                        disk_doc.close()
                    QMessageBox.warning(self, "エラー", "パスワードが正しくありません。")
                    return None

            return disk_doc
        except Exception as e:
            if disk_doc is not None:
                with FITZ_LOCK:
                    disk_doc.close()
            QMessageBox.critical(self, "読み込みエラー", str(e))
            return None

    def open_file(self):
        if self.viewer.is_modified:
            reply = QMessageBox.question(
                self,
                "確認",
                "未保存の編集があります。破棄して新しいPDFを開きますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "PDFを開く",
            "",
            "PDF Files (*.pdf)",
        )

        if filepath:
            self.open_file_from_path(filepath)

    def open_file_from_path(self, filepath):
        filepath = os.path.abspath(filepath)
        doc = self.open_pdf_with_password(filepath)
        if doc is None:
            return

        if len(doc) == 0:
            doc.close()
            QMessageBox.warning(self, "エラー", "ページが存在しません。")
            return

        if not self._close_active_reader():
            with FITZ_LOCK:
                doc.close()
            return
        if self.viewer.current_pdf:
            with FITZ_LOCK:
                self.viewer.current_pdf.close()

        self.viewer.current_file = filepath
        self.viewer.current_pdf = doc
        self._reset_text_data_session()
        self._current_doc_from_bulk = False
        self.viewer.current_pdf_was_encrypted = bool(
            getattr(self, "_last_opened_was_encrypted", False)
            or PDFDocumentManager._is_encrypted_document(doc)
        )
        self.viewer._encryption_strip_approved = False
        self.viewer.reset_history()
        self.page_manager.clear_thumbnail_cache()

        self.viewer.load_pdf(doc, 0, fit_view=True)
        self.page_manager.refresh_thumbnails()
        self._add_recent_file(filepath)

        self.setWindowTitle(f"{APP_NAME} - {Path(filepath).name}")
        self.statusBar().showMessage(f"読み込み完了: {filepath}")

    def _encryption_strip_confirmed(self):
        """Return (proceed, allow_strip_encryption) for saving the current doc.

        A saved copy is always unencrypted, so when the opened PDF had
        password protection the user must confirm the strip first.
        """
        doc = self.viewer.current_pdf
        is_enc = bool(
            getattr(self.viewer, "current_pdf_was_encrypted", False)
            or (doc is not None and PDFDocumentManager._is_encrypted_document(doc))
        )
        if not is_enc:
            return True, False
        if getattr(self.viewer, "_encryption_strip_approved", False):
            return True, True
        reply = QMessageBox.question(
            self,
            "パスワード保護解除の確認",
            "元のPDFにはパスワード保護が設定されていました。\n"
            "保存するとパスワード保護が解除された状態で保存されます。続行しますか？\n"
            "（元の暗号化ファイルを残す場合は「いいえ」を選び、別名保存してください）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False, False
        return True, True

    def _should_force_save_as(self):
        """上書き保存をせず、別名保存ダイアログを使うべきかを返す。

        複数ファイルの一括読み込み由来の文書は、元のファイル群のどれにも
        上書きしないよう常に別名保存にする。保存先が未設定の文書も同様。
        """
        if self._current_doc_from_bulk:
            return True
        return not self.viewer.current_file

    def save_file(self):
        if not self.viewer.current_pdf:
            QMessageBox.information(self, "案内", "保存するPDFが開かれていません。先にPDFを開いてください。")
            return

        if self._should_force_save_as():
            if self._current_doc_from_bulk:
                QMessageBox.information(
                    self,
                    "別名保存の案内",
                    "複数のファイルから作ったドキュメントは上書き保存できません。別名で保存してください。",
                )
            self.save_as()
            return

        filepath = self.viewer.current_file
        if not filepath.lower().endswith(".pdf"):
            filepath += ".pdf"

        proceed, allow_strip = self._encryption_strip_confirmed()
        if not proceed:
            return

        self.save_to_path(filepath, allow_strip_encryption=allow_strip)

    def save_as(self):
        if not self.viewer.current_pdf:
            QMessageBox.information(self, "案内", "保存するPDFが開かれていません。先にPDFを開いてください。")
            return

        filepath, _ = QFileDialog.getSaveFileName(
            self,
            "PDFを別名保存",
            "",
            "PDF Files (*.pdf)",
        )

        if not filepath:
            return

        if not filepath.lower().endswith(".pdf"):
            filepath += ".pdf"

        proceed, allow_strip = self._encryption_strip_confirmed()
        if not proceed:
            return

        self.save_to_path(filepath, allow_strip_encryption=allow_strip)

    def print_file(self):
        """
        高精細（HD）リアルタイム印刷プレビューとプリンター設定を統合した印刷ダイアログを開く。
        """
        doc = self.viewer.current_pdf
        if not doc or len(doc) == 0:
            QMessageBox.information(self, "印刷", "印刷するPDFが開かれていません。")
            return

        self.viewer.cancel_active_interaction()

        import math
        import re
        import unicodedata
        from PyQt6.QtGui import QBrush, QColor, QPageSize, QPen, QPixmap
        from PyQt6.QtPrintSupport import QPrinterInfo
        from PyQt6.QtWidgets import (
            QCheckBox,
            QFormLayout,
            QGraphicsPixmapItem,
            QGraphicsRectItem,
            QGraphicsScene,
            QGroupBox,
            QHBoxLayout,
            QSpinBox,
            QVBoxLayout,
            QWidget,
        )

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            all_indices = list(range(len(doc)))
            work_doc = PDFDocumentManager.extract_pages_as_doc(
                doc,
                all_indices,
                self.viewer.page_annotations,
            )
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "印刷準備エラー", f"印刷用データの準備に失敗しました。\n{e}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        total_doc_pages = len(work_doc)
        if total_doc_pages == 0:
            work_doc.close()
            return

        cur_idx = max(0, min(self.viewer.current_page_idx, total_doc_pages - 1))
        doc_title = (
            Path(self.viewer.current_file).name if self.viewer.current_file else APP_NAME
        )

        avail_printers = QPrinterInfo.availablePrinters()
        default_pinfo = QPrinterInfo.defaultPrinter()
        default_pname = default_pinfo.printerName() if not default_pinfo.isNull() else ""

        class _HDPreviewCanvas(QGraphicsView):
            """マウスホイール拡大縮小・ドラッグ移動に対応した高精細プレビューキャンバス"""

            def __init__(self, scene, parent=None):
                super().__init__(scene, parent)
                self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                self.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
                self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
                self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
                self.setStyleSheet(
                    "QGraphicsView { background-color: #525659; border: 1px solid #b0b5bd; border-radius: 6px; }"
                )
                self.auto_fit_mode = "page"

            def wheelEvent(self, event):
                angle = event.angleDelta().y()
                cur_scale = self.transform().m11()
                if angle > 0 and cur_scale < 16.0:
                    self.auto_fit_mode = "manual"
                    self.scale(1.22, 1.22)
                elif angle < 0 and cur_scale > 0.08:
                    self.auto_fit_mode = "manual"
                    self.scale(1.0 / 1.22, 1.0 / 1.22)
                event.accept()

            def resizeEvent(self, event):
                super().resizeEvent(event)
                if self.auto_fit_mode == "page":
                    self.fit_whole_scene()
                elif self.auto_fit_mode == "width":
                    self.fit_scene_width()

            def fit_whole_scene(self):
                sr = self.scene().sceneRect()
                if not sr.isEmpty():
                    self.auto_fit_mode = "page"
                    self.fitInView(sr.adjusted(-24, -24, 24, 24), Qt.AspectRatioMode.KeepAspectRatio)

            def fit_scene_width(self):
                sr = self.scene().sceneRect()
                if not sr.isEmpty() and self.viewport().width() > 20:
                    self.auto_fit_mode = "width"
                    self.resetTransform()
                    target_w = max(100.0, float(self.viewport().width() - 40))
                    s = target_w / max(1.0, sr.width())
                    self.scale(s, s)
                    self.centerOn(sr.center().x(), sr.top() + (self.viewport().height() / (2.0 * s)))

        dlg = QDialog(self)
        dlg.setWindowFlags(
            dlg.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        dlg.setWindowTitle(f"🖨 印刷（高精細プレビュー） - {doc_title}")
        dlg.resize(1260, 860)
        dlg.setStyleSheet(
            "QDialog { background-color: #f4f5f7; color: #1a1a1a; }"
            "QGroupBox { font-weight: bold; border: 1px solid #c8ccd4; border-radius: 6px; "
            "  margin-top: 8px; padding-top: 10px; background-color: #ffffff; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
            "QLabel { color: #1a1a1a; }"
            "QComboBox, QSpinBox, QLineEdit { background-color: #ffffff; color: #111111; "
            "  border: 1px solid #b0b5bd; border-radius: 4px; padding: 4px 6px; }"
            "QPushButton { background-color: #ffffff; color: #1a1a1a; border: 1px solid #a0a5ad; "
            "  border-radius: 5px; padding: 5px 10px; font-weight: bold; }"
            "QPushButton:hover { background-color: #e9eef5; }"
        )

        root_layout = QHBoxLayout(dlg)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(12)

        # 左パネル：プリンター＆印刷設定
        left_panel = QWidget(dlg)
        left_panel.setFixedWidth(360)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        grp_printer = QGroupBox("1. プリンターと部数", left_panel)
        form_printer = QFormLayout(grp_printer)
        form_printer.setSpacing(8)

        combo_printer = QComboBox(grp_printer)
        default_combo_idx = 0
        for idx_p, p_info in enumerate(avail_printers):
            p_name = p_info.printerName()
            combo_printer.addItem(f"🖨 {p_name}", p_name)
            if p_name == default_pname:
                default_combo_idx = idx_p
        combo_printer.addItem("📄 PDFファイルとして保存...", "__SAVE_AS_PDF__")
        combo_printer.setCurrentIndex(default_combo_idx)
        form_printer.addRow("プリンター:", combo_printer)

        spin_copies = QSpinBox(grp_printer)
        spin_copies.setRange(1, 999)
        spin_copies.setValue(1)
        spin_copies.setSuffix(" 部")
        form_printer.addRow("部数:", spin_copies)

        chk_collate = QCheckBox("部単位で印刷する", grp_printer)
        chk_collate.setChecked(True)
        form_printer.addRow("", chk_collate)
        left_layout.addWidget(grp_printer)

        grp_range = QGroupBox("2. 印刷するページ", left_panel)
        form_range = QFormLayout(grp_range)
        form_range.setSpacing(8)

        combo_scope = QComboBox(grp_range)
        combo_scope.addItem(f"すべてのページ (全 {total_doc_pages} ページ)", "all")
        combo_scope.addItem(f"現在のページのみ ({cur_idx + 1} ページ目)", "current")
        combo_scope.addItem("ページ番号を指定...", "custom")
        form_range.addRow("範囲:", combo_scope)

        edit_custom_pages = QLineEdit(grp_range)
        edit_custom_pages.setPlaceholderText(f"例: 1-3, 5 （1〜{total_doc_pages}）")
        edit_custom_pages.setText(f"1-{total_doc_pages}" if total_doc_pages > 1 else "1")
        edit_custom_pages.setEnabled(False)
        form_range.addRow("ページ指定:", edit_custom_pages)
        left_layout.addWidget(grp_range)

        grp_layout = QGroupBox("3. レイアウト・用紙設定", left_panel)
        form_layout = QFormLayout(grp_layout)
        form_layout.setSpacing(8)

        combo_orient = QComboBox(grp_layout)
        combo_orient.addItem("自動（ページの縦横に合わせる）", "auto")
        combo_orient.addItem("縦向き (Portrait)", "portrait")
        combo_orient.addItem("横向き (Landscape)", "landscape")
        form_layout.addRow("用紙の向き:", combo_orient)

        combo_paper = QComboBox(grp_layout)
        paper_specs = [
            ("A4 (210 × 297 mm)", QPageSize.PageSizeId.A4),
            ("A3 (297 × 420 mm)", QPageSize.PageSizeId.A3),
            ("B5 (182 × 257 mm)", QPageSize.PageSizeId.B5),
            ("JIS B4 (257 × 364 mm)", getattr(QPageSize.PageSizeId, "JisB4", QPageSize.PageSizeId.B4)),
            ("A5 (148 × 210 mm)", QPageSize.PageSizeId.A5),
            ("レター (Letter)", QPageSize.PageSizeId.Letter),
        ]
        for label_str, ps_id in paper_specs:
            combo_paper.addItem(label_str, ps_id)
        form_layout.addRow("用紙サイズ:", combo_paper)

        combo_color = QComboBox(grp_layout)
        combo_color.addItem("カラー", "color")
        combo_color.addItem("白黒（グレースケール）", "grayscale")
        form_layout.addRow("カラーモード:", combo_color)

        combo_duplex = QComboBox(grp_layout)
        combo_duplex.addItem("片面印刷", QPrinter.DuplexMode.DuplexNone)
        combo_duplex.addItem("両面印刷（長辺とじ）", QPrinter.DuplexMode.DuplexLongSide)
        combo_duplex.addItem("両面印刷（短辺とじ）", QPrinter.DuplexMode.DuplexShortSide)
        form_layout.addRow("両面印刷:", combo_duplex)

        combo_scale = QComboBox(grp_layout)
        combo_scale.addItem("用紙に合わせる（余白あり・推奨）", "fit_margin")
        combo_scale.addItem("用紙いっぱいに合わせる（フチなし相当）", "fit_full")
        combo_scale.addItem("実際のサイズ (100%等倍)", "actual")
        form_layout.addRow("拡大・縮小:", combo_scale)

        left_layout.addWidget(grp_layout)
        left_layout.addStretch()

        btn_sys_dlg = QPushButton("⚙ システム印刷ダイアログで開く...", left_panel)
        btn_sys_dlg.setToolTip("メーカー固有の給紙トレイや詳細ドライバ設定を開く場合に使用します")
        left_layout.addWidget(btn_sys_dlg)

        action_row = QHBoxLayout()
        btn_cancel = QPushButton("キャンセル", left_panel)
        btn_print_now = QPushButton("🖨 印刷する", left_panel)
        btn_print_now.setDefault(True)
        btn_print_now.setStyleSheet(
            "QPushButton { background-color: #1971c2; color: #ffffff; border: 1px solid #145a9e; "
            "  padding: 9px 18px; font-size: 14px; font-weight: bold; }"
            "QPushButton:hover { background-color: #1864ab; }"
        )
        action_row.addWidget(btn_cancel)
        action_row.addWidget(btn_print_now, stretch=1)
        left_layout.addLayout(action_row)

        root_layout.addWidget(left_panel)

        # 右パネル：高精細（HD）リアルタイムプレビュー
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        preview_bar = QHBoxLayout()
        preview_bar.setSpacing(6)

        lbl_preview_title = QLabel("📄 高精細プレビュー", dlg)
        lbl_preview_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        preview_bar.addWidget(lbl_preview_title)
        preview_bar.addSpacing(6)

        btn_prev = QPushButton("◀ 前へ", dlg)
        lbl_page_status = QLabel(" 1 / 1 ", dlg)
        lbl_page_status.setStyleSheet("font-weight: bold; padding: 0 4px;")
        btn_next = QPushButton("次へ ▶", dlg)
        preview_bar.addWidget(btn_prev)
        preview_bar.addWidget(lbl_page_status)
        preview_bar.addWidget(btn_next)

        preview_bar.addSpacing(8)

        btn_zoom_out = QPushButton("🔎－", dlg)
        btn_zoom_in = QPushButton("🔍＋", dlg)
        btn_fit_one = QPushButton("📄 1ページ表示", dlg)
        btn_fit_width = QPushButton("↔ 幅に合わせる", dlg)
        btn_all_pages = QPushButton("🔲 一覧表示", dlg)
        preview_bar.addWidget(btn_zoom_out)
        preview_bar.addWidget(btn_zoom_in)
        preview_bar.addWidget(btn_fit_one)
        preview_bar.addWidget(btn_fit_width)
        preview_bar.addWidget(btn_all_pages)
        preview_bar.addStretch()

        right_layout.addLayout(preview_bar)

        preview_scene = QGraphicsScene(dlg)
        preview_view = _HDPreviewCanvas(preview_scene, dlg)
        right_layout.addWidget(preview_view, stretch=1)

        hint_lbl = QLabel(
            "💡 ヒント: プレビュー上でマウスホイールを回すと高画質のまま拡大・縮小、ドラッグで移動できます。",
            dlg,
        )
        hint_lbl.setStyleSheet("color: #555; font-size: 11px;")
        right_layout.addWidget(hint_lbl)

        root_layout.addLayout(right_layout, stretch=1)

        state = {
            "view_mode": "single",
            "preview_pos": 0,
            "target_indices": list(range(total_doc_pages)),
        }

        def _parse_custom_page_indices(spec_str: str):
            norm = unicodedata.normalize("NFKC", spec_str or "").strip()
            if not norm:
                return list(range(total_doc_pages))
            norm = re.sub(r"[ー―‐–—−〜~]", "-", norm)
            norm = re.sub(r"[、，；;]", ",", norm)
            indices = []
            seen = set()
            for part in norm.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    halves = part.split("-", 1)
                    try:
                        start_p = max(1, int(halves[0].strip()))
                        end_p = min(total_doc_pages, int(halves[1].strip()))
                        if end_p < start_p:
                            start_p, end_p = end_p, start_p
                        for p_num in range(start_p, end_p + 1):
                            idx0 = p_num - 1
                            if 0 <= idx0 < total_doc_pages and idx0 not in seen:
                                seen.add(idx0)
                                indices.append(idx0)
                    except ValueError:
                        continue
                else:
                    try:
                        idx0 = int(part) - 1
                        if 0 <= idx0 < total_doc_pages and idx0 not in seen:
                            seen.add(idx0)
                            indices.append(idx0)
                    except ValueError:
                        continue
            return indices if indices else [cur_idx]

        def _get_selected_page_indices():
            scope = combo_scope.currentData()
            if scope == "current":
                return [cur_idx]
            if scope == "custom":
                return _parse_custom_page_indices(edit_custom_pages.text())
            return list(range(total_doc_pages))

        def _get_paper_size_points(page_idx: int):
            """選択中の用紙サイズと向きにおける用紙の (幅pt, 高さpt) を返す"""
            ps_id = combo_paper.currentData() or QPageSize.PageSizeId.A4
            pt_size = QPageSize(ps_id).size(QPageSize.Unit.Point)
            pw = max(100.0, float(pt_size.width()))
            ph = max(100.0, float(pt_size.height()))
            if pw > ph:
                pw, ph = ph, pw

            orient_mode = combo_orient.currentData()
            if orient_mode == "landscape":
                return ph, pw
            if orient_mode == "portrait":
                return pw, ph
            page_r = work_doc[page_idx].rect
            if page_r.width > page_r.height:
                return ph, pw
            return pw, ph

        def _render_high_res_sheet_pixmap(page_idx: int, render_dpi: int = 260) -> QPixmap:
            paper_w_pt, paper_h_pt = _get_paper_size_points(page_idx)
            scale_pt_to_px = render_dpi / 72.0
            sheet_w_px = max(200, int(round(paper_w_pt * scale_pt_to_px)))
            sheet_h_px = max(200, int(round(paper_h_pt * scale_pt_to_px)))

            page = work_doc[page_idx]
            pr = page.rect
            scale_mode = combo_scale.currentData()
            margin_pt = 14.0 if scale_mode == "fit_margin" else 0.0

            avail_w_pt = max(20.0, paper_w_pt - margin_pt * 2.0)
            avail_h_pt = max(20.0, paper_h_pt - margin_pt * 2.0)

            if scale_mode == "actual":
                draw_w_pt = pr.width
                draw_h_pt = pr.height
            else:
                ratio = min(avail_w_pt / max(1.0, pr.width), avail_h_pt / max(1.0, pr.height))
                draw_w_pt = pr.width * ratio
                draw_h_pt = pr.height * ratio

            draw_w_px = max(1, int(round(draw_w_pt * scale_pt_to_px)))
            draw_h_px = max(1, int(round(draw_h_pt * scale_pt_to_px)))
            draw_x_px = int(round((sheet_w_px - draw_w_px) * 0.5))
            draw_y_px = int(round((sheet_h_px - draw_h_px) * 0.5))

            mat_sx = draw_w_px / max(1.0, pr.width)
            mat_sy = draw_h_px / max(1.0, pr.height)

            with FITZ_LOCK:
                pix = page.get_pixmap(
                    matrix=fitz.Matrix(mat_sx, mat_sy),
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

            if combo_color.currentData() == "grayscale":
                qimg = (
                    qimg.convertToFormat(QImage.Format.Format_Grayscale8)
                    .convertToFormat(QImage.Format.Format_RGB32)
                )

            sheet_img = QImage(sheet_w_px, sheet_h_px, QImage.Format.Format_RGB32)
            sheet_img.fill(QColor(255, 255, 255))

            p = QPainter(sheet_img)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawImage(
                QRectF(draw_x_px, draw_y_px, draw_w_px, draw_h_px),
                qimg,
                QRectF(0.0, 0.0, float(qimg.width()), float(qimg.height())),
            )

            if margin_pt > 0:
                m_px = margin_pt * scale_pt_to_px
                pen = QPen(QColor(210, 215, 222), max(1.0, scale_pt_to_px * 0.5), Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(QRectF(m_px, m_px, sheet_w_px - m_px * 2, sheet_h_px - m_px * 2))

            p.end()
            return QPixmap.fromImage(sheet_img)

        def _rebuild_preview_scene(reset_fit: bool = False):
            if getattr(work_doc, "is_closed", False):
                return
            indices = _get_selected_page_indices()
            state["target_indices"] = indices
            total_sel = len(indices)

            preview_scene.clear()
            if total_sel == 0:
                lbl_page_status.setText(" 0 / 0 ")
                btn_prev.setEnabled(False)
                btn_next.setEnabled(False)
                return

            state["preview_pos"] = max(0, min(state["preview_pos"], total_sel - 1))
            cur_pos = state["preview_pos"]

            if state["view_mode"] == "single":
                lbl_page_status.setText(
                    f" {cur_pos + 1} / {total_sel} （元PDF: {indices[cur_pos] + 1}P） "
                )
                btn_prev.setEnabled(cur_pos > 0)
                btn_next.setEnabled(cur_pos < total_sel - 1)

                sheet_pix = _render_high_res_sheet_pixmap(indices[cur_pos], render_dpi=280)
                pw = float(sheet_pix.width())
                ph = float(sheet_pix.height())

                shadow = QGraphicsRectItem(QRectF(14.0, 14.0, pw, ph))
                shadow.setBrush(QBrush(QColor(0, 0, 0, 85)))
                shadow.setPen(QPen(Qt.PenStyle.NoPen))
                preview_scene.addItem(shadow)

                pix_item = QGraphicsPixmapItem(sheet_pix)
                pix_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
                preview_scene.addItem(pix_item)

                border = QGraphicsRectItem(QRectF(0.0, 0.0, pw, ph))
                border.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                border.setPen(QPen(QColor(120, 125, 130), 2.0))
                preview_scene.addItem(border)

                preview_scene.setSceneRect(QRectF(-20.0, -20.0, pw + 50.0, ph + 50.0))
            else:
                lbl_page_status.setText(f" 全 {total_sel} ページ一覧 ")
                btn_prev.setEnabled(False)
                btn_next.setEnabled(False)

                max_grid_pages = min(total_sel, 24)
                cols = max(1, min(4, int(math.ceil(math.sqrt(max_grid_pages)))))
                grid_dpi = 130 if max_grid_pages <= 9 else 96

                cur_x = 0.0
                cur_y = 0.0
                row_h = 0.0
                gap = 48.0
                max_right = 0.0

                for i in range(max_grid_pages):
                    p_idx = indices[i]
                    sheet_pix = _render_high_res_sheet_pixmap(p_idx, render_dpi=grid_dpi)
                    pw = float(sheet_pix.width())
                    ph = float(sheet_pix.height())

                    if i > 0 and (i % cols) == 0:
                        cur_x = 0.0
                        cur_y += row_h + gap
                        row_h = 0.0

                    shadow = QGraphicsRectItem(QRectF(cur_x + 8.0, cur_y + 8.0, pw, ph))
                    shadow.setBrush(QBrush(QColor(0, 0, 0, 80)))
                    shadow.setPen(QPen(Qt.PenStyle.NoPen))
                    preview_scene.addItem(shadow)

                    pix_item = QGraphicsPixmapItem(sheet_pix)
                    pix_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
                    pix_item.setPos(cur_x, cur_y)
                    preview_scene.addItem(pix_item)

                    border = QGraphicsRectItem(QRectF(cur_x, cur_y, pw, ph))
                    border.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                    border.setPen(QPen(QColor(110, 115, 120), 1.5))
                    preview_scene.addItem(border)

                    cur_x += pw + gap
                    row_h = max(row_h, ph)
                    max_right = max(max_right, cur_x - gap)

                total_h = cur_y + row_h
                preview_scene.setSceneRect(QRectF(-24.0, -24.0, max_right + 48.0, total_h + 48.0))

            if reset_fit or preview_view.auto_fit_mode == "page":
                preview_view.fit_whole_scene()
            elif preview_view.auto_fit_mode == "width":
                preview_view.fit_scene_width()

        def _apply_settings_to_printer(target_printer: QPrinter, first_page_idx: int):
            ps_id = combo_paper.currentData()
            if ps_id is not None:
                target_printer.setPageSize(QPageSize(ps_id))

            orient_mode = combo_orient.currentData()
            if orient_mode == "portrait":
                target_printer.setPageOrientation(QPageLayout.Orientation.Portrait)
            elif orient_mode == "landscape":
                target_printer.setPageOrientation(QPageLayout.Orientation.Landscape)
            else:
                p_ref = work_doc[first_page_idx]
                target_printer.setPageOrientation(
                    QPageLayout.Orientation.Landscape
                    if p_ref.rect.width > p_ref.rect.height
                    else QPageLayout.Orientation.Portrait
                )

            is_gray = combo_color.currentData() == "grayscale"
            target_printer.setColorMode(
                QPrinter.ColorMode.GrayScale if is_gray else QPrinter.ColorMode.Color
            )
            duplex_mode = combo_duplex.currentData()
            if duplex_mode is not None:
                target_printer.setDuplex(duplex_mode)

        def _render_pages_to_printer(target_printer: QPrinter, page_indices=None) -> int:
            indices = page_indices if page_indices is not None else _get_selected_page_indices()
            if not indices:
                return 0

            orient_mode = combo_orient.currentData()
            is_gray = combo_color.currentData() == "grayscale"
            scale_mode = combo_scale.currentData()
            target_printer.setFullPage(scale_mode == "fit_full")

            _apply_settings_to_printer(target_printer, indices[0])

            painter = QPainter()
            if not painter.begin(target_printer):
                raise RuntimeError("プリンターへの描画開始に失敗しました。")

            try:
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                dpi = target_printer.resolution()
                render_dpi = max(300, min(600, int(dpi) if dpi > 0 else 360))
                scale = render_dpi / 72.0

                for i, page_idx in enumerate(indices):
                    page = work_doc[page_idx]
                    if i > 0:
                        if orient_mode == "auto":
                            target_printer.setPageOrientation(
                                QPageLayout.Orientation.Landscape
                                if page.rect.width > page.rect.height
                                else QPageLayout.Orientation.Portrait
                            )
                        if not target_printer.newPage():
                            raise RuntimeError(f"{page_idx + 1}ページ目の改ページ処理に失敗しました。")

                    with FITZ_LOCK:
                        pix = page.get_pixmap(
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

                    if is_gray:
                        qimg = (
                            qimg.convertToFormat(QImage.Format.Format_Grayscale8)
                            .convertToFormat(QImage.Format.Format_RGB888)
                        )

                    target_rect = QRectF(painter.viewport())
                    if (
                        target_rect.width() <= 0
                        or target_rect.height() <= 0
                        or qimg.width() <= 0
                        or qimg.height() <= 0
                    ):
                        continue

                    if scale_mode == "actual" and dpi > 0:
                        draw_w = (page.rect.width / 72.0) * dpi
                        draw_h = (page.rect.height / 72.0) * dpi
                        draw_x = target_rect.left() + (target_rect.width() - draw_w) / 2.0
                        draw_y = target_rect.top() + (target_rect.height() - draw_h) / 2.0
                    else:
                        img_ratio = qimg.width() / qimg.height()
                        rect_ratio = target_rect.width() / target_rect.height()
                        if img_ratio > rect_ratio:
                            draw_w = target_rect.width()
                            draw_h = draw_w / img_ratio
                            draw_x = target_rect.left()
                            draw_y = target_rect.top() + (target_rect.height() - draw_h) / 2.0
                        else:
                            draw_h = target_rect.height()
                            draw_w = draw_h * img_ratio
                            draw_x = target_rect.left() + (target_rect.width() - draw_w) / 2.0
                            draw_y = target_rect.top()

                    painter.drawImage(
                        QRectF(draw_x, draw_y, draw_w, draw_h),
                        qimg,
                        QRectF(0.0, 0.0, float(qimg.width()), float(qimg.height())),
                    )

                return len(indices)
            finally:
                if painter.isActive():
                    painter.end()

        def _on_setting_changed(*_args):
            edit_custom_pages.setEnabled(combo_scope.currentData() == "custom")
            _rebuild_preview_scene(reset_fit=False)

        def _go_prev():
            if state["preview_pos"] > 0:
                state["view_mode"] = "single"
                state["preview_pos"] -= 1
                _rebuild_preview_scene(reset_fit=False)

        def _go_next():
            if state["preview_pos"] < len(state["target_indices"]) - 1:
                state["view_mode"] = "single"
                state["preview_pos"] += 1
                _rebuild_preview_scene(reset_fit=False)

        def _zoom_in():
            if preview_view.transform().m11() < 16.0:
                preview_view.auto_fit_mode = "manual"
                preview_view.scale(1.25, 1.25)

        def _zoom_out():
            if preview_view.transform().m11() > 0.08:
                preview_view.auto_fit_mode = "manual"
                preview_view.scale(1.0 / 1.25, 1.0 / 1.25)

        def _set_single_page_fit():
            state["view_mode"] = "single"
            preview_view.auto_fit_mode = "page"
            _rebuild_preview_scene(reset_fit=True)

        def _set_single_page_width():
            state["view_mode"] = "single"
            preview_view.auto_fit_mode = "width"
            _rebuild_preview_scene(reset_fit=False)

        def _set_grid_overview():
            state["view_mode"] = "grid"
            preview_view.auto_fit_mode = "page"
            _rebuild_preview_scene(reset_fit=True)

        def _execute_direct_print():
            indices = _get_selected_page_indices()
            if not indices:
                QMessageBox.warning(dlg, "ページ範囲エラー", "印刷する有効なページが選択されていません。")
                return

            sel_printer = combo_printer.currentData()
            job_printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            job_printer.setDocName(doc_title)
            job_printer.setCopyCount(spin_copies.value())
            job_printer.setCollateCopies(chk_collate.isChecked())

            if sel_printer == "__SAVE_AS_PDF__":
                out_path, _ = QFileDialog.getSaveFileName(
                    dlg, "印刷イメージをPDFとして保存", "", "PDF Files (*.pdf)"
                )
                if not out_path:
                    return
                if not out_path.lower().endswith(".pdf"):
                    out_path += ".pdf"
                job_printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
                job_printer.setOutputFileName(out_path)
            else:
                if not sel_printer:
                    QMessageBox.warning(dlg, "プリンター未選択", "出力先のプリンターが見つかりません。")
                    return
                job_printer.setOutputFormat(QPrinter.OutputFormat.NativeFormat)
                job_printer.setPrinterName(sel_printer)

            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                count = _render_pages_to_printer(job_printer, indices)
            except Exception as e:
                QApplication.restoreOverrideCursor()
                QMessageBox.critical(dlg, "印刷エラー", f"印刷中にエラーが発生しました。\n{e}")
                return
            finally:
                QApplication.restoreOverrideCursor()

            self.statusBar().showMessage(
                f"🖨 印刷ジョブを送信しました（全 {count} ページ / {spin_copies.value()} 部）"
            )
            dlg.accept()

        def _open_system_print_dialog():
            sys_printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            sys_printer.setDocName(doc_title)
            sel_printer = combo_printer.currentData()
            if sel_printer and sel_printer != "__SAVE_AS_PDF__":
                sys_printer.setPrinterName(sel_printer)
            sys_printer.setCopyCount(spin_copies.value())
            sys_printer.setCollateCopies(chk_collate.isChecked())

            sys_dlg = QPrintDialog(sys_printer, dlg)
            sys_dlg.setWindowTitle("システム印刷ダイアログ")
            sys_dlg.setOption(QPrintDialog.PrintDialogOption.PrintToFile, True)
            sys_dlg.setOption(QPrintDialog.PrintDialogOption.PrintPageRange, True)
            sys_dlg.setOption(QPrintDialog.PrintDialogOption.PrintCurrentPage, True)
            sys_dlg.setOption(QPrintDialog.PrintDialogOption.PrintCollateCopies, True)
            sys_dlg.setMinMax(1, total_doc_pages)
            sys_printer.setFromTo(1, total_doc_pages)
            if sys_dlg.exec() != QDialog.DialogCode.Accepted:
                return

            p_range = sys_printer.printRange()
            if p_range == QPrinter.PrintRange.PageRange:
                from_p = max(1, sys_printer.fromPage())
                to_p = min(total_doc_pages, sys_printer.toPage())
                if to_p < from_p:
                    from_p, to_p = to_p, from_p
                indices = list(range(from_p - 1, to_p))
            elif p_range == QPrinter.PrintRange.CurrentPage:
                indices = [cur_idx]
            else:
                indices = _get_selected_page_indices()

            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                count = _render_pages_to_printer(sys_printer, indices)
            except Exception as e:
                QApplication.restoreOverrideCursor()
                QMessageBox.critical(dlg, "印刷エラー", f"印刷中にエラーが発生しました。\n{e}")
                return
            finally:
                QApplication.restoreOverrideCursor()

            self.statusBar().showMessage(f"🖨 印刷ジョブを送信しました（全 {count} ページ）")
            dlg.accept()

        combo_scope.currentIndexChanged.connect(_on_setting_changed)
        edit_custom_pages.textChanged.connect(_on_setting_changed)
        combo_orient.currentIndexChanged.connect(_on_setting_changed)
        combo_paper.currentIndexChanged.connect(_on_setting_changed)
        combo_color.currentIndexChanged.connect(_on_setting_changed)
        combo_scale.currentIndexChanged.connect(_on_setting_changed)

        btn_prev.clicked.connect(_go_prev)
        btn_next.clicked.connect(_go_next)
        btn_zoom_in.clicked.connect(_zoom_in)
        btn_zoom_out.clicked.connect(_zoom_out)
        btn_fit_one.clicked.connect(_set_single_page_fit)
        btn_fit_width.clicked.connect(_set_single_page_width)
        btn_all_pages.clicked.connect(_set_grid_overview)

        btn_sys_dlg.clicked.connect(_open_system_print_dialog)
        btn_cancel.clicked.connect(dlg.reject)
        btn_print_now.clicked.connect(_execute_direct_print)

        try:
            QTimer.singleShot(10, lambda: _rebuild_preview_scene(reset_fit=True))
            dlg.exec()
        finally:
            work_doc.close()

    def export_to_excel(self):
        """PDF内の表やテキストを追加ライブラリなしで Excel (.xlsx) に変換して保存する"""
        doc = self.viewer.current_pdf
        if not doc or len(doc) == 0:
            QMessageBox.information(self, "Excel出力", "Excelに変換するPDFが開かれていません。先にPDFを開いてください。")
            return

        self.viewer.cancel_active_interaction()

        from PyQt6.QtWidgets import QCheckBox, QFormLayout, QVBoxLayout
        from core.excel_exporter import export_pdf_document_to_xlsx

        dlg = QDialog(self)
        dlg.setWindowTitle("📊 Excel (.xlsx) 変換・出力設定")
        dlg.setMinimumWidth(420)
        dlg.setStyleSheet(
            "QDialog { background-color: #f8f9fa; color: #1a1a1a; }"
            "QLabel, QCheckBox { color: #1a1a1a; background-color: transparent; }"
            "QComboBox { background-color: #ffffff; color: #111111; border: 1px solid #b0b5bd; border-radius: 4px; padding: 4px; }"
            "QPushButton { background-color: #ffffff; color: #1a1a1a; border: 1px solid #a0a5ad; border-radius: 4px; padding: 6px 14px; font-weight: bold; }"
            "QPushButton:hover { background-color: #eef2f7; }"
        )

        layout = QVBoxLayout(dlg)
        form = QFormLayout()
        form.setSpacing(10)

        combo_scope = QComboBox(dlg)
        combo_scope.addItem(f"現在のページのみ ({self.viewer.current_page_idx + 1}ページ目)", "current")
        if len(doc) > 1:
            combo_scope.addItem(f"すべてのページ一括 (全 {len(doc)} ページ)", "all")
        form.addRow("対象ページ:", combo_scope)

        combo_mode = QComboBox(dlg)
        combo_mode.addItem("自動（表の罫線を優先＋周辺の文字も出力）", "auto")
        combo_mode.addItem("表（罫線テーブル）のみ抽出", "tables_only")
        combo_mode.addItem("ページ全体の文字配置を行・列化", "layout")
        form.addRow("抽出方法:", combo_mode)

        combo_sheet = QComboBox(dlg)
        combo_sheet.addItem("ページごとにシートを分ける（推奨）", True)
        combo_sheet.addItem("1つのシートに縦につなげて出力", False)
        if len(doc) <= 1:
            combo_sheet.setEnabled(False)
        form.addRow("シート構成:", combo_sheet)

        chk_num = QCheckBox("金額・数字をExcelの数値セルとして出力（SUM計算可能）", dlg)
        chk_num.setChecked(True)
        form.addRow("", chk_num)

        layout.addLayout(form)

        info_lbl = QLabel(
            "※スキャンした画像PDFの場合は、先に「🎯 範囲OCR」または「🧼 スキャン色抜き・OCR」で文字認識するとExcelへ出力できます。"
        )
        info_lbl.setWordWrap(True)
        info_lbl.setStyleSheet("color: #555; font-size: 11px; margin-top: 6px;")
        layout.addWidget(info_lbl)

        from PyQt6.QtWidgets import QHBoxLayout
        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("キャンセル", dlg)
        btn_cancel.clicked.connect(dlg.reject)
        btn_ok = QPushButton("📊 保存先を選んでExcel出力", dlg)
        btn_ok.setDefault(True)
        btn_ok.setStyleSheet(
            "QPushButton { background-color: #1f6f43; color: #ffffff; border: 1px solid #165231; }"
            "QPushButton:hover { background-color: #288a54; }"
        )
        btn_ok.clicked.connect(dlg.accept)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        layout.addLayout(btn_row)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        default_stem = Path(self.viewer.current_file).stem if self.viewer.current_file else "kinako_export"
        default_dir = str(Path(self.viewer.current_file).parent) if self.viewer.current_file else ""
        default_out = str(Path(default_dir) / f"{default_stem}.xlsx") if default_dir else f"{default_stem}.xlsx"

        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Excelファイル (.xlsx) として保存",
            default_out,
            "Excel Workbook (*.xlsx)",
        )
        if not out_path:
            return
        if not out_path.lower().endswith(".xlsx"):
            out_path += ".xlsx"

        indices = list(range(len(doc))) if combo_scope.currentData() == "all" else [self.viewer.current_page_idx]
        extract_mode = combo_mode.currentData() or "auto"
        split_sheets = bool(combo_sheet.currentData())
        convert_numbers = chk_num.isChecked()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            sheet_cnt, row_cnt = export_pdf_document_to_xlsx(
                doc=doc,
                page_indices=indices,
                output_path=out_path,
                page_annotations=self.viewer.page_annotations,
                mode=extract_mode,
                split_sheets=split_sheets,
                convert_numbers=convert_numbers,
            )
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Excel出力エラー", f"Excelファイルの作成に失敗しました。\n{e}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.statusBar().showMessage(f"📊 Excel出力完了: {out_path} ({sheet_cnt}シート / {row_cnt}行)")
        reply = QMessageBox.question(
            self,
            "Excel出力完了",
            f"Excelファイルを保存しました！\n\n"
            f"保存先: {out_path}\n"
            f"出力内容: {sheet_cnt} シート / 合計 {row_cnt} 行\n\n"
            "今すぐこのExcelファイルを開きますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                if sys.platform == "win32":
                    os.startfile(out_path)
                else:
                    import subprocess as _sp
                    _sp.Popen(["open" if sys.platform == "darwin" else "xdg-open", out_path])
            except Exception as open_err:
                QMessageBox.warning(self, "起動エラー", f"Excelファイルを開けませんでした。\n{open_err}")

    def save_to_path(self, filepath, allow_strip_encryption=False):
        doc = self.viewer.current_pdf
        if not doc:
            self.statusBar().showMessage("保存するPDFが開かれていません。")
            return

        try:
            self.viewer.cancel_active_interaction()
            try:
                active_doc, replace_err = PDFDocumentManager.save_safely(
                    source_doc=doc,
                    page_annotations=self.viewer.page_annotations,
                    filepath=filepath,
                    current_file=self.viewer.current_file,
                    allow_strip_encryption=allow_strip_encryption,
                )
            except TypeError:
                # Core without the allow_strip_encryption guard yet.
                active_doc, replace_err = PDFDocumentManager.save_safely(
                    source_doc=doc,
                    page_annotations=self.viewer.page_annotations,
                    filepath=filepath,
                    current_file=self.viewer.current_file,
                )

            self.viewer.current_pdf = active_doc
            if replace_err is not None:
                raise replace_err

            # 複数ファイル由来の文書は、別名保存しても保存先をそのファイルに
            # 固定しない（次回保存も別名保存にし、元のファイル群や保存した
            # 結合ファイルへの上書きを防ぐ）。
            self.viewer.current_file = (
                None if self._current_doc_from_bulk else filepath
            )
            self.viewer.current_pdf_was_encrypted = False
            if allow_strip_encryption:
                self.viewer._encryption_strip_approved = True
            self.viewer.history.mark_saved()
            self._add_recent_file(filepath)

            self.setWindowTitle(f"{APP_NAME} - {Path(filepath).name}")
            self.statusBar().showMessage(f"保存完了: {filepath}")

            QMessageBox.information(
                self,
                "保存完了",
                "PDFを検証し、正常に保存しました。",
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "保存エラー",
                f"保存に失敗しました（編集中のデータは保護されています）。\n{e}",
            )

    def _close_active_reader(self):
        """Close the fullscreen reader if one is open.

        Returns True when there was no reader or it closed, False when
        the user cancelled the close (unsaved reader edits). Callers
        must not proceed when this returns False.
        """
        if self._active_reader is None:
            return True
        reader = self._active_reader
        try:
            reader.closed.disconnect()
        except Exception:
            logger.debug("Active reader cleanup step skipped", exc_info=True)
        try:
            accepted = bool(reader.close())
        except Exception:
            logger.debug("Active reader cleanup step skipped", exc_info=True)
            accepted = False
        if not accepted:
            # User cancelled: keep the reader and restore its closed
            # handler so a later close still syncs/clears it.
            handler = getattr(self, "_active_reader_closed_handler", None)
            if handler is not None:
                try:
                    reader.closed.connect(handler)
                except Exception:
                    logger.debug("Active reader cleanup step skipped", exc_info=True)
            return False
        self._active_reader = None
        self._active_reader_closed_handler = None
        return True

    def _adopt_reader_doc_to_main(
        self,
        doc: fitz.Document,
        annotations: dict,
        page_idx: int = 0,
        target_mode: str = "highlight",
        has_edited: bool = False,
        from_bulk: bool = False,
    ) -> bool:
        if self.viewer.is_modified:
            reply = QMessageBox.question(
                self,
                "確認",
                "メイン画面に未保存の編集があります。破棄して見開き画面のPDFをメイン画面で開きますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False

        if self.viewer.current_pdf and self.viewer.current_pdf is not doc:
            with FITZ_LOCK:
                self.viewer.current_pdf.close()

        self.viewer.current_file = None
        self.viewer.current_pdf = doc
        self._reset_text_data_session()
        # 複数ファイルの一括読み込み由来なら上書き禁止フラグを引き継ぐ
        self._current_doc_from_bulk = bool(from_bulk)
        self.viewer.current_pdf_was_encrypted = False
        self.viewer.reset_history()
        self.viewer.page_annotations = copy.deepcopy(annotations) if annotations else {}
        if has_edited or any(bool(v) for v in self.viewer.page_annotations.values()):
            self.viewer.history.mark_unsaved()

        self.page_manager.clear_thumbnail_cache()
        target_p = max(0, min(page_idx, len(doc) - 1)) if len(doc) > 0 else 0
        self.viewer.load_pdf(doc, target_p, fit_view=True)
        self.page_manager.refresh_thumbnails()
        self.set_mode(target_mode)
        self.update_window_title()
        self.statusBar().showMessage("見開き全画面のPDFをメインエディタに引き継ぎました")
        return True

    def _build_fullscreen_editor_callbacks(self):
        return {
            "AddAnnotationCommand": AddAnnotationCommand,
            "RemoveAnnotationCommand": RemoveAnnotationCommand,
            "RemoveMultipleAnnotationsCommand": RemoveMultipleAnnotationsCommand,
            "ReplacePageAnnotationsCommand": ReplacePageAnnotationsCommand,
            "EditAnnotationCommand": EditAnnotationCommand,
            "TextEditDeleteDialog": TextEditDeleteDialog,
            "adjust_updated_after_on_remove": adjust_updated_after_on_remove,
            "get_later_user_erase_rects": get_later_user_erase_rects,
            "is_rect_substantially_erased": is_rect_substantially_erased,
            "estimate_text_bbox_in_pdf": estimate_text_bbox_in_pdf,
            "collect_text_only_redact_raw_rects": collect_text_only_redact_raw_rects,
            "annotation_to_erase_rects": annotation_to_erase_rects,
            "clip_stroke_by_rects": clip_stroke_by_rects,
            "save_safely": PDFDocumentManager.save_safely,
            "get_pdf_text_tokens": self.viewer._get_page_pdf_text_tokens,
            "adopt_doc_to_main": self._adopt_reader_doc_to_main,
        }

    def open_fullscreen_reader(self):
        """現在のPDF（未保存の注釈含む）を全画面見開きビューアで開く"""
        if not self.viewer.current_pdf or len(self.viewer.current_pdf) == 0:
            QMessageBox.information(self, "案内", "先にPDFを開いてください。")
            return

        self.viewer.cancel_active_interaction()
        if not self._close_active_reader():
            return

        cur_idx = self.viewer.current_page_idx
        # 一括読み込み由来の文書は単一ファイルではないのでレジューム対象外
        source_path = None if self._current_doc_from_bulk else self.viewer.current_file
        resume_page = None
        if self._pending_reader_resume is not None:
            pending_path, pending_page = self._pending_reader_resume
            self._pending_reader_resume = None
            if source_path and os.path.normcase(pending_path) == os.path.normcase(
                source_path
            ):
                resume_page = pending_page
        self._active_reader = FullscreenSpreadReader(
            doc=self.viewer.current_pdf,
            start_page=cur_idx,
            right_to_left=self._reader_right_to_left,
            cover_single=self._reader_cover_single,
            annotations=self.viewer.page_annotations,
            draw_annot_callback=PageManager.draw_annotations_on_image,
            redact_page_callback=PDFDocumentManager.apply_text_only_redactions_to_page,
            font_family=self.viewer.preview_font_family,
            own_doc=False,
            fitz_lock=FITZ_LOCK,
            viewer=self.viewer,
            editor_callbacks=self._build_fullscreen_editor_callbacks(),
            source_path=source_path,
            resume_page=resume_page,
            history_open_callback=self.open_file_in_reader_from_history,
            store=self._reader_store,
            parent=None,
        )
        self._active_reader_closed_handler = self._on_reader_closed
        self._active_reader.closed.connect(self._active_reader_closed_handler)

    def open_file_in_reader_from_history(self, filepath):
        """見開きリーダーの簡易履歴から選ばれたファイルを、続きから見開きで開く"""
        filepath = os.path.abspath(filepath)
        if not os.path.isfile(filepath):
            QMessageBox.information(
                self, "履歴", f"ファイルが見つかりませんでした。\n{filepath}"
            )
            self._reader_store.remove_entry(filepath)
            return
        if not self._close_active_reader():
            return
        self.open_file_from_path(filepath)
        if not self.viewer.current_file or os.path.normcase(
            self.viewer.current_file
        ) != os.path.normcase(filepath):
            # 開くのに失敗した／キャンセルされた場合は何もしない
            return
        pos = self._reader_store.get_position(filepath)
        if pos is not None:
            self._pending_reader_resume = (filepath, pos)
        self.open_fullscreen_reader()

    def _on_reader_closed(self):
        """全画面を閉じたとき、読んでいたページと編集内容をメインエディタに反映する"""
        if self._active_reader and self.viewer.current_pdf and self._active_reader.spreads:
            # 表紙の有無・綴じ方向の選択を覚えておく（次に開くときも同じ設定）
            self._reader_cover_single = bool(self._active_reader.cover_single)
            self._reader_right_to_left = bool(self._active_reader.right_to_left)
            idx = max(0, min(self._active_reader.current_spread_idx, len(self._active_reader.spreads) - 1))
            sp = self._active_reader.spreads[idx]
            edited_page = getattr(self._active_reader, "_last_edited_page_idx", None)
            has_edited = getattr(self._active_reader, "_has_edited", False)

            target_page = self.viewer.current_page_idx
            if edited_page is not None and edited_page in sp.page_indices:
                target_page = edited_page
            elif sp.page_indices and self.viewer.current_page_idx not in sp.page_indices:
                target_page = sp.page_indices[0]

            if 0 <= target_page < len(self.viewer.current_pdf):
                if target_page != self.viewer.current_page_idx:
                    self.viewer.load_pdf(self.viewer.current_pdf, target_page, fit_view=True)
                elif has_edited:
                    self.viewer.render_page(fit_view=False, rebuild_base=True)
            if has_edited:
                self.viewer.notify_pages_updated(None)
                self.update_window_title()
        self._active_reader = None
        self._active_reader_closed_handler = None

    def open_pdf_folder(self):
        """フォルダを選択し、中の複数PDFをまとめて開く"""
        folder_dir = QFileDialog.getExistingDirectory(self, "PDFが含まれるフォルダを選択")
        if not folder_dir:
            return

        def _password_callback(fname):
            # パスワード入力中は待機カーソルを一時解除し、終わったら戻す。
            QApplication.restoreOverrideCursor()
            try:
                return QInputDialog.getText(
                    self, "パスワード", f"「{fname}」のパスワードを入力:", QLineEdit.EchoMode.Password
                )
            finally:
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = load_folder_as_merged_document(
                folder_dir,
                password_callback=_password_callback,
            )
        finally:
            QApplication.restoreOverrideCursor()
        merged_doc = result[0]
        manifest = result[1]
        # Coordinated contract: core returns (doc, manifest, skipped);
        # older core still returns a 2-tuple, so tolerate both.
        skipped = list(result[2]) if len(result) > 2 else []

        if merged_doc is None:
            QMessageBox.warning(self, "エラー", "フォルダ内に読み込み可能なPDFが見つかりませんでした。")
            return

        if skipped:
            def _fmt_skipped(item):
                if isinstance(item, (tuple, list)):
                    return f"・{item[0]}: {item[1]}" if len(item) > 1 else f"・{item[0]}"
                return f"・{item}"
            shown = "\n".join(_fmt_skipped(s) for s in skipped[:10])
            extra = f"\n他 {len(skipped) - 10} 件" if len(skipped) > 10 else ""
            QMessageBox.warning(
                self,
                "一部のPDFをスキップしました",
                "フォルダ内の次のファイルは読み込めなかったため、結合から除外しました。\n"
                f"{shown}{extra}",
            )

        if not self._close_active_reader():
            with FITZ_LOCK:
                merged_doc.close()
            return

        # 結合した文書の開き方を選んでもらう（見開きで読む／メイン画面で編集）
        choice = QMessageBox(self)
        choice.setWindowTitle("フォルダ読込")
        choice.setText(
            f"{len(manifest)} ファイル（全 {len(merged_doc)} ページ）を結合しました。\n"
            "どのように開きますか？"
        )
        btn_read = choice.addButton(
            "📖 見開きで読む", QMessageBox.ButtonRole.AcceptRole
        )
        btn_edit = choice.addButton(
            "✏️ メイン画面で編集する", QMessageBox.ButtonRole.ActionRole
        )
        choice.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        choice.setDefaultButton(btn_read)
        choice.exec()
        clicked = choice.clickedButton()
        if clicked is btn_edit:
            # ページの並べ替え・削除はメイン画面のページ一覧で行う。
            # 未保存編集の確認や後始末は _adopt_reader_doc_to_main に任せる。
            adopted = self._adopt_reader_doc_to_main(
                merged_doc,
                {},
                0,
                target_mode="highlight",
                has_edited=False,
                from_bulk=True,
            )
            if not adopted:
                with FITZ_LOCK:
                    merged_doc.close()
            return
        if clicked is not btn_read:
            with FITZ_LOCK:
                merged_doc.close()
            return

        self._active_reader = FullscreenSpreadReader(
            doc=merged_doc,
            start_page=0,
            right_to_left=self._reader_right_to_left,
            cover_single=self._reader_cover_single,
            file_manifest=manifest,
            draw_annot_callback=PageManager.draw_annotations_on_image,
            redact_page_callback=PDFDocumentManager.apply_text_only_redactions_to_page,
            font_family=self.viewer.preview_font_family,
            own_doc=True,
            fitz_lock=FITZ_LOCK,
            viewer=None,
            editor_callbacks=self._build_fullscreen_editor_callbacks(),
            history_open_callback=self.open_file_in_reader_from_history,
            store=self._reader_store,
            parent=None,
        )
        self._active_reader_closed_handler = self._on_folder_reader_closed
        self._active_reader.closed.connect(self._active_reader_closed_handler)


    def _on_folder_reader_closed(self):
        if self._active_reader is not None:
            self._reader_cover_single = bool(self._active_reader.cover_single)
            self._reader_right_to_left = bool(self._active_reader.right_to_left)
        self._active_reader = None
        self._active_reader_closed_handler = None

    def _reset_text_data_session(self):
        """文字データダイアログの作業状態を破棄する（文書の差し替え時に呼ぶ）。"""
        self._text_data_session = {"pages": {}, "all": None, "join": {}}
        self._text_data_session_doc = None

    def open_text_data_dialog(self):
        """PDFに埋め込まれた文字データを表示・手直し・コピーするダイアログを開く"""
        doc = self.viewer.current_pdf
        if not doc or len(doc) == 0:
            QMessageBox.information(self, "案内", "先にPDFファイルを開いてください。")
            return
        # 作業状態は文書ごとに保持する。別の文書に替わっていたら破棄する。
        if self._text_data_session_doc is not doc:
            self._reset_text_data_session()
            self._text_data_session_doc = doc
        try:
            page_texts = extract_page_texts(doc, self.viewer.page_annotations)
            page_spans = extract_page_spans(doc, self.viewer.page_annotations)
        except Exception as e:
            QMessageBox.critical(
                self,
                "文字データエラー",
                f"文字データの読み取りに失敗しました。\n{e}",
            )
            return
        page_rects = get_page_view_rects(doc)

        def _refresh():
            annotations = self.viewer.page_annotations
            return (
                extract_page_texts(doc, annotations),
                extract_page_spans(doc, annotations),
            )

        def _image(page_idx, zoom=2.0):
            from PyQt6.QtGui import QPixmap

            try:
                data = render_page_png_bytes(
                    doc, page_idx, self.viewer.page_annotations, zoom=zoom
                )
            except Exception:
                return None
            if not data:
                return None
            pix = QPixmap()
            if not pix.loadFromData(data):
                return None
            return pix

        from ui.text_data_dialog import TextDataDialog

        current_file = getattr(self.viewer, "current_file", None)
        strip_stem = Path(current_file).stem if current_file else "PDF"
        dlg = TextDataDialog(
            page_texts,
            parent=self,
            page_spans=page_spans,
            page_rects=page_rects,
            session_state=self._text_data_session,
            apply_handler=self._apply_text_data_replacement,
            refresh_handler=_refresh,
            image_provider=_image,
            strip_handler=self._save_image_only_pdf,
            strip_filename=f"{strip_stem}_画像のみ.pdf",
        )
        dlg.exec()

    def _save_image_only_pdf(self, path):
        """文字データ画面の「画像だけのPDFとして保存」を実行する。

        現在の文書（注釈を焼き込んだ見た目）から、文字データだけを消した
        PDFを path に保存し、(成功したか, メッセージ) を返す。
        開いている文書や現在のファイルは変更しない。
        """
        doc = self.viewer.current_pdf
        if not doc or len(doc) == 0:
            return False, "PDFが開かれていません。"
        try:
            save_image_only_pdf(doc, self.viewer.page_annotations, path)
        except Exception as e:
            return False, f"画像だけのPDFを保存できませんでした。\n{e}"
        return True, (
            "画像だけのPDFを保存しました。"
            "見た目はそのままで、文字データは入っていません。"
        )

    def _apply_text_data_replacement(self, page_idx, spans, joined_text):
        """文字データ画面の「PDFに反映」を実行する。

        既存の「PDF内の文字を編集」と同じ仕組み（注釈＋Undoコマンド）を使う:
        - 断片が元のPDF文字なら、断片の矩形ちょうどの text_redact 注釈で消す
          （断片の外の文字には触れない）。
        - 断片が過去の編集で入れたテキスト注釈そのものなら、その注釈を取り除く
          （redact_rect を持つ注釈は、元の消去を引き継ぐため text_redact に変える）。
        - つなげた文は、断片の先頭位置・代表フォントサイズで1件の text 注釈として
          入れる（収まらない場合はフォントを縮める）。
        - 断片がすべてスキャン画像の上に乗ったOCR文字の場合は、つなげた文を
          不可視テキスト（レンダリングモード3）として入れ、断片の消去でも
          背景画像のピクセルを消さない。見た目は元の画像のままで、
          内部の文字データだけが差し替わる。
        全体を ReplacePageAnnotationsCommand 1件で積むため、1回の Undo で戻せる。
        成功したら True、対象を特定できず安全に置き換えられない場合は False。
        """
        viewer = self.viewer
        doc = viewer.current_pdf
        joined = str(joined_text or "").strip()
        if not doc or not (0 <= page_idx < len(doc)) or not spans or not joined:
            return False

        ann_list = viewer.page_annotations.get(page_idx, [])

        span_rects = []
        for sp in spans:
            r = fitz.Rect(sp["raw_rect"])
            r.normalize()
            if r.is_empty:
                return False
            span_rects.append(r)

        # 断片がすべてスキャン画像の上に乗ったOCR文字なら、置換後の文字は
        # 画像の上に表示せず、文字データだけを差し替える（通常のOCR
        # レイヤーと同じ不可視テキストにする）。判定はダイアログのスパン
        # 抽出元と同じ焼き込み済みページで行う。
        invisible = False
        try:
            baked_doc = PDFDocumentManager.extract_pages_as_doc(
                doc, [page_idx], viewer.page_annotations
            )
            try:
                invisible = spans_are_scan_overlay(baked_doc[0], span_rects)
            finally:
                baked_doc.close()
        except Exception:
            invisible = False

        # 注釈由来の断片を特定する: スパンの文字が注釈のいずれかの行と一致し、
        # スパン中心がその注釈の推定BBox（少し拡大）に入っているもの。
        span_ann_match = []
        for sp, r in zip(spans, span_rects):
            center = fitz.Point((r.x0 + r.x1) / 2.0, (r.y0 + r.y1) / 2.0)
            found = None
            for idx, ann in enumerate(ann_list):
                if ann.get("type") != "text":
                    continue
                if sp["text"] not in str(ann.get("text", "")).split("\n"):
                    continue
                try:
                    bbox = estimate_text_bbox_in_pdf(ann)
                except Exception:
                    continue
                expanded = fitz.Rect(
                    bbox.x0 - 2.0, bbox.y0 - 2.0, bbox.x1 + 2.0, bbox.y1 + 2.0
                )
                if expanded.contains(center):
                    found = idx
                    break
            span_ann_match.append(found)

        matched_ann_indices = {i for i in span_ann_match if i is not None}

        new_list = [
            copy.deepcopy(ann)
            for idx, ann in enumerate(ann_list)
            if idx not in matched_ann_indices
        ]
        # 取り除く注釈が元の文字を消していた場合は、その消去を引き継ぐ。
        # 元の文字が見えないOCR文字だった（または今回の置換が不可視になる）
        # 場合は、背景画像を傷つけないよう keep_image を引き継ぐ。
        for idx in sorted(matched_ann_indices):
            redact_rect = ann_list[idx].get("redact_rect")
            if redact_rect:
                redact_ann = {
                    "type": "text_redact",
                    "rect": tuple(float(v) for v in redact_rect),
                }
                if invisible or ann_list[idx].get("invisible"):
                    redact_ann["keep_image"] = True
                new_list.append(redact_ann)
        # 元のPDF文字の断片は、断片の矩形ちょうどで個別に消す。
        # 不可視の置換では、画像側の字形を消さないよう keep_image を付ける。
        for sp, matched in zip(spans, span_ann_match):
            if matched is None:
                redact_ann = {
                    "type": "text_redact",
                    "rect": tuple(float(v) for v in sp["raw_rect"]),
                }
                if invisible:
                    redact_ann["keep_image"] = True
                new_list.append(redact_ann)

        first = spans[0]
        union = span_rects[0]
        for r in span_rects[1:]:
            union = union | r
        if int(first["rotation"]) % 180 == 0:
            available = union.width
        else:
            available = union.height
        fontsize = fit_fontsize_to_width(
            joined, float(first["fontsize"]), available
        )
        new_list.append(
            build_text_annotation(
                text=joined,
                pos=first["pos"],
                baseline_pos=first["baseline_pos"],
                fontsize=fontsize,
                color=first["color"],
                rotation=int(first["rotation"]),
                updated_after=len(new_list),
                invisible=invisible,
            )
        )
        self._last_text_data_apply_invisible = invisible

        old_snapshot = copy.deepcopy(ann_list)
        if new_list:
            viewer.page_annotations[page_idx] = copy.deepcopy(new_list)
        else:
            viewer.page_annotations.pop(page_idx, None)
        viewer.history.push(
            ReplacePageAnnotationsCommand(page_idx, old_snapshot, new_list)
        )
        if viewer.current_page_idx == page_idx:
            viewer.render_page(fit_view=False, rebuild_base=True)
        viewer.notify_pages_updated([page_idx])
        self.update_window_title()
        return True

    def open_scan_clean_ocr_dialog(self):
        """スキャン用紙の色抜き（黄・赤・青・点々消去）＆Windows標準OCRダイアログを開く"""
        if not self.viewer.current_pdf or len(self.viewer.current_pdf) == 0:
            QMessageBox.information(self, "案内", "先にスキャンされたPDFファイルを開いてください。")
            return
        if sys.platform != "win32":
            QMessageBox.information(
                self,
                "Windows専用機能",
                "スキャン色抜き・OCRはWindows専用です。\n"
                "この環境（Windows以外）では利用できません。",
            )
            return
        self.viewer.cancel_active_interaction()
        from ui.ocr_dialog import ScanCleanOcrDialog
        dlg = ScanCleanOcrDialog(self.viewer, parent=self)
        dlg.exec()

    def closeEvent(self, event):
        if self.viewer.is_modified:
            reply = QMessageBox.question(
                self,
                "確認",
                "未保存の編集があります。\n終了しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )

            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        if not self._close_active_reader():
            event.ignore()
            return
        try:
            from core.ocr_cleaner import cleanup_stale_ocr_temp_dirs, terminate_all_active_ocr_processes
            terminate_all_active_ocr_processes()
            QThreadPool.globalInstance().waitForDone(500)
            cleanup_stale_ocr_temp_dirs(max_age_sec=60.0)
        except Exception:
            QThreadPool.globalInstance().waitForDone(300)

        self.viewer._close_clean_redacted_doc()
        if self.viewer.current_pdf:
            with FITZ_LOCK:
                self.viewer.current_pdf.close()

        self.page_manager.close_sub_pdf()

        if self in MainWindow._open_windows:
            MainWindow._open_windows.remove(self)

        event.accept()
