# -*- coding: utf-8 -*-
"""Secure Local PDF Editor - コアロジック・座標変換・コマンド単体テスト"""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

# tests/ サブフォルダから直接実行された場合でもプロジェクトルートを import 可能にする
_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent if (_THIS_DIR.name == "tests" or not (_THIS_DIR / "core").is_dir()) else _THIS_DIR
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import fitz

from core import (
    adjust_updated_after_on_insert,
    adjust_updated_after_on_remove,
    clip_stroke_by_rects,
    collect_all_erased_raw_rects,
    find_draw_annotation_at_point,
    find_draws_along_eraser_stroke,
    find_highlight_annotation_at_point,
    find_highlights_in_rect,
    find_pdf_token_at_point,
    find_text_annotation_at_point,
    find_text_targets_in_rect,
    is_rect_substantially_erased,
    point_to_segment_distance,
)
from core.geometry import (
    estimate_text_bbox_in_pdf,
    extract_number_from_text,
    format_number_value,
    format_pt,
    get_crop_outside_rects,
    raw_pdf_to_view_point,
    raw_pdf_to_view_point_from_geom,
    raw_rect_to_view_rect,
    srgb_int_to_rgb_tuple,
    view_point_to_raw_pdf,
    view_point_to_raw_pdf_from_geom,
    view_rect_to_raw_rect,
)


