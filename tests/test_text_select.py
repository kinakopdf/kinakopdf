# -*- coding: utf-8 -*-
"""テキスト選択→コピーボタンの単体テスト。

core/text_select の純粋関数を GUI なしで検証し、ビューア側のドラッグ選択・
ポップアップ・コピー動作を offscreen の実 GUI で検証する。
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

from core.text_select import (
    extract_selected_text,
    select_words_in_rect,
    word_matches_rect,
    words_to_text,
)


def _w(x0, y0, x1, y1, text, block=0, line=0, no=0):
    return (float(x0), float(y0), float(x1), float(y1), text, block, line, no)


class TestSelectWordsInRect(unittest.TestCase):
    def test_select_middle_words_on_one_line(self):
        words = [
            _w(0, 0, 40, 20, "aaa", 0, 0, 0),
            _w(50, 0, 90, 20, "bbb", 0, 0, 1),
            _w(100, 0, 140, 20, "ccc", 0, 0, 2),
            _w(150, 0, 190, 20, "ddd", 0, 0, 3),
        ]
        selected = select_words_in_rect(words, (45, -5, 145, 25))
        self.assertEqual([w[4] for w in selected], ["bbb", "ccc"])

    def test_word_partly_covered_by_center(self):
        # 中心が選択範囲の外でも、重なりが 35% 以上あれば選択対象。
        words = [_w(0, 0, 100, 20, "longword", 0, 0, 0)]
        self.assertTrue(word_matches_rect(words[0], (60, -5, 200, 25)))
        self.assertEqual(len(select_words_in_rect(words, (60, -5, 200, 25))), 1)

    def test_word_slightly_touched_is_excluded(self):
        # 重なりが 35% 未満で中心も外なら対象外。
        words = [_w(0, 0, 100, 20, "longword", 0, 0, 0)]
        self.assertFalse(word_matches_rect(words[0], (90, -5, 200, 25)))
        self.assertEqual(select_words_in_rect(words, (90, -5, 200, 25)), [])

    def test_empty_rect_or_no_words(self):
        words = [_w(0, 0, 40, 20, "aaa", 0, 0, 0)]
        self.assertEqual(select_words_in_rect(words, (0, 0, 0, 20)), [])
        self.assertEqual(select_words_in_rect(words, (10, 10, 10, 50)), [])
        self.assertEqual(select_words_in_rect([], (0, 0, 100, 100)), [])

    def test_reversed_rect_is_normalized(self):
        words = [_w(50, 0, 90, 20, "bbb", 0, 0, 1)]
        self.assertEqual(len(select_words_in_rect(words, (100, 25, 40, -5))), 1)

    def test_reading_order_by_block_line_word(self):
        words = [
            _w(100, 40, 140, 60, "second-line", 0, 1, 0),
            _w(0, 0, 40, 20, "first-b", 0, 0, 1),
            _w(150, 40, 190, 60, "second-line-b", 0, 1, 1),
            _w(0, 0, 40, 20, "first-a", 0, 0, 0),
        ]
        # 座標が重なっていても番号順（読み順）で並ぶ。
        selected = select_words_in_rect(words, (0, 0, 200, 60))
        self.assertEqual(
            [w[4] for w in selected],
            ["first-a", "first-b", "second-line", "second-line-b"],
        )

    def test_blank_words_are_skipped(self):
        words = [
            _w(0, 0, 40, 20, "   ", 0, 0, 0),
            _w(50, 0, 90, 20, "real", 0, 0, 1),
        ]
        selected = select_words_in_rect(words, (0, 0, 100, 20))
        self.assertEqual([w[4] for w in selected], ["real"])

    def test_zero_size_word_is_excluded(self):
        words = [_w(10, 10, 10, 20, "flat", 0, 0, 0)]
        self.assertFalse(word_matches_rect(words[0], (0, 0, 100, 100)))


class TestWordsToText(unittest.TestCase):
    def test_same_line_joined_with_space(self):
        words = [_w(0, 0, 40, 20, "Hello", 0, 0, 0), _w(50, 0, 90, 20, "World", 0, 0, 1)]
        self.assertEqual(words_to_text(words), "Hello World")

    def test_line_change_inserts_newline(self):
        words = [
            _w(0, 0, 40, 20, "line1", 0, 0, 0),
            _w(0, 40, 40, 60, "line2", 0, 1, 0),
            _w(50, 40, 90, 60, "cont", 0, 1, 1),
        ]
        self.assertEqual(words_to_text(words), "line1\nline2 cont")

    def test_block_change_inserts_newline(self):
        words = [
            _w(0, 0, 40, 20, "block0", 0, 0, 0),
            _w(0, 40, 40, 60, "block1", 1, 0, 0),
        ]
        self.assertEqual(words_to_text(words), "block0\nblock1")

    def test_empty_selection(self):
        self.assertEqual(words_to_text([]), "")


class TestExtractSelectedTextWithRealPdf(unittest.TestCase):
    def _raw_words(self, page):
        """ビューアと同じ変換（単語矩形 + cropbox 原点）で raw 座標の単語一覧を作る。"""
        crop = page.cropbox
        out = []
        for w in page.get_text("words") or []:
            out.append((
                float(w[0]) + crop.x0, float(w[1]) + crop.y0,
                float(w[2]) + crop.x0, float(w[3]) + crop.y0,
                str(w[4]), int(w[5]), int(w[6]), int(w[7]),
            ))
        return out

    def test_extract_from_real_pdf(self):
        doc = fitz.open()
        try:
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 100), "Hello World test", fontsize=20)
            page.insert_text((72, 140), "Second line here", fontsize=20)
            words = self._raw_words(page)
            # 1行目の Hello World だけを囲む。
            text = extract_selected_text(words, (60, 70, 180, 110))
            self.assertEqual(text, "Hello World")
            # 2行にまたがると改行が入る。
            text2 = extract_selected_text(words, (60, 70, 260, 150))
            self.assertEqual(text2, "Hello World test\nSecond line here")
            # 文字の無い場所は空文字。
            self.assertEqual(extract_selected_text(words, (400, 700, 500, 750)), "")
        finally:
            doc.close()

    def test_extract_on_rotated_page(self):
        # 回転しても単語矩形は変わらないため、同じ raw 矩形で同じ文字が取れる。
        doc = fitz.open()
        try:
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 100), "Hello World test", fontsize=20)
            page.set_rotation(90)
            words = self._raw_words(page)
            text = extract_selected_text(words, (60, 70, 180, 110))
            self.assertEqual(text, "Hello World")
        finally:
            doc.close()

    def test_extract_on_cropped_page(self):
        # cropbox がずれていても、cropbox 原点を足せば raw 座標で一致する。
        doc = fitz.open()
        try:
            page = doc.new_page(width=595, height=842)
            page.insert_text((150, 150), "Hello Crop", fontsize=20)
            page.set_cropbox(fitz.Rect(100, 100, 500, 700))
            words = self._raw_words(page)
            text = extract_selected_text(words, (140, 120, 250, 160))
            self.assertEqual(text, "Hello Crop")
        finally:
            doc.close()


def _make_text_doc():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Hello World test", fontsize=20)
    page.insert_text((72, 140), "Second line here", fontsize=20)
    return doc


class TestTextSelectViewer(unittest.TestCase):
    """ビューア側のドラッグ選択・ポップアップ・コピー動作（実GUI・offscreen）。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])
        from ui.pdf_viewer import PDFViewer

        cls._viewer_cls = PDFViewer

    def setUp(self):
        self.doc = _make_text_doc()
        self.viewer = self._viewer_cls()
        self.viewer.resize(800, 600)
        self.viewer.show()
        self.viewer.load_pdf(self.doc, 0, fit_view=False)
        self.viewer.mode = "text_select"
        self._app.processEvents()

    def tearDown(self):
        # 保留中の削除イベント（ポップアップの遅延削除など）を先に処理してから
        # ビューアと文書を閉じる。処理せずに閉じると次のテストの processEvents で
        # 残っていたイベントが走り、offscreen 環境でクラッシュすることがある。
        self.viewer.clear_text_selection()
        self._app.processEvents()
        self.viewer.close()
        self._app.processEvents()
        # MainWindow を使ったテストでは win.close() が同じ文書を閉じるため確認してから閉じる。
        if not self.doc.is_closed:
            self.doc.close()

    def _viewport_point(self, pdf_x, pdf_y):
        """PDF座標（raw）→ ビューポート座標に変換する。"""
        from PyQt6.QtCore import QPointF

        sx, sy = self.viewer.pdf_to_scene(fitz.Point(pdf_x, pdf_y))
        return self.viewer.mapFromScene(QPointF(sx, sy))

    def _drag(self, start_pdf, end_pdf):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        viewport = self.viewer.viewport()
        QTest.mousePress(
            viewport, Qt.MouseButton.LeftButton, pos=self._viewport_point(*start_pdf)
        )
        QTest.mouseMove(viewport, pos=self._viewport_point(*end_pdf))
        QTest.mouseRelease(
            viewport, Qt.MouseButton.LeftButton, pos=self._viewport_point(*end_pdf)
        )
        self._app.processEvents()

    def test_drag_selects_text_and_shows_popup(self):
        self._drag((60, 70), (180, 110))
        self.assertEqual(self.viewer._text_sel_text, "Hello World")
        self.assertIsNotNone(self.viewer._copy_popup)
        self.assertTrue(self.viewer._copy_popup.isVisible())
        self.assertIsNotNone(self.viewer._text_sel_highlight_group)

    def test_copy_button_copies_and_closes_popup(self):
        from PyQt6.QtTest import QTest
        from PyQt6.QtCore import Qt

        self._drag((60, 70), (180, 110))
        self.assertEqual(self.viewer._text_sel_text, "Hello World")
        popup = self.viewer._copy_popup
        self.assertIsNotNone(popup)
        QTest.mouseClick(popup._button, Qt.MouseButton.LeftButton)
        self._app.processEvents()
        clipboard = self._app.clipboard()
        self.assertEqual(clipboard.text(), "Hello World")
        # ボタンを押したらポップアップは自動で閉じ、選択も解除される。
        self.assertIsNone(self.viewer._copy_popup)
        self.assertEqual(self.viewer._text_sel_text, "")

    def test_drag_over_empty_area_shows_no_popup(self):
        self._drag((400, 700), (500, 750))
        self.assertEqual(self.viewer._text_sel_text, "")
        self.assertIsNone(self.viewer._copy_popup)

    def test_escape_on_popup_closes_selection(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        self._drag((60, 70), (180, 110))
        popup = self.viewer._copy_popup
        self.assertIsNotNone(popup)
        QTest.keyPress(popup, Qt.Key.Key_Escape)
        self._app.processEvents()
        self.assertIsNone(self.viewer._copy_popup)
        self.assertEqual(self.viewer._text_sel_text, "")

    def test_clear_text_selection_removes_highlight(self):
        self._drag((60, 70), (180, 110))
        self.assertIsNotNone(self.viewer._text_sel_highlight_group)
        self.viewer.clear_text_selection()
        self.assertIsNone(self.viewer._copy_popup)
        self.assertIsNone(self.viewer._text_sel_highlight_group)
        self.assertEqual(self.viewer._text_sel_words, [])

    def test_mode_switch_clears_selection(self):
        from ui.main_window import MainWindow

        win = MainWindow()
        win.resize(800, 600)
        win.show()
        win.viewer.load_pdf(self.doc, 0, fit_view=False)
        win.viewer.mode = "text_select"
        self._app.processEvents()
        raw = fitz.Rect(60, 70, 180, 110)
        # 直接確定処理を呼んで選択状態を作る。
        from PyQt6.QtCore import QRectF

        sx0, sy0 = win.viewer.pdf_to_scene(fitz.Point(60, 70))
        sx1, sy1 = win.viewer.pdf_to_scene(fitz.Point(180, 110))
        win.viewer._finish_text_selection(raw, QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0))
        self.assertEqual(win.viewer._text_sel_text, "Hello World")
        win.set_mode("view")
        self.assertIsNone(win.viewer._copy_popup)
        self.assertEqual(win.viewer._text_sel_text, "")
        win.close()


if __name__ == "__main__":
    unittest.main()
