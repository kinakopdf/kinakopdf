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
from ui.dialogs import TextEditDeleteDialog
from ui.page_manager import PageManager
from ui.pdf_viewer import PDFViewer
from ui.region_ocr_ext import DirectViewerRegionOCRController
from ui.spread_reader import FullscreenSpreadReader


class MainWindow(QMainWindow):

    _open_windows = []

    def __init__(self):
        super().__init__()
        MainWindow._open_windows.append(self)

        self.setWindowTitle(APP_NAME)
        self._initial_positioned = False
        self._active_reader = None
        self._active_reader_closed_handler = None

        self.viewer = PDFViewer()
        self.setCentralWidget(self.viewer)

        self.page_manager = PageManager(self)
        self.addDockWidget(
            Qt.DockWidgetArea.LeftDockWidgetArea,
            self.page_manager,
        )

        self.viewer.pagesUpdated.connect(self.page_manager.update_thumbnails)
        self.viewer.pageSelectionChanged.connect(self.page_manager.sync_selection)
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
            "PDFを開いてください（Ctrl+Fで文字検索 / Ctrl+クリックで表内の数字を合計 / ホイールでページ送り）"
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
        self.btn_recent_files.setText("🕘 最近開いたファイル")
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

        self.add_action(top_toolbar, "🗔 新規ウィンドウ", self.spawn_new_window)

        act_read = self.add_action(top_toolbar, "📖 見開き全画面", self.open_fullscreen_reader)
        act_read.setShortcut(QKeySequence("F5"))
        act_read.setToolTip("現在のPDFを全画面見開きモードで閲覧します (F5)")

        act_folder = self.add_action(top_toolbar, "📚 フォルダ一括読込", self.open_pdf_folder)
        act_folder.setToolTip("フォルダ内の複数PDFをまとめて1つの本として読み込みます")

        act_ocr = self.add_action(top_toolbar, "🧼 スキャン色抜き・OCR", self.open_scan_clean_ocr_dialog)
        act_ocr.setShortcut(QKeySequence("Ctrl+Shift+O"))
        act_ocr.setToolTip("スキャンした明細から黄・赤・青の背景や点々を消し、Windows標準OCRで文字認識します (Ctrl+Shift+O)")

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
        self.add_action(top_toolbar, "画面に合わせる", self.viewer.fit_page)

        top_toolbar.addSeparator()

        lbl_search = QLabel(" 🔎 検索:")
        lbl_search.setStyleSheet("font-weight: bold; padding-left: 4px;")
        top_toolbar.addWidget(lbl_search)

        self.edit_search = QLineEdit(self)
        self.edit_search.setPlaceholderText("文字を検索 (Ctrl+F)")
        self.edit_search.setMinimumWidth(150)
        self.edit_search.setMaximumWidth(220)
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
                "⚙ 既定のPDFに設定",
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
            ("✂ トリミング", "crop"),
            ("✏ ペン", "draw"),
            ("🖍 マーカー", "highlight"),
            ("🖍✕ マーカー消去", "highlight_erase"),
            ("🔲 範囲墨消し", "box_erase"),
            ("🧽 消しゴム", "erase"),
            ("⌨ テキスト（既存文字編集・追加）", "type"),
            ("🧹 テキスト消去", "text_erase"),
            ("🎯 範囲OCR", "region_ocr"),
        ]

        for name, mode in modes:
            action = QAction(name, self)
            action.setCheckable(True)
            action.setData(mode)
            if mode == "view":
                action.setChecked(True)
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
        self.lbl_calc_sum.setToolTip("Ctrlキーを押しながらPDF内の数字をクリックすると足し算します")
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
                "続けて以下の手順でデフォルト設定を完了してください：\n"
                "1. 任意のPDFファイルを右クリック →「プログラムから開く」→「別のプログラムを選択」をクリック\n"
                f"2. 一覧から「{APP_NAME}」を選択し、「常に使う」をクリック"
            )
            QMessageBox.information(self, "既定のPDFソフトへの登録完了", msg)
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

    def update_window_title(self):
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

    def set_mode(self, mode):
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
            self._last_opened_was_encrypted = bool(needs_pass)
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
        self.viewer.current_pdf_was_encrypted = bool(getattr(self, "_last_opened_was_encrypted", False))
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
        if not getattr(self.viewer, "current_pdf_was_encrypted", False):
            return True, False
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

    def save_file(self):
        if not self.viewer.current_pdf:
            QMessageBox.information(self, "案内", "保存するPDFが開かれていません。先にPDFを開いてください。")
            return

        if self.viewer.current_file:
            filepath = self.viewer.current_file
        else:
            filepath, _ = QFileDialog.getSaveFileName(
                self,
                "PDFを保存",
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
        """現在開いているPDF（未保存の注釈・墨消し・テキスト編集を含む）をプリンターで印刷する"""
        doc = self.viewer.current_pdf
        if not doc or len(doc) == 0:
            QMessageBox.information(self, "印刷", "印刷するPDFが開かれていません。")
            return

        self.viewer.cancel_active_interaction()

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if self.viewer.current_file:
            printer.setDocName(Path(self.viewer.current_file).name)
        else:
            printer.setDocName(APP_NAME)

        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("PDFの印刷")
        dialog.setOption(QPrintDialog.PrintDialogOption.PrintToFile, True)
        dialog.setOption(QPrintDialog.PrintDialogOption.PrintPageRange, True)
        dialog.setOption(QPrintDialog.PrintDialogOption.PrintCurrentPage, True)
        dialog.setOption(QPrintDialog.PrintDialogOption.PrintCollateCopies, True)
        dialog.setMinMax(1, len(doc))

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        work_doc = None
        painter = QPainter()
        try:
            print_range = printer.printRange()
            if print_range == QPrinter.PrintRange.PageRange:
                from_p = max(1, printer.fromPage())
                to_p = min(len(doc), printer.toPage())
                if to_p < from_p:
                    from_p, to_p = to_p, from_p
                target_orig_indices = list(range(from_p - 1, to_p))
            elif print_range == QPrinter.PrintRange.CurrentPage:
                target_orig_indices = [self.viewer.current_page_idx]
            else:
                target_orig_indices = list(range(len(doc)))

            if not target_orig_indices:
                return

            work_doc = PDFDocumentManager.extract_pages_as_doc(
                doc,
                target_orig_indices,
                self.viewer.page_annotations,
            )
            page_indices = list(range(len(work_doc)))

            first_page = work_doc[page_indices[0]]
            if first_page.rect.width > first_page.rect.height:
                printer.setPageOrientation(QPageLayout.Orientation.Landscape)
            else:
                printer.setPageOrientation(QPageLayout.Orientation.Portrait)

            if not painter.begin(printer):
                raise RuntimeError("プリンターの描画開始に失敗しました。")

            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

            dpi = printer.resolution()
            render_dpi = max(150, min(600, int(dpi) if dpi > 0 else 300))
            scale = render_dpi / 72.0

            for i, page_idx in enumerate(page_indices):
                page = work_doc[page_idx]

                if i > 0:
                    if page.rect.width > page.rect.height:
                        printer.setPageOrientation(QPageLayout.Orientation.Landscape)
                    else:
                        printer.setPageOrientation(QPageLayout.Orientation.Portrait)
                    if not printer.newPage():
                        raise RuntimeError(f"{page_idx + 1}ページ目の改ページ処理に失敗しました。")

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

                target_rect = QRectF(painter.viewport())
                if target_rect.width() <= 0 or target_rect.height() <= 0 or qimg.width() <= 0 or qimg.height() <= 0:
                    continue

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

            painter.end()
            self.statusBar().showMessage(f"🖨 印刷ジョブを送信しました（全 {len(page_indices)} ページ）")

        except Exception as e:
            if painter.isActive():
                painter.end()
            QMessageBox.critical(
                self,
                "印刷エラー",
                f"印刷中にエラーが発生しました。\n{e}",
            )
        finally:
            if work_doc is not None:
                work_doc.close()

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

            self.viewer.current_file = filepath
            self.viewer.current_pdf_was_encrypted = False
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
        self._active_reader = FullscreenSpreadReader(
            doc=self.viewer.current_pdf,
            start_page=cur_idx,
            annotations=self.viewer.page_annotations,
            draw_annot_callback=PageManager.draw_annotations_on_image,
            redact_page_callback=PDFDocumentManager.apply_text_only_redactions_to_page,
            font_family=self.viewer.preview_font_family,
            own_doc=False,
            fitz_lock=FITZ_LOCK,
            viewer=self.viewer,
            editor_callbacks=self._build_fullscreen_editor_callbacks(),
            parent=None,
        )
        self._active_reader_closed_handler = self._on_reader_closed
        self._active_reader.closed.connect(self._active_reader_closed_handler)

    def _on_reader_closed(self):
        """全画面を閉じたとき、読んでいたページと編集内容をメインエディタに反映する"""
        if self._active_reader and self.viewer.current_pdf and self._active_reader.spreads:
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
                self.update_window_title()
        self._active_reader = None
        self._active_reader_closed_handler = None

    def open_pdf_folder(self):
        """フォルダを選択し、中の複数PDFをまとめて開く"""
        folder_dir = QFileDialog.getExistingDirectory(self, "PDFが含まれるフォルダを選択")
        if not folder_dir:
            return

        result = load_folder_as_merged_document(
            folder_dir,
            password_callback=lambda fname: QInputDialog.getText(
                self, "パスワード", f"「{fname}」のパスワードを入力:", QLineEdit.EchoMode.Password
            ),
        )
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
        self._active_reader = FullscreenSpreadReader(
            doc=merged_doc,
            start_page=0,
            file_manifest=manifest,
            draw_annot_callback=PageManager.draw_annotations_on_image,
            redact_page_callback=PDFDocumentManager.apply_text_only_redactions_to_page,
            font_family=self.viewer.preview_font_family,
            own_doc=True,
            fitz_lock=FITZ_LOCK,
            viewer=None,
            editor_callbacks=self._build_fullscreen_editor_callbacks(),
            parent=None,
        )
        self._active_reader_closed_handler = self._on_folder_reader_closed
        self._active_reader.closed.connect(self._active_reader_closed_handler)


    def _on_folder_reader_closed(self):
        self._active_reader = None
        self._active_reader_closed_handler = None

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
