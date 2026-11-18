# -*- coding: utf-8 -*-
"""監査レポート（4.docx / Gemini .tex）対応の修正に対するテスト。

- 監査 #1: 試作機能（スタンプ・フォーム・しおり・ヘッダー等）を
  ReplacePagesContentCommand / ReplaceTocCommand 経由で Undo 可能にしたこと
- 監査 #2: save_safely で元のメタデータ（タイトル・作成者など）が保持されること
- 監査 #5: テキストドラッグ移動中に Escape を押すとドラッグがキャンセルされること
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent if (_THIS_DIR.name == "tests" or not (_THIS_DIR / "core").is_dir()) else _THIS_DIR
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import fitz


def _make_doc_with_text(text="HELLO", pages=1):
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=400, height=400)
        page.insert_text(fitz.Point(20, 40), text, fontsize=12)
    return doc


class TestAuditMetadataPreserved(unittest.TestCase):
    def test_save_safely_keeps_original_metadata(self):
        """監査 #2: 保存後もタイトル・作成者・作成日時などが消えないこと。"""
        from core.pdf_engine import PDFDocumentManager

        doc = _make_doc_with_text()
        doc.set_metadata({
            "title": "請求書 2026年11月分",
            "author": "経理部",
            "creator": "きなこPDF",
            "subject": "テスト用",
            "keywords": "請求書, テスト",
            "creationDate": "D:20260101090000+09'00'",
            "modDate": "D:20261118090000+09'00'",
        })
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "out.pdf")
                _, err = PDFDocumentManager.save_safely(doc, {}, out, None)
                self.assertIsNone(err, f"save failed: {err}")
                saved = fitz.open(out)
                try:
                    meta = saved.metadata
                    self.assertEqual(meta.get("title"), "請求書 2026年11月分")
                    self.assertEqual(meta.get("author"), "経理部")
                    self.assertEqual(meta.get("creator"), "きなこPDF")
                    self.assertEqual(meta.get("subject"), "テスト用")
                    self.assertEqual(meta.get("keywords"), "請求書, テスト")
                    self.assertTrue(meta.get("creationDate", "").startswith("D:20260101"))
                    self.assertTrue(meta.get("producer"))
                finally:
                    saved.close()
        finally:
            doc.close()


class TestAuditReplaceTocCommand(unittest.TestCase):
    def test_toc_command_undo_redo(self):
        """監査 #1（しおり）: ReplaceTocCommand の Undo/Redo でしおりが戻ること。"""
        from core.commands import ReplaceTocCommand

        doc = _make_doc_with_text(pages=3)
        try:
            doc.set_toc([[1, "表紙", 1]])

            class _StubViewer:
                current_pdf = doc

            viewer = _StubViewer()
            old_toc = [list(e) for e in doc.get_toc()]
            new_toc = [[1, "表紙", 1], [1, "明細", 2], [2, "内訳", 3]]
            cmd = ReplaceTocCommand(old_toc, new_toc)
            cmd.redo(viewer)
            self.assertEqual([list(e) for e in doc.get_toc()], new_toc)
            cmd.undo(viewer)
            self.assertEqual([list(e) for e in doc.get_toc()], old_toc)
            cmd.redo(viewer)
            self.assertEqual([list(e) for e in doc.get_toc()], new_toc)
        finally:
            doc.close()


