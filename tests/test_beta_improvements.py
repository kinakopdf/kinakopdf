# -*- coding: utf-8 -*-
"""ベータ版の機能改善（2026-11-24、MIWA 依頼）に対するテスト。

- ページ並び替えへの「逆順にする」追加：全ページが逆順になり、
  Undo で戻ること。
- 取り込み元のファイル名の右に「新規ウインドウで開く」追加：
  取り込み元を読み込むとボタンが有効になり、押すと取り込み元の
  パスで新規ウインドウが開くこと。
"""
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


class TestReverseAllPages(_WindowTestBase):
    def test_reverse_and_undo(self):
        path = self._write_pdf("main.pdf", ["P1", "P2", "P3"])
        self._open(path)
        pm = self.window.page_manager
        doc = self.window.viewer.current_pdf
        self.assertEqual(
            [doc[i].get_text().strip() for i in range(3)],
            ["P1", "P2", "P3"])

        pm.reverse_all_pages()
        self._app.processEvents()
        doc = self.window.viewer.current_pdf
        self.assertEqual(
            [doc[i].get_text().strip() for i in range(3)],
            ["P3", "P2", "P1"])

        self.window.viewer.history.undo(self.window.viewer)
        self._app.processEvents()
        doc = self.window.viewer.current_pdf
        self.assertEqual(
            [doc[i].get_text().strip() for i in range(3)],
            ["P1", "P2", "P3"])


class TestSubNewWindowButton(_WindowTestBase):
    def test_button_enables_and_opens_sub_in_new_window(self):
        main_path = self._write_pdf("main.pdf", ["M1"])
        sub_path = self._write_pdf("source.pdf", ["S1", "S2"])
        self._open(main_path)
        pm = self.window.page_manager

        self.assertFalse(pm.btn_open_sub_new_window.isEnabled())

        pm.load_sub_pdf_from_path(sub_path)
        self._app.processEvents()
        self.assertTrue(pm.btn_open_sub_new_window.isEnabled())
        self.assertIn("source.pdf", pm.lbl_sub.text())

        with mock.patch.object(
                self.window, "spawn_new_window") as spawn:
            pm.btn_open_sub_new_window.click()
            spawn.assert_called_once_with(sub_path)


if __name__ == "__main__":
    unittest.main()
