# -*- coding: utf-8 -*-
"""写真をPDF化（多機能版）の単体テスト。

合成画像（暗い背景＋回転した白い長方形＝書類）で、
四隅の自動検出・透視変換・PDF化を確認する。
"""
import math
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


def _ensure_app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def make_photo_image(angle_deg=12.0, doc_w=460, doc_h=320, size=(800, 600)):
    """暗い背景に、少し回転した白い長方形（書類）を描いた合成写真。

    戻り値: (QImage, 真の四隅 [(x, y) x4] 左上・右上・右下・左下の順)
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QImage, QPainter

    _ensure_app()
    w, h = size
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(50, 50, 55))
    cx, cy = w / 2, h / 2
    painter = QPainter(img)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(245, 245, 240))
    painter.translate(cx, cy)
    painter.rotate(angle_deg)
    painter.drawRect(int(-doc_w / 2), int(-doc_h / 2), doc_w, doc_h)
    painter.end()
    rad = math.radians(angle_deg)
    cosr, sinr = math.cos(rad), math.sin(rad)
    corners = []
    for lx, ly in ((-doc_w / 2, -doc_h / 2), (doc_w / 2, -doc_h / 2),
                   (doc_w / 2, doc_h / 2), (-doc_w / 2, doc_h / 2)):
        corners.append((cx + lx * cosr - ly * sinr,
                        cy + lx * sinr + ly * cosr))
    return img, corners


def make_trapezoid_image():
    """台形（透視がかかった書類）の合成写真。戻り値: (QImage, 四隅)"""
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


def _corner_errors(detected, truth):
    return [math.hypot(d[0] - t[0], d[1] - t[1])
            for d, t in zip(detected, truth)]


class TestDetectDocumentQuad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = _ensure_app()

    def test_detect_rotated_rectangle(self):
        from core.photo_to_pdf import detect_document_quad

        img, truth = make_photo_image(angle_deg=12.0)
        quad = detect_document_quad(img)
        self.assertIsNotNone(quad, "回転した書類を検出できませんでした")
        errors = _corner_errors(quad, truth)
        self.assertTrue(
            all(e < 30 for e in errors),
            f"四隅の誤差が大きすぎます: {[round(e, 1) for e in errors]}")

    def test_detect_trapezoid(self):
        from core.photo_to_pdf import detect_document_quad

        img, truth = make_trapezoid_image()
        quad = detect_document_quad(img)
        self.assertIsNotNone(quad, "台形の書類を検出できませんでした")
        errors = _corner_errors(quad, truth)
        self.assertTrue(
            all(e < 30 for e in errors),
            f"四隅の誤差が大きすぎます: {[round(e, 1) for e in errors]}")

    def test_detect_fails_on_uniform_image(self):
        from PyQt6.QtGui import QColor, QImage

        from core.photo_to_pdf import detect_document_quad

        dark = QImage(400, 300, QImage.Format.Format_RGB32)
        dark.fill(QColor(40, 40, 40))
        self.assertIsNone(detect_document_quad(dark))
        white = QImage(400, 300, QImage.Format.Format_RGB32)
        white.fill(QColor(250, 250, 250))
        self.assertIsNone(detect_document_quad(white))

    def test_detect_fails_on_tiny_document(self):
        """書類が小さすぎる写真は検出しない（誤検出防止）。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QImage, QPainter

        from core.photo_to_pdf import detect_document_quad

        img = QImage(800, 600, QImage.Format.Format_RGB32)
        img.fill(QColor(50, 50, 55))
        painter = QPainter(img)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(245, 245, 240))
        painter.drawRect(350, 250, 60, 40)  # 画面の1%程度
        painter.end()
        self.assertIsNone(detect_document_quad(img))


