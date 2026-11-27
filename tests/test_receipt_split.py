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


def make_sheet_with_logos(count=3):
    """白背景シート。各レシートの上部に、文字と離れた大きな□ロゴがある。

    ロゴはインク系統では文字の塊と別成分になりやすく、そのままでは
    別のレシートとして数えられてしまう題材。
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QImage, QPainter, QPen

    _ensure_app()
    W, H = 1654, 2339
    img = QImage(W, H, QImage.Format.Format_RGB32)
    img.fill(QColor(255, 255, 255))
    painter = QPainter(img)
    try:
        ink = QColor(0, 0, 0)
        rw, rh = 540, 700
        xs = [100, 980]
        ys = [100, 880, 1660]
        placed = 0
        for row, y in enumerate(ys):
            for col, x in enumerate(xs):
                if placed >= count:
                    break
                painter.save()
                painter.translate(x, y)
                # □ロゴ（太い枠線だけの正方形）。下の店名帯まで間隔を空ける
                painter.setPen(QPen(ink, 30))
                painter.drawRect(160, 20, 180, 180)
                painter.setPen(Qt.PenStyle.NoPen)
                # 店名帯（ロゴの下端から110px空ける）
                painter.fillRect(150, 310, 240, 34, ink)
                for i in range(5):
                    yy = 390 + i * 48
                    painter.fillRect(30, yy, 230, 24, ink)
                    painter.fillRect(400, yy, 110, 24, ink)
                painter.fillRect(20, 640, rw - 40, 4, ink)
                painter.restore()
                placed += 1
    finally:
        painter.end()
    return img


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

    def test_logo_not_counted_as_receipt(self):
        from core.receipt_split import analyze_document

        doc = fitz.open(stream=sheet_pdf_bytes(make_sheet_with_logos(3)),
                        filetype="pdf")
        try:
            pages = analyze_document(doc)
        finally:
            doc.close()
        self.assertEqual(len(pages[0]["quads"]), 3)
        # ロゴの内側だけの小さい枠ではなく、ロゴごとレシート全体を
        # 含む枠になっていること
        for quad in pages[0]["quads"]:
            ys = [p[1] for p in quad]
            self.assertGreater(max(ys) - min(ys), 500)

    def test_filter_outlier_quads(self):
        from core.receipt_split import _filter_outlier_quads

        big1 = [(0, 0), (200, 0), (200, 300), (0, 300)]
        big2 = [(300, 0), (500, 0), (500, 300), (300, 300)]
        logo = [(600, 400), (650, 400), (650, 450), (600, 450)]
        inner = [(20, 20), (60, 20), (60, 60), (20, 60)]
        out = _filter_outlier_quads([big1, logo, big2, inner])
        self.assertEqual(out, [big1, big2])
        # 似た大きさ同士は落とさない
        small = [(600, 0), (740, 0), (740, 210), (600, 210)]
        out2 = _filter_outlier_quads([big1, big2, small])
        self.assertEqual(len(out2), 3)

    def test_merge_nearby_small_quads(self):
        from core.receipt_split import _merge_nearby_small_quads

        big = [(100, 200), (300, 200), (300, 500), (100, 500)]
        big2 = [(500, 200), (700, 200), (700, 500), (500, 500)]
        # ロゴ想定: 大きい枠のすぐ上にある小さい枠 → 統合される
        logo = [(150, 120), (250, 120), (250, 180), (150, 180)]
        out = _merge_nearby_small_quads([big, logo, big2], 1100)
        self.assertEqual(len(out), 2)
        merged = out[0]
        ys = [p[1] for p in merged]
        self.assertLessEqual(min(ys), 120)
        self.assertGreaterEqual(max(ys), 500)
        # 遠く離れた小さい枠は統合しない
        far = [(950, 900), (1010, 900), (1010, 960), (950, 960)]
        out2 = _merge_nearby_small_quads([big, big2, far], 1100)
        self.assertEqual(len(out2), 3)

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


class QuadEditorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = _ensure_app()

    def _make_editor(self):
        from PyQt6.QtGui import QColor, QImage

        from ui.receipt_split_dialog import QuadEditorWidget

        img = QImage(800, 1000, QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))
        quad = [(100.0, 100.0), (300.0, 100.0), (300.0, 400.0), (100.0, 400.0)]
        editor = QuadEditorWidget(img, [quad])
        editor.show()
        return editor

    def _mouse(self, editor, kind, x, y):
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        ev = QMouseEvent(
            QEvent.Type(kind), QPointF(x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        if kind == QEvent.Type.MouseButtonPress:
            editor.mousePressEvent(ev)
        elif kind == QEvent.Type.MouseMove:
            editor.mouseMoveEvent(ev)
        else:
            editor.mouseReleaseEvent(ev)

    def test_select_move_corner_delete(self):
        from PyQt6.QtCore import QEvent

        editor = self._make_editor()
        s = editor._scale
        # 中央を押して選択＋ドラッグで移動
        self._mouse(editor, QEvent.Type.MouseButtonPress, 200 * s, 250 * s)
        self.assertEqual(editor.selected, 0)
        self._mouse(editor, QEvent.Type.MouseMove, 250 * s, 300 * s)
        self._mouse(editor, QEvent.Type.MouseButtonRelease, 250 * s, 300 * s)
        self.assertAlmostEqual(editor.quads[0][0][0], 150.0, places=3)
        self.assertAlmostEqual(editor.quads[0][0][1], 150.0, places=3)
        # 角（右下）をドラッグして形を変える
        self._mouse(editor, QEvent.Type.MouseButtonPress, 350 * s, 450 * s)
        self._mouse(editor, QEvent.Type.MouseMove, 420 * s, 520 * s)
        self._mouse(editor, QEvent.Type.MouseButtonRelease, 420 * s, 520 * s)
        self.assertAlmostEqual(editor.quads[0][2][0], 420.0, places=3)
        self.assertAlmostEqual(editor.quads[0][2][1], 520.0, places=3)
        # 削除
        editor.delete_selected()
        self.assertEqual(len(editor.quads), 0)
        # 元に戻す
        editor.reset_quads()
        self.assertEqual(len(editor.quads), 1)
        editor.close()

    def test_add_mode_creates_quad(self):
        from PyQt6.QtCore import QEvent

        editor = self._make_editor()
        s = editor._scale
        editor.set_add_mode(True)
        self._mouse(editor, QEvent.Type.MouseButtonPress, 450 * s, 100 * s)
        self._mouse(editor, QEvent.Type.MouseMove, 650 * s, 300 * s)
        self._mouse(editor, QEvent.Type.MouseButtonRelease, 650 * s, 300 * s)
        self.assertEqual(len(editor.quads), 2)
        new = editor.quads[1]
        self.assertAlmostEqual(new[0][0], 450.0, places=3)
        self.assertAlmostEqual(new[2][1], 300.0, places=3)
        editor.close()


if __name__ == "__main__":
    unittest.main()