class TestAuditTrialUndoFlow(unittest.TestCase):
    def _make_window(self):
        from PyQt6.QtWidgets import QApplication
        from ui.main_window import MainWindow

        # QApplication の参照をテストケースに保持しないと、ヘルパーから
        # 戻った時点で回収されてウィンドウごと消えてしまう。
        self._qt_app = QApplication.instance() or QApplication([])
        return MainWindow()

    def test_stamp_via_command_undo_redo(self):
        """監査 #1（スタンプ）: ヘルパー経由のスタンプが Undo/Redo できること。"""
        from core.trial_tools import apply_stamp_to_doc

        win = self._make_window()
        try:
            doc = _make_doc_with_text()
            win.viewer.load_pdf(doc, 0, fit_view=False)
            win._trial_apply_to_pages(
                [0],
                lambda tmp: apply_stamp_to_doc(
                    tmp, 1, "stamp", fitz.Rect(20, 60, 80, 90), text="済"),
            )
            self.assertIn("済", win.viewer.current_pdf[0].get_text("text"))
            win.viewer.history.undo(win.viewer)
            self.assertNotIn("済", win.viewer.current_pdf[0].get_text("text"))
            win.viewer.history.redo(win.viewer)
            self.assertIn("済", win.viewer.current_pdf[0].get_text("text"))
        finally:
            # 未保存の編集があると closeEvent がモーダル確認を出して止まるため、
            # 閉じる前に保存済み扱いにしておく。
            win.viewer.history.mark_saved()
            win.close()

    def test_later_command_undo_keeps_stamp(self):
        """監査 #1の核心: スタンプ後に別の置き換えコマンドを作って Undo しても
        スタンプが消えないこと（バックアップにスタンプが含まれているため）。"""
        from core.commands import ReplacePagesContentCommand
        from core.trial_tools import apply_stamp_to_doc

        win = self._make_window()
        try:
            doc = _make_doc_with_text()
            win.viewer.load_pdf(doc, 0, fit_view=False)
            win._trial_apply_to_pages(
                [0],
                lambda tmp: apply_stamp_to_doc(
                    tmp, 1, "stamp", fitz.Rect(20, 60, 80, 90), text="済"),
            )
            self.assertIn("済", win.viewer.current_pdf[0].get_text("text"))

            # スタンプ後の文書から「OCR結果風」の後継ページを作る
            after_doc = fitz.open()
            after_doc.insert_pdf(win.viewer.current_pdf, from_page=0, to_page=0)
            after_doc[0].insert_text(fitz.Point(20, 100), "HELLO2", fontsize=12)
            cmd = ReplacePagesContentCommand(
                doc=win.viewer.current_pdf,
                page_indices=[0],
                new_pages_doc=after_doc,
                page_annotations=win.viewer.page_annotations,
                prev_page_idx=0,
                take_ownership=True,
            )
            cmd.redo(win.viewer)
            win.viewer.history.push(cmd)
            self.assertIn("HELLO2", win.viewer.current_pdf[0].get_text("text"))
            self.assertIn("済", win.viewer.current_pdf[0].get_text("text"))

            win.viewer.history.undo(win.viewer)
            text = win.viewer.current_pdf[0].get_text("text")
            self.assertNotIn("HELLO2", text)
            self.assertIn("済", text)
        finally:
            # 未保存の編集があると closeEvent がモーダル確認を出して止まるため、
            # 閉じる前に保存済み扱いにしておく。
            win.viewer.history.mark_saved()
            win.close()

    def test_form_fill_via_command_undo_redo(self):
        """監査 #1（フォーム）: FillFormFieldsCommand で入力が Undo/Redo でき、
        欄名が変わらないこと（ページ置き換えだと「氏名 [17]」のように変わる）。"""
        from core.commands import FillFormFieldsCommand

        win = self._make_window()
        try:
            doc = fitz.open()
            page = doc.new_page(width=400, height=400)
            widget = fitz.Widget()
            widget.field_name = "氏名"
            widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            widget.rect = fitz.Rect(20, 20, 200, 40)
            widget.field_value = ""
            page.add_widget(widget)

            win.viewer.load_pdf(doc, 0, fit_view=False)

            def _field_value():
                for w in win.viewer.current_pdf[0].widgets() or []:
                    if w.field_name == "氏名":
                        return w.field_value
                return None

            cmd = FillFormFieldsCommand(win.viewer.current_pdf, {"氏名": "山田"})
            cmd.redo(win.viewer)
            win.viewer.history.push(cmd)
            self.assertEqual(_field_value(), "山田")
            names = [w.field_name for w in win.viewer.current_pdf[0].widgets() or []]
            self.assertIn("氏名", names)
            win.viewer.history.undo(win.viewer)
            self.assertEqual(_field_value(), "")
            win.viewer.history.redo(win.viewer)
            self.assertEqual(_field_value(), "山田")
        finally:
            # 未保存の編集があると closeEvent がモーダル確認を出して止まるため、
            # 閉じる前に保存済み扱いにしておく。
            win.viewer.history.mark_saved()
            win.close()

    def test_escape_cancels_text_drag(self):
        """監査 #5: テキストドラッグ移動中に Escape を押すとドラッグが解除され、
        移動中のアイテムが元の位置に戻ること。"""
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtWidgets import QGraphicsRectItem

        win = self._make_window()
        try:
            doc = _make_doc_with_text()
            win.viewer.load_pdf(doc, 0, fit_view=False)
            win.viewer.page_annotations[win.viewer.current_page_idx] = [
                {"type": "text", "text": "A", "pos": (20, 40),
                 "baseline_pos": (20, 40), "fontsize": 11.0}
            ]
            item = QGraphicsRectItem(0, 0, 50, 20)
            win.viewer.scene.addItem(item)
            start_pos = QPointF(10, 10)
            item.setPos(start_pos)
            win.viewer._dragging_text_ann_idx = 0
            win.viewer._dragging_text_item = item
            win.viewer._text_drag_start_item_pos = start_pos
            win.viewer._text_drag_moved = True
            item.setPos(QPointF(100, 100))

            event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
            win.viewer.keyPressEvent(event)

            self.assertIsNone(win.viewer._dragging_text_ann_idx)
            self.assertIsNone(win.viewer._dragging_text_item)
            self.assertFalse(win.viewer._text_drag_moved)
            self.assertEqual(item.pos(), start_pos)
        finally:
            # 未保存の編集があると closeEvent がモーダル確認を出して止まるため、
            # 閉じる前に保存済み扱いにしておく。
            win.viewer.history.mark_saved()
            win.close()


if __name__ == "__main__":
    unittest.main()
