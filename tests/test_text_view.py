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

from core.text_tools import (
    extract_page_spans,
    extract_page_texts,
    fit_fontsize_to_width,
    join_fragments,
    join_span_texts,
)


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
            def __init__(self, page_texts, parent=None, **kwargs):
                captured["texts"] = page_texts
                captured["parent"] = parent
                captured["kwargs"] = kwargs

            def exec(self):
                return 0

        with mock.patch("ui.text_data_dialog.TextDataDialog", FakeDialog):
            win.open_text_data_dialog()

        self.assertEqual(len(captured["texts"]), 1)
        self.assertIn("HELLO_PAGE", captured["texts"][0])
        # 配置表示用のスパン・ページ矩形・各種ハンドラも渡っていること
        kwargs = captured["kwargs"]
        self.assertEqual(len(kwargs["page_spans"]), 1)
        self.assertTrue(
            any(sp["text"] == "HELLO_PAGE" for sp in kwargs["page_spans"][0])
        )
        self.assertEqual(len(kwargs["page_rects"]), 1)
        self.assertIs(kwargs["session_state"], win._text_data_session)
        self.assertTrue(callable(kwargs["apply_handler"]))
        self.assertTrue(callable(kwargs["refresh_handler"]))
        self.assertTrue(callable(kwargs["image_provider"]))

    def test_handler_without_document_shows_guidance(self):
        win = self.window
        with mock.patch("ui.main_window.QMessageBox.information") as info:
            win.open_text_data_dialog()
        self.assertTrue(info.called)


if __name__ == "__main__":
    unittest.main()



def _make_jp_doc():
    """日本語フォントで、1ページに断片化した文＋無関係な文字を入れた文書を作る。"""
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    page.insert_text((20, 40), "これは", fontsize=12, fontname="japan")
    page.insert_text((20, 70), "テストです", fontsize=12, fontname="japan")
    page.insert_text((20, 200), "関係ない文字", fontsize=12, fontname="japan")
    return doc


def _make_jp_pdf_bytes():
    doc = _make_jp_doc()
    try:
        return doc.tobytes()
    finally:
        doc.close()


class TestJoinSpanTexts(unittest.TestCase):
    def test_japanese_spans_join_without_space(self):
        self.assertEqual(
            join_span_texts(["これは", "テストです"]), "これはテストです"
        )

    def test_ascii_spans_join_with_space(self):
        self.assertEqual(join_span_texts(["Hello", "world"]), "Hello world")

    def test_extraction_order_is_respected(self):
        self.assertEqual(
            join_span_texts(["テストです", "これは"]), "テストですこれは"
        )

    def test_empty_and_blank_spans(self):
        self.assertEqual(join_span_texts([]), "")
        self.assertEqual(join_span_texts(["", "  "]), "")


class TestFitFontsizeToWidth(unittest.TestCase):
    def test_short_text_keeps_base_size(self):
        self.assertEqual(fit_fontsize_to_width("短い", 12.0, 500.0), 12.0)

    def test_long_text_shrinks_to_fit(self):
        size = fit_fontsize_to_width("これはテストです" * 10, 12.0, 100.0)
        self.assertLess(size, 12.0)
        self.assertGreaterEqual(size, 5.0)

    def test_invalid_inputs_fall_back_to_base(self):
        self.assertEqual(fit_fontsize_to_width("", 12.0, 100.0), 12.0)
        self.assertEqual(fit_fontsize_to_width("abc", 12.0, 0.0), 12.0)


class TestExtractPageSpans(unittest.TestCase):
    def test_positions_sizes_and_text(self):
        doc = _make_jp_doc()
        self.addCleanup(doc.close)
        spans = extract_page_spans(doc)
        self.assertEqual(len(spans), 1)
        page_spans = spans[0]
        texts = [sp["text"] for sp in page_spans]
        self.assertEqual(texts, ["これは", "テストです", "関係ない文字"])
        first = page_spans[0]
        self.assertAlmostEqual(first["fontsize"], 12.0, places=1)
        # 表示座標の矩形と基線原点が、挿入位置 (20, 40) に対応していること
        self.assertAlmostEqual(first["rect"][0], 20.0, places=1)
        self.assertAlmostEqual(first["origin"][0], 20.0, places=1)
        self.assertAlmostEqual(first["origin"][1], 40.0, places=1)
        # 書き戻し用の生座標も入っていること
        self.assertEqual(len(first["raw_rect"]), 4)
        self.assertEqual(len(first["pos"]), 2)
        self.assertEqual(len(first["baseline_pos"]), 2)
        self.assertEqual(first["rotation"], 0)

    def test_redaction_is_reflected_and_source_untouched(self):
        doc = _make_doc(["KEEP_ME", "SECRET_WORD"])
        self.addCleanup(doc.close)
        annotations = {
            1: [{"type": "text_redact", "rect": (10.0, 20.0, 200.0, 60.0)}]
        }
        spans = extract_page_spans(doc, annotations)
        self.assertEqual(len(spans), 2)
        self.assertEqual([sp["text"] for sp in spans[0]], ["KEEP_ME"])
        self.assertNotIn("SECRET_WORD", [sp["text"] for sp in spans[1]])
        # 呼び出し側の文書は変更されていない
        self.assertIn("SECRET_WORD", doc[1].get_text())

    def test_empty_and_none_document(self):
        doc = fitz.open()
        self.addCleanup(doc.close)
        self.assertEqual(extract_page_spans(doc), [])
        self.assertEqual(extract_page_spans(None), [])