class TestWarpAndRotate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = _ensure_app()

    def test_warp_recovers_rectangle(self):
        from PyQt6.QtGui import QColor

        from core.photo_to_pdf import warp_perspective

        img, truth = make_photo_image(angle_deg=12.0, doc_w=460, doc_h=320)
        out = warp_perspective(img, truth)
        self.assertAlmostEqual(out.width(), 460, delta=8)
        self.assertAlmostEqual(out.height(), 320, delta=8)
        # 補正後は全体が書類（白っぽい）はず
        center = QColor(out.pixel(230, 160))
        self.assertGreater(center.red(), 200)
        corner = QColor(out.pixel(4, 4))
        self.assertGreater(corner.red(), 180)

    def test_warp_invalid_quad(self):
        from core.photo_to_pdf import warp_perspective

        img, _ = make_photo_image()
        with self.assertRaises(ValueError):
            warp_perspective(img, [(0, 0), (10, 10)])  # 点が足りない
        with self.assertRaises(ValueError):
            warp_perspective(img, [(5, 5), (5, 5), (5, 5), (5, 5)])  # 潰れている

    def test_rotate_90_swaps_size_and_quad(self):
        from core.photo_to_pdf import rotate_image_90, rotate_quad_90

        img, _ = make_photo_image(size=(800, 600))
        rotated = rotate_image_90(img, clockwise=True)
        self.assertEqual((rotated.width(), rotated.height()), (600, 800))
        quad = [(0.0, 0.0), (800.0, 0.0), (800.0, 600.0), (0.0, 600.0)]
        rq = rotate_quad_90(quad, 800, 600, clockwise=True)
        self.assertEqual(rq, [(0.0, 0.0), (600.0, 0.0),
                              (600.0, 800.0), (0.0, 800.0)])
        # 往復で元に戻る
        back = rotate_quad_90(rq, 600, 800, clockwise=False)
        self.assertEqual(back, quad)


class TestPhotoToPdfBytes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = _ensure_app()

    def test_jpeg_bytes(self):
        from core.photo_to_pdf import qimage_to_jpeg_bytes

        img, _ = make_photo_image()
        data = qimage_to_jpeg_bytes(img)
        self.assertTrue(data[:3] == b"\xff\xd8\xff")

    def test_pdf_two_pages_a4(self):
        from PyQt6.QtGui import QColor, QImage

        from core.photo_to_pdf import photos_to_pdf_bytes

        portrait = QImage(300, 400, QImage.Format.Format_RGB32)
        portrait.fill(QColor(200, 200, 200))
        landscape = QImage(500, 300, QImage.Format.Format_RGB32)
        landscape.fill(QColor(180, 180, 180))
        data = photos_to_pdf_bytes([portrait, landscape])
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            self.assertEqual(len(doc), 2)
            p0 = doc[0].rect
            self.assertAlmostEqual(p0.width, 595.28, delta=1.0)
            self.assertAlmostEqual(p0.height, 841.89, delta=1.0)
            p1 = doc[1].rect
            self.assertAlmostEqual(p1.width, 841.89, delta=1.0)
            self.assertAlmostEqual(p1.height, 595.28, delta=1.0)
            for page in doc:
                self.assertEqual(len(page.get_images()), 1)
                # 画像は余白の内側に収まっている
                for img_info in page.get_images(full=True):
                    for rect in page.get_image_rects(img_info[0]):
                        self.assertGreaterEqual(rect.x0, 35.0)
                        self.assertGreaterEqual(rect.y0, 35.0)
        finally:
            doc.close()

    def test_load_photo_image(self):
        from core.photo_to_pdf import load_photo_image

        img, _ = make_photo_image()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "photo.png")
            self.assertTrue(img.save(path, "PNG"))
            loaded = load_photo_image(path)
            self.assertEqual(
                (loaded.width(), loaded.height()), (img.width(), img.height()))
            with self.assertRaises(ValueError):
                load_photo_image(os.path.join(tmp, "nai.png"))


class TestPhotoDialogSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = _ensure_app()

    def test_dialog_add_and_rotate(self):
        from ui.photo_to_pdf_dialog import PhotoToPdfDialog, _PhotoItem
        from core.photo_to_pdf import detect_document_quad, load_photo_image

        img, _truth = make_photo_image()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "photo.png")
            self.assertTrue(img.save(path, "PNG"))
            loaded = load_photo_image(path)
            quad = detect_document_quad(loaded)
            self.assertIsNotNone(quad)

            dlg = PhotoToPdfDialog()
            try:
                dlg._items.append(
                    _PhotoItem(path, loaded, quad, detected=True))
                dlg.list_widget.addItem(Path(path).name)
                dlg.list_widget.setCurrentRow(0)
                self.assertNotEqual(dlg.status_label.text(), "")
                self.assertTrue(dlg.chk_crop.isChecked())
                # 90度回転で画像サイズと四隅が追随する
                dlg._rotate_current(True)
                item = dlg._items[0]
                self.assertEqual(
                    (item.image.width(), item.image.height()), (600, 800))
                dlg._rotate_current(False)
                item = dlg._items[0]
                self.assertEqual(
                    (item.image.width(), item.image.height()), (800, 600))
            finally:
                dlg.close()


if __name__ == "__main__":
    unittest.main()
