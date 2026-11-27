# -*- coding: utf-8 -*-
"""多機能版「FAX結合（2ページを1枚に）」に対するテスト。

- core: PDFDocumentManager.build_merged_two_pages_doc
  （左右/上下、切り取り量、サイズ違い、回転付きページ、テキスト保持）
- command: MergeTwoPagesCommand（ページ数・注釈の付け替え・Undo/Redo）
- UI: MergePagesDialog のスモークと PageManager.merge_selected_pages の配線
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz


def _make_doc(specs):
    """specs: [(width, height, [ (x, y, text), ... ]), ...] から文書を作る。"""
    doc = fitz.open()
    for width, height, texts in specs:
        page = doc.new_page(width=width, height=height)
        for x, y, text in texts:
            page.insert_text((x, y), text, fontsize=12)
    return doc


class TestBuildMergedTwoPagesDoc(unittest.TestCase):
    def tearDown(self):
        for d in getattr(self, "_docs", []):
            try:
                d.close()
            except Exception:
                pass

    def _track(self, doc):
        self._docs = getattr(self, "_docs", [])
        self._docs.append(doc)
        return doc

    def test_horizontal_basic(self):
        src = self._track(_make_doc([
            (300, 400, [(20, 40, "FIRST_PAGE")]),
            (300, 400, [(20, 40, "SECOND_PAGE")]),
        ]))
        merged = self._track(PDFDocumentManager_build(src, 0, 1))
        self.assertEqual(len(merged), 1)
        page = merged[0]
        self.assertAlmostEqual(page.rect.width, 600, places=3)
        self.assertAlmostEqual(page.rect.height, 400, places=3)
        text = page.get_text()
        self.assertIn("FIRST_PAGE", text)
        self.assertIn("SECOND_PAGE", text)
        # 元文書は変更されていない
        self.assertEqual(len(src), 2)

    def test_vertical_basic(self):
        src = self._track(_make_doc([
            (300, 400, [(20, 40, "TOP_PAGE")]),
            (300, 400, [(20, 40, "BOTTOM_PAGE")]),
        ]))
        merged = self._track(PDFDocumentManager_build(
            src, 0, 1, orientation="vertical"))
        page = merged[0]
        self.assertAlmostEqual(page.rect.width, 300, places=3)
        self.assertAlmostEqual(page.rect.height, 800, places=3)
        text = page.get_text()
        self.assertIn("TOP_PAGE", text)
        self.assertIn("BOTTOM_PAGE", text)

    def test_trim_cuts_inner_edges(self):
        # 1枚目の右端（x=260）と2枚目の左端（x=10）に置いた文字は、
        # 内側を削ると消える。外側の文字は残る。
        src = self._track(_make_doc([
            (300, 400, [(20, 40, "KEEP_LEFT"), (255, 40, "CUT_A")]),
            (300, 400, [(8, 40, "CUT_B"), (200, 40, "KEEP_RIGHT")]),
        ]))
        merged = self._track(PDFDocumentManager_build(
            src, 0, 1, trim_first=60, trim_second=40))
        page = merged[0]
        self.assertAlmostEqual(page.rect.width, 300 - 60 + 300 - 40, places=3)
        text = page.get_text()
        self.assertIn("KEEP_LEFT", text)
        self.assertIn("KEEP_RIGHT", text)
        self.assertNotIn("CUT_A", text)
        self.assertNotIn("CUT_B", text)

    def test_trim_vertical(self):
        src = self._track(_make_doc([
            (300, 400, [(20, 40, "TOP_KEEP")]),
            (300, 400, [(20, 200, "BOTTOM_KEEP")]),
        ]))
        merged = self._track(PDFDocumentManager_build(
            src, 0, 1, orientation="vertical",
            trim_first=100, trim_second=50))
        page = merged[0]
        self.assertAlmostEqual(page.rect.height, 400 - 100 + 400 - 50, places=3)
        self.assertIn("TOP_KEEP", page.get_text())
        self.assertIn("BOTTOM_KEEP", page.get_text())

    def test_different_sizes_centered(self):
        src = self._track(_make_doc([
            (300, 400, [(20, 40, "BIG_PAGE")]),
            (200, 300, [(20, 40, "SMALL_PAGE")]),
        ]))
        merged = self._track(PDFDocumentManager_build(src, 0, 1))
        page = merged[0]
        # 横並び: 幅は合計、高さは大きい方
        self.assertAlmostEqual(page.rect.width, 500, places=3)
        self.assertAlmostEqual(page.rect.height, 400, places=3)
        text = page.get_text()
        self.assertIn("BIG_PAGE", text)
        self.assertIn("SMALL_PAGE", text)

    def test_rotated_page_merged_as_displayed(self):
        src = fitz.open()
        self._track(src)
        page = src.new_page(width=400, height=300)
        page.insert_text((20, 100), "ROTATED_TEXT", fontsize=12)
        page.set_rotation(90)
        page2 = src.new_page(width=300, height=400)
        page2.insert_text((20, 40), "NORMAL_TEXT", fontsize=12)

        merged = self._track(PDFDocumentManager_build(src, 0, 1))
        mpage = merged[0]
        # 回転ページの表示サイズは 300x400 なので、横並びで 600x400
        self.assertAlmostEqual(mpage.rect.width, 600, places=3)
        self.assertAlmostEqual(mpage.rect.height, 400, places=3)
        text = mpage.get_text()
        self.assertIn("ROTATED_TEXT", text)
        self.assertIn("NORMAL_TEXT", text)
        # 元ページの回転はそのまま（元文書を変更していない）
        self.assertEqual(src[0].rotation, 90)

    def test_offset_vertical_shift_expands_height(self):
        # 左右並びで2枚目を下に30ptずらすと、高さが30pt広がり文字は残る
        src = self._track(_make_doc([
            (300, 400, [(20, 40, "FIRST_PAGE")]),
            (300, 400, [(20, 40, "SECOND_PAGE")]),
        ]))
        merged = self._track(PDFDocumentManager_build(
            src, 0, 1, offset_second_y=30))
        page = merged[0]
        self.assertAlmostEqual(page.rect.width, 600, places=3)
        self.assertAlmostEqual(page.rect.height, 430, places=3)
        self.assertIn("SECOND_PAGE", page.get_text())

    def test_offset_up_shift_keeps_content(self):
        # 上にずらしても端が切れず、ページが上に広がった分だけ高くなる
        src = self._track(_make_doc([
            (300, 400, [(20, 40, "FIRST_PAGE")]),
            (300, 400, [(20, 40, "SECOND_PAGE")]),
        ]))
        merged = self._track(PDFDocumentManager_build(
            src, 0, 1, offset_second_y=-20))
        page = merged[0]
        self.assertAlmostEqual(page.rect.height, 420, places=3)
        self.assertIn("FIRST_PAGE", page.get_text())
        self.assertIn("SECOND_PAGE", page.get_text())

    def test_offset_horizontal_in_vertical_merge(self):
        # 上下並びで2枚目を右に25ptずらすと、幅が25pt広がる
        src = self._track(_make_doc([
            (300, 400, [(20, 40, "TOP_PAGE")]),
            (300, 400, [(20, 40, "BOTTOM_PAGE")]),
        ]))
        merged = self._track(PDFDocumentManager_build(
            src, 0, 1, orientation="vertical", offset_second_x=25))
        page = merged[0]
        self.assertAlmostEqual(page.rect.width, 325, places=3)
        self.assertAlmostEqual(page.rect.height, 800, places=3)
        self.assertIn("BOTTOM_PAGE", page.get_text())

    def test_invalid_arguments(self):
        src = self._track(_make_doc([
            (300, 400, [(20, 40, "A")]),
            (300, 400, [(20, 40, "B")]),
        ]))
        with self.assertRaises(ValueError):
            PDFDocumentManager_build(src, 0, 0)
        with self.assertRaises(ValueError):
            PDFDocumentManager_build(src, 0, 5)
        with self.assertRaises(ValueError):
            PDFDocumentManager_build(src, 0, 1, orientation="diagonal")


def PDFDocumentManager_build(doc, first, second, **kwargs):
    from core.pdf_engine import PDFDocumentManager

    return PDFDocumentManager.build_merged_two_pages_doc(
        doc, first, second, **kwargs)


class _WindowTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

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

    def _write_pdf(self, name, texts, width=300, height=400):
        doc = fitz.open()
        for t in texts:
            page = doc.new_page(width=width, height=height)
            page.insert_text((20, 40), t, fontsize=12)
        path = Path(self._tmp.name) / name
        try:
            path.write_bytes(doc.tobytes())
        finally:
            doc.close()
        return str(path)

    def _open(self, path):
        self.window.open_file_from_path(path)
        self._app.processEvents()


class TestMergeCommandViaWindow(_WindowTestBase):
    def _run_merge_command(self, first_idx, second_idx, keep_originals=False):
        from core.commands import MergeTwoPagesCommand
        from core.pdf_engine import PDFDocumentManager

        viewer = self.window.viewer
        doc = viewer.current_pdf
        src = PDFDocumentManager.extract_pages_as_doc(
            doc, [first_idx, second_idx], viewer.page_annotations)
        try:
            merged = PDFDocumentManager.build_merged_two_pages_doc(src, 0, 1)
        finally:
            src.close()
        cmd = MergeTwoPagesCommand(
            doc, first_idx, second_idx, merged,
            viewer.page_annotations, viewer.current_page_idx,
            take_ownership=True, keep_originals=keep_originals)
        cmd.redo(viewer)
        viewer.history.push(cmd)
        self._app.processEvents()
        return cmd

    def test_merge_consecutive_and_undo_redo(self):
        src = self._write_pdf("src.pdf", ["PAGE_ONE", "PAGE_TWO", "PAGE_THREE"])
        self._open(src)
        viewer = self.window.viewer
        self.assertEqual(len(viewer.current_pdf), 3)

        self._run_merge_command(0, 1)
        doc = viewer.current_pdf
        self.assertEqual(len(doc), 2)
        merged_text = doc[0].get_text()
        self.assertIn("PAGE_ONE", merged_text)
        self.assertIn("PAGE_TWO", merged_text)
        self.assertIn("PAGE_THREE", doc[1].get_text())

        viewer.history.undo(viewer)
        self._app.processEvents()
        doc = viewer.current_pdf
        self.assertEqual(len(doc), 3)
        self.assertIn("PAGE_ONE", doc[0].get_text())
        self.assertIn("PAGE_TWO", doc[1].get_text())
        self.assertIn("PAGE_THREE", doc[2].get_text())

        viewer.history.redo(viewer)
        self._app.processEvents()
        self.assertEqual(len(viewer.current_pdf), 2)

    def test_merge_non_consecutive(self):
        src = self._write_pdf(
            "src.pdf", ["PAGE_ONE", "PAGE_TWO", "PAGE_THREE", "PAGE_FOUR"])
        self._open(src)
        viewer = self.window.viewer

        self._run_merge_command(0, 2)
        doc = viewer.current_pdf
        self.assertEqual(len(doc), 3)
        merged_text = doc[0].get_text()
        self.assertIn("PAGE_ONE", merged_text)
        self.assertIn("PAGE_THREE", merged_text)
        self.assertIn("PAGE_TWO", doc[1].get_text())
        self.assertIn("PAGE_FOUR", doc[2].get_text())

        viewer.history.undo(viewer)
        self._app.processEvents()
        doc = viewer.current_pdf
        self.assertEqual(len(doc), 4)
        for i, t in enumerate(
                ["PAGE_ONE", "PAGE_TWO", "PAGE_THREE", "PAGE_FOUR"]):
            self.assertIn(t, doc[i].get_text())

    def test_merge_keep_originals_and_undo_redo(self):
        src = self._write_pdf("src.pdf", ["PAGE_ONE", "PAGE_TWO", "PAGE_THREE"])
        self._open(src)
        viewer = self.window.viewer
        self.assertEqual(len(viewer.current_pdf), 3)

        self._run_merge_command(0, 1, keep_originals=True)
        doc = viewer.current_pdf
        # 結合ページが追加され、元の2ページも残る（3→4ページ）
        self.assertEqual(len(doc), 4)
        self.assertIn("PAGE_ONE", doc[0].get_text())
        self.assertIn("PAGE_TWO", doc[0].get_text())
        self.assertIn("PAGE_ONE", doc[1].get_text())
        self.assertIn("PAGE_TWO", doc[2].get_text())
        self.assertIn("PAGE_THREE", doc[3].get_text())

        viewer.history.undo(viewer)
        self._app.processEvents()
        self.assertEqual(len(viewer.current_pdf), 3)

        viewer.history.redo(viewer)
        self._app.processEvents()
        self.assertEqual(len(viewer.current_pdf), 4)

    def test_annotations_remapped(self):
        src = self._write_pdf("src.pdf", ["PAGE_ONE", "PAGE_TWO", "PAGE_THREE"])
        self._open(src)
        viewer = self.window.viewer
        ann_merged = {"type": "highlight", "rect": (10.0, 10.0, 50.0, 30.0)}
        ann_later = {
            "type": "text", "text": "LATER_NOTE",
            "pos": (20.0, 100.0), "fontsize": 14.0,
        }
        viewer.page_annotations[0] = [dict(ann_merged)]
        viewer.page_annotations[2] = [dict(ann_later)]

        self._run_merge_command(0, 1)
        # 結合したページの注釈は破棄され、後ろのページの注釈は1つ前に詰まる
        # （空リストのキーは viewer 側の都合で残ることがあるため、中身で判定）
        self.assertFalse(viewer.page_annotations.get(0))
        self.assertIn(1, viewer.page_annotations)
        self.assertEqual(viewer.page_annotations[1][0]["text"], "LATER_NOTE")

        viewer.history.undo(viewer)
        self._app.processEvents()
        # Undo で元のページの注釈も戻る
        self.assertIn(0, viewer.page_annotations)
        self.assertIn(2, viewer.page_annotations)
        self.assertEqual(viewer.page_annotations[2][0]["text"], "LATER_NOTE")


class TestMergePagesDialogSmoke(_WindowTestBase):
    def test_dialog_orientation_slider_and_build(self):
        from ui.merge_pages_dialog import MergePagesDialog

        src = self._write_pdf("src.pdf", ["PAGE_ONE", "PAGE_TWO"])
        self._open(src)
        viewer = self.window.viewer

        dialog = MergePagesDialog(
            self.window, viewer.current_pdf, 0, 1, viewer.page_annotations)
        try:
            dialog.show()
            self._app.processEvents()
            w, h = dialog.preview.result_size_pt()
            self.assertAlmostEqual(w, 600, places=3)
            self.assertAlmostEqual(h, 400, places=3)

            # 上下に切り替え
            dialog.radio_vertical.setChecked(True)
            self._app.processEvents()
            w, h = dialog.preview.result_size_pt()
            self.assertAlmostEqual(w, 300, places=3)
            self.assertAlmostEqual(h, 800, places=3)

            # 切り取り量を動かすとプレビュー寸法へ即反映される
            dialog.radio_horizontal.setChecked(True)
            dialog._trim_sliders[0].setValue(50)
            dialog._trim_sliders[1].setValue(10)
            self._app.processEvents()
            w, h = dialog.preview.result_size_pt()
            self.assertAlmostEqual(w, 600 - 60, places=3)

            merged = dialog.build_merged_doc()
            try:
                self.assertEqual(len(merged), 1)
                self.assertAlmostEqual(merged[0].rect.width, 540, places=3)
                self.assertIn("PAGE_ONE", merged[0].get_text())
                self.assertIn("PAGE_TWO", merged[0].get_text())
            finally:
                merged.close()
        finally:
            dialog.close()
            dialog.dispose()

    def test_dialog_offset_keep_and_fullscreen(self):
        from ui.merge_pages_dialog import MergePagesDialog

        src = self._write_pdf("src.pdf", ["PAGE_ONE", "PAGE_TWO"])
        self._open(src)
        viewer = self.window.viewer

        dialog = MergePagesDialog(
            self.window, viewer.current_pdf, 0, 1, viewer.page_annotations)
        try:
            dialog.show()
            self._app.processEvents()

            # 画質アップ: プレビュー画像の長辺が旧版(1000px)より大きい
            img = dialog.preview._images[0]
            self.assertGreater(max(img.width(), img.height()), 1000)

            # 元のページを残すオプション（初期値はオフ）
            self.assertFalse(dialog.keep_originals())
            dialog.check_keep_originals.setChecked(True)
            self.assertTrue(dialog.keep_originals())

            # 上下調整: 2枚目を下に30ptずらすとプレビューと実物が高くなる
            dialog._offset_sliders[0].setValue(30)
            self._app.processEvents()
            w, h = dialog.preview.result_size_pt()
            self.assertAlmostEqual(h, 430, places=3)
            merged = dialog.build_merged_doc()
            try:
                self.assertAlmostEqual(merged[0].rect.height, 430, places=3)
            finally:
                merged.close()
            dialog._offset_sliders[0].setValue(0)

            # 全画面の切り替え（Esc相当の set_fullscreen(False) で戻れる）
            dialog.set_fullscreen(True)
            self._app.processEvents()
            self.assertTrue(dialog._is_fullscreen)
            self.assertTrue(dialog.isFullScreen())
            dialog.set_fullscreen(False)
            self._app.processEvents()
            self.assertFalse(dialog._is_fullscreen)
        finally:
            dialog.close()
            dialog.dispose()


class TestPageManagerMergeWiring(_WindowTestBase):
    def test_merge_selected_pages_via_page_manager(self):
        from PyQt6.QtWidgets import QDialog

        from core.pdf_engine import PDFDocumentManager

        src = self._write_pdf("src.pdf", ["PAGE_ONE", "PAGE_TWO", "PAGE_THREE"])
        self._open(src)
        pm = self.window.page_manager
        viewer = self.window.viewer

        pm.list_widget.clearSelection()
        for row in (0, 1):
            pm.list_widget.item(row).setSelected(True)

        class _FakeDialog:
            def __init__(self, parent, doc, first_idx, second_idx, annots=None):
                self._doc = doc
                self._rows = (first_idx, second_idx)
                self._annots = annots

            def exec(self):
                return QDialog.DialogCode.Accepted

            def build_merged_doc(self):
                src_doc = PDFDocumentManager.extract_pages_as_doc(
                    self._doc, list(self._rows), self._annots)
                try:
                    return PDFDocumentManager.build_merged_two_pages_doc(
                        src_doc, 0, 1)
                finally:
                    src_doc.close()

            def dispose(self):
                pass

        with mock.patch("ui.merge_pages_dialog.MergePagesDialog", _FakeDialog):
            pm.merge_selected_pages()
        self._app.processEvents()

        doc = viewer.current_pdf
        self.assertEqual(len(doc), 2)
        self.assertIn("PAGE_ONE", doc[0].get_text())
        self.assertIn("PAGE_TWO", doc[0].get_text())
        self.assertIn("PAGE_THREE", doc[1].get_text())

        # ページ管理からの実行も Undo できる
        viewer.history.undo(viewer)
        self._app.processEvents()
        self.assertEqual(len(viewer.current_pdf), 3)


if __name__ == "__main__":
    unittest.main()
