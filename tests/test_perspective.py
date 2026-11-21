# -*- coding: utf-8 -*-
"""多機能版「台形補正」に対するテスト。

- core: core.photo_to_pdf.build_corrected_image_page_doc / page_render_dpi
  （台形の四隅 → 補正後のページ寸法・アスペクト、画像のみになること）
- command: PerspectiveCorrectPageCommand（差し替え・注釈の破棄/復元・Undo/Redo）
- UI: PerspectiveCorrectionDialog のスモーク（自動検出・ビルド）と
  PageManager.perspective_correct_page の配線
"""
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz


def _ensure_app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _make_trapezoid_qimage():
    """暗い背景に白い台形（歪んだ書類）を描いた合成画像と、その四隅。"""
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter, QPolygonF

    _ensure_app()
    img = QImage(800, 600, QImage.Format.Format_RGB32)
    img.fill(QColor(45, 45, 50))
    corners = [(190.0, 130.0), (650.0, 185.0), (605.0, 480.0), (150.0, 415.0)]
    painter = QPainter(img)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(240, 240, 235))
    painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in corners]))
    painter.end()
    return img, corners


def _edge(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class TestPageRenderDpi(unittest.TestCase):
    def test_a4_uses_target_dpi(self):
        from core.photo_to_pdf import page_render_dpi

        self.assertAlmostEqual(page_render_dpi(595.28, 841.89), 200.0)

    def test_huge_page_is_capped(self):
        from core.photo_to_pdf import PAGE_RENDER_MAX_DIM, page_render_dpi

        dpi = page_render_dpi(4000.0, 4000.0)
        self.assertLess(dpi, 200.0)
        self.assertAlmostEqual(4000.0 * dpi / 72.0, PAGE_RENDER_MAX_DIM,
                               places=3)


class TestBuildCorrectedImagePageDoc(unittest.TestCase):
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

    def test_trapezoid_corrected_aspect_and_size(self):
        from core.photo_to_pdf import build_corrected_image_page_doc

        img, quad = _make_trapezoid_qimage()
        dpi = 144.0
        doc = self._track(build_corrected_image_page_doc(img, quad, dpi))
        self.assertEqual(len(doc), 1)
        page = doc[0]

        # 補正後の寸法は「対向辺の長い方」を dpi で pt 換算したもの
        exp_w = max(_edge(quad[0], quad[1]), _edge(quad[3], quad[2]))
        exp_h = max(_edge(quad[0], quad[3]), _edge(quad[1], quad[2]))
        self.assertAlmostEqual(page.rect.width, exp_w * 72.0 / dpi, delta=1.0)
        self.assertAlmostEqual(page.rect.height, exp_h * 72.0 / dpi, delta=1.0)
        # 台形（上が長く下が短い・左右が斜め）でも長方形らしい比率になる
        self.assertAlmostEqual(
            page.rect.width / page.rect.height, exp_w / exp_h, delta=0.03)

        # 画像だけのページになり、文字データは無い
        self.assertTrue(page.get_images())
        self.assertEqual(page.get_text().strip(), "")

    def test_full_frame_quad_keeps_size(self):
        from core.photo_to_pdf import build_corrected_image_page_doc

        img, _ = _make_trapezoid_qimage()
        w, h = img.width(), img.height()
        quad = [(0.0, 0.0), (float(w), 0.0), (float(w), float(h)), (0.0, float(h))]
        doc = self._track(build_corrected_image_page_doc(img, quad, 72.0))
        page = doc[0]
        self.assertAlmostEqual(page.rect.width, float(w), delta=1.0)
        self.assertAlmostEqual(page.rect.height, float(h), delta=1.0)

    def test_invalid_quad_raises(self):
        from core.photo_to_pdf import build_corrected_image_page_doc

        img, _ = _make_trapezoid_qimage()
        with self.assertRaises(ValueError):
            build_corrected_image_page_doc(img, [(0, 0), (10, 0), (5, 5)], 144.0)
        # 自己交差する四隅（蝶ネクタイ型）
        with self.assertRaises(ValueError):
            build_corrected_image_page_doc(
                img, [(0, 0), (100, 100), (100, 0), (0, 100)], 144.0)


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

    def _build_corrected_for_page(self, page_idx, dpi=120.0):
        """対象ページを画像化し、内側9割の四隅で補正した文書を作る。"""
        from core.geometry import FITZ_LOCK
        from core.photo_to_pdf import build_corrected_image_page_doc

        viewer = self.window.viewer
        page = viewer.current_pdf[page_idx]
        with FITZ_LOCK:
            pix = page.get_pixmap(
                matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                colorspace=fitz.csRGB, alpha=False)
        from PyQt6.QtGui import QImage

        img = QImage(
            pix.samples, pix.width, pix.height, pix.stride,
            QImage.Format.Format_RGB888).copy()
        w, h = img.width(), img.height()
        quad = [(w * 0.05, h * 0.05), (w * 0.95, h * 0.05),
                (w * 0.95, h * 0.95), (w * 0.05, h * 0.95)]
        return build_corrected_image_page_doc(img, quad, dpi)


class TestPerspectiveCommandViaWindow(_WindowTestBase):
    def _run_command(self, page_idx):
        from core.commands import PerspectiveCorrectPageCommand

        viewer = self.window.viewer
        corrected = self._build_corrected_for_page(page_idx)
        cmd = PerspectiveCorrectPageCommand(
            viewer.current_pdf, page_idx, corrected,
            viewer.page_annotations, viewer.current_page_idx,
            take_ownership=True)
        cmd.redo(viewer)
        viewer.history.push(cmd)
        self._app.processEvents()
        return cmd

    def test_correct_and_undo_redo(self):
        src = self._write_pdf("src.pdf", ["PAGE_ONE", "PAGE_TWO", "PAGE_THREE"])
        self._open(src)
        viewer = self.window.viewer
        doc = viewer.current_pdf
        self.assertEqual(len(doc), 3)
        orig_size = (doc[1].rect.width, doc[1].rect.height)

        self._run_command(1)
        doc = viewer.current_pdf
        # ページ数は変わらず、対象ページだけが画像ページに差し替わる
        self.assertEqual(len(doc), 3)
        self.assertIn("PAGE_ONE", doc[0].get_text())
        self.assertIn("PAGE_THREE", doc[2].get_text())
        corrected = doc[1]
        self.assertEqual(corrected.get_text().strip(), "")
        self.assertTrue(corrected.get_images())
        # 四隅は内側9割なので、ページは元より少し小さくなる
        self.assertLess(corrected.rect.width, orig_size[0])
        self.assertGreater(corrected.rect.width, orig_size[0] * 0.8)

        viewer.history.undo(viewer)
        self._app.processEvents()
        doc = viewer.current_pdf
        self.assertIn("PAGE_TWO", doc[1].get_text())
        self.assertAlmostEqual(doc[1].rect.width, orig_size[0], places=3)
        self.assertAlmostEqual(doc[1].rect.height, orig_size[1], places=3)

        viewer.history.redo(viewer)
        self._app.processEvents()
        self.assertEqual(viewer.current_pdf[1].get_text().strip(), "")

    def test_annotations_discarded_and_restored(self):
        src = self._write_pdf("src.pdf", ["PAGE_ONE", "PAGE_TWO", "PAGE_THREE"])
        self._open(src)
        viewer = self.window.viewer
        viewer.page_annotations[1] = [
            {"type": "highlight", "rect": (10.0, 10.0, 50.0, 30.0)}]
        viewer.page_annotations[2] = [{
            "type": "text", "text": "LATER_NOTE",
            "pos": (20.0, 100.0), "fontsize": 14.0,
        }]

        self._run_command(1)
        # 補正したページの注釈は焼き込み規約で破棄、他ページは不変
        self.assertFalse(viewer.page_annotations.get(1))
        self.assertEqual(viewer.page_annotations[2][0]["text"], "LATER_NOTE")

        viewer.history.undo(viewer)
        self._app.processEvents()
        self.assertEqual(len(viewer.page_annotations[1]), 1)
        self.assertEqual(
            viewer.page_annotations[1][0]["type"], "highlight")
        self.assertEqual(viewer.page_annotations[2][0]["text"], "LATER_NOTE")


def _write_trapezoid_pdf(path):
    """暗いページに、少し回転した白い長方形（書類）を描いたPDFを作る。

    戻り値: 書類の四隅（pt、左上・右上・右下・左下の順）。
    """
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    shape = page.new_shape()
    shape.draw_rect(page.rect)
    shape.finish(color=(0.25, 0.25, 0.28), fill=(0.25, 0.25, 0.28),
                 closePath=True)
    cx, cy = 200.0, 150.0
    ang = math.radians(10.0)
    cosr, sinr = math.cos(ang), math.sin(ang)
    corners = []
    for lx, ly in ((-110.0, -70.0), (110.0, -70.0),
                   (110.0, 70.0), (-110.0, 70.0)):
        corners.append((
            cx + lx * cosr - ly * sinr,
            cy + lx * sinr + ly * cosr))
    shape.draw_polyline([fitz.Point(x, y) for x, y in corners])
    shape.finish(color=(0.96, 0.96, 0.94), fill=(0.96, 0.96, 0.94),
                 closePath=True)
    shape.commit()
    try:
        Path(path).write_bytes(doc.tobytes())
    finally:
        doc.close()
    return corners


class TestPerspectiveDialogSmoke(_WindowTestBase):
    def test_dialog_detects_and_builds(self):
        from ui.perspective_dialog import PerspectiveCorrectionDialog

        path = Path(self._tmp.name) / "trap.pdf"
        truth_pt = _write_trapezoid_pdf(path)
        self._open(str(path))
        viewer = self.window.viewer

        dialog = PerspectiveCorrectionDialog(
            self.window, viewer.current_pdf, 0, viewer.page_annotations)
        try:
            dialog.show()
            self._app.processEvents()
            # 暗い背景＋明るい書類なので自動検出できるはず
            self.assertTrue(dialog.detected)
            scale = dialog._dpi / 72.0
            quad = dialog.quad
            self.assertIsNotNone(quad)
            for (gx, gy), (tx, ty) in zip(quad, truth_pt):
                self.assertAlmostEqual(gx, tx * scale, delta=45.0)
                self.assertAlmostEqual(gy, ty * scale, delta=45.0)

            corrected = dialog.build_corrected_doc()
            try:
                self.assertEqual(len(corrected), 1)
                page = corrected[0]
                # 書類部分（220x140pt）がおおよその実寸で取り出せる
                self.assertAlmostEqual(page.rect.width, 220.0, delta=30.0)
                self.assertAlmostEqual(page.rect.height, 140.0, delta=30.0)
                self.assertTrue(page.get_images())
                self.assertEqual(page.get_text().strip(), "")
            finally:
                corrected.close()
        finally:
            dialog.close()
            dialog.dispose()

    def test_dialog_default_quad_when_not_detected(self):
        from ui.perspective_dialog import PerspectiveCorrectionDialog

        # 文字だけのページ（書類らしい塊がない）でもダイアログは開き、
        # 四隅の初期値（ページ全体）が入り、手動で補正をビルドできる
        src = self._write_pdf("plain.pdf", ["ONLY_TEXT"])
        self._open(src)
        viewer = self.window.viewer

        dialog = PerspectiveCorrectionDialog(
            self.window, viewer.current_pdf, 0, viewer.page_annotations)
        try:
            dialog.show()
            self._app.processEvents()
            self.assertIsNotNone(dialog.quad)
            corrected = dialog.build_corrected_doc()
            try:
                self.assertEqual(len(corrected), 1)
                self.assertTrue(corrected[0].get_images())
            finally:
                corrected.close()
        finally:
            dialog.close()
            dialog.dispose()


class TestPageManagerPerspectiveWiring(_WindowTestBase):
    def test_perspective_correct_page_via_page_manager(self):
        from PyQt6.QtWidgets import QDialog

        src = self._write_pdf("src.pdf", ["PAGE_ONE", "PAGE_TWO", "PAGE_THREE"])
        self._open(src)
        pm = self.window.page_manager
        viewer = self.window.viewer

        outer = self

        class _FakeDialog:
            def __init__(self, parent, doc, page_idx, annots=None):
                self._page_idx = page_idx

            def exec(self):
                return QDialog.DialogCode.Accepted

            def build_corrected_doc(self):
                return outer._build_corrected_for_page(self._page_idx)

            def dispose(self):
                pass

        pm.list_widget.clearSelection()
        pm.list_widget.item(1).setSelected(True)

        with mock.patch(
                "ui.perspective_dialog.PerspectiveCorrectionDialog",
                _FakeDialog):
            pm.perspective_correct_page()
        self._app.processEvents()

        doc = viewer.current_pdf
        self.assertEqual(len(doc), 3)
        self.assertIn("PAGE_ONE", doc[0].get_text())
        self.assertEqual(doc[1].get_text().strip(), "")
        self.assertTrue(doc[1].get_images())
        self.assertIn("PAGE_THREE", doc[2].get_text())

        # ページ管理からの実行も Undo できる
        viewer.history.undo(viewer)
        self._app.processEvents()
        self.assertIn("PAGE_TWO", viewer.current_pdf[1].get_text())


if __name__ == "__main__":
    unittest.main()