def _fake_span(text, x, y, fontsize=12.0):
    return {
        "text": text,
        "rect": (x, y - fontsize, x + 40.0, y + 2.4),
        "raw_rect": (x, y - fontsize, x + 40.0, y + 2.4),
        "origin": (x, y),
        "fontsize": fontsize,
        "color": (0.0, 0.0, 0.0),
        "pos": (x, y - fontsize * 0.88),
        "baseline_pos": (x, y),
        "rotation": 0,
    }


class TestTextDataDialogRework(_QtTestBase):
    def _dialog(self, session=None, **kwargs):
        from ui.text_data_dialog import TextDataDialog

        spans = [[_fake_span("これは", 20, 40), _fake_span("テストです", 20, 70)]]
        return TextDataDialog(
            ["これは\nテストです"],
            page_spans=spans,
            page_rects=[(0.0, 0.0, 400.0, 400.0)],
            session_state=session if session is not None else {},
            **kwargs,
        )

    def test_mode_switch_and_layout_join(self):
        dlg = self._dialog()
        try:
            self.assertEqual(dlg.tabs.currentIndex(), 0)  # 配置表示が既定
            dlg.page_combo.setCurrentIndex(1)
            dlg.layout_canvas.set_selected({0, 1})
            dlg.btn_layout_join.click()
            self.assertEqual(dlg.join_preview.toPlainText(), "これはテストです")
            dlg.tabs.setCurrentIndex(1)
            self.assertEqual(dlg.tabs.currentIndex(), 1)
            self.assertIn("これは", dlg.text_edit.toPlainText())
            dlg.tabs.setCurrentIndex(0)
            self.assertEqual(dlg.tabs.currentIndex(), 0)
        finally:
            dlg.close()

    def test_canvas_builds_on_textless_page(self):
        from ui.text_data_dialog import TextDataDialog

        dlg = TextDataDialog(
            [""], page_spans=[[]], page_rects=[(0.0, 0.0, 300.0, 300.0)]
        )
        try:
            dlg.page_combo.setCurrentIndex(1)
            dlg.layout_canvas.set_selected(set())
            dlg.btn_layout_join.click()
            self.assertEqual(dlg.join_preview.toPlainText(), "")
            self.assertIn("選んでください", dlg.lbl_status.text())
        finally:
            dlg.close()

    def test_text_edit_persists_across_reopen(self):
        from ui.text_data_dialog import TextDataDialog

        session = {}
        dlg = TextDataDialog(["元の文章", "2ページ目"], session_state=session)
        try:
            dlg.page_combo.setCurrentIndex(1)
            dlg.text_edit.setPlainText("直した文章")
        finally:
            dlg.close()

        dlg2 = TextDataDialog(["元の文章", "2ページ目"], session_state=session)
        try:
            dlg2.page_combo.setCurrentIndex(1)
            self.assertEqual(dlg2.text_edit.toPlainText(), "直した文章")
        finally:
            dlg2.close()

    def test_join_preview_persists_across_reopen(self):
        session = {}
        dlg = self._dialog(session=session)
        try:
            dlg.page_combo.setCurrentIndex(1)
            dlg.layout_canvas.set_selected({0, 1})
            dlg.btn_layout_join.click()
            self.assertEqual(session["join"][0], "これはテストです")
        finally:
            dlg.close()

        dlg2 = self._dialog(session=session)
        try:
            dlg2.page_combo.setCurrentIndex(1)
            self.assertEqual(dlg2.join_preview.toPlainText(), "これはテストです")
        finally:
            dlg2.close()

    def test_apply_flow_with_fake_handlers(self):
        session = {}
        calls = {}

        def apply_handler(page_idx, spans, joined):
            calls["args"] = (page_idx, list(spans), joined)
            return True

        joined_span = _fake_span("これはテストです", 20, 40)

        def refresh_handler():
            return (["これはテストです"], [[joined_span]])

        dlg = self._dialog(
            session=session,
            apply_handler=apply_handler,
            refresh_handler=refresh_handler,
        )
        try:
            dlg.page_combo.setCurrentIndex(1)
            dlg.layout_canvas.set_selected({0, 1})
            dlg.btn_layout_join.click()
            dlg.btn_apply.click()
            page_idx, spans, joined = calls["args"]
            self.assertEqual(page_idx, 0)
            self.assertEqual(len(spans), 2)
            self.assertEqual(joined, "これはテストです")
            # 反映後は選択が解除され、最新データで表示し直される
            self.assertEqual(dlg.layout_canvas.selected_indices(), [])
            self.assertIn("反映しました", dlg.lbl_status.text())
            self.assertNotIn(0, session["join"])
        finally:
            dlg.close()


