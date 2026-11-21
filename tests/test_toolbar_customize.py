# -*- coding: utf-8 -*-
"""ツールバー配置カスタマイズ（ベータ版2・第2弾）のテスト。

- 既定配置: 別名保存は保存メニュー内、OCR系（色抜き・OCR／範囲OCR）は
  「🧰 その他」メニュー内、「🛠 バーを編集...」はその他メニューの末尾固定。
- 題名変更が各ボタンへ反映され、空欄で既定名に戻ること。
- 配置変更（隠す／バーへ戻す等）がその場で反映され、QSettings に
  保存されて次のウィンドウにも引き継がれること。
- 初期化で既定配置に戻ること。
- メニュー内でも OCR系のショートカットが効くこと。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QToolBar, QToolButton

from ui.main_window import MainWindow
from ui.toolbar_layout_dialog import ToolbarLayoutDialog


def clear_toolbar_layout_settings():
    settings = QSettings("SecureLocalPDFEditor", "SecureLocalPDFEditor")
    settings.beginGroup("toolbar_layout")
    settings.remove("")
    settings.endGroup()
    settings.sync()


class TestToolbarCustomize(unittest.TestCase):
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

    # ---------------------------------------------------------------
    # 既定配置
    # ---------------------------------------------------------------
    def test_default_layout(self):
        win = self.win
        # 別名保存は保存メニューの中（バー直置きではない）
        self.assertIn(win.act_save_as, win.menu_save.actions())
        self.assertNotIn(win.act_save_as, self.top_toolbar.actions())
        self.assertEqual(
            win.btn_save.popupMode(),
            QToolButton.ToolButtonPopupMode.MenuButtonPopup,
        )
        # OCR系は「🧰 その他」メニューの中
        self.assertIn(win.act_ocr, win.menu_more_features.actions())
        self.assertNotIn(win.act_ocr, self.top_toolbar.actions())
        region_ocr = win.mode_actions["region_ocr"]
        self.assertIn(region_ocr, win.menu_more_features.actions())
        self.assertNotIn(region_ocr, self.edit_toolbar.actions())
        # 「🛠 バーを編集...」はその他メニューの末尾に固定
        last = win.menu_more_features.actions()[-1]
        self.assertIs(last, win.act_edit_toolbar_layout)

    def test_ocr_shortcuts_kept(self):
        self.assertEqual(
            self.win.act_ocr.shortcut(), QKeySequence("Ctrl+Shift+O")
        )
        self.assertEqual(
            self.win.mode_actions["region_ocr"].shortcut(),
            QKeySequence("Ctrl+Alt+O"),
        )
        self.assertEqual(
            self.win.act_save.shortcut(),
            QKeySequence(QKeySequence.StandardKey.Save),
        )
        self.assertEqual(
            self.win.act_save_as.shortcut(),
            QKeySequence(QKeySequence.StandardKey.SaveAs),
        )

    def test_ocr_shortcut_fires_from_menu(self):
        # メニューに入っていても Ctrl+Shift+O で色抜き・OCRが呼ばれること
        win = self.win
        fired = []
        win.act_ocr.triggered.disconnect()
        win.act_ocr.triggered.connect(
            lambda checked=False: fired.append("ocr")
        )
        win.show()
        self._app.processEvents()
        QTest.qWaitForWindowActive(win, timeout=3000)
        QTest.keyClick(
            win.viewer,
            Qt.Key.Key_O,
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier,
        )
        self._app.processEvents()
        self.assertEqual(fired, ["ocr"])
        win.hide()

    # ---------------------------------------------------------------
    # 題名変更
    # ---------------------------------------------------------------
    def test_rename_buttons(self):
        win = self.win
        win._save_toolbar_layout(
            {"more": "🧰 道具箱", "erase": "消す道具", "save": "💾 ほぞん"}, {}
        )
        win._apply_toolbar_layout()
        self.assertEqual(win.btn_more_features.text(), "🧰 道具箱")
        self.assertEqual(win.btn_erase_modes.text(), "消す道具")
        self.assertEqual(win.btn_save.text(), "💾 ほぞん")
        # 空欄に戻すと既定名になる
        win._save_toolbar_layout({"more": "", "erase": "", "save": ""}, {})
        win._apply_toolbar_layout()
        self.assertEqual(win.btn_more_features.text(), "🧰 その他")
        self.assertEqual(win.btn_erase_modes.text(), "🧽 消去")
        self.assertEqual(win.btn_save.text(), "💾 保存")

    # ---------------------------------------------------------------
    # 配置変更
    # ---------------------------------------------------------------
    def test_move_items_and_persist(self):
        win = self.win
        win._save_toolbar_layout(
            {},
            {"print": "hidden", "excel": "bar", "mode:erase": "bar",
             "save_as": "more"},
        )
        win._apply_toolbar_layout()
        self._app.processEvents()
        # 印刷はどこにもいない（隠す）
        for container in (
            self.top_toolbar,
            self.edit_toolbar,
            win.menu_more_features,
            win.menu_save,
            win.menu_erase_modes,
        ):
            self.assertNotIn(win.act_print, container.actions())
        # Excelはバーへ、消しゴムモードは編集バーへ、別名保存はその他へ
        self.assertIn(win.act_excel, self.top_toolbar.actions())
        self.assertIn(win.mode_actions["erase"], self.edit_toolbar.actions())
        self.assertIn(win.act_save_as, win.menu_more_features.actions())
        self.assertEqual(len(win.menu_save.actions()), 0)
        # 「🛠 バーを編集...」は移動後もその他メニューの末尾
        self.assertIs(
            win.menu_more_features.actions()[-1], win.act_edit_toolbar_layout
        )
        # QSettings に保存されている
        _titles, places = win._load_toolbar_layout()
        self.assertEqual(places["print"], "hidden")
        self.assertEqual(places["excel"], "bar")
        # 新しいウィンドウ（＝次回起動）にも配置が引き継がれる
        win2 = MainWindow()
        try:
            self._app.processEvents()
            top2 = win2.findChildren(QToolBar)[0]
            self.assertIn(win2.act_excel, top2.actions())
            self.assertNotIn(win2.act_print, top2.actions())
        finally:
            win2.close()
            win2.deleteLater()
            self._app.processEvents()

    def test_reset_to_defaults(self):
        win = self.win
        win._save_toolbar_layout(
            {"more": "🧰 道具箱", "erase": "", "save": ""},
            {"print": "hidden", "excel": "bar"},
        )
        win._apply_toolbar_layout()
        win._clear_toolbar_layout()
        win._apply_toolbar_layout()
        self._app.processEvents()
        self.assertEqual(win.btn_more_features.text(), "🧰 その他")
        self.assertIn(win.act_print, self.top_toolbar.actions())
        self.assertIn(win.act_excel, win.menu_more_features.actions())
        self.assertIn(win.act_save_as, win.menu_save.actions())
        self.assertIn(win.act_ocr, win.menu_more_features.actions())

    def test_anchor_order_preserved(self):
        # 同じアンカーを共有する項目をまとめてバーへ出しても、相対順序は
        # レジストリ登録順のまま崩れないこと（レビュー指摘の検証）。
        # 「その他」へ出してからバーへ戻す往復でも順序が保たれること。
        win = self.win
        keys = list(win._movable_items.keys())
        top_keys = [
            k for k in keys if win._movable_items[k]["toolbar"] is self.top_toolbar
        ]
        edit_keys = [
            k for k in keys if win._movable_items[k]["toolbar"] is self.edit_toolbar
        ]

        def bar_sequence(toolbar, group_keys):
            acts = toolbar.actions()
            placed = [
                k for k in group_keys if win._movable_items[k]["action"] in acts
            ]
            return sorted(
                placed, key=lambda k: acts.index(win._movable_items[k]["action"])
            )

        for via in ("bar", "more"):
            win._save_toolbar_layout({}, {k: via for k in keys})
            win._apply_toolbar_layout()
            self._app.processEvents()
            win._save_toolbar_layout({}, {k: "bar" for k in keys})
            win._apply_toolbar_layout()
            self._app.processEvents()
            self.assertEqual(bar_sequence(self.top_toolbar, top_keys), top_keys)
            self.assertEqual(bar_sequence(self.edit_toolbar, edit_keys), edit_keys)

    def test_empty_save_menu_detaches_from_button(self):
        # 保存メニューが空になったら、矢印が空メニューを開かないよう
        # ボタンからメニューが外れること。項目が戻れば付け直されること。
        win = self.win
        self.assertIs(win.btn_save.menu(), win.menu_save)
        win._save_toolbar_layout({}, {"save_as": "more"})
        win._apply_toolbar_layout()
        self.assertEqual(len(win.menu_save.actions()), 0)
        self.assertIsNone(win.btn_save.menu())
        win._save_toolbar_layout({}, {"save_as": "save"})
        win._apply_toolbar_layout()
        self.assertIs(win.btn_save.menu(), win.menu_save)
        self.assertIn(win.act_save_as, win.menu_save.actions())

    def test_hidden_items_keep_shortcuts(self):
        # 「隠す」にした項目でもショートカットは効き続けること
        # （ウィンドウへも登録してあるため）。
        win = self.win
        fired = []
        win.act_ocr.triggered.disconnect()
        win.act_ocr.triggered.connect(
            lambda checked=False: fired.append("ocr")
        )
        win._save_toolbar_layout(
            {}, {"ocr": "hidden", "mode:region_ocr": "hidden"}
        )
        win._apply_toolbar_layout()
        win.show()
        self._app.processEvents()
        QTest.qWaitForWindowActive(win, timeout=3000)
        QTest.keyClick(
            win.viewer,
            Qt.Key.Key_O,
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier,
        )
        self._app.processEvents()
        self.assertEqual(fired, ["ocr"])
        QTest.keyClick(
            win.viewer,
            Qt.Key.Key_O,
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier,
        )
        self._app.processEvents()
        self.assertEqual(win.viewer.mode, "region_ocr")
        win.hide()

    # ---------------------------------------------------------------
    # 編集ダイアログ
    # ---------------------------------------------------------------
    def _make_dialog(self):
        win = self.win
        titles, places = win._load_toolbar_layout()
        items = [
            (key, item["label"], item["default"])
            for key, item in win._movable_items.items()
        ]
        return ToolbarLayoutDialog(win, items, titles, places)

    def test_dialog_roundtrip(self):
        dialog = self._make_dialog()
        try:
            # 既定では全項目が既定配置で並ぶ
            places = dialog.result_places()
            self.assertEqual(places["save_as"], "save")
            self.assertEqual(places["ocr"], "more")
            self.assertEqual(places["mode:region_ocr"], "more")
            self.assertEqual(places["mode:erase"], "erase")
            self.assertEqual(places["print"], "bar")
            # コンボと題名を変えると結果に反映される
            combo = dialog._place_combos["print"]
            combo.setCurrentIndex(combo.findData("hidden"))
            dialog._title_edits["more"].setText("🧰 道具箱")
            self.assertEqual(dialog.result_places()["print"], "hidden")
            self.assertEqual(dialog.result_titles()["more"], "🧰 道具箱")
            # 「初期状態に戻す」でコンボも題名も既定に戻る
            dialog._reset_to_defaults()
            self.assertEqual(dialog.result_places()["print"], "bar")
            self.assertEqual(dialog.result_titles()["more"], "")
        finally:
            dialog.close()
            dialog.deleteLater()
            self._app.processEvents()


if __name__ == "__main__":
    unittest.main()