class TestCoreGeometryAndAnnotations(unittest.TestCase):
    def test_point_to_segment_distance(self):
        self.assertAlmostEqual(point_to_segment_distance(5.0, 3.0, 0.0, 0.0, 10.0, 0.0), 3.0)
        self.assertAlmostEqual(point_to_segment_distance(-3.0, 4.0, 0.0, 0.0, 10.0, 0.0), 5.0)

    def test_clip_stroke_by_rects(self):
        stroke = [(0.0, 0.0), (100.0, 0.0)]
        erase_rect = [fitz.Rect(40.0, -10.0, 60.0, 10.0)]
        segments = clip_stroke_by_rects(stroke, erase_rect, step=2.0)
        self.assertEqual(len(segments), 2)
        self.assertTrue(all(px < 40.0 for px, _ in segments[0]))
        self.assertTrue(all(px >= 60.0 for px, _ in segments[1]))

    def test_is_rect_substantially_erased(self):
        target = fitz.Rect(0, 0, 100, 20)
        partial_erase = [fitz.Rect(0, 0, 20, 20)]
        full_center_erase = [fitz.Rect(45, 5, 55, 15)]
        self.assertFalse(is_rect_substantially_erased(target, partial_erase))
        self.assertTrue(is_rect_substantially_erased(target, full_center_erase))

    def test_find_draw_and_highlight_with_later_erase(self):
        ann_list = [
            {"type": "draw", "points": [(10.0, 10.0), (90.0, 10.0)], "width": 2.0},
            {"type": "highlight", "rect": (10.0, 30.0, 90.0, 50.0)},
            {"type": "box_erase", "rect": (40.0, 0.0, 60.0, 20.0)},
        ]
        idx, _, _ = find_draw_annotation_at_point(ann_list, fitz.Point(20.0, 10.0))
        self.assertEqual(idx, 0)
        idx_erased, _, _ = find_draw_annotation_at_point(ann_list, fitz.Point(50.0, 10.0))
        self.assertIsNone(idx_erased)
        matched = find_draws_along_eraser_stroke(ann_list, [(20.0, 5.0), (20.0, 15.0)])
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0][0], 0)
        h_idx, _ = find_highlight_annotation_at_point(ann_list, fitz.Point(50.0, 40.0))
        self.assertEqual(h_idx, 1)

    def test_adjust_updated_after_indices(self):
        ann_list = [
            {"type": "draw", "points": [(0, 0), (10, 10)]},
            {"type": "erase", "points": [(5, 5), (6, 6)]},
            {"type": "text", "text": "A", "updated_after": 2},
        ]
        ann_list.pop(0)
        adjust_updated_after_on_remove(ann_list, 0)
        self.assertEqual(ann_list[1]["updated_after"], 1)
        ann_list.insert(0, {"type": "draw", "points": [(0, 0), (10, 10)]})
        adjust_updated_after_on_insert(ann_list, 0)
        self.assertEqual(ann_list[2]["updated_after"], 2)

    def test_extract_number_from_text_and_formatting(self):
        self.assertEqual(extract_number_from_text("¥1,234,567"), 1234567.0)
        self.assertEqual(extract_number_from_text("▲ 500"), -500.0)
        self.assertEqual(extract_number_from_text("△1,200円"), -1200.0)
        self.assertEqual(extract_number_from_text("(3,400)"), -3400.0)
        self.assertAlmostEqual(extract_number_from_text("-3.5%"), -3.5)
        self.assertIsNone(extract_number_from_text("合計金額なし"))
        self.assertEqual(format_pt(14.0), "14")
        self.assertEqual(format_pt(10.5), "10.5")
        self.assertEqual(format_number_value(1234567.0), "1,234,567")
        self.assertEqual(srgb_int_to_rgb_tuple(0xFF0000), (1.0, 0.0, 0.0))

    def test_get_crop_outside_rects(self):
        media = fitz.Rect(0, 0, 200, 300)
        crop = fitz.Rect(10, 20, 190, 280)
        outside = get_crop_outside_rects(media, crop)
        self.assertEqual(len(outside), 4)

    def test_all_modularized_imports(self):
        import core.commands
        import core.geometry
        import core.pdf_engine
        import ui.dialogs
        import ui.main_window
        import ui.page_manager
        import ui.pdf_viewer

        self.assertTrue(hasattr(core.commands, "HistoryManager"))
        self.assertTrue(hasattr(core.pdf_engine, "PDFDocumentManager"))
        self.assertTrue(hasattr(ui.dialogs, "TextEditDeleteDialog"))
        self.assertTrue(hasattr(ui.pdf_viewer, "PDFViewer"))
        self.assertTrue(hasattr(ui.page_manager, "PageManager"))
        self.assertTrue(hasattr(ui.main_window, "MainWindow"))

    def test_pdf_engine_bake_and_redact(self):
        from core.pdf_engine import PDFDocumentManager

        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        page.insert_text(fitz.Point(20, 50), "SECRET123", fontsize=12)
        annots = {
            0: [
                {"type": "box_erase", "rect": (15.0, 35.0, 120.0, 60.0)},
                {"type": "highlight", "rect": (20.0, 80.0, 100.0, 100.0), "color": (0.98, 0.9, 0.05)},
            ]
        }
        PDFDocumentManager.bake_annotations_to_doc(doc, annots, verify=True)
        self.assertNotIn("SECRET123", doc[0].get_text("text"))
        doc.close()

    def test_coordinate_roundtrip_with_rotation_and_cropbox(self):
        doc = fitz.open()
        page = doc.new_page(width=400, height=600)
        crop = fitz.Rect(20, 30, 360, 550)
        page.set_cropbox(crop)
        for rot in (0, 90, 180, 270):
            page.set_rotation(rot)
            raw_pt = fitz.Point(125.5, 240.25)
            view_pt = raw_pdf_to_view_point(page, raw_pt)
            restored_pt = view_point_to_raw_pdf(page, view_pt)
            self.assertAlmostEqual(raw_pt.x, restored_pt.x, places=4)
            self.assertAlmostEqual(raw_pt.y, restored_pt.y, places=4)

            geom_view_pt = raw_pdf_to_view_point_from_geom(crop, page.rect, rot, raw_pt)
            geom_restored_pt = view_point_to_raw_pdf_from_geom(crop, page.rect, rot, geom_view_pt)
            self.assertAlmostEqual(raw_pt.x, geom_restored_pt.x, places=4)
            self.assertAlmostEqual(raw_pt.y, geom_restored_pt.y, places=4)

            raw_r = fitz.Rect(60.0, 80.0, 220.0, 310.0)
            view_r = raw_rect_to_view_rect(page, raw_r)
            restored_r = view_rect_to_raw_rect(page, view_r)
            self.assertAlmostEqual(raw_r.x0, restored_r.x0, places=4)
            self.assertAlmostEqual(raw_r.y0, restored_r.y0, places=4)
            self.assertAlmostEqual(raw_r.x1, restored_r.x1, places=4)
            self.assertAlmostEqual(raw_r.y1, restored_r.y1, places=4)
        doc.close()

    def test_history_manager_state_and_dispose(self):
        from core.commands import BaseCommand, HistoryManager, RotatePagesCommand

        disposed = []

        class DummyCmd(BaseCommand):
            def __init__(self, tag):
                super().__init__()
                self.tag = tag

            def dispose(self):
                disposed.append(self.tag)

            def undo(self, viewer):
                pass

            def redo(self, viewer):
                pass

        hm = HistoryManager(max_steps=2)
        self.assertFalse(hm.is_modified)
        hm.push(DummyCmd("c1"))
        self.assertTrue(hm.is_modified)
        hm.mark_saved()
        self.assertFalse(hm.is_modified)
        hm.push(DummyCmd("c2"))
        hm.push(DummyCmd("c3"))
        self.assertEqual(disposed, ["c1"])
        hm.clear()
        self.assertIn("c2", disposed)
        self.assertIn("c3", disposed)

        rot_cmd = RotatePagesCommand([(0, -90, 450)])
        self.assertEqual(rot_cmd.rotations, [(0, 270, 90)])

    def test_shared_annotation_queries_and_ui_separation(self):
        from ui import compute_scaled_baseline_top_pos

        ann_list = [
            {"type": "highlight", "rect": (10.0, 10.0, 80.0, 30.0)},
            {"type": "text", "text": "Hello", "pos": (20.0, 50.0), "fontsize": 14.0},
            {"type": "box_erase", "rect": (100.0, 100.0, 150.0, 140.0)},
        ]
        tokens = [
            {"text": "ActiveTok", "raw_rect": (20.0, 160.0, 70.0, 175.0)},
            {"text": "ErasedTok", "raw_rect": (110.0, 110.0, 140.0, 125.0)},
        ]
        erased = collect_all_erased_raw_rects(ann_list)
        self.assertEqual(len(erased), 1)
        hl_hits = find_highlights_in_rect(ann_list, fitz.Rect(0.0, 0.0, 50.0, 50.0))
        self.assertEqual(len(hl_hits), 1)
        self.assertEqual(hl_hits[0][0], 0)
        t_idx, _ = find_text_annotation_at_point(
            ann_list, fitz.Point(25.0, 55.0), estimate_text_bbox_in_pdf
        )
        self.assertEqual(t_idx, 1)
        tok_hit = find_pdf_token_at_point(tokens, ann_list, fitz.Point(30.0, 165.0))
        self.assertIsNotNone(tok_hit)
        self.assertEqual(tok_hit["text"], "ActiveTok")
        self.assertIsNone(find_pdf_token_at_point(tokens, ann_list, fitz.Point(120.0, 118.0)))
        ann_ids, tok_hits = find_text_targets_in_rect(
            ann_list, tokens, fitz.Rect(0.0, 0.0, 200.0, 200.0), estimate_text_bbox_in_pdf
        )
        self.assertEqual(ann_ids, [1])
        self.assertEqual(len(tok_hits), 1)
        new_top = compute_scaled_baseline_top_pos((10.0, 10.0), (10.0, 20.0), 20.0)
        self.assertAlmostEqual(new_top[0], 10.0, places=4)
        self.assertAlmostEqual(new_top[1], 20.0 - 20.0 * 0.88, places=4)


    def test_v21_clean_scanned_qimage_color_drop_and_despeckle(self):
        from PyQt6.QtGui import QColor, QImage
        from core.ocr_cleaner import clean_scanned_qimage

        img = QImage(60, 60, QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))

        # 1) セゾン明細のような黄色背景 (x: 5..15, y: 5..15)
        for y in range(5, 15):
            for x in range(5, 15):
                img.setPixelColor(x, y, QColor(255, 245, 130))

        # 2) JCB明細のような赤・青の網掛け (x: 20..30, y: 5..15)
        for y in range(5, 15):
            for x in range(20, 25):
                img.setPixelColor(x, y, QColor(250, 150, 150))
            for x in range(25, 30):
                img.setPixelColor(x, y, QColor(145, 185, 250))

        # 3) 孤立した1pxの黒点ノイズ (35, 35)
        img.setPixelColor(35, 35, QColor(40, 40, 40))

        # 4) 実際の黒文字を模した太い黒ブロック (x: 10..20, y: 30..40)
        for y in range(30, 40):
            for x in range(10, 20):
                img.setPixelColor(x, y, QColor(20, 20, 20))

        # 5) 濃い赤文字を模したブロック (x: 42..50, y: 30..40)
        for y in range(30, 40):
            for x in range(42, 50):
                img.setPixelColor(x, y, QColor(125, 10, 10))

        cleaned = clean_scanned_qimage(img, despeckle_level=1, binarize=True, keep_dark_color_text=False)
        # 黄色・赤・青の背景、および孤立1pxノイズはすべて白(255)に消去されていること
        self.assertEqual(cleaned.pixelColor(10, 10).red(), 255)
        self.assertEqual(cleaned.pixelColor(22, 10).red(), 255)
        self.assertEqual(cleaned.pixelColor(27, 10).red(), 255)
        self.assertEqual(cleaned.pixelColor(35, 35).red(), 255)
        # 黒文字の中心は黒(0)として残っていること
        self.assertEqual(cleaned.pixelColor(15, 35).red(), 0)

        # 色文字保持モードでは濃い赤文字も黒(0)として保持されること
        cleaned_keep = clean_scanned_qimage(img, despeckle_level=1, binarize=True, keep_dark_color_text=True)
        self.assertEqual(cleaned_keep.pixelColor(10, 10).red(), 255)
        self.assertEqual(cleaned_keep.pixelColor(46, 35).red(), 0)

    def test_v21_ocr_numeric_normalization_and_chunk_merging(self):
        from core.ocr_cleaner import (
            _merge_ocr_line_words_into_chunks,
            _normalize_numeric_ocr_misreads,
        )

        self.assertEqual(_normalize_numeric_ocr_misreads("¥12,34O"), "¥12,340")
        self.assertEqual(_normalize_numeric_ocr_misreads("¥1O,OOO円"), "¥10,000円")
        self.assertEqual(_normalize_numeric_ocr_misreads("(I,2OO)"), "(1,200)")
        self.assertEqual(_normalize_numeric_ocr_misreads("OIL"), "OIL")

        words = [
            {"t": "セ", "x": 10, "y": 10, "w": 12, "h": 14},
            {"t": "ゾ", "x": 23, "y": 10, "w": 12, "h": 14},
            {"t": "ン", "x": 36, "y": 10, "w": 12, "h": 14},
            {"t": "¥1", "x": 150, "y": 10, "w": 16, "h": 14},
            {"t": "2,3", "x": 167, "y": 10, "w": 20, "h": 14},
            {"t": "4O", "x": 188, "y": 10, "w": 16, "h": 14},
        ]
        chunks = _merge_ocr_line_words_into_chunks(words, fix_numeric=True)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["text"], "セゾン")
        self.assertEqual(chunks[1]["text"], "¥12,340")

    def test_v21_replace_pages_content_command_and_mode_sync(self):
        from PyQt6.QtWidgets import QApplication
        from core.commands import ReplacePagesContentCommand
        from ui.main_window import MainWindow

        _app = QApplication.instance() or QApplication([])
        win = MainWindow()
        try:
            # モードアクションがハードコードインデックス無しで同期されることを検証
            for target_m in ("view", "crop", "draw", "highlight", "type", "text_erase"):
                win.set_mode(target_m)
                checked_act = win.mode_group.checkedAction()
                self.assertIsNotNone(checked_act)
                self.assertEqual(checked_act.data(), target_m)

            # ReplacePagesContentCommand の Undo / Redo / Dispose 検証
            orig_doc = fitz.open()
            p0 = orig_doc.new_page(width=200, height=200)
            p0.insert_text(fitz.Point(20, 40), "BEFORE_OCR", fontsize=12)

            win.viewer.load_pdf(orig_doc, 0, fit_view=False)
            win.viewer.page_annotations[0] = [{"type": "highlight", "rect": (10, 10, 50, 30)}]

            after_doc = fitz.open()
            p_new = after_doc.new_page(width=200, height=200)
            p_new.insert_text(fitz.Point(20, 40), "AFTER_OCR", fontsize=12)

            cmd = ReplacePagesContentCommand(
                doc=win.viewer.current_pdf,
                page_indices=[0],
                new_pages_doc=after_doc,
                page_annotations=win.viewer.page_annotations,
                prev_page_idx=0,
                take_ownership=True,
            )
            cmd.redo(win.viewer)
            self.assertIn("AFTER_OCR", win.viewer.current_pdf[0].get_text("text"))

            cmd.undo(win.viewer)
            self.assertIn("BEFORE_OCR", win.viewer.current_pdf[0].get_text("text"))
            self.assertEqual(len(win.viewer.page_annotations[0]), 1)

            cmd.dispose()
            self.assertIsNone(cmd.backup_doc)
            self.assertIsNone(cmd.new_doc)
        finally:
            win.close()

    def test_v22_rotated_page_ocr_and_status_messages(self):
        from PyQt6.QtGui import QColor, QImage
        from PyQt6.QtWidgets import QApplication
        from core.ocr_cleaner import apply_cleaned_image_and_ocr_to_page
        from ui.main_window import MainWindow

        _app = QApplication.instance() or QApplication([])

        doc = fitz.open()
        page = doc.new_page(width=400, height=200)
        page.set_rotation(90)

        dummy_img = QImage(200, 400, QImage.Format.Format_RGB32)
        dummy_img.fill(QColor(255, 255, 255))
        ocr_chunks = [
            {"text": "セゾン明細12340", "bbox": (20.0, 50.0, 120.0, 70.0)},
        ]

        apply_cleaned_image_and_ocr_to_page(
            doc=doc,
            page_idx=0,
            cleaned_qimg=dummy_img,
            ocr_chunks=ocr_chunks,
            ocr_img_size=(200, 400),
            apply_cleaned_visual=True,
        )
        self.assertEqual(doc[0].rotation, 90)
        self.assertEqual(doc[0].rect.width, 200.0)
        self.assertEqual(doc[0].rect.height, 400.0)
        extracted = doc[0].get_text("text").replace("\u0332", "_")
        self.assertIn("セゾン明細12340", extracted)
        hits = doc[0].search_for("セゾン明細12340")
        self.assertGreaterEqual(len(hits), 1)
        doc.close()

        win = MainWindow()
        try:
            for m in ("view", "crop", "draw", "highlight", "highlight_erase", "box_erase", "erase", "type", "text_erase"):
                win.set_mode(m)
                status_text = win.statusBar().currentMessage()
                self.assertNotIn(f"現在のモード: {m}", status_text)
        finally:
            win.close()


    def test_v23_100pt_improvements(self):
        import os
        import tempfile
        from PyQt6.QtWidgets import QApplication
        from core import (
            build_pdf_token_replacement_annotation,
            build_text_annotation,
            plan_text_annotation_removal,
        )
        from core.commands import ReplacePagesContentCommand
        from core.geometry import MAX_RENDER_PIXELS, clamp_render_scales
        from core.ocr_cleaner import cleanup_stale_ocr_temp_dirs
        from ui.main_window import MainWindow

        _app = QApplication.instance() or QApplication([])

        # 1) 大判ページの最大ピクセル数クランプ検証
        sx, sy = clamp_render_scales(2400.0, 3400.0, 8.0, 8.0)
        self.assertLessEqual((2400.0 * sx) * (3400.0 * sy), MAX_RENDER_PIXELS * 1.01)

        # 2) 共通注釈ヘルパーの検証
        tok = {
            "text": "1,000",
            "pos": (10.0, 20.0),
            "baseline_pos": (10.0, 32.0),
            "fontsize": 12.0,
            "color": (0.0, 0.0, 0.0),
            "rotation": 0,
            "raw_rect": (10.0, 18.0, 50.0, 34.0),
        }
        rep_ann = build_pdf_token_replacement_annotation(tok, new_text="2,000", updated_after=3)
        self.assertEqual(rep_ann["text"], "2,000")
        self.assertEqual(rep_ann["redact_rect"], (10.0, 18.0, 50.0, 34.0))

        new_ann = build_text_annotation("新規", (5.0, 5.0), (5.0, 15.0), 14.0, (1.0, 0.0, 0.0), 90)
        ann_list = [rep_ann, new_ann]
        act0, _, red0 = plan_text_annotation_removal(ann_list, 0)
        self.assertEqual(act0, "replace_with_redact")
        self.assertEqual(red0["type"], "text_redact")
        act1, _, _ = plan_text_annotation_removal(ann_list, 1)
        self.assertEqual(act1, "pop")

        # 3) 元々注釈が空だったページにOCR適用→注釈追加→Undoした際に注釈がクリアされるか検証
        win = MainWindow()
        try:
            doc = fitz.open()
            doc.new_page(width=200, height=200)
            win.viewer.load_pdf(doc, 0, fit_view=False)
            self.assertFalse(win.viewer.page_annotations.get(0))

            after_doc = fitz.open()
            after_doc.new_page(width=200, height=200)
            cmd = ReplacePagesContentCommand(
                doc=win.viewer.current_pdf,
                page_indices=[0],
                new_pages_doc=after_doc,
                page_annotations=win.viewer.page_annotations,
                prev_page_idx=0,
                take_ownership=True,
            )
            cmd.redo(win.viewer)
            win.viewer.page_annotations[0] = [{"type": "highlight", "rect": (10, 10, 40, 20)}]
            cmd.undo(win.viewer)
            self.assertFalse(win.viewer.page_annotations.get(0))
            cmd.dispose()
        finally:
            win.close()

        # 4) 古いOCR一時ディレクトリの自動掃除検証
        stale_dir = tempfile.mkdtemp(prefix="secure_pdf_ocr_")
        self.assertTrue(os.path.isdir(stale_dir))
        cleanup_stale_ocr_temp_dirs(max_age_sec=0.0)
        self.assertFalse(os.path.exists(stale_dir))



