# -*- coding: utf-8 -*-
"""複数ページへの一括適用（トリミング・範囲墨消し）の単体テスト

GUI なしで実行できる。対象ページの決定とサイズ違いのスキップ判定は
core/batch_apply の純粋関数で検証し、複数ページ用コマンドは
最小限のフェイクビューアで redo/undo を検証する。
"""
import copy
import sys
import unittest
from pathlib import Path

# tests/ サブフォルダから直接実行された場合でもプロジェクトルートを import 可能にする
_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent if (_THIS_DIR.name == "tests" or not (_THIS_DIR / "core").is_dir()) else _THIS_DIR
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import fitz

from core.batch_apply import (
    SCOPE_ALL,
    SCOPE_CURRENT,
    SCOPE_SELECTED,
    format_batch_result,
    partition_compatible_pages,
    resolve_target_pages,
    same_page_geometry,
)
from core.commands import AddAnnotationsMultiPageCommand, CropPagesCommand


class _FakeSignal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


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


def _make_doc(page_sizes):
    doc = fitz.open()
    for width, height in page_sizes:
        doc.new_page(width=width, height=height)
    return doc


class TestResolveTargetPages(unittest.TestCase):
    def test_current(self):
        self.assertEqual(resolve_target_pages(SCOPE_CURRENT, 2, [0, 1, 2], 5), [2])

    def test_selected_sorted_unique(self):
        self.assertEqual(
            resolve_target_pages(SCOPE_SELECTED, 0, [3, 1, 3, 2], 5), [1, 2, 3]
        )

    def test_selected_empty_falls_back_to_current(self):
        self.assertEqual(resolve_target_pages(SCOPE_SELECTED, 4, [], 5), [4])

    def test_selected_ignores_out_of_range(self):
        self.assertEqual(
            resolve_target_pages(SCOPE_SELECTED, 0, [-1, 1, 5, 9], 5), [1]
        )

    def test_all(self):
        self.assertEqual(resolve_target_pages(SCOPE_ALL, 0, [], 3), [0, 1, 2])

    def test_no_pages(self):
        self.assertEqual(resolve_target_pages(SCOPE_ALL, 0, [], 0), [])
        self.assertEqual(resolve_target_pages(SCOPE_CURRENT, 0, [], 0), [])

    def test_current_clamped(self):
        self.assertEqual(resolve_target_pages(SCOPE_CURRENT, 99, [], 3), [2])
        self.assertEqual(resolve_target_pages(SCOPE_CURRENT, -5, [], 3), [0])

    def test_unknown_scope(self):
        with self.assertRaises(ValueError):
            resolve_target_pages("bogus", 0, [], 3)


class TestSamePageGeometry(unittest.TestCase):
    def test_identical(self):
        self.assertTrue(same_page_geometry((0, 0, 595, 842, 0), (0, 0, 595, 842, 0)))

    def test_different_size(self):
        self.assertFalse(same_page_geometry((0, 0, 595, 842, 0), (0, 0, 612, 792, 0)))

    def test_different_origin(self):
        self.assertFalse(same_page_geometry((0, 0, 595, 842, 0), (10, 0, 605, 842, 0)))

    def test_different_rotation(self):
        self.assertFalse(same_page_geometry((0, 0, 595, 842, 0), (0, 0, 595, 842, 90)))

    def test_tiny_difference_within_tolerance(self):
        self.assertTrue(same_page_geometry((0, 0, 595, 842, 0), (0, 0, 595.5, 842, 0)))


class TestPartitionCompatiblePages(unittest.TestCase):
    def test_all_same_size(self):
        geoms = [(0, 0, 595, 842, 0)] * 4
        applicable, skipped = partition_compatible_pages(geoms, 0, [0, 1, 2, 3])
        self.assertEqual(applicable, [0, 1, 2, 3])
        self.assertEqual(skipped, [])

    def test_different_size_skipped(self):
        geoms = [
            (0, 0, 595, 842, 0),
            (0, 0, 595, 842, 0),
            (0, 0, 612, 792, 0),  # Letter サイズだけ違う
            (0, 0, 595, 842, 0),
        ]
        applicable, skipped = partition_compatible_pages(geoms, 0, [0, 1, 2, 3])
        self.assertEqual(applicable, [0, 1, 3])
        self.assertEqual(skipped, [2])

    def test_reference_page_always_applicable(self):
        geoms = [(0, 0, 595, 842, 0), (0, 0, 612, 792, 0)]
        applicable, skipped = partition_compatible_pages(geoms, 1, [0, 1])
        self.assertEqual(applicable, [1])
        self.assertEqual(skipped, [0])


