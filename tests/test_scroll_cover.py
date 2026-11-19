# -*- coding: utf-8 -*-
"""ページスクロール表示（ベータ版）と全画面の表紙ボタンに対するテスト。

- スクロール表示：全ページが縦に並ぶこと、ページ位置の判定、
  スクロールでの現在ページの追従、1ページ表示への復帰。
- 表紙：見開きの組み立てで表紙あり/なしが変わること、
  全画面リーダーの「📕 表紙」ボタンで切り替えられること。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz


def _make_doc(page_sizes):
    doc = fitz.open()
    for i, (w, h) in enumerate(page_sizes):
        page = doc.new_page(width=w, height=h)
        page.insert_text((40, 60), f"page {i + 1}", fontsize=16)
    return doc


class TestScrollDisplay(unittest.TestCase):
    """通常画面のページスクロール表示。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])
        from ui.pdf_viewer import PDFViewer

        cls._viewer_cls = PDFViewer

    def setUp(self):
        self.doc = _make_doc([(595, 842), (595, 842), (420, 595), (595, 842)])
        self.viewer = self._viewer_cls()
        self.viewer.resize(800, 600)
        self.viewer.show()
        self.viewer.load_pdf(self.doc, 0, fit_view=False)
        self._app.processEvents()

    def tearDown(self):
        if not self.doc.is_closed:
            self.doc.close()
        self.viewer.close()
        self._app.processEvents()

    def test_scroll_layout_stacks_pages_vertically(self):
        self.viewer.set_scroll_mode(True)
        self.assertTrue(self.viewer.scroll_mode)
        rects = self.viewer._scroll_page_rects
        self.assertEqual(sorted(rects), [0, 1, 2, 3])
        # 縦に並んでいる（上端が単調に増える）こと
        tops = [rects[i].top() for i in range(4)]
        self.assertEqual(tops, sorted(tops))
        self.assertTrue(all(tops[i + 1] > rects[i].bottom() for i in range(3)))
        # 幅は画面幅に収まっていること
        for i in range(4):
            self.assertLessEqual(rects[i].width(), self.viewer.viewport().width())
        # 見えているページは描画済みであること
        self.assertTrue(len(self.viewer._scroll_rendered) >= 1)

    def test_scroll_page_at_scene_pos(self):
        self.viewer.set_scroll_mode(True)
        rects = self.viewer._scroll_page_rects
        for i in range(4):
            center = rects[i].center()
            self.assertEqual(self.viewer._scroll_page_at_scene_pos(center), i)

    def test_scroll_to_page_updates_current_page(self):
        self.viewer.set_scroll_mode(True)
        self.assertTrue(self.viewer._scroll_to_page(2))
        self.assertEqual(self.viewer.current_page_idx, 2)
        self.assertTrue(self.viewer._scroll_to_page(0))
        self.assertEqual(self.viewer.current_page_idx, 0)

    def test_load_pdf_in_scroll_mode_scrolls_instead_of_switching(self):
        self.viewer.set_scroll_mode(True)
        self.viewer.load_pdf(self.doc, 3, fit_view=True)
        self.assertTrue(self.viewer.scroll_mode)
        self.assertEqual(self.viewer.current_page_idx, 3)
        self.assertEqual(len(self.viewer._scroll_page_rects), 4)

    def test_go_next_prev_page_in_scroll_mode(self):
        self.viewer.set_scroll_mode(True)
        self.assertTrue(self.viewer.go_next_page())
        self.assertEqual(self.viewer.current_page_idx, 1)
        self.assertTrue(self.viewer.go_prev_page())
        self.assertEqual(self.viewer.current_page_idx, 0)

    def test_exit_scroll_mode_restores_single_page(self):
        self.viewer.set_scroll_mode(True)
        self.viewer._scroll_to_page(2)
        self.viewer.set_scroll_mode(False)
        self.assertFalse(self.viewer.scroll_mode)
        self.assertEqual(self.viewer.current_page_idx, 2)
        self.assertIsNotNone(self.viewer._base_pixmap_item)

    def test_scroll_zoom_changes_layout(self):
        self.viewer.set_scroll_mode(True)
        before = self.viewer._scroll_page_rects[0].width()
        self.viewer.zoom_in()
        after = self.viewer._scroll_page_rects[0].width()
        self.assertGreater(after, before)
        self.viewer.zoom_out()
        self.viewer.zoom_out()
        self.assertLess(self.viewer._scroll_page_rects[0].width(), after)

    def test_scroll_mode_without_doc_is_rejected(self):
        viewer = self._viewer_cls()
        viewer.resize(800, 600)
        viewer.show()
        viewer.set_scroll_mode(True)
        self.assertFalse(viewer.scroll_mode)
        viewer.close()
        self._app.processEvents()

    def test_scroll_pixmap_cache_invalidated_on_render_page(self):
        # 内容の変更（コマンドの実行・Undo/Redo）で render_page が呼ばれたら、
        # 古い画像キャッシュではなく現在の内容で描き直されること。
        self.viewer.set_scroll_mode(True)
        self.viewer._render_scroll_page(0)
        key = next(k for k in self.viewer._scroll_pixmap_cache if k[0] == 0)
        before = self.viewer._scroll_pixmap_cache[key].toImage()
        self.doc[0].insert_text((40, 400), "CHANGED TEXT", fontsize=40)
        self.viewer.render_page()
        self._app.processEvents()
        self.assertIn(key, self.viewer._scroll_pixmap_cache)
        after = self.viewer._scroll_pixmap_cache[key].toImage()
        self.assertEqual(before.size(), after.size())
        self.assertNotEqual(before, after)

    def test_scroll_layout_rebuilt_when_page_deleted_via_load_pdf(self):
        # ページ削除を load_pdf で通知してくるコマンド系でも、
        # スクロール表示の並びが新しいページ数で作り直されること。
        self.viewer.set_scroll_mode(True)
        self.assertEqual(len(self.viewer._scroll_page_rects), 4)
        del self.doc[3]
        self.viewer.load_pdf(self.doc, 0, fit_view=True)
        self.assertTrue(self.viewer.scroll_mode)
        self.assertEqual(len(self.viewer._scroll_page_rects), 3)

    def test_scroll_layout_rebuilt_when_page_rotated_via_load_pdf(self):
        self.viewer.set_scroll_mode(True)
        rect_before = self.viewer._scroll_page_rects[0]
        self.assertGreater(rect_before.height(), rect_before.width())
        self.doc[0].set_rotation(90)
        self.viewer.load_pdf(self.doc, 0, fit_view=True)
        rect_after = self.viewer._scroll_page_rects[0]
        self.assertGreater(rect_after.width(), rect_after.height())