# --- Audit regression tests (2026-11-13 fixes, coordinator-merged) ---
# The three area subagents' /tmp test files were not visible from the
# coordinator session (isolated /tmp), so equivalent regression tests were
# recreated here from the audits and verified by running this suite.


class _AuditFakeSignal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _AuditFakeViewer:
    """Minimal stand-in for PDFViewer, enough for the command classes."""

    def __init__(self, doc, page_annotations=None):
        self.current_pdf = doc
        self.page_annotations = page_annotations if page_annotations is not None else {}
        self.current_page_idx = 0
        self.pageSelectionChanged = _AuditFakeSignal()
        self.rendered = []

    def render_page(self, fit_view=False, rebuild_base=False):
        self.rendered.append((fit_view, rebuild_base))

    def notify_pages_updated(self, idxs):
        pass

    def cancel_active_interaction(self):
        pass

    def clear_calc_selection(self):
        pass

    def load_pdf(self, doc, page_idx=0, fit_view=True):
        self.current_pdf = doc
        n = len(doc)
        self.current_page_idx = max(0, min(int(page_idx), n - 1)) if n else 0


def _audit_make_doc(pages=1, text="hello"):
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=400, height=600)
        if text:
            page.insert_text(fitz.Point(20, 50), text, fontsize=12)
    return doc


