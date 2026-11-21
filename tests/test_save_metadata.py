# -*- coding: utf-8 -*-
"""save_safely で元のメタデータとしおりが保持されることのテスト。

- 保存のたびに title・author・subject・keywords・creator・
  creationDate・modDate が消える問題の回帰テスト。
- しおり（TOC）も insert_pdf では引き継がれないため、保存後も残ること。
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


class TestSaveSafelyKeepsMetadata(unittest.TestCase):
    def test_save_safely_keeps_original_metadata(self):
        """保存後もタイトル・作成者・作成日時などが消えないこと。"""
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

    def test_save_safely_keeps_toc(self):
        """保存後もしおり（TOC）が消えないこと。"""
        from core.pdf_engine import PDFDocumentManager

        doc = _make_doc_with_text(pages=2)
        doc.set_toc([[1, "表紙", 1], [1, "明細", 2]])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "out.pdf")
                _, err = PDFDocumentManager.save_safely(doc, {}, out, None)
                self.assertIsNone(err, f"save failed: {err}")
                saved = fitz.open(out)
                try:
                    toc = [list(entry) for entry in saved.get_toc()]
                    self.assertEqual(toc, [[1, "表紙", 1], [1, "明細", 2]])
                finally:
                    saved.close()
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main()
