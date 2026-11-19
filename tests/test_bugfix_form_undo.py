# -*- coding: utf-8 -*-
"""Geminiチャットレビュー #4（フォーム入力のUndo）の修正テスト。

同名フィールドでもウィジェット単位で旧値を復元することと、
redo/undo 後に viewer.notify_pages_updated が呼ばれることを確認する。
"""
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent if (_THIS_DIR.name == "tests" or not (_THIS_DIR / "core").is_dir()) else _THIS_DIR
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import fitz


def make_dup_field_doc():
    """同名フィールド2つが別々の値を持つ文書を作る。

    1つ目は親フィールドの値（"AAA"）、2つ目はウィジェット自身の /V（"BBB"）。
    他のツールで作られた実在PDFで起こり得る形。
    """
    from core.trial_tools import _pdf_string_literal

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    w1 = fitz.Widget()
    w1.field_name = "dup"
    w1.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w1.rect = fitz.Rect(50, 50, 250, 75)
    w1.field_value = "AAA"
    page.add_widget(w1)
    w2 = fitz.Widget()
    w2.field_name = "dup"
    w2.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w2.rect = fitz.Rect(50, 100, 250, 125)
    page.add_widget(w2)
    widgets = list(doc[0].widgets())
    doc.xref_set_key(widgets[1].xref, "V", _pdf_string_literal("BBB"))
    return doc


def widget_values(doc, name="dup"):
    return [w.field_value for w in doc[0].widgets() or []
            if (w.field_name or "") == name]


class _StubSignal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _StubViewer:
    def __init__(self, doc):
        self.current_pdf = doc
        self.current_page_idx = 0
        self.pageSelectionChanged = _StubSignal()
        self.notified = []

    def render_page(self, **_kwargs):
        pass

    def notify_pages_updated(self, page_indices=None):
        self.notified.append(page_indices)


class TestPdfStringLiteral(unittest.TestCase):
    def test_ascii_and_japanese_roundtrip(self):
        from core.trial_tools import _pdf_string_literal

        doc = fitz.open()
        try:
            page = doc.new_page(width=300, height=300)
            w = fitz.Widget()
            w.field_name = "f"
            w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            w.rect = fitz.Rect(20, 20, 200, 40)
            page.add_widget(w)
            xref = list(doc[0].widgets())[0].xref
            doc.xref_set_key(xref, "V", _pdf_string_literal("ABC (x)"))
            self.assertEqual(list(doc[0].widgets())[0].field_value, "ABC (x)")
            doc.xref_set_key(xref, "V", _pdf_string_literal("請求書 あ"))
            self.assertEqual(list(doc[0].widgets())[0].field_value, "請求書 あ")
        finally:
            doc.close()


class TestFormUndoPerWidget(unittest.TestCase):
    def test_snapshot_fill_restore(self):
        """エンジン層: スナップショット→入力→復元で別々の値に戻ること。"""
        from core.trial_tools import (
            fill_form_fields,
            set_form_field_values_by_widget,
            snapshot_form_field_values,
        )

        doc = make_dup_field_doc()
        try:
            self.assertEqual(widget_values(doc), ["AAA", "BBB"])
            snapshot = snapshot_form_field_values(doc, names={"dup"})
            self.assertEqual(len(snapshot), 2)
            self.assertFalse(snapshot[0]["own_value"])
            self.assertTrue(snapshot[1]["own_value"])
            fill_form_fields(doc, {"dup": "XXX"})
            result = set_form_field_values_by_widget(doc, snapshot)
            self.assertEqual(result["filled"], 2)
            self.assertEqual(widget_values(doc), ["AAA", "BBB"])
        finally:
            doc.close()

    def test_command_undo_restores_distinct_values_and_notifies(self):
        """コマンド層: Undo で同名欄が別々の値に戻り、サムネイル更新通知も出ること。"""
        from core.commands import FillFormFieldsCommand

        doc = make_dup_field_doc()
        try:
            viewer = _StubViewer(doc)
            cmd = FillFormFieldsCommand(doc, {"dup": "XXX"})
            cmd.redo(viewer)
            self.assertEqual(widget_values(doc)[0], "XXX")
            self.assertEqual(viewer.notified, [[0]])
            cmd.undo(viewer)
            self.assertEqual(widget_values(doc), ["AAA", "BBB"])
            self.assertEqual(viewer.notified, [[0], [0]])
            cmd.redo(viewer)
            cmd.undo(viewer)
            self.assertEqual(widget_values(doc), ["AAA", "BBB"])
        finally:
            doc.close()

    def test_command_undo_single_field(self):
        """通常の（同名でない）欄でも Undo で空に戻ること（回帰防止）。"""
        from core.commands import FillFormFieldsCommand

        doc = fitz.open()
        try:
            page = doc.new_page(width=400, height=400)
            w = fitz.Widget()
            w.field_name = "氏名"
            w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            w.rect = fitz.Rect(20, 20, 200, 40)
            w.field_value = ""
            page.add_widget(w)
            viewer = _StubViewer(doc)
            cmd = FillFormFieldsCommand(doc, {"氏名": "山田"})
            cmd.redo(viewer)
            self.assertEqual(widget_values(doc, "氏名"), ["山田"])
            cmd.undo(viewer)
            self.assertEqual(widget_values(doc, "氏名"), [""])
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main()
