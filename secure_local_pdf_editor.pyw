# -*- coding: utf-8 -*-
"""
Secure Local PDF Editor - メインエントリポイント
各機能は以下のモジュールに分割・整理されています:
  - core/geometry.py   : 座標変換・数値抽出・フォント検索・Windows関連付け・共通定数
  - core/pdf_engine.py : PDFDocumentManager (物理墨消し・注釈焼き込み・アトミック保存)
  - core/commands.py   : HistoryManager & Undo/Redo コマンド群
  - ui/dialogs.py      : PreciseTextAnnotationItem & TextEditDeleteDialog
  - ui/pdf_viewer.py   : CrispPagePixmapItem & PDFViewer
  - ui/page_manager.py : PageManager & 非同期サムネイル生成 & D&Dリストウィジェット
  - ui/main_window.py  : MainWindow
"""
import os
import sys
import threading
import traceback

from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import QGuiApplication, QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

# 後方互換性のための主要シンボル再エクスポート
from core.commands import (  # noqa: F401
    AddAnnotationCommand,
    BaseCommand,
    CropPageCommand,
    DeletePagesCommand,
    EditAnnotationCommand,
    HistoryManager,
    InsertPagesCommand,
    RemoveAnnotationCommand,
    RemoveMultipleAnnotationsCommand,
    ReorderPagesCommand,
    ReplacePageAnnotationsCommand,
    ReplacePagesContentCommand,
    RotatePageCommand,
    RotatePagesCommand,
)
from core.geometry import (  # noqa: F401
    APP_NAME,
    DEFAULT_COLOR,
    DRAW_WIDTH,
    FITZ_LOCK,
    JAPANESE_FONT,
    RENDER_SCALE,
    TEXT_FONT_SIZE,
    estimate_text_bbox_in_pdf,
    extract_number_from_text,
    format_number_value,
    format_pt,
    get_app_icon_path,
    get_crop_outside_rects,
    get_effective_cropbox,
    raw_pdf_to_view_point,
    raw_rect_to_view_rect,
    view_point_to_raw_pdf,
    view_rect_to_raw_rect,
)
from core.pdf_engine import PDFDocumentManager  # noqa: F401
from ui.dialogs import PreciseTextAnnotationItem, TextEditDeleteDialog  # noqa: F401
from ui.main_window import MainWindow
from ui.page_manager import PageManager  # noqa: F401
from ui.pdf_viewer import CrispPagePixmapItem, PDFViewer  # noqa: F401


class PDFEditorApplication(QApplication):
    """OSからPDFファイルをダブルクリックして開かれた際のイベントを受け取るカスタムQApplication"""

    def event(self, ev):
        if ev.type() == QEvent.Type.FileOpen:
            filepath = ev.file()
            if filepath and os.path.isfile(filepath) and filepath.lower().endswith(".pdf"):
                self.open_pdf_in_available_window(filepath)
                return True
        return super().event(ev)

    def open_pdf_in_available_window(self, filepath):
        for win in MainWindow._open_windows:
            if win.viewer.current_pdf is None:
                win.open_file_from_path(filepath)
                win.raise_()
                win.activateWindow()
                return

        visible_wins = [w for w in MainWindow._open_windows if w.isVisible()]
        if visible_wins:
            visible_wins[-1].spawn_new_window(filepath=filepath)
        else:
            win = MainWindow()
            win.show()
            QTimer.singleShot(50, lambda p=filepath: win.open_file_from_path(p))


def _show_error_dialog(message: str):
    """メインスレッド上で安全にエラーダイアログを表示する"""
    app = QApplication.instance()
    if app is not None:
        active_win = app.activeWindow()
        QMessageBox.critical(
            active_win,
            "エラーが発生しました",
            f"処理中にエラーが発生しましたが、編集中のデータは維持されています。\n\n{message}",
        )


def _global_exception_hook(exctype, value, tb):
    """万が一予期せぬ例外が発生してもアプリごと強制終了せずエラー内容を表示して作業データを守る"""
    err_msg = "".join(traceback.format_exception(exctype, value, tb))
    if sys.__stderr__ is not None:
        try:
            sys.__stderr__.write(err_msg)
        except Exception:
            pass
    app = QApplication.instance()
    if app is None:
        return
    msg_text = str(value)
    # メインスレッドなら直接表示、サブスレッドならメインスレッドのイベントキューへ送る
    # （ワーカースレッドから直接 QMessageBox を出すとクラッシュするため）。
    if threading.current_thread() is threading.main_thread():
        _show_error_dialog(msg_text)
    else:
        QTimer.singleShot(0, app, lambda m=msg_text: _show_error_dialog(m))


def _threading_exception_hook(args):
    """Python 3.8以降のサブスレッド例外用フック"""
    _global_exception_hook(args.exc_type, args.exc_value, args.exc_traceback)


def main():
    # .pyw実行時に標準出力/エラー出力がNoneになってクラッシュするのを防ぐ
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    sys.excepthook = _global_exception_hook
    threading.excepthook = _threading_exception_hook
    try:
        from core.ocr_cleaner import cleanup_stale_ocr_temp_dirs
        cleanup_stale_ocr_temp_dirs(max_age_sec=300.0)
    except Exception:
        pass

    try:
        from core.ocr_cleaner import cleanup_bytecode_cache
        cleanup_bytecode_cache()
    except Exception:
        pass

    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "KinakoPDF.SecureLocalPDFEditor.1"
            )
        except Exception:
            pass

    app = PDFEditorApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon_path = get_app_icon_path(prefer_ico=(sys.platform == "win32"))
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))

    window = MainWindow()
    window.show()

    startup_pdfs = [
        os.path.abspath(arg)
        for arg in sys.argv[1:]
        if isinstance(arg, str) and os.path.isfile(arg) and arg.lower().endswith(".pdf")
    ]
    if startup_pdfs:
        def _load_startup_files():
            window.open_file_from_path(startup_pdfs[0])
            last_win = window
            for extra_pdf in startup_pdfs[1:]:
                last_win = last_win.spawn_new_window(filepath=extra_pdf)

        QTimer.singleShot(0, _load_startup_files)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
