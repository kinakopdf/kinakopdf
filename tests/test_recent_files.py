# -*- coding: utf-8 -*-
"""「最近のファイル」履歴の上限テスト。

ツールバーの履歴（QSettings の recent_files）は MAX_RECENT_FILES 件まで。
以前は5件で、MIWAの依頼で20件に増やした（見開きリーダーの続き位置の
履歴とは別の仕組みなので、こちらも個別に確認する）。
"""
import os
import tempfile
import unittest

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication


class TestRecentFilesLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        settings = QSettings("SecureLocalPDFEditor", "SecureLocalPDFEditor")
        settings.setValue("recent_files", [])
        settings.sync()

    def tearDown(self):
        settings = QSettings("SecureLocalPDFEditor", "SecureLocalPDFEditor")
        settings.setValue("recent_files", [])
        settings.sync()

    def test_keeps_up_to_20_files(self):
        from core.geometry import MAX_RECENT_FILES
        from ui.main_window import MainWindow

        self.assertEqual(MAX_RECENT_FILES, 20)
        win = MainWindow()
        try:
            paths = []
            with tempfile.TemporaryDirectory() as td:
                for i in range(7):
                    p = os.path.join(td, f"doc{i}.pdf")
                    with open(p, "wb") as f:
                        f.write(b"%PDF-1.4\n")
                    paths.append(p)
                for p in paths:
                    win._add_recent_file(p)
                got = win._get_recent_files()
                # 5件で切れず、7件とも残る（新しい順）
                self.assertEqual(len(got), 7)
                self.assertEqual(got[0], os.path.abspath(paths[-1]))
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
