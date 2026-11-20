# -*- coding: utf-8 -*-
"""ボタン整理（ベータ版2）の配線テスト。

- 上部ツールバー: たまに使う機能（Excel出力・フォルダ読込・新規）は
  「🧰 その他」メニューに入り、ツールバー直置きではなくなること。
  よく使う機能（開く・保存・見開き・文字データ等）は直置きのまま。
- 編集ツールバー: 消去系モード4つは「🧽 消去」メニューに入り、
  それ以外のモードは直置きのまま。mode_group には全12モードが残り、
  メニュー内のモードも set_mode で選べること。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QToolBar

from ui.main_window import MainWindow

ALL_MODES = [
    "view",
    "text_select",
    "crop",
    "fold",
    "draw",
    "highlight",
    "highlight_erase",
    "box_erase",
    "erase",
    "type",
    "text_erase",
    "region_ocr",
]
ERASE_MODES = {"highlight_erase", "box_erase", "erase", "text_erase"}


class TestButtonCleanup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.win = MainWindow()
        self._app.processEvents()
        toolbars = self.win.findChildren(QToolBar)
        self.top_toolbar = toolbars[0]
        self.edit_toolbar = toolbars[1]

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        self._app.processEvents()

    def test_more_features_moved_into_menu(self):
        top_texts = [a.text() for a in self.top_toolbar.actions()]
        # たまに使う機能はツールバー直置きではない
        self.assertNotIn("📊 Excel出力", top_texts)
        self.assertNotIn("📚 フォルダ読込", top_texts)
        self.assertNotIn("🗔 新規", top_texts)
        # 「🧰 その他」メニューの中にある
        menu_texts = [a.text() for a in self.win.menu_more_features.actions()]
        self.assertIn("📊 Excel出力", menu_texts)
        self.assertIn("📚 フォルダ読込", menu_texts)
        self.assertIn("🗔 新規", menu_texts)
        # よく使う機能は直置きのまま
        for text in ("📁 開く", "💾 保存", "💾 別名保存", "🖨 印刷", "📖 見開き", "📝 文字データ"):
            self.assertIn(text, top_texts)

    def test_text_data_action_still_wired(self):
        # メニュー化しても「📝 文字データ」の有効/無効切り替えは壊れない
        self.assertIn(self.win.act_text_data, self.top_toolbar.actions())
        self.assertFalse(self.win.act_text_data.isEnabled())

    def test_erase_modes_moved_into_menu(self):
        group_modes = {a.data() for a in self.win.mode_group.actions()}
        self.assertEqual(group_modes, set(ALL_MODES))

        toolbar_modes = {
            a.data() for a in self.edit_toolbar.actions() if a.data() in ALL_MODES
        }
        self.assertEqual(toolbar_modes, set(ALL_MODES) - ERASE_MODES)

        menu_modes = {a.data() for a in self.win.menu_erase_modes.actions()}
        self.assertEqual(menu_modes, ERASE_MODES)

    def test_erase_mode_selectable_from_menu(self):
        for mode in sorted(ERASE_MODES):
            self.win.set_mode(mode)
            checked = self.win.mode_group.checkedAction()
            self.assertIsNotNone(checked)
            self.assertEqual(checked.data(), mode)
        self.win.set_mode("view")
        self.assertEqual(self.win.mode_group.checkedAction().data(), "view")


if __name__ == "__main__":
    unittest.main()
