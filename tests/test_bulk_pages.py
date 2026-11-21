# -*- coding: utf-8 -*-
"""ベータ版「一括ページ操作」機能に対するテスト。

- 選択ページのみ抽出して保存：選んだページだけが順序どおり抽出されること、
  注釈（テキスト追加・墨消し）が通常保存と同じく反映されること、
  抽出元の文書が変更されないこと。
- フォルダ一括読み込み：結合ドキュメントをメイン画面で開く経路の後始末
  （current_file が None・上書き禁止フラグ True）と、開き方の選択ダイアログで
  「メイン画面で編集する」を選んだ場合の振る舞い。
- 上書き禁止：フラグ True の間は保存が別名保存に振り分けられ、
  別名保存後も current_file が None のままになること。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz


def _make_pdf_bytes(texts):
    doc = fitz.open()
    for t in texts:
        page = doc.new_page(width=300, height=300)
        page.insert_text((20, 40), t, fontsize=12)
    try:
        return doc.tobytes()
    finally:
        doc.close()


class _WindowTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.main_window import MainWindow

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.window = MainWindow()
        self.window.show()
        self._app.processEvents()
        self.addCleanup(self._cleanup_window)

    def _cleanup_window(self):
        win = self.window
        if win is None:
            return
        try:
            win.page_manager._thumb_timer.stop()
        except Exception:
            pass
        try:
            win.viewer.current_pdf = None
        except Exception:
            pass
        try:
            win.viewer.history.mark_saved()
        except Exception:
            pass
        try:
            win.close()
        except Exception:
            pass
        self.window = None

    def _write_pdf(self, name, texts):
        path = Path(self._tmp.name) / name
        path.write_bytes(_make_pdf_bytes(texts))
        return str(path)

    def _open(self, path):
        self.window.open_file_from_path(path)
        self._app.processEvents()


class TestExtractSelectedPages(_WindowTestBase):
    def test_extract_selected_pages_in_order(self):
        src = self._write_pdf(
            "src.pdf", ["PAGE_ONE", "PAGE_TWO", "PAGE_THREE", "PAGE_FOUR"]
        )
        self._open(src)
        pm = self.window.page_manager
        self.assertEqual(pm.list_widget.count(), 4)

        pm.list_widget.clearSelection()
        for row in (0, 2):
            pm.list_widget.item(row).setSelected(True)

        out = str(Path(self._tmp.name) / "out.pdf")
        with mock.patch(
            "ui.page_manager.QFileDialog.getSaveFileName", return_value=(out, "")
        ), mock.patch(
            "ui.page_manager.QMessageBox.information"
        ), mock.patch(
            "ui.page_manager.QMessageBox.critical",
            side_effect=AssertionError("抽出エラーが表示されました"),
        ):
            pm.extract_selected_pages()
        self._app.processEvents()

        check = fitz.open(out)
        try:
            self.assertEqual(len(check), 2)
            self.assertIn("PAGE_ONE", check[0].get_text())
            self.assertIn("PAGE_THREE", check[1].get_text())
        finally:
            check.close()

        # 抽出元の文書は変更されていない
        self.assertEqual(len(self.window.viewer.current_pdf), 4)

    def test_extract_applies_annotations_and_redaction(self):
        src = self._write_pdf("src.pdf", ["KEEP_ME", "SECRET_WORD"])
        self._open(src)
        viewer = self.window.viewer
        viewer.page_annotations[0] = [
            {
                "type": "text",
                "text": "ADDED_NOTE",
                "pos": (20.0, 100.0),
                "fontsize": 14.0,
            }
        ]
        viewer.page_annotations[1] = [
            {"type": "text_redact", "rect": (10.0, 20.0, 200.0, 60.0)}
        ]

        pm = self.window.page_manager
        pm.list_widget.clearSelection()
        for row in (0, 1):
            pm.list_widget.item(row).setSelected(True)

        out = str(Path(self._tmp.name) / "out.pdf")
        with mock.patch(
            "ui.page_manager.QFileDialog.getSaveFileName", return_value=(out, "")
        ), mock.patch(
            "ui.page_manager.QMessageBox.information"
        ), mock.patch(
            "ui.page_manager.QMessageBox.critical",
            side_effect=AssertionError("抽出エラーが表示されました"),
        ):
            pm.extract_selected_pages()

        check = fitz.open(out)
        try:
            self.assertEqual(len(check), 2)
            text0 = check[0].get_text()
            self.assertIn("KEEP_ME", text0)
            # 追加したテキスト注釈が焼き込まれている
            self.assertIn("ADDED_NOTE", text0)
            # 墨消しした文字は抽出後のPDFに残っていない
            self.assertNotIn("SECRET_WORD", check[1].get_text())
        finally:
            check.close()

    def test_extract_without_selection_shows_guidance(self):
        src = self._write_pdf("src.pdf", ["PAGE_ONE", "PAGE_TWO"])
        self._open(src)
        pm = self.window.page_manager
        pm.list_widget.clearSelection()

        with mock.patch(
            "ui.page_manager.QMessageBox.information"
        ) as info, mock.patch(
            "ui.page_manager.QFileDialog.getSaveFileName"
        ) as save_dlg:
            pm.extract_selected_pages()

        self.assertTrue(info.called)
        messages = [str(c.args[2]) for c in info.call_args_list if len(c.args) > 2]
        self.assertTrue(any("選択" in m for m in messages))
        save_dlg.assert_not_called()


class TestBulkSaveRouting(_WindowTestBase):
    def test_single_file_open_clears_bulk_flag(self):
        src = self._write_pdf("single.pdf", ["ONLY_PAGE"])
        self.window._current_doc_from_bulk = True  # 前の状態を汚しておく
        self._open(src)
        self.assertFalse(self.window._current_doc_from_bulk)
        self.assertFalse(self.window._should_force_save_as())
        self.assertEqual(
            self.window.viewer.current_file, os.path.abspath(src)
        )

    def test_save_file_with_bulk_flag_goes_to_save_as(self):
        src = self._write_pdf("single.pdf", ["ONLY_PAGE"])
        self._open(src)
        win = self.window
        win._current_doc_from_bulk = True
        self.assertTrue(win._should_force_save_as())

        messages = []
        with mock.patch.object(win, "save_as") as save_as_mock, mock.patch.object(
            win, "save_to_path"
        ) as save_to_path_mock, mock.patch(
            "ui.main_window.QMessageBox.information",
            side_effect=lambda *a, **k: messages.append(a),
        ):
            win.save_file()

        # 上書き経路（save_to_path）には入らず、別名保存に振り分けられる
        save_as_mock.assert_called_once()
        save_to_path_mock.assert_not_called()
        texts = [a[2] for a in messages if len(a) > 2]
        self.assertTrue(any("上書き保存できません" in t for t in texts))

    def test_save_to_path_keeps_current_file_none_for_bulk(self):
        src = self._write_pdf("single.pdf", ["ONLY_PAGE"])
        self._open(src)
        win = self.window
        win._current_doc_from_bulk = True

        out = str(Path(self._tmp.name) / "combined.pdf")
        with mock.patch(
            "ui.main_window.QMessageBox.information"
        ), mock.patch(
            "ui.main_window.QMessageBox.critical",
            side_effect=AssertionError("保存に失敗しました"),
        ):
            win.save_to_path(out)

        self.assertTrue(Path(out).exists())
        # 別名保存しても保存先は固定されず、次回も別名保存になる
        self.assertIsNone(win.viewer.current_file)
        self.assertTrue(win._should_force_save_as())

    def test_save_to_path_sets_current_file_for_single(self):
        src = self._write_pdf("single.pdf", ["ONLY_PAGE"])
        self._open(src)
        win = self.window
        self.assertFalse(win._current_doc_from_bulk)

        out = str(Path(self._tmp.name) / "copy.pdf")
        with mock.patch(
            "ui.main_window.QMessageBox.information"
        ), mock.patch(
            "ui.main_window.QMessageBox.critical",
            side_effect=AssertionError("保存に失敗しました"),
        ):
            win.save_to_path(out)

        self.assertTrue(Path(out).exists())
        self.assertEqual(win.viewer.current_file, out)
        self.assertFalse(win._should_force_save_as())


class TestFolderBulkToMain(_WindowTestBase):
    def _make_folder(self):
        folder = Path(self._tmp.name) / "docs"
        folder.mkdir()
        (folder / "a.pdf").write_bytes(_make_pdf_bytes(["FILE_A1", "FILE_A2"]))
        (folder / "b.pdf").write_bytes(_make_pdf_bytes(["FILE_B1"]))
        return str(folder)

    def test_adopt_merged_doc_sets_bulk_flag(self):
        from core.pdf_loader import load_folder_as_merged_document

        folder = self._make_folder()
        merged, manifest, skipped = load_folder_as_merged_document(folder)
        self.assertIsNotNone(merged)
        self.assertEqual(len(merged), 3)
        self.assertEqual(len(manifest), 2)

        win = self.window
        adopted = win._adopt_reader_doc_to_main(
            merged, {}, 0, "highlight", False, from_bulk=True
        )
        try:
            self.assertTrue(adopted)
            self.assertIs(win.viewer.current_pdf, merged)
            self.assertIsNone(win.viewer.current_file)
            self.assertTrue(win._current_doc_from_bulk)
            self.assertTrue(win._should_force_save_as())
            self.assertIn("新規結合ドキュメント", win.windowTitle())
        finally:
            win.viewer.current_pdf = None
            merged.close()

    def test_open_pdf_folder_choose_main_editor(self):
        from PyQt6.QtWidgets import QMessageBox

        folder = self._make_folder()
        win = self.window

        class FakeChoiceBox:
            """開き方の選択ダイアログの代役（「メイン画面で編集する」を選ぶ）。"""

            ButtonRole = QMessageBox.ButtonRole
            StandardButton = QMessageBox.StandardButton

            def __init__(self, parent=None):
                self._buttons = []

            def setWindowTitle(self, title):
                pass

            def setText(self, text):
                self.text = text

            def addButton(self, text, role):
                btn = (text, role)
                self._buttons.append(btn)
                return btn

            def setDefaultButton(self, btn):
                pass

            def exec(self):
                return 0

            def clickedButton(self):
                for btn in self._buttons:
                    if "メイン画面で編集" in btn[0]:
                        return btn
                return None

        with mock.patch(
            "ui.main_window.QFileDialog.getExistingDirectory",
            return_value=folder,
        ), mock.patch("ui.main_window.QMessageBox", FakeChoiceBox):
            win.open_pdf_folder()
        self._app.processEvents()

        doc = win.viewer.current_pdf
        self.assertIsNotNone(doc)
        self.assertEqual(len(doc), 3)
        self.assertIsNone(win.viewer.current_file)
        self.assertTrue(win._current_doc_from_bulk)
        self.assertTrue(win._should_force_save_as())
        self.assertIn("新規結合ドキュメント", win.windowTitle())


if __name__ == "__main__":
    unittest.main()
