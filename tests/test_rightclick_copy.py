# -*- coding: utf-8 -*-
"""ベータ版の機能改善（2026-11-26、MIWA 依頼）に対するテスト。

テキスト編集モード（type）の右クリックを「その位置のテキストをコピー」に
変更する。対象は PDF 内の既存文字と、追加したテキスト注釈の両方。
テキストが無い場所の右クリックは従来どおりパン操作に回ること、
文字消去モード（text_erase）の右クリック削除は従来どおりであること。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz


def _make_text_doc():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Hello World test", fontsize=20)
    return doc


class TestRightClickCopy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])
        from ui.pdf_viewer import PDFViewer

        cls._viewer_cls = PDFViewer

    def setUp(self):
        from PyQt6.QtWidgets import QApplication

        self.doc = _make_text_doc()
        self.viewer = self._viewer_cls()
        self.viewer.resize(800, 600)
        self.viewer.show()
        self.viewer.load_pdf(self.doc, 0, fit_view=False)
        self.viewer.mode = "type"
        QApplication.clipboard().setText("")
        self._app.processEvents()

    def tearDown(self):
        self._app.processEvents()
        self.viewer.close()
        self._app.processEvents()
        if not self.doc.is_closed:
            self.doc.close()

    def _viewport_point(self, pdf_x, pdf_y):
        from PyQt6.QtCore import QPointF

        sx, sy = self.viewer.pdf_to_scene(fitz.Point(pdf_x, pdf_y))
        return self.viewer.mapFromScene(QPointF(sx, sy))

    def _right_click(self, pdf_x, pdf_y):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        QTest.mouseClick(
            self.viewer.viewport(),
            Qt.MouseButton.RightButton,
            pos=self._viewport_point(pdf_x, pdf_y),
        )
        self._app.processEvents()

    def _clipboard_text(self):
        from PyQt6.QtWidgets import QApplication

        return QApplication.clipboard().text()

    def _add_text_annotation(self, text, pdf_x, pdf_y):
        from PyQt6.QtCore import QPointF

        sx, sy = self.viewer.pdf_to_scene(fitz.Point(pdf_x, pdf_y))
        pdf_pos, baseline_pos = self.viewer._compute_text_pdf_positions(
            QPointF(sx, sy), 16.0, 0
        )
        annot = {
            "type": "text",
            "text": text,
            "pos": pdf_pos,
            "baseline_pos": baseline_pos,
            "fontsize": 16.0,
            "color": (0.0, 0.0, 0.0),
            "rotation": 0,
            "updated_after": len(self.viewer.current_annotations),
        }
        self.viewer.current_annotations.append(annot)
        self.viewer.render_page(fit_view=False, rebuild_base=False)
        self._app.processEvents()
        return annot

    def test_right_click_copies_existing_pdf_text(self):
        # "Hello World test" の文字上（PDF座標で単語の内側）を右クリック
        self._right_click(90, 95)
        copied = self._clipboard_text()
        self.assertTrue(copied)
        self.assertIn("Hello", copied)

    def test_right_click_copies_added_annotation_without_deleting(self):
        self._add_text_annotation("追加メモ", 200, 200)
        count_before = len(self.viewer.current_annotations)
        self._right_click(205, 195)
        self.assertEqual(self._clipboard_text(), "追加メモ")
        # コピーであって削除ではないこと
        self.assertEqual(len(self.viewer.current_annotations), count_before)

    def test_right_click_on_empty_area_does_not_copy(self):
        self._right_click(500, 700)
        self.assertEqual(self._clipboard_text(), "")

    def test_text_erase_mode_right_click_still_deletes(self):
        self._add_text_annotation("消えるメモ", 200, 200)
        self.viewer.mode = "text_erase"
        count_before = len(self.viewer.current_annotations)
        self._right_click(205, 195)
        self.assertEqual(len(self.viewer.current_annotations), count_before - 1)
        self.assertEqual(self._clipboard_text(), "")


if __name__ == "__main__":
    unittest.main()