class TestCoverToggle(unittest.TestCase):
    """全画面見開きの表紙の有無。"""

    def test_build_spreads_cover_on_and_off(self):
        from ui.spread_engine import SpreadLayoutEngine

        doc = _make_doc([(595, 842)] * 5)
        try:
            with_cover = SpreadLayoutEngine.build_spreads(doc, cover_single=True)
            self.assertEqual([s.page_indices for s in with_cover], [[0], [1, 2], [3, 4]])
            without_cover = SpreadLayoutEngine.build_spreads(doc, cover_single=False)
            self.assertEqual([s.page_indices for s in without_cover], [[0, 1], [2, 3], [4]])
        finally:
            doc.close()

    def test_reader_cover_button_toggles(self):
        from PyQt6.QtWidgets import QApplication
        from ui.spread_reader import FullscreenSpreadReader

        app = QApplication.instance() or QApplication([])
        doc = _make_doc([(595, 842)] * 5)
        try:
            reader = FullscreenSpreadReader(doc=doc, start_page=0, cover_single=True)
            reader.show()
            app.processEvents()
            try:
                self.assertTrue(hasattr(reader, "btn_hud_cover"))
                self.assertTrue(reader.btn_hud_cover.isChecked())
                self.assertEqual(
                    [s.page_indices for s in reader.spreads], [[0], [1, 2], [3, 4]]
                )
                reader.btn_hud_cover.click()
                self.assertFalse(reader.cover_single)
                self.assertFalse(reader.btn_hud_cover.isChecked())
                self.assertEqual(
                    [s.page_indices for s in reader.spreads], [[0, 1], [2, 3], [4]]
                )
                reader.btn_hud_cover.click()
                self.assertTrue(reader.cover_single)
                self.assertTrue(reader.btn_hud_cover.isChecked())
            finally:
                reader.close()
        finally:
            doc.close()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