class TestApplyTextReplacement(_WindowTestBase):
    def _open_fragments(self):
        path = Path(self._tmp.name) / "frag.pdf"
        path.write_bytes(_make_jp_pdf_bytes())
        self._open(str(path))

    def test_apply_joins_fragments_and_undo_restores(self):
        win = self.window
        self._open_fragments()
        viewer = win.viewer
        self.assertFalse(viewer.is_modified)

        spans = extract_page_spans(viewer.current_pdf, viewer.page_annotations)
        frag = [
            sp for sp in spans[0] if sp["text"] in ("これは", "テストです")
        ]
        self.assertEqual(len(frag), 2)
        joined = join_span_texts([sp["text"] for sp in frag])
        self.assertEqual(joined, "これはテストです")

        self.assertTrue(win._apply_text_data_replacement(0, frag, joined))
        self.assertTrue(viewer.is_modified)

        texts_after = [
            sp["text"]
            for sp in extract_page_spans(
                viewer.current_pdf, viewer.page_annotations
            )[0]
        ]
        # つなげた文が1つのスパンとして入り、断片は残らない
        self.assertIn("これはテストです", texts_after)
        self.assertNotIn("これは", texts_after)
        self.assertNotIn("テストです", texts_after)
        # 断片の外の文字はそのまま
        self.assertIn("関係ない文字", texts_after)

        viewer.history.undo(viewer)
        texts_undo = [
            sp["text"]
            for sp in extract_page_spans(
                viewer.current_pdf, viewer.page_annotations
            )[0]
        ]
        self.assertIn("これは", texts_undo)
        self.assertIn("テストです", texts_undo)
        self.assertNotIn("これはテストです", texts_undo)
        self.assertIn("関係ない文字", texts_undo)
        self.assertFalse(viewer.is_modified)

    def test_session_cleared_when_document_changes(self):
        win = self.window
        src1 = self._write_pdf("a.pdf", ["AAA"])
        self._open(src1)
        win._text_data_session["pages"][0] = "作業中テキスト"
        win._text_data_session["join"][0] = "作業中プレビュー"

        src2 = self._write_pdf("b.pdf", ["BBB"])
        self._open(src2)
        self.assertEqual(win._text_data_session["pages"], {})
        self.assertEqual(win._text_data_session["join"], {})
        self.assertIsNone(win._text_data_session["all"])


class TestLargeDialogSize(_QtTestBase):
    def test_helper_is_large_but_within_screen(self):
        from PyQt6.QtGui import QGuiApplication

        from ui.text_data_dialog import large_dialog_size

        avail = QGuiApplication.primaryScreen().availableGeometry()
        size = large_dialog_size()
        self.assertLessEqual(size.width(), avail.width())
        self.assertLessEqual(size.height(), avail.height())
        self.assertGreaterEqual(size.width(), int(avail.width() * 0.7))
        self.assertGreaterEqual(size.height(), int(avail.height() * 0.7))

    def test_dialog_opens_large(self):
        from PyQt6.QtGui import QGuiApplication

        from ui.text_data_dialog import TextDataDialog

        avail = QGuiApplication.primaryScreen().availableGeometry()
        dlg = TextDataDialog(["本文"])
        try:
            self.assertGreaterEqual(dlg.width(), int(avail.width() * 0.7))
            self.assertGreaterEqual(dlg.height(), int(avail.height() * 0.7))
            self.assertLessEqual(dlg.width(), avail.width())
            self.assertLessEqual(dlg.height(), avail.height())
        finally:
            dlg.close()