class TestAuditFixes2026(unittest.TestCase):
    # OCR #1: numeric normalizer must not corrupt English words.
    def test_ocr_normalizer_keeps_english_words(self):
        from core.ocr_cleaner import _normalize_numeric_ocr_misreads

        self.assertEqual(_normalize_numeric_ocr_misreads("hello"), "hello")
        self.assertEqual(_normalize_numeric_ocr_misreads("lollipop"), "lollipop")
        self.assertEqual(_normalize_numeric_ocr_misreads("Bill"), "Bill")
        self.assertEqual(_normalize_numeric_ocr_misreads("¥12,34O"), "¥12,340")
        self.assertEqual(_normalize_numeric_ocr_misreads("¥1O,OOO円"), "¥10,000円")

    # OCR #2: dark red text survives the default colour clean.
    def test_dark_red_text_survives_default_clean(self):
        from PyQt6.QtGui import QColor, QImage
        from PyQt6.QtWidgets import QApplication
        from core.ocr_cleaner import clean_scanned_qimage

        QApplication.instance() or QApplication([])
        img = QImage(60, 60, QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))
        for y in range(30, 40):
            for x in range(42, 50):
                img.setPixelColor(x, y, QColor(125, 10, 10))
        cleaned = clean_scanned_qimage(img)
        self.assertEqual(cleaned.pixelColor(46, 35).red(), 0)

    # OCR #3: a 1px vertical line survives despeckle level 2.
    def test_thin_line_survives_despeckle_level2(self):
        from PyQt6.QtGui import QColor, QImage
        from PyQt6.QtWidgets import QApplication
        from core.ocr_cleaner import clean_scanned_qimage

        QApplication.instance() or QApplication([])
        img = QImage(60, 60, QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))
        for y in range(10, 50):
            img.setPixelColor(30, y, QColor(20, 20, 20))
        cleaned = clean_scanned_qimage(img, despeckle_level=2)
        self.assertEqual(cleaned.pixelColor(30, 30).red(), 0)

    # OCR #5/#14: amount columns must not merge; words must sort by x.
    def test_chunk_merge_amount_columns_stay_separate(self):
        from core.ocr_cleaner import _merge_ocr_line_words_into_chunks

        words = [
            {"t": "1,000", "x": 10, "y": 10, "w": 50, "h": 24},
            {"t": "2,000", "x": 78, "y": 10, "w": 50, "h": 24},
        ]
        chunks = _merge_ocr_line_words_into_chunks(words)
        self.assertEqual([c["text"] for c in chunks], ["1,000", "2,000"])

    def test_chunk_merge_sorts_words_by_position(self):
        from core.ocr_cleaner import _merge_ocr_line_words_into_chunks

        words = [
            {"t": "B", "x": 100, "y": 10, "w": 12, "h": 14},
            {"t": "A", "x": 10, "y": 10, "w": 12, "h": 14},
        ]
        chunks = _merge_ocr_line_words_into_chunks(words)
        self.assertEqual(chunks[0]["text"], "A")

    # Core #1: snapshot undo restores the page list verbatim.
    def test_remove_undo_restores_list_verbatim_with_snapshot(self):
        from core.commands import RemoveAnnotationCommand, RemoveMultipleAnnotationsCommand

        anns = [
            {"type": "text", "text": "A", "pos": (10.0, 20.0), "fontsize": 12.0, "updated_after": 3},
            {"type": "highlight", "rect": (10.0, 30.0, 90.0, 50.0)},
            {"type": "draw", "points": [(10.0, 10.0), (90.0, 10.0)], "width": 2.0},
        ]
        before = copy.deepcopy(anns)
        viewer = _AuditFakeViewer(_audit_make_doc(), {0: copy.deepcopy(anns)})
        cmd = RemoveAnnotationCommand(0, 1, anns[1], before_annotations=before)
        viewer.page_annotations[0] = [anns[0], anns[2]]
        cmd.undo(viewer)
        self.assertEqual(viewer.page_annotations[0], before)

        viewer2 = _AuditFakeViewer(_audit_make_doc(), {0: copy.deepcopy(anns)})
        cmd2 = RemoveMultipleAnnotationsCommand(
            0, [(1, anns[1]), (2, anns[2])], before_annotations=before
        )
        viewer2.page_annotations[0] = [anns[0]]
        cmd2.undo(viewer2)
        self.assertEqual(viewer2.page_annotations[0], before)

    # Core #2/#3: bake validates before mutating; rotation/crop restored.
    def test_bake_validation_rejects_bad_highlight_and_keeps_rotation(self):
        from core.pdf_engine import PDFDocumentManager

        doc = _audit_make_doc()
        doc[0].set_rotation(90)
        self.assertRaises(
            ValueError,
            PDFDocumentManager.bake_annotations_to_doc,
            doc, {0: [{"type": "highlight", "color": (1.0, 0.0, 0.0)}]},
        )
        self.assertEqual(doc[0].rotation, 90)
        doc.close()

    def test_extract_pages_propagates_bake_error(self):
        from core.pdf_engine import PDFDocumentManager

        doc = _audit_make_doc()
        with self.assertRaises(ValueError):
            PDFDocumentManager.extract_pages_as_doc(
                doc, [0], {0: [{"type": "highlight"}]}
            )
        doc.close()

    # Core #5: evicting the saved state marks the doc unsaved.
    def test_history_eviction_marks_saved_state_unreachable(self):
        from core.commands import BaseCommand, HistoryManager

        class DummyCmd(BaseCommand):
            def undo(self, viewer):
                pass

            def redo(self, viewer):
                pass

        hm = HistoryManager(max_steps=2)
        hm.push(DummyCmd())
        hm.mark_saved()
        self.assertFalse(hm.is_modified)
        hm.push(DummyCmd())
        hm.push(DummyCmd())
        self.assertTrue(hm.is_modified)

    # Core #7/#8: 0-page save and encrypted-strip guards.
    def test_save_safely_zero_page_document(self):
        from core.pdf_engine import PDFDocumentManager

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "out.pdf")
            _, err = PDFDocumentManager.save_safely(fitz.open(), {}, target, None)
            self.assertIsInstance(err, ValueError)
            self.assertFalse(os.path.exists(target))

    def test_save_safely_refuses_encrypted_unless_allowed(self):
        from core.pdf_engine import PDFDocumentManager

        with tempfile.TemporaryDirectory() as tmp:
            src_path = os.path.join(tmp, "enc.pdf")
            enc = _audit_make_doc()
            enc.save(
                src_path,
                encryption=fitz.PDF_ENCRYPT_RC4_128,
                user_pw="pw",
                owner_pw="pw",
            )
            enc.close()
            doc = fitz.open(src_path)
            doc.authenticate("pw")
            target = os.path.join(tmp, "out.pdf")
            _, err = PDFDocumentManager.save_safely(doc, {}, target, None)
            self.assertIsInstance(err, ValueError)
            self.assertFalse(os.path.exists(target))
            _, err2 = PDFDocumentManager.save_safely(
                doc, {}, target, None, allow_strip_encryption=True
            )
            self.assertIsNone(err2)
            self.assertTrue(os.path.exists(target))
            doc.close()

    # Core #12: overlapping erase rects must not double-count.
    def test_is_rect_substantially_erased_union_area(self):
        from core import is_rect_substantially_erased

        target = fitz.Rect(0, 0, 100, 20)
        same_rect_twice = [fitz.Rect(0, 0, 30, 20), fitz.Rect(0, 0, 30, 20)]
        self.assertFalse(is_rect_substantially_erased(target, same_rect_twice))

    # Core #11: insert count is always derived from the inserted doc.
    def test_insert_pages_rejects_mismatched_count(self):
        from core.commands import InsertPagesCommand

        src = _audit_make_doc(pages=2)
        with self.assertRaises(ValueError):
            InsertPagesCommand(0, src, 1, 0)
        src.close()

    # Core #9: folder merge reports skipped files.
    def test_folder_merge_reports_skipped_files(self):
        from core.pdf_loader import load_folder_as_merged_document

        with tempfile.TemporaryDirectory() as tmp:
            good = _audit_make_doc()
            good.save(os.path.join(tmp, "01_good.pdf"))
            good.close()
            with open(os.path.join(tmp, "02_corrupt.pdf"), "wb") as f:
                f.write(b"not a pdf at all")
            doc, manifest, skipped = load_folder_as_merged_document(tmp)
            self.assertIsNotNone(doc)
            self.assertEqual(len(manifest), 1)
            self.assertEqual(len(skipped), 1)
            self.assertIn("02_corrupt.pdf", skipped[0][0])
            doc.close()

    # Core #14: default-app command points at the existing .py file.
    def test_windows_open_command_points_at_existing_py(self):
        from core.geometry import build_windows_open_command

        cmd, icon = build_windows_open_command()
        self.assertIn("secure_local_pdf_editor.py", cmd)
        self.assertNotIn(".pyw", cmd)
        script_path = os.path.join(
            str(Path(__file__).resolve().parent.parent),
            "secure_local_pdf_editor.py",
        )
        self.assertTrue(os.path.isfile(script_path))
        with self.assertRaises(FileNotFoundError):
            build_windows_open_command("/nonexistent/definitely_missing.py")


