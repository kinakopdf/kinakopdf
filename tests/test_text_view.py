# -*- coding: utf-8 -*-
"""ベータ版「文字データ」表示・手直し機能に対するテスト。

- join_fragments: 改行でバラバラになった文を1行につなげる純粋関数。
  日本語はスペースなし、半角英数字同士の境目だけスペース1つ、
  remove_spaces=True で半角・全角スペースを除去すること。
- extract_page_texts: ページごとの埋め込み文字が取れること、
  墨消し注釈がある場合は保存後と同じく墨消し済みの文字になること、
  元の文書が変更されないこと。
- TextDataDialog: ページ切り替えと「くっつける」（選択なし＝全体）のスモーク。
- MainWindow 配線: 文書を開くまではボタン無効、開くと有効になり、
  ダイアログへページ文字が渡ること。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz

from core.text_tools import extract_page_texts, join_fragments


def _make_doc(texts):
    doc = fitz.open()
    for t in texts:
        page = doc.new_page(width=300, height=300)
        if t:
            page.insert_text((20, 40), t, fontsize=12)
    return doc


def _make_pdf_bytes(texts):
    doc = _make_doc(texts)
    try:
        return doc.tobytes()
    finally:
        doc.close()


class TestJoinFragments(unittest.TestCase):
    def test_japanese_lines_join_without_space(self):
        self.assertEqual(join_fragments("これは\nテストです"), "これはテストです")

    def test_ascii_word_lines_join_with_single_space(self):
        self.assertEqual(join_fragments("Hello\nworld"), "Hello world")
        self.assertEqual(join_fragments("abc\n123"), "abc 123")

    def test_mixed_boundaries_join_without_space(self):
        # 行末が日本語 → 次が英数字でもスペースなし
        self.assertEqual(join_fragments("合計は\n100円です"), "合計は100円です")
        # 次の行頭が日本語 → 行末が英数字でもスペースなし
        self.assertEqual(join_fragments("Total\n円です"), "Total円です")

    def test_remove_spaces_removes_half_and_full_width(self):
        self.assertEqual(
            join_fragments("これは テスト　です", remove_spaces=True),
            "これはテストです",
        )
        # 連結時に挿入されたスペースも取り除かれる
        self.assertEqual(
            join_fragments("Hello\nworld", remove_spaces=True), "Helloworld"
        )

    def test_crlf_and_cr_input(self):
        self.assertEqual(join_fragments("これは\r\nテストです"), "これはテストです")
        self.assertEqual(join_fragments("これは\rテストです"), "これはテストです")

    def test_empty_input(self):
        self.assertEqual(join_fragments(""), "")
        self.assertEqual(join_fragments(None), "")
        self.assertEqual(join_fragments("\n\n  \n"), "")

    def test_surrounding_whitespace_and_blank_lines(self):
        self.assertEqual(
            join_fragments("  これは  \n\n  テストです \n"), "これはテストです"
        )


class TestExtractPageTexts(unittest.TestCase):
    def test_texts_on_right_pages(self):
        doc = _make_doc(["PAGE_ONE_TEXT", "", "PAGE_THREE_TEXT"])
        self.addCleanup(doc.close)
        texts = extract_page_texts(doc)
        self.assertEqual(len(texts), 3)
        self.assertIn("PAGE_ONE_TEXT", texts[0])
        self.assertEqual(texts[1], "")
        self.assertIn("PAGE_THREE_TEXT", texts[2])

    def test_redaction_is_reflected_and_source_untouched(self):
        doc = _make_doc(["KEEP_ME", "SECRET_WORD"])
        self.addCleanup(doc.close)
        annotations = {
            1: [{"type": "text_redact", "rect": (10.0, 20.0, 200.0, 60.0)}]
        }
        texts = extract_page_texts(doc, annotations)
        self.assertEqual(len(texts), 2)
        self.assertIn("KEEP_ME", texts[0])
        # 墨消しした文字は、保存したファイルと同じく残らない
        self.assertNotIn("SECRET_WORD", texts[1])
        # 呼び出し側の文書は変更されていない
        self.assertIn("SECRET_WORD", doc[1].get_text())

    def test_empty_annotations_behave_like_none(self):
        doc = _make_doc(["SOME_TEXT"])
        self.addCleanup(doc.close)
        self.assertIn("SOME_TEXT", extract_page_texts(doc, {})[0])
        self.assertIn("SOME_TEXT", extract_page_texts(doc, {0: []})[0])

    def test_empty_and_none_document(self):
        doc = fitz.open()
        self.addCleanup(doc.close)
        self.assertEqual(extract_page_texts(doc), [])
        self.assertEqual(extract_page_texts(None), [])


class _QtTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])


class TestTextDataDialog(_QtTestBase):
    def test_all_pages_mode_shows_separators(self):
        from ui.text_data_dialog import TextDataDialog

        dlg = TextDataDialog(["1ページ目の本文", "2ページ目の本文"])
        try:
            shown = dlg.text_edit.toPlainText()
            self.assertIn("―― 1ページ目 ――", shown)
            self.assertIn("1ページ目の本文", shown)
            self.assertIn("―― 2ページ目 ――", shown)
            self.assertIn("2ページ目の本文", shown)
        finally:
            dlg.close()

    def test_switch_page_and_join_all(self):
        from ui.text_data_dialog import TextDataDialog

        dlg = TextDataDialog(["これは\nテストです", "2ページ目の\n文章です"])
        try:
            dlg.page_combo.setCurrentIndex(1)
            self.assertEqual(dlg.text_edit.toPlainText(), "これは\nテストです")
            dlg.btn_join.click()
            result = dlg.text_edit.toPlainText()
            self.assertNotIn("\n", result)
            self.assertEqual(result, "これはテストです")
        finally:
            dlg.close()

    def test_empty_page_shows_no_text_message(self):
        from ui.text_data_dialog import TextDataDialog

        dlg = TextDataDialog(["", "本文あり"])
        try:
            dlg.page_combo.setCurrentIndex(1)
            self.assertIn(
                "（このページに文字データはありません。画像だけのPDFです）",
                dlg.text_edit.toPlainText(),
            )
            dlg.page_combo.setCurrentIndex(2)
            self.assertEqual(dlg.text_edit.toPlainText(), "本文あり")
        finally:
            dlg.close()

    def test_join_with_remove_spaces_checkbox(self):
        from ui.text_data_dialog import TextDataDialog

        dlg = TextDataDialog(["これは テスト　です"])
        try:
            dlg.page_combo.setCurrentIndex(1)
            dlg.chk_remove_spaces.setChecked(True)
            dlg.btn_join.click()
            self.assertEqual(dlg.text_edit.toPlainText(), "これはテストです")
        finally:
            dlg.close()


class _WindowTestBase(_QtTestBase):
    def setUp(self):
        from ui.main_window import MainWindow

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.window = MainWindow()
        self.window.show()
        self._app.processEvents()
        self.addCleanup(self._cleanup_window)

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
            win.viewer.history.mark_saved()
        except Exception:
            pass
        try:
            win.close()
        except Exception:
            pass
        self.window = None

    def _write_pdf(self, name, texts):
        path = Path(self._tmp.name) / name
        path.write_bytes(_make_pdf_bytes(texts))
        return str(path)

    def _open(self, path):
        self.window.open_file_from_path(path)
        self._app.processEvents()


class TestMainWindowTextData(_WindowTestBase):
    def test_button_state_and_dialog_receives_page_texts(self):
        win = self.window
        # 文書を開く前は無効
        self.assertFalse(win.act_text_data.isEnabled())

        src = self._write_pdf("src.pdf", ["HELLO_PAGE"])
        self._open(src)
        self.assertTrue(win.act_text_data.isEnabled())

        captured = {}

        class FakeDialog:
            def __init__(self, page_texts, parent=None):
                captured["texts"] = page_texts
                captured["parent"] = parent

            def exec(self):
                return 0

        with mock.patch("ui.text_data_dialog.TextDataDialog", FakeDialog):
            win.open_text_data_dialog()

        self.assertEqual(len(captured["texts"]), 1)
        self.assertIn("HELLO_PAGE", captured["texts"][0])

    def test_handler_without_document_shows_guidance(self):
        win = self.window
        with mock.patch("ui.main_window.QMessageBox.information") as info:
            win.open_text_data_dialog()
        self.assertTrue(info.called)


if __name__ == "__main__":
    unittest.main()
