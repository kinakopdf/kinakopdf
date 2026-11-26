# -*- coding: utf-8 -*-
"""レシート分割（多機能版）の単体テスト。

合成シート画像（レシートを並べたスキャン相当）を PDF 化し、
暗い背景・明るい背景のどちらでも件数を検出できることと、
1枚分の PDF を作れることを確認する。
"""
import os
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent if (_THIS_DIR.name == "tests" or not (_THIS_DIR / "core").is_dir()) else _THIS_DIR
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz


def _ensure_app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def make_sheet_image(dark_bg, count=6):
    """レシートを2列×3段に並べたシート画像（1654×2339）を作る。

    dark_bg=True: 暗い背景に白いレシート。
    dark_bg=False: 白い背景に、白い紙（見えない）＋黒い文字だけ。
    """
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen

    _ensure_app()
    W, H = 1654, 2339
    img = QImage(W, H, QImage.Format.Format_RGB32)
    img.fill(QColor(38, 38, 42) if dark_bg else QColor(255, 255, 255))
    painter = QPainter(img)
    try:
        # 文字の代わりに黒い帯を描く（この実行環境では Qt のフォント
        # 描画が不安定なため。インクの塊としての性質は文字と同じ）
        ink = QColor(0, 0, 0)
        rw, rh = 540, 700
        xs = [100, 980]
        ys = [100, 880, 1660]
        angles = [-8, 6, 10, -5, 4, -9]
        placed = 0
        for row, y in enumerate(ys):
            for col, x in enumerate(xs):
                if placed >= count:
                    break
                cx, cy = x + rw / 2, y + rh / 2
                painter.save()
                painter.translate(cx, cy)
                painter.rotate(angles[placed % len(angles)])
                painter.translate(-rw / 2, -rh / 2)
                if dark_bg:
                    painter.fillRect(0, 0, rw, rh, QColor(255, 255, 255))
                # 店名（太い帯）
                painter.fillRect(150, 34, 240, 34, ink)
                # 明細行（左の品名帯＋右の金額帯）
                for i in range(8):
                    yy = 118 + i * 58
                    painter.fillRect(30, yy, 230, 24, ink)
                    painter.fillRect(400, yy, 110, 24, ink)
                # 罫線と合計行
                painter.fillRect(20, 606, rw - 40, 4, ink)
                painter.fillRect(30, 636, 110, 32, ink)
                painter.fillRect(350, 636, 160, 32, ink)
                painter.restore()
                placed += 1
    finally:
        painter.end()
    return img


def sheet_pdf_bytes(image):
    """シート画像を A4 1ページの画像だけの PDF にする。"""
    from core.photo_to_pdf import qimage_to_jpeg_bytes

    data = qimage_to_jpeg_bytes(image, quality=95)
    doc = fitz.open()
    try:
        page = doc.new_page(width=595.28, height=841.89)
        page.insert_image(page.rect, stream=data)
        return doc.tobytes()
    finally:
        doc.close()


class ReceiptSplitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_app()

    def test_detect_dark_background(self):
        from core.receipt_split import analyze_document

        doc = fitz.open(stream=sheet_pdf_bytes(make_sheet_image(True)),
                        filetype="pdf")
        try:
            pages = analyze_document(doc)
        finally:
            doc.close()
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(pages[0]["quads"]), 6)

    def test_detect_white_background(self):
        from core.receipt_split import analyze_document

        doc = fitz.open(stream=sheet_pdf_bytes(make_sheet_image(False)),
                        filetype="pdf")
        try:
            pages = analyze_document(doc)
        finally:
            doc.close()
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(pages[0]["quads"]), 6)

    def test_build_receipt_pdf(self):
        from core.receipt_split import analyze_document, build_receipt_pdf_bytes

        doc = fitz.open(stream=sheet_pdf_bytes(make_sheet_image(True)),
                        filetype="pdf")
        try:
            pages = analyze_document(doc)
            info = pages[0]
            self.assertTrue(info["quads"])
            data = build_receipt_pdf_bytes(
                info["image"], info["quads"][0], info["dpi"])
        finally:
            doc.close()
        out = fitz.open(stream=data, filetype="pdf")
        try:
            self.assertEqual(len(out), 1)
            self.assertGreater(out[0].rect.width, 50)
            self.assertGreater(out[0].rect.height, 50)
        finally:
            out.close()


if __name__ == "__main__":
    unittest.main()