def _send_mouse(canvas, kind, pos, button, buttons):
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    event = QMouseEvent(
        kind,
        QPointF(pos),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    if kind == QEvent.Type.MouseButtonPress:
        canvas.mousePressEvent(event)
    elif kind == QEvent.Type.MouseMove:
        canvas.mouseMoveEvent(event)
    else:
        canvas.mouseReleaseEvent(event)


class TestLayoutCanvasZoom(_QtTestBase):
    def _canvas(self, spans=None):
        from ui.text_data_dialog import LayoutCanvas

        canvas = LayoutCanvas()
        canvas.resize(400, 300)
        if spans is None:
            spans = [_fake_span("これは", 20, 40), _fake_span("テストです", 20, 70)]
        canvas.set_page((0.0, 0.0, 400.0, 400.0), spans)
        return canvas

    def _click(self, canvas, point):
        from PyQt6.QtCore import QEvent, Qt

        _send_mouse(
            canvas, QEvent.Type.MouseButtonPress, point,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        )
        _send_mouse(
            canvas, QEvent.Type.MouseButtonRelease, point,
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        )

    def _span_center(self, canvas, idx):
        area, scale = canvas._page_area()
        rect = canvas._span_widget_rect(canvas._spans[idx], area, scale)
        return rect.center()

    def test_zoom_changes_effective_scale_and_clamps(self):
        from PyQt6.QtCore import QPointF

        canvas = self._canvas()
        base = canvas.effective_scale()
        self.assertGreater(base, 0)
        canvas.set_zoom(2.0)
        self.assertEqual(canvas.zoom_factor(), 2.0)
        self.assertAlmostEqual(canvas.effective_scale(), base * 2.0)
        canvas.set_zoom(100.0)
        self.assertEqual(canvas.zoom_factor(), canvas.MAX_ZOOM)
        canvas.set_zoom(0.01)
        self.assertEqual(canvas.zoom_factor(), canvas.MIN_ZOOM)
        canvas.zoom_fit()
        self.assertEqual(canvas.zoom_factor(), 1.0)
        self.assertEqual(canvas.pan_offset(), QPointF(0.0, 0.0))

    def test_click_selects_span_at_fit(self):
        canvas = self._canvas()
        self._click(canvas, self._span_center(canvas, 1))
        self.assertEqual(canvas.selected_indices(), [1])
        self._click(canvas, self._span_center(canvas, 1))
        self.assertEqual(canvas.selected_indices(), [])

    def test_hit_testing_at_zoomed_and_panned_state(self):
        from PyQt6.QtCore import QPointF

        canvas = self._canvas(spans=[_fake_span("中央の文字", 180, 200)])
        canvas.set_zoom(2.0)
        canvas.set_pan_offset(QPointF(35.0, 25.0))
        center = self._span_center(canvas, 0)
        self.assertTrue(canvas.rect().contains(center.toPoint()))
        self._click(canvas, center)
        self.assertEqual(canvas.selected_indices(), [0])

    def test_drag_pans_instead_of_selecting(self):
        from PyQt6.QtCore import QEvent, QPointF, Qt

        canvas = self._canvas(spans=[_fake_span("中央の文字", 180, 200)])
        canvas.set_zoom(2.0)
        start = self._span_center(canvas, 0)
        before = canvas.pan_offset()
        _send_mouse(
            canvas, QEvent.Type.MouseButtonPress, start,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        )
        moved = start + QPointF(60.0, 0.0)
        _send_mouse(
            canvas, QEvent.Type.MouseMove, moved,
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        )
        _send_mouse(
            canvas, QEvent.Type.MouseButtonRelease, moved,
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        )
        self.assertEqual(canvas.selected_indices(), [])
        self.assertAlmostEqual(canvas.pan_offset().x(), before.x() + 60.0, places=0)
        self.assertAlmostEqual(canvas.pan_offset().y(), before.y(), places=0)


class TestUnderlayZoomBucket(_QtTestBase):
    def test_provider_receives_bucketed_zoom(self):
        from ui.text_data_dialog import TextDataDialog

        calls = []

        def provider(page_idx, zoom=2.0):
            calls.append((page_idx, zoom))
            return None

        dlg = TextDataDialog(
            ["本文"],
            page_spans=[[_fake_span("本文", 180, 200)]],
            page_rects=[(0.0, 0.0, 400.0, 400.0)],
            image_provider=provider,
        )
        try:
            dlg.page_combo.setCurrentIndex(1)
            dlg.layout_canvas.resize(800, 600)
            dlg.chk_underlay.setChecked(True)
            # 全体表示（実効倍率1.5）では従来どおり2倍で要求される
            self.assertIn((0, 2.0), calls)
            calls.clear()
            # 4倍ズームで実効倍率が6になるので、上限バケット6で要求される
            dlg.layout_canvas.set_zoom(4.0)
            dlg._refresh_underlay()
            self.assertEqual(calls, [(0, 6.0)])
            # 同じバケットではキャッシュを使い、取り直さない
            dlg._refresh_underlay()
            self.assertEqual(calls, [(0, 6.0)])
        finally:
            dlg.close()