class TestFormatBatchResult(unittest.TestCase):
    def test_no_skip(self):
        self.assertEqual(
            format_batch_result("トリミング", 3, 0), "トリミングを3ページに適用しました"
        )

    def test_with_skip(self):
        self.assertEqual(
            format_batch_result("範囲墨消し", 3, 1),
            "範囲墨消しを3ページに適用しました（1ページはサイズや向きが違うためスキップしました）",
        )


class TestCropPagesCommand(unittest.TestCase):
    def test_redo_and_undo_multiple_pages(self):
        doc = _make_doc([(595, 842), (595, 842), (595, 842)])
        try:
            viewer = _FakeViewer(doc)
            new_crop = fitz.Rect(10, 20, 585, 822)
            entries = [
                (0, fitz.Rect(doc[0].cropbox), new_crop),
                (2, fitz.Rect(doc[2].cropbox), new_crop),
            ]
            cmd = CropPagesCommand(entries)
            cmd.redo(viewer)
            self.assertEqual(doc[0].cropbox, new_crop)
            self.assertEqual(doc[2].cropbox, new_crop)
            # 対象外のページは変わらない
            self.assertEqual(doc[1].cropbox, doc[1].mediabox)
            # 描画はまとめて1回
            self.assertEqual(len(viewer.render_calls), 1)
            self.assertEqual(viewer.notified, [[0, 2]])

            cmd.undo(viewer)
            self.assertEqual(doc[0].cropbox, doc[0].mediabox)
            self.assertEqual(doc[2].cropbox, doc[2].mediabox)
            self.assertEqual(len(viewer.render_calls), 2)
        finally:
            doc.close()

    def test_redo_after_undo(self):
        doc = _make_doc([(595, 842), (595, 842)])
        try:
            viewer = _FakeViewer(doc)
            new_crop = fitz.Rect(0, 0, 300, 400)
            cmd = CropPagesCommand([(1, fitz.Rect(doc[1].cropbox), new_crop)])
            cmd.redo(viewer)
            cmd.undo(viewer)
            cmd.redo(viewer)
            self.assertEqual(doc[1].cropbox, new_crop)
        finally:
            doc.close()


class TestAddAnnotationsMultiPageCommand(unittest.TestCase):
    def _annot(self):
        return {"type": "box_erase", "rect": (10.0, 20.0, 100.0, 60.0)}

    def test_redo_adds_to_each_page_and_undo_removes(self):
        doc = _make_doc([(595, 842), (595, 842), (595, 842)])
        try:
            viewer = _FakeViewer(doc)
            viewer.page_annotations[0] = [
                {"type": "highlight", "rect": (0.0, 0.0, 50.0, 10.0)}
            ]
            cmd = AddAnnotationsMultiPageCommand([(0, self._annot()), (2, self._annot())])
            cmd.redo(viewer)
            self.assertEqual(len(viewer.page_annotations[0]), 2)
            self.assertEqual(viewer.page_annotations[0][1], self._annot())
            self.assertEqual(viewer.page_annotations[2], [self._annot()])
            self.assertNotIn(1, viewer.page_annotations)
            self.assertEqual(len(viewer.render_calls), 1)

            cmd.undo(viewer)
            self.assertEqual(len(viewer.page_annotations[0]), 1)
            self.assertEqual(
                viewer.page_annotations[0][0]["type"], "highlight"
            )
            self.assertEqual(viewer.page_annotations.get(2, []), [])

            cmd.redo(viewer)
            self.assertEqual(len(viewer.page_annotations[0]), 2)
            self.assertEqual(viewer.page_annotations[2], [self._annot()])
        finally:
            doc.close()

    def test_undo_only_removes_the_added_annotation(self):
        doc = _make_doc([(595, 842)])
        try:
            viewer = _FakeViewer(doc)
            existing = {"type": "box_erase", "rect": (1.0, 1.0, 2.0, 2.0)}
            viewer.page_annotations[0] = [copy.deepcopy(existing)]
            cmd = AddAnnotationsMultiPageCommand([(0, self._annot())])
            cmd.redo(viewer)
            self.assertEqual(len(viewer.page_annotations[0]), 2)
            cmd.undo(viewer)
            self.assertEqual(viewer.page_annotations[0], [existing])
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main()