class TestUiAuditFixes2026(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.main_window import MainWindow

        self.window = MainWindow()
        self.window.show()
        self._docs = []

    def tearDown(self):
        try:
            self.window.page_manager._thumb_timer.stop()
        except Exception:
            pass
        try:
            self.window.viewer.current_pdf = None
        except Exception:
            pass
        try:
            self.window.viewer.history.mark_saved()
        except Exception:
            pass
        try:
            self.window.close()
        except Exception:
            pass
        self.window = None
        for doc in self._docs:
            try:
                doc.close()
            except Exception:
                pass
        self._docs.clear()

    def _track(self, doc):
        self._docs.append(doc)
        return doc

    # UI #3: empty-doc load must not leave dangling scene items.
    def test_empty_doc_load_pdf_clears_scene_items(self):
        viewer = self.window.viewer
        viewer.load_pdf(self._track(_audit_make_doc()), 0, fit_view=True)
        viewer.load_pdf(self._track(fitz.open()), 0, fit_view=True)
        self.assertIsNone(viewer._base_pixmap_item)
        self.assertIsNone(viewer.page_rect)
        viewer.resize(800, 600)

    # UI #13: recent files prune deleted entries.
    def test_recent_files_prune_deleted(self):
        from PyQt6.QtCore import QSettings

        settings = QSettings("SecureLocalPDFEditor", "SecureLocalPDFEditor")
        settings.setValue(
            "recent_files",
            ["/nonexistent/definitely_deleted.pdf"],
        )
        settings.sync()
        try:
            self.assertEqual(self.window._get_recent_files(), [])
        finally:
            settings.setValue("recent_files", [])
            settings.sync()

    # Core #1 end-to-end: viewer remove + undo restores the list verbatim.
    def test_viewer_remove_highlight_undo_restores_list(self):
        viewer = self.window.viewer
        viewer.load_pdf(self._track(_audit_make_doc()), 0, fit_view=True)
        before = [
            {"type": "text", "text": "A", "pos": (10.0, 20.0),
             "baseline_pos": (10.0, 32.0), "fontsize": 12.0, "updated_after": 3},
            {"type": "highlight", "rect": (10.0, 30.0, 90.0, 50.0)},
        ]
        viewer.page_annotations[0] = copy.deepcopy(before)
        self.assertTrue(viewer.remove_highlight_annotation_at(1))
        self.assertEqual(len(viewer.page_annotations[0]), 1)
        viewer.undo()
        self.assertEqual(viewer.page_annotations[0], before)

if __name__ == "__main__":
    unittest.main()
