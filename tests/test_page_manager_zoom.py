# -*- coding: utf-8 -*-
"""ページ管理の名称変更と拡大・ズーム（2026-11-26、MIWA 依頼）に対するテスト。

- 「取り込み元」→「サブPDF」への名称変更：見出し・ボタン・読み込み後の表示。
- サムネイルのズーム：ボタン／Ctrl+ホイールで両リストのアイコンサイズが
  連動して変わり、下限・上限で止まり、標準に戻せること。
- 拡大表示：ページ管理をフローティングに切り替えられ、戻せること。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz


def _make_pdf(path, pages=3):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=300, height=300)
        page.insert_text((20, 40), f"page {i + 1}", fontsize=12)
    doc.save(path)
    doc.close()


class TestPageManagerRenameAndZoom(unittest.TestCase):
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
        self.pm = self.window.page_manager
        self.pm.reset_thumb_zoom()

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
            win.close()
        except Exception:
            pass

    # --- 名称変更 ---
    def test_sub_label_and_button_wording(self):
        self.assertIn("サブPDF", self.pm.lbl_sub.text())
        self.assertNotIn("取り込み元", self.pm.lbl_sub.text())
        self.assertEqual(self.pm.btn_open_sub.text(), "📂 サブPDFを開く")

    def test_sub_label_after_load(self):
        path = str(Path(self._tmp.name) / "sub.pdf")
        _make_pdf(path)
        self.pm.load_sub_pdf_from_path(path)
        self.assertTrue(self.pm.lbl_sub.text().startswith("📥 サブPDF: "))
        self.assertIn("sub.pdf", self.pm.lbl_sub.text())

    # --- ズーム ---
    def test_zoom_buttons_change_both_lists(self):
        base_main = self.pm.list_widget.iconSize()
        self.assertEqual(base_main.width(), 95)
        self.assertEqual(base_main.height(), 130)
        self.pm.zoom_thumbnails(1)
        bigger = self.pm.list_widget.iconSize()
        self.assertGreater(bigger.width(), base_main.width())
        self.assertEqual(bigger, self.pm.sub_list_widget.iconSize())
        self.pm.reset_thumb_zoom()
        self.assertEqual(self.pm.list_widget.iconSize(), base_main)

    def test_zoom_clamps_at_min_and_max(self):
        self.pm.zoom_thumbnails(100)
        self.assertAlmostEqual(self.pm._thumb_scale, self.pm._THUMB_SCALE_MAX)
        self.pm.zoom_thumbnails(-100)
        self.assertAlmostEqual(self.pm._thumb_scale, self.pm._THUMB_SCALE_MIN)

    def test_ctrl_wheel_zooms(self):
        from PyQt6.QtCore import Qt

        self.assertIsNotNone(self.pm.list_widget.zoom_callback)
        event = mock.Mock()
        event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        event.angleDelta.return_value.y.return_value = 120
        before = self.pm.list_widget.iconSize()
        self.pm.list_widget.wheelEvent(event)
        event.accept.assert_called_once()
        self.assertGreater(self.pm.list_widget.iconSize().width(), before.width())

    # --- サブPDF欄の畳み込み ---
    def test_sub_collapse_hides_body(self):
        self.pm.set_sub_collapsed(False)
        self._app.processEvents()
        self.assertTrue(self.pm.sub_body.isVisible())
        self.pm.set_sub_collapsed(True)
        self._app.processEvents()
        self.assertTrue(self.pm._sub_collapsed)
        self.assertFalse(self.pm.sub_body.isVisible())
        self.assertEqual(self.pm.btn_sub_collapse.text(), "▶ ひらく")
        self.pm.set_sub_collapsed(False)
        self._app.processEvents()
        self.assertTrue(self.pm.sub_body.isVisible())
        self.assertEqual(self.pm.btn_sub_collapse.text(), "▼ たたむ")

    def test_expand_auto_collapses_sub_and_restores(self):
        from PyQt6.QtCore import QSettings

        self.pm.set_sub_collapsed(False)  # 普段は開いた状態にしておく
        self.pm.setFloating(True)
        self._app.processEvents()
        self.assertTrue(self.pm.isFloating())
        # 拡大表示中は自動で畳まれる（保存済みの好みは書き換わらない）
        self.assertTrue(self.pm._sub_collapsed)
        settings = QSettings("SecureLocalPDFEditor", "SecureLocalPDFEditor")
        saved = settings.value("page_manager/sub_collapsed", False)
        self.assertIn(saved, (False, "false", "False", 0))
        self.pm.setFloating(False)
        self._app.processEvents()
        self.assertFalse(self.pm.isFloating())
        # 戻すと元の開いた状態に戻る
        self.assertFalse(self.pm._sub_collapsed)

    # --- 拡大表示（タイトルバーのダブルクリック等で切り離す） ---
    def test_float_and_redock(self):
        self.assertFalse(self.pm.isFloating())
        self.assertFalse(hasattr(self.pm, "btn_expand"))
        self.pm.setFloating(True)
        self._app.processEvents()
        self.assertTrue(self.pm.isFloating())
        self.pm.setFloating(False)
        self._app.processEvents()
        self.assertFalse(self.pm.isFloating())

    # --- 画質（ズーム時の描画解像度） ---
    def test_render_scale_doubles_when_zoomed(self):
        from core.geometry import THUMBNAIL_RENDER_SCALE

        self.assertEqual(self.pm._current_render_scale(), THUMBNAIL_RENDER_SCALE)
        self.pm.zoom_thumbnails(1)
        self.assertEqual(
            self.pm._current_render_scale(), THUMBNAIL_RENDER_SCALE * 2.0)
        self.pm.reset_thumb_zoom()
        self.assertEqual(self.pm._current_render_scale(), THUMBNAIL_RENDER_SCALE)

    def test_zoom_rerenders_sub_thumbnails_at_higher_quality(self):
        path = str(Path(self._tmp.name) / "subq.pdf")
        _make_pdf(path)
        self.pm.load_sub_pdf_from_path(path)
        self._app.processEvents()
        gen_before = self.pm._sub_generation_id
        self.pm.zoom_thumbnails(1)
        self.assertGreater(self.pm._sub_generation_id, gen_before)


if __name__ == "__main__":
    unittest.main()
