# -*- coding: utf-8 -*-
"""監査レポート（upload.txt / 9件）対応の修正に対するテスト（コピー試作版 feature/text-copy-button 用）。

BUG-01: 回転ページでのテキスト選択座標。レポートの提案
  （単語矩形に view_rect_to_raw_rect を適用）は誤りで、現行の
  cropbox 原点加算が全回転角で正しいことを回帰テストで固定する。
  さらに消去済み単語が選択対象から除外されることを検証する。
- BUG-02: 折りたたみ中の範囲OCR座標変換（scene <-> view）の往復。
- BUG-03: 複数注釈同時削除フォールバック復元の updated_after 往復。
  昇順・`>` が正しいこと（逆順にすると曖昧でない値が壊れる）。
- BUG-04: 見開きリーダーのスタンドアロン Redo 時のページ移動。
- BUG-05: 一括操作コマンドの Undo/Redo 時ページ遷移と空リスト掃除。
- BUG-09: Excel 出力の -0.0 正規化。
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent if (_THIS_DIR.name == "tests" or not (_THIS_DIR / "core").is_dir()) else _THIS_DIR
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import fitz


def _make_text_doc():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Hello World test", fontsize=20)
    page.insert_text((72, 140), "Second line here", fontsize=20)
    return doc


class _FakeSignal:
    def emit(self, *args):
        pass


class _FakeViewer:
    """コマンドの redo/undo に必要な最小限のビューア。"""

    def __init__(self, doc):
        self.current_pdf = doc
        self.current_page_idx = 0
        self.page_annotations = {}
        self.render_calls = []
        self.pageSelectionChanged = _FakeSignal()
        self.notified = []

    def render_page(self, fit_view=False, rebuild_base=False):
        self.render_calls.append((fit_view, rebuild_base))

    def notify_pages_updated(self, page_indices=None):
        self.notified.append(page_indices)


class TestBug01WordRectsRotation(unittest.TestCase):
    """BUG-01（座標変換の提案は誤り）: 単語矩形は全回転角で raw 座標を返すこと。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])
        from ui.pdf_viewer import PDFViewer

        cls._viewer_cls = PDFViewer

    def _viewer_for_rotation(self, rotation):
        doc = _make_text_doc()
        doc[0].set_rotation(rotation)
        viewer = self._viewer_cls()
        viewer.resize(800, 600)
        viewer.show()
        viewer.load_pdf(doc, 0, fit_view=False)
        self._app.processEvents()
        return doc, viewer

    def _close(self, doc, viewer):
        viewer.clear_text_selection()
        self._app.processEvents()
        viewer.close()
        self._app.processEvents()
        if not doc.is_closed:
            doc.close()

    def test_word_rects_are_raw_at_all_rotations(self):
        # "Hello" は raw 座標で x=72, y=100-20*0.88 付近（baseline 100）。
        # どの回転角でも同じ raw 矩形が返らなければならない。
        # view_rect_to_raw_rect を単語に適用すると 90°以上でズレる（誤修正の防止）。
        from core.geometry import view_rect_to_raw_rect

        for rotation in (0, 90, 180, 270):
            doc, viewer = self._viewer_for_rotation(rotation)
            try:
                words = viewer._get_page_word_rects_raw()
                hello = [w for w in words if w[4] == "Hello"]
                self.assertEqual(len(hello), 1, f"rotation={rotation}")
                x0, y0, x1, y1 = hello[0][:4]
                self.assertAlmostEqual(x0, 72.0, delta=2.0)
                self.assertAlmostEqual(y0, 78.0, delta=3.0)
                self.assertAlmostEqual(x1, 117.6, delta=3.0)
                self.assertAlmostEqual(y1, 106.0, delta=3.0)
                if rotation != 0:
                    # 提案された変換は回転時に全く別の場所を指すことを確認
                    # （＝適用してはいけない）。
                    wrong = view_rect_to_raw_rect(
                        doc[0], fitz.Rect(x0, y0, x1, y1)
                    )
                    self.assertFalse(
                        abs(wrong.x0 - 72.0) < 2.0 and abs(wrong.y0 - 78.0) < 3.0,
                        f"rotation={rotation}: report fix unexpectedly matched",
                    )
            finally:
                self._close(doc, viewer)

    def test_drag_selects_text_on_rotated_page(self):
        from PyQt6.QtCore import QRectF

        for rotation in (90, 270):
            doc, viewer = self._viewer_for_rotation(rotation)
            try:
                viewer.mode = "text_select"
                # raw 矩形 (60,70)-(180,110) は "Hello World" を含む。
                raw_rect = fitz.Rect(60, 70, 180, 110)
                x0, y0 = viewer.pdf_to_scene(fitz.Point(raw_rect.x0, raw_rect.y0))
                x1, y1 = viewer.pdf_to_scene(fitz.Point(raw_rect.x1, raw_rect.y1))
                viewer._finish_text_selection(
                    raw_rect, QRectF(x0, y0, x1 - x0, y1 - y0)
                )
                self.assertEqual(viewer._text_sel_text, "Hello World")
            finally:
                self._close(doc, viewer)

    def test_erased_word_is_excluded_from_selection(self):
        from PyQt6.QtCore import QRectF

        doc, viewer = self._viewer_for_rotation(0)
        try:
            viewer.mode = "text_select"
            # "World" の単語矩形を白塗り墨消しで覆う。
            words = viewer._get_page_word_rects_raw()
            world = [w for w in words if w[4] == "World"][0]
            viewer.page_annotations[0] = [
                {
                    "type": "box_erase",
                    "rect": (
                        world[0] - 1,
                        world[1] - 1,
                        world[2] + 1,
                        world[3] + 1,
                    ),
                }
            ]
            raw_rect = fitz.Rect(60, 70, 260, 110)
            x0, y0 = viewer.pdf_to_scene(fitz.Point(raw_rect.x0, raw_rect.y0))
            x1, y1 = viewer.pdf_to_scene(fitz.Point(raw_rect.x1, raw_rect.y1))
            viewer._finish_text_selection(
                raw_rect, QRectF(x0, y0, x1 - x0, y1 - y0)
            )
            self.assertEqual(viewer._text_sel_text, "Hello test")
        finally:
            self._close(doc, viewer)


