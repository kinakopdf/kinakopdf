# -*- coding: utf-8 -*-
"""表示だけの折りたたみ（横帯を非表示にして下を詰めて表示）の単体テスト。

core/fold の純粋関数を GUI なしで検証し、ビューア側の座標変換と
描画の詰め具合を offscreen の実 GUI で検証する。
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# tests/ サブフォルダから直接実行された場合でもプロジェクトルートを import 可能にする
_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent if (_THIS_DIR.name == "tests" or not (_THIS_DIR / "core").is_dir()) else _THIS_DIR
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import fitz

from core.fold import (
    MAX_FOLD_BAND_FRACTION,
    MIN_FOLD_BAND_HEIGHT_PT,
    clamp_fold_band_to_page,
    fold_band_pixel_rows,
    fold_display_height,
    folded_y_to_view_y,
    validate_fold_band,
    view_point_in_band,
    view_rect_intersects_band,
    view_y_to_folded_y,
)


class TestValidateFoldBand(unittest.TestCase):
    def test_valid_band(self):
        self.assertIsNone(validate_fold_band(0.0, 842.0, 300.0, 400.0))

    def test_too_small_band_rejected(self):
        err = validate_fold_band(0.0, 842.0, 300.0, 300.0 + MIN_FOLD_BAND_HEIGHT_PT - 0.1)
        self.assertIsNotNone(err)
        self.assertIn("小さすぎ", err)

    def test_min_height_boundary_ok(self):
        self.assertIsNone(validate_fold_band(0.0, 842.0, 300.0, 300.0 + MIN_FOLD_BAND_HEIGHT_PT))

    def test_almost_whole_page_rejected(self):
        err = validate_fold_band(0.0, 842.0, 0.0, 842.0 * MAX_FOLD_BAND_FRACTION)
        self.assertIsNotNone(err)
        self.assertIn("ほとんど", err)

    def test_zero_page_height_rejected(self):
        self.assertIsNotNone(validate_fold_band(0.0, 0.0, 0.0, 100.0))


class TestFoldMapping(unittest.TestCase):
    BAND = (300.0, 400.0)

    def test_display_height(self):
        self.assertAlmostEqual(fold_display_height(842.0, self.BAND), 742.0)
        self.assertAlmostEqual(fold_display_height(842.0, None), 842.0)

    def test_view_to_folded_above_band(self):
        self.assertAlmostEqual(view_y_to_folded_y(100.0, self.BAND), 100.0)

    def test_view_to_folded_below_band(self):
        self.assertAlmostEqual(view_y_to_folded_y(500.0, self.BAND), 400.0)

    def test_view_to_folded_inside_band_clamps_to_seam(self):
        self.assertAlmostEqual(view_y_to_folded_y(350.0, self.BAND), 300.0)

    def test_folded_to_view_below_seam(self):
        self.assertAlmostEqual(folded_y_to_view_y(400.0, self.BAND), 500.0)

    def test_folded_to_view_above_seam(self):
        self.assertAlmostEqual(folded_y_to_view_y(100.0, self.BAND), 100.0)

    def test_roundtrip_below_band(self):
        for y in (401.0, 500.0, 841.0):
            folded = view_y_to_folded_y(y, self.BAND)
            self.assertAlmostEqual(folded_y_to_view_y(folded, self.BAND), y)

    def test_roundtrip_above_band(self):
        for y in (0.0, 100.0, 299.0):
            folded = view_y_to_folded_y(y, self.BAND)
            self.assertAlmostEqual(folded_y_to_view_y(folded, self.BAND), y)

    def test_no_band_is_identity(self):
        self.assertAlmostEqual(view_y_to_folded_y(500.0, None), 500.0)
        self.assertAlmostEqual(folded_y_to_view_y(500.0, None), 500.0)


class TestBandGeometry(unittest.TestCase):
    BAND = (300.0, 400.0)

    def test_clamp_reversed_and_outside(self):
        self.assertEqual(clamp_fold_band_to_page(0.0, 842.0, 400.0, 300.0), (300.0, 400.0))
        self.assertEqual(clamp_fold_band_to_page(0.0, 842.0, -50.0, 100.0), (0.0, 100.0))
        self.assertEqual(clamp_fold_band_to_page(0.0, 842.0, 800.0, 900.0), (800.0, 842.0))

    def test_point_in_band(self):
        self.assertTrue(view_point_in_band(350.0, self.BAND))
        self.assertFalse(view_point_in_band(300.0 - 0.1, self.BAND))
        self.assertFalse(view_point_in_band(400.0, self.BAND))
        self.assertFalse(view_point_in_band(350.0, None))

    def test_rect_intersects_band(self):
        self.assertTrue(view_rect_intersects_band((0, 350, 100, 360), self.BAND))
        self.assertTrue(view_rect_intersects_band((0, 250, 100, 350), self.BAND))
        self.assertTrue(view_rect_intersects_band((0, 100, 100, 800), self.BAND))
        self.assertFalse(view_rect_intersects_band((0, 100, 100, 300), self.BAND))
        self.assertFalse(view_rect_intersects_band((0, 400, 100, 500), self.BAND))
        self.assertFalse(view_rect_intersects_band((0, 350, 100, 360), None))

    def test_pixel_rows(self):
        top, bottom = fold_band_pixel_rows(0.0, self.BAND, 3.0)
        self.assertEqual((top, bottom), (900, 1200))
        top, bottom = fold_band_pixel_rows(10.0, (310.0, 410.0), 2.0)
        self.assertEqual((top, bottom), (600, 800))


def _make_doc():
    """上部と下部に文字がある2ページのPDFを作る。"""
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 50), "TOP-TEXT", fontsize=20)
        page.insert_text((50, 800), "BOTTOM-TEXT", fontsize=20)
    return doc


class TestFoldViewer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])
        from ui.pdf_viewer import PDFViewer

        cls._viewer_cls = PDFViewer

    def setUp(self):
        self.doc = _make_doc()
        self.viewer = self._viewer_cls()
        self.viewer.resize(800, 600)
        self.viewer.show()
        self.viewer.load_pdf(self.doc, 0, fit_view=False)

    def tearDown(self):
        self.viewer.close()
        self.doc.close()

    def test_fold_shrinks_pixmap_height(self):
        before = self.viewer.pixmap_height
        err = self.viewer.set_fold_band_for_current_page(300.0, 400.0)
        self.assertIsNone(err)
        # 帯100pt分だけ表示が短くなる（render_scale=3.0）
        self.assertEqual(self.viewer.pixmap_height, before - 300)
        self.assertTrue(self.viewer.has_fold_band())

    def test_fold_cleared_restores_height(self):
        before = self.viewer.pixmap_height
        self.viewer.set_fold_band_for_current_page(300.0, 400.0)
        self.assertTrue(self.viewer.clear_fold_band())
        self.assertEqual(self.viewer.pixmap_height, before)
        self.assertFalse(self.viewer.has_fold_band())

    def test_invalid_band_rejected(self):
        self.assertIsNotNone(self.viewer.set_fold_band_for_current_page(300.0, 302.0))
        self.assertFalse(self.viewer.has_fold_band())
        self.assertIsNotNone(self.viewer.set_fold_band_for_current_page(0.0, 800.0))
        self.assertFalse(self.viewer.has_fold_band())

    def test_scene_to_pdf_below_band_maps_to_correct_raw_y(self):
        self.viewer.set_fold_band_for_current_page(300.0, 400.0)
        # 詰めた後の表示で y=450 の位置は、元のページでは y=550 にあたる。
        from PyQt6.QtCore import QPointF

        scale = self.viewer.pixmap_height / self.viewer._fold_display_height()
        scene_y = (450.0 - self.viewer.page_rect.y()) * scale
        raw = self.viewer.scene_to_pdf(QPointF(100.0, scene_y))
        self.assertAlmostEqual(raw.y, 550.0, places=3)

    def test_pdf_to_scene_below_band_shifts_up(self):
        self.viewer.set_fold_band_for_current_page(300.0, 400.0)
        x, y = self.viewer.pdf_to_scene(fitz.Point(100.0, 550.0))
        scale = self.viewer.pixmap_height / self.viewer._fold_display_height()
        self.assertAlmostEqual(y, 450.0 * scale, places=3)
        self.assertAlmostEqual(x, 100.0 * scale, places=3)

    def test_coordinate_roundtrip_below_band(self):
        self.viewer.set_fold_band_for_current_page(300.0, 400.0)
        from PyQt6.QtCore import QPointF

        orig = QPointF(200.0, 600.0)
        raw = self.viewer.scene_to_pdf(orig)
        x, y = self.viewer.pdf_to_scene(raw)
        self.assertAlmostEqual(x, orig.x(), places=3)
        self.assertAlmostEqual(y, orig.y(), places=3)

    def test_fold_persists_across_page_moves(self):
        self.viewer.set_fold_band_for_current_page(300.0, 400.0)
        self.viewer.load_pdf(self.doc, 1, fit_view=False)
        self.assertFalse(self.viewer.has_fold_band())
        self.viewer.load_pdf(self.doc, 0, fit_view=False)
        self.assertTrue(self.viewer.has_fold_band())

    def test_fold_does_not_modify_document(self):
        self.viewer.set_fold_band_for_current_page(300.0, 400.0)
        page = self.doc[0]
        self.assertEqual(tuple(page.rect), (0.0, 0.0, 595.0, 842.0))
        self.assertEqual(tuple(page.mediabox), (0.0, 0.0, 595.0, 842.0))
        self.assertEqual(self.viewer.page_annotations.get(0, []), [])
        self.assertFalse(self.viewer.is_modified)

    def test_folded_rendering_hides_band_rows(self):
        """折りたたんだ描画では、帯の部分の文字が表示されないこと。"""
        self.viewer.set_fold_band_for_current_page(300.0, 400.0)
        pixmap = self.viewer._cached_base_pixmap
        self.assertIsNotNone(pixmap)
        # 折りたたみ後の画像の高さは元の高さから帯分だけ短い。
        self.assertLess(pixmap.height(), 842 * 3)


class TestFoldToggle(unittest.TestCase):
    """折りたたみボタンのON/OFF切り替え（MainWindow.set_mode）。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def test_fold_button_toggles_band_off(self):
        from ui.main_window import MainWindow

        doc = _make_doc()
        try:
            win = MainWindow()
            win.resize(800, 600)
            win.show()
            win.viewer.load_pdf(doc, 0, fit_view=False)
            self.assertIsNone(win.viewer.set_fold_band_for_current_page(300.0, 400.0))
            self.assertTrue(win.viewer.has_fold_band())
            # 帯がある状態で折りたたみボタンを押すと解除されて閲覧に戻る。
            win.set_mode("fold")
            self.assertFalse(win.viewer.has_fold_band())
            self.assertEqual(win.viewer.mode, "view")
            # 帯がない状態で押すと折りたたみモードに入る。
            win.set_mode("fold")
            self.assertEqual(win.viewer.mode, "fold")
            win.close()
        finally:
            # ウィンドウを閉じると文書も閉じられることがあるため確認してから閉じる。
            if not doc.is_closed:
                doc.close()


if __name__ == "__main__":
    unittest.main()
