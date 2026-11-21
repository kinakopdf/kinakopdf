# -*- coding: utf-8 -*-
"""ボタン整理（ベータ版2）の配線テスト。

- 上部ツールバー: たまに使う機能（Excel出力・フォルダ読込・新規）と
  OCR系（色抜き・OCR／範囲OCR）は「🧰 その他」メニューに入り、
  ツールバー直置きではなくなること。
  よく使う機能（開く・印刷・見開き・文字データ等）は直置きのまま。
- 「💾 保存」はドロップダウン付きボタンになり、「💾 別名保存」は
  そのメニュー（保存メニュー）に入ること。
- 編集ツールバー: 消去系モード4つは「🧽 消去」メニューに入り、
  それ以外のモード（範囲OCRを除く）は直置きのまま。
  mode_group には全12モードが残り、メニュー内のモードも
  set_mode で選べること。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QToolBar, QToolButton

from ui.main_window import MainWindow


def clear_toolbar_layout_settings():
    """配置カスタマイズの保存値を消して、既定配置で起動できるようにする。"""
    settings = QSettings("SecureLocalPDFEditor", "SecureLocalPDFEditor")
    settings.beginGroup("toolbar_layout")
    settings.remove("")
    settings.endGroup()
    settings.sync()

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
        clear_toolbar_layout_settings()
        self.win = MainWindow()
        self._app.processEvents()
        toolbars = self.win.findChildren(QToolBar)
        self.top_toolbar = toolbars[0]
        self.edit_toolbar = toolbars[1]

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        self._app.processEvents()
        clear_toolbar_layout_settings()

    def test_more_features_moved_into_menu(self):
        top_texts = [a.text() for a in self.top_toolbar.actions()]
        # たまに使う機能とOCR系はツールバー直置きではない
        self.assertNotIn("📊 Excel出力", top_texts)
        self.assertNotIn("📚 フォルダ読込", top_texts)
        self.assertNotIn("🗔 新規", top_texts)
        self.assertNotIn("🧼 色抜き・OCR", top_texts)
        self.assertNotIn("💾 別名保存", top_texts)
        # 「🧰 その他」メニューの中にある
        menu_texts = [a.text() for a in self.win.menu_more_features.actions()]
        self.assertIn("📊 Excel出力", menu_texts)
        self.assertIn("📚 フォルダ読込", menu_texts)
        self.assertIn("🗔 新規", menu_texts)
        self.assertIn("🧼 色抜き・OCR", menu_texts)
        # よく使う機能は直置きのまま
        for text in ("📁 開く", "🖨 印刷", "📖 見開き", "📝 文字データ"):
            self.assertIn(text, top_texts)

    def test_save_button_has_save_as_in_dropdown(self):
        # 「💾 保存」はドロップダウン付きボタン（本体=保存、矢印=メニュー）
        self.assertEqual(self.win.btn_save.text(), "💾 保存")
        self.assertEqual(
            self.win.btn_save.popupMode(),
            QToolButton.ToolButtonPopupMode.MenuButtonPopup,
        )
        # 「💾 別名保存」は保存メニューの中にある
        save_texts = [a.text() for a in self.win.menu_save.actions()]
        self.assertIn("💾 別名保存", save_texts)
        self.assertIs(self.win.act_save_as, self.win.menu_save.actions()[0])

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
        # 範囲OCRは「🧰 その他」メニューへ移ったので編集バーにも残らない
        self.assertEqual(
            toolbar_modes, set(ALL_MODES) - ERASE_MODES - {"region_ocr"}
        )

        menu_modes = {a.data() for a in self.win.menu_erase_modes.actions()}
        self.assertEqual(menu_modes, ERASE_MODES)

        more_modes = {
            a.data() for a in self.win.menu_more_features.actions()
        }
        self.assertIn("region_ocr", more_modes)

    def test_erase_mode_selectable_from_menu(self):
        for mode in sorted(ERASE_MODES):
            self.win.set_mode(mode)
            checked = self.win.mode_group.checkedAction()
            self.assertIsNotNone(checked)
            self.assertEqual(checked.data(), mode)
        # 「🧰 その他」メニュー内の範囲OCRも set_mode で選べる
        self.win.set_mode("region_ocr")
        self.assertEqual(
            self.win.mode_group.checkedAction().data(), "region_ocr"
        )
        self.win.set_mode("view")
        self.assertEqual(self.win.mode_group.checkedAction().data(), "view")


if __name__ == "__main__":
    unittest.main()