class TestBug02FoldOcrRectConversion(unittest.TestCase):
    """BUG-02: 折りたたみ中の scene <-> view 座標変換の往復。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])
        from ui.pdf_viewer import PDFViewer

        cls._viewer_cls = PDFViewer

    def _viewer_with_fold(self, orientation):
        from core.fold import FOLD_HORIZONTAL, FOLD_VERTICAL

        doc = _make_text_doc()
        viewer = self._viewer_cls()
        viewer.resize(800, 600)
        viewer.show()
        viewer.load_pdf(doc, 0, fit_view=False)
        self._app.processEvents()
        if orientation == FOLD_HORIZONTAL:
            err = viewer.set_fold_band_for_current_page(300.0, 400.0, FOLD_HORIZONTAL)
        else:
            err = viewer.set_fold_band_for_current_page(200.0, 300.0, FOLD_VERTICAL)
        self.assertIsNone(err)
        self._app.processEvents()
        return doc, viewer

    def _close(self, doc, viewer):
        self._app.processEvents()
        viewer.close()
        self._app.processEvents()
        if not doc.is_closed:
            doc.close()

    def _check_roundtrip(self, orientation):
        from PyQt6.QtCore import QRectF
        from core.geometry import raw_rect_to_view_rect
        from ui.region_ocr_ext import (
            _scene_rect_to_view_rect,
            _view_rect_to_scene_rect,
        )

        doc, viewer = self._viewer_with_fold(orientation)
        try:
            page = doc[0]
            # 帯の後ろ側にある既知の raw 矩形（横帯なら y=500 付近、縦帯なら x=400 付近）。
            raw = (
                fitz.Rect(72, 500, 300, 530)
                if orientation == "h"
                else fitz.Rect(400, 100, 500, 130)
            )
            vr = raw_rect_to_view_rect(page, raw)
            sr = _view_rect_to_scene_rect(viewer, vr)
            self.assertFalse(sr.isEmpty())
            vr2 = _scene_rect_to_view_rect(viewer, sr)
            self.assertIsNotNone(vr2)
            self.assertAlmostEqual(vr2.x0, vr.x0, delta=1.5)
            self.assertAlmostEqual(vr2.y0, vr.y0, delta=1.5)
            self.assertAlmostEqual(vr2.x1, vr.x1, delta=1.5)
            self.assertAlmostEqual(vr2.y1, vr.y1, delta=1.5)
            # 逆方向も確認する。
            sr2 = _view_rect_to_scene_rect(viewer, vr2)
            self.assertAlmostEqual(sr2.x(), sr.x(), delta=2.0)
            self.assertAlmostEqual(sr2.y(), sr.y(), delta=2.0)
            self.assertAlmostEqual(sr2.width(), sr.width(), delta=2.0)
            self.assertAlmostEqual(sr2.height(), sr.height(), delta=2.0)
        finally:
            self._close(doc, viewer)

    def test_roundtrip_horizontal_fold(self):
        self._check_roundtrip("h")

    def test_roundtrip_vertical_fold(self):
        self._check_roundtrip("v")

    def test_roundtrip_without_fold(self):
        from core.geometry import raw_rect_to_view_rect
        from ui.region_ocr_ext import (
            _scene_rect_to_view_rect,
            _view_rect_to_scene_rect,
        )

        doc = _make_text_doc()
        viewer = self._viewer_cls()
        viewer.resize(800, 600)
        viewer.show()
        viewer.load_pdf(doc, 0, fit_view=False)
        self._app.processEvents()
        try:
            vr = raw_rect_to_view_rect(doc[0], fitz.Rect(72, 100, 200, 130))
            sr = _view_rect_to_scene_rect(viewer, vr)
            vr2 = _scene_rect_to_view_rect(viewer, sr)
            self.assertAlmostEqual(vr2.x0, vr.x0, delta=1.5)
            self.assertAlmostEqual(vr2.y0, vr.y0, delta=1.5)
            self.assertAlmostEqual(vr2.x1, vr.x1, delta=1.5)
            self.assertAlmostEqual(vr2.y1, vr.y1, delta=1.5)
        finally:
            self._close(doc, viewer)


class TestBug03RestoreUpdatedAfter(unittest.TestCase):
    """BUG-03: フォールバック復元の updated_after 往復。

    昇順・`>` が正しい。逆順にすると曖昧でない値（下の例では 5）が壊れる。
    """

    def _run_roundtrip(self, before):
        from core.commands import RemoveMultipleAnnotationsCommand

        doc = fitz.open()
        doc.new_page(width=400, height=400)
        viewer = _FakeViewer(doc)
        viewer.page_annotations[0] = [dict(a) for a in before]
        removed = [(1, before[1]), (3, before[3])]
        cmd = RemoveMultipleAnnotationsCommand(0, removed, before_annotations=None)
        cmd.redo(viewer)
        self.assertEqual(len(viewer.page_annotations[0]), 3)
        cmd.undo(viewer)
        result = viewer.page_annotations[0]
        doc.close()
        return result

    def test_unambiguous_values_roundtrip(self):
        before = [
            {"type": "text", "text": "A", "updated_after": 0},
            {"type": "erase", "points": [(0, 0)]},
            {"type": "text", "text": "B", "updated_after": 5},
            {"type": "erase", "points": [(1, 1)]},
            {"type": "text", "text": "C", "updated_after": 6},
        ]
        result = self._run_roundtrip(before)
        self.assertEqual([a.get("text") for a in result if a["type"] == "text"], ["A", "B", "C"])
        by_text = {a["text"]: a for a in result if a["type"] == "text"}
        self.assertEqual(by_text["A"]["updated_after"], 0)
        self.assertEqual(by_text["B"]["updated_after"], 5)
        self.assertEqual(by_text["C"]["updated_after"], 6)

    def test_ambiguous_value_picks_smaller_preimage(self):
        # 元の値が 3 か 4 かは復元後の状態だけでは判別できない（既知の制限）。
        # コードは小さい方（3）を選ぶ。これは get_later_user_erase_rects の
        # 意味論上必要（復元した消去が「後からの消去」とみなされるため）。
        before = [
            {"type": "text", "text": "A", "updated_after": 0},
            {"type": "erase", "points": [(0, 0)]},
            {"type": "text", "text": "B", "updated_after": 4},
            {"type": "erase", "points": [(1, 1)]},
            {"type": "text", "text": "C", "updated_after": 6},
        ]
        result = self._run_roundtrip(before)
        by_text = {a["text"]: a for a in result if a["type"] == "text"}
        self.assertEqual(by_text["B"]["updated_after"], 3)
        self.assertEqual(by_text["C"]["updated_after"], 6)


class TestBug04SpreadReaderRedo(unittest.TestCase):
    """BUG-04: 見開きリーダーのスタンドアロン Redo でページへ移動すること。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def test_local_redo_moves_to_edited_page(self):
        from ui.spread_reader import FullscreenSpreadReader

        doc = fitz.open()
        for _ in range(4):
            page = doc.new_page(width=595, height=842)
            page.insert_text((50, 50), "PAGE", fontsize=20)
        try:
            reader = FullscreenSpreadReader(doc=doc, start_page=0, viewer=None)
            reader.show()
            self._app.processEvents()
            try:
                # 現在の見開きは 0 ページ目のみ。2 ページ目を編集したことにする。
                reader.annotations[2] = [{"type": "highlight", "rect": (10, 10, 50, 30)}]
                reader._local_undo_stack.append((2, []))
                reader._local_redo_stack.append(
                    (2, [{"type": "highlight", "rect": (10, 10, 50, 30)}])
                )
                reader.annotations[2] = []
                reader.set_current_page(0)
                self.assertNotIn(2, reader.spreads[reader.current_spread_idx].page_indices)
                reader._perform_redo()
                self.assertEqual(reader._last_edited_page_idx, 2)
                self.assertIn(2, reader.spreads[reader.current_spread_idx].page_indices)
                self.assertEqual(
                    reader.annotations[2],
                    [{"type": "highlight", "rect": (10, 10, 50, 30)}],
                )
            finally:
                reader.close()
                self._app.processEvents()
        finally:
            doc.close()


