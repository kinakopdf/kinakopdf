# -*- coding: utf-8 -*-
"""Geminiチャットレビュー #5（印刷）の修正テスト。

プレビューと実印刷で共通の余白・フィット計算を使うことと、
一覧ラベルが最大表示ページ数と一致することを確認する。
"""
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent if (_THIS_DIR.name == "tests" or not (_THIS_DIR / "core").is_dir()) else _THIS_DIR
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))


class TestPrintHelpers(unittest.TestCase):
    def test_margin_pt_by_mode(self):
        from ui.main_window import print_margin_pt

        self.assertEqual(print_margin_pt("fit_margin"), 14.0)
        self.assertEqual(print_margin_pt("fit_full"), 0.0)
        self.assertEqual(print_margin_pt("actual"), 0.0)

    def test_fit_size_within(self):
        from ui.main_window import fit_size_within

        self.assertEqual(fit_size_within(200, 100, 100, 100), (100.0, 50.0))
        self.assertEqual(fit_size_within(100, 200, 100, 100), (50.0, 100.0))
        self.assertEqual(fit_size_within(100, 100, 300, 200), (200.0, 200.0))
        self.assertEqual(fit_size_within(0, 100, 100, 100), (0.0, 0.0))
        self.assertEqual(fit_size_within(100, 100, 0, 100), (0.0, 0.0))

    def test_grid_status_text(self):
        from ui.main_window import PRINT_GRID_MAX_PAGES, print_grid_status_text

        self.assertEqual(print_grid_status_text(10), " 全 10 ページ一覧 ")
        self.assertEqual(
            print_grid_status_text(PRINT_GRID_MAX_PAGES),
            f" 全 {PRINT_GRID_MAX_PAGES} ページ一覧 ")
        text = print_grid_status_text(30)
        self.assertIn("30", text)
        self.assertIn(f"最大{PRINT_GRID_MAX_PAGES}ページまで表示", text)
        self.assertNotIn("全 30 ページ一覧", text)

    def test_preview_and_printer_share_margin_math(self):
        """プレビュー（pt計算）と実印刷（dpi換算）で描画サイズが一致すること。"""
        from ui.main_window import fit_size_within, print_margin_pt

        paper_w, paper_h = 595.28, 841.89  # A4縦
        page_w, page_h = 595.28, 841.89
        margin = print_margin_pt("fit_margin")

        # プレビュー側: 用紙ptから余白を引いた領域にフィット
        avail_w = paper_w - margin * 2
        avail_h = paper_h - margin * 2
        prev_w, prev_h = fit_size_within(page_w, page_h, avail_w, avail_h)

        # 実印刷側: ビューポート（用紙をdpi換算したpx）から余白pxを引いてフィット
        dpi = 300
        vp_w, vp_h = paper_w / 72.0 * dpi, paper_h / 72.0 * dpi
        m_px = margin / 72.0 * dpi
        draw_w_px, draw_h_px = fit_size_within(
            page_w / 72.0 * dpi, page_h / 72.0 * dpi,
            vp_w - m_px * 2, vp_h - m_px * 2)
        print_w, print_h = draw_w_px / dpi * 72.0, draw_h_px / dpi * 72.0

        self.assertAlmostEqual(prev_w, print_w, places=4)
        self.assertAlmostEqual(prev_h, print_h, places=4)
        # 余白なし（fit_full）より小さく描かれる＝余白が効いている
        full_w, _ = fit_size_within(page_w, page_h, paper_w, paper_h)
        self.assertLess(prev_w, full_w)


if __name__ == "__main__":
    unittest.main()
