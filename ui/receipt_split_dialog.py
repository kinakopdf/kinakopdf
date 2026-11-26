# -*- coding: utf-8 -*-
"""レシート分割ダイアログ（多機能版 feature/trial-multi）。

原稿台いっぱいに並べて取り込んだページを解析し、検出した書類の
位置をプレビューで確認してから、フォルダへ1枚ずつ PDF で保存する。
"""
from pathlib import Path

import fitz
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.receipt_split import analyze_document, build_receipt_pdf_bytes

PREVIEW_WIDTH = 660


class ReceiptSplitDialog(QDialog):
    """検出結果のプレビューと、フォルダへの一括保存を行うダイアログ。"""

    def __init__(self, parent, doc, source_stem=""):
        super().__init__(parent)
        self.setWindowTitle("レシート分割（多機能版）")
        self.resize(760, 820)
        self._doc = doc
        self._stem = source_stem or "スキャン"
        self._pages = []
        self._total = 0

        layout = QVBoxLayout(self)
        self._summary = QLabel("解析しています…")
        layout.addWidget(self._summary)
        note = QLabel(
            "オレンジの枠が検出した書類です。番号の順に、レシートごとの"
            "PDFファイルとして保存します。\n"
            "レシート同士が重なっていたり、くっついていたりすると、"
            "1枚に数えてしまうことがあります。")
        note.setWordWrap(True)
        layout.addWidget(note)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll_content = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.addStretch(1)
        self._scroll.setWidget(self._scroll_content)
        layout.addWidget(self._scroll, 1)

        buttons = QHBoxLayout()
        self._save_btn = QPushButton("フォルダを選んで保存")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._save_all)
        buttons.addWidget(self._save_btn)
        buttons.addStretch(1)
        close_btn = QPushButton("閉じる")
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self._analyze()

    # ------------------------------------------------------------
    def _analyze(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._pages = analyze_document(self._doc)
        except Exception as e:
            QMessageBox.critical(self, "解析エラー", f"解析に失敗しました。\n{e}")
            self._pages = []
        finally:
            QApplication.restoreOverrideCursor()
        self._total = sum(len(p["quads"]) for p in self._pages)
        seq = 0
        for page_info in self._pages:
            header = QLabel(
                f"ページ {page_info['page_index'] + 1}："
                f"{len(page_info['quads'])} 件")
            header.setStyleSheet("font-weight: bold;")
            self._scroll_layout.insertWidget(
                self._scroll_layout.count() - 1, header)
            preview = QLabel()
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pix = self._make_preview(page_info, seq)
            preview.setPixmap(pix)
            self._scroll_layout.insertWidget(
                self._scroll_layout.count() - 1, preview)
            seq += len(page_info["quads"])
        if self._total:
            self._summary.setText(
                f"合計 {self._total} 件の書類を検出しました。"
                "枠の位置を確認してから保存してください。")
            self._save_btn.setEnabled(True)
        else:
            self._summary.setText(
                "書類を検出できませんでした。レシート同士の間隔を"
                "空けて、取り込み直してお試しください。")

    def _make_preview(self, page_info, seq_start):
        image = page_info["image"].copy()
        painter = QPainter(image)
        try:
            pen_w = max(3, image.width() // 300)
            painter.setPen(QPen(QColor(230, 81, 0), pen_w))
            font = QFont()
            font.setBold(True)
            font.setPixelSize(max(28, image.width() // 45))
            painter.setFont(font)
            for i, quad in enumerate(page_info["quads"]):
                pts = [(float(x), float(y)) for (x, y) in quad]
                for a in range(4):
                    b = (a + 1) % 4
                    painter.drawLine(
                        int(pts[a][0]), int(pts[a][1]),
                        int(pts[b][0]), int(pts[b][1]))
                label = str(seq_start + i + 1)
                tx = int(min(p[0] for p in pts)) + pen_w * 2
                ty = int(min(p[1] for p in pts)) + pen_w * 2
                r = max(30, image.width() // 40)
                painter.setBrush(QColor(230, 81, 0))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(tx, ty, r * 2, r * 2)
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(
                    tx, ty, r * 2, r * 2,
                    Qt.AlignmentFlag.AlignCenter, label)
                painter.setPen(QPen(QColor(230, 81, 0), pen_w))
        finally:
            painter.end()
        pix = QPixmap.fromImage(image)
        if pix.width() > PREVIEW_WIDTH:
            pix = pix.scaledToWidth(
                PREVIEW_WIDTH, Qt.TransformationMode.SmoothTransformation)
        return pix

    # ------------------------------------------------------------
    def _save_all(self):
        folder = QFileDialog.getExistingDirectory(
            self, "レシートPDFの保存先フォルダ")
        if not folder:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        saved = []
        errors = []
        try:
            seq = 0
            for page_info in self._pages:
                for quad in page_info["quads"]:
                    seq += 1
                    name = f"{self._stem}_レシート{seq:03d}.pdf"
                    try:
                        data = build_receipt_pdf_bytes(
                            page_info["image"], quad, page_info["dpi"])
                        (Path(folder) / name).write_bytes(data)
                        saved.append(name)
                    except Exception as e:
                        errors.append(f"{name}: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        if errors:
            QMessageBox.warning(
                self, "保存結果",
                f"{len(saved)} 件を保存しました。\n"
                f"{len(errors)} 件で失敗しました。\n" + "\n".join(errors[:5]))
        else:
            QMessageBox.information(
                self, "保存完了",
                f"{len(saved)} 件のレシートPDFを保存しました。\n{folder}")
        if saved:
            self.accept()