class TestBug05MultiPageCommands(unittest.TestCase):
    """BUG-05: 一括操作コマンドの Undo/Redo 時ページ遷移と空リスト掃除。"""

    def _make_doc(self, pages=3):
        doc = fitz.open()
        for _ in range(pages):
            doc.new_page(width=400, height=600)
        return doc

    def test_crop_pages_undo_moves_to_affected_page(self):
        from core.commands import CropPagesCommand

        doc = self._make_doc(3)
        try:
            viewer = _FakeViewer(doc)
            viewer.current_page_idx = 2
            old0 = fitz.Rect(doc[0].cropbox)
            old1 = fitz.Rect(doc[1].cropbox)
            new0 = fitz.Rect(10, 10, 390, 590)
            new1 = fitz.Rect(20, 20, 380, 580)
            cmd = CropPagesCommand(
                [(0, old0, new0), (1, old1, new1)]
            )
            cmd.redo(viewer)
            self.assertEqual(viewer.current_page_idx, 0)
            viewer.current_page_idx = 2
            cmd.undo(viewer)
            self.assertEqual(viewer.current_page_idx, 0)
            self.assertEqual(fitz.Rect(doc[0].cropbox), old0)
        finally:
            doc.close()

    def test_add_annotations_multipage_undo_moves_and_cleans_empty_list(self):
        from core.commands import AddAnnotationsMultiPageCommand

        doc = self._make_doc(3)
        try:
            viewer = _FakeViewer(doc)
            viewer.current_page_idx = 2
            ann = {"type": "highlight", "rect": (10, 10, 50, 30)}
            cmd = AddAnnotationsMultiPageCommand([(0, ann), (1, ann)])
            cmd.redo(viewer)
            self.assertEqual(viewer.current_page_idx, 0)
            self.assertEqual(len(viewer.page_annotations[0]), 1)
            viewer.current_page_idx = 2
            cmd.undo(viewer)
            self.assertEqual(viewer.current_page_idx, 0)
            self.assertNotIn(0, viewer.page_annotations)
            self.assertNotIn(1, viewer.page_annotations)
        finally:
            doc.close()


class TestBug09NegativeZero(unittest.TestCase):
    """BUG-09: "-0.0%" が -0.0 として出力されないこと。"""

    def test_negative_zero_percent_normalized(self):
        from core.excel_exporter import parse_excel_numeric_cell

        for text in ("-0.0%", "▲0.0%", "(0.0)%", "-0%", "−0.0%"):
            val, is_percent = parse_excel_numeric_cell(text)
            self.assertEqual(val, 0.0)
            self.assertTrue(is_percent)
            self.assertEqual(str(val), "0.0")


if __name__ == "__main__":
    unittest.main()
