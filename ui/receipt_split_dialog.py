# -*- coding: utf-8 -*-
"""レシート分割ダイアログ（多機能版 feature/trial-multi）。

原稿台いっぱいに並べて取り込んだページを解析し、検出した書類の
位置をプレビューで確認してから、フォルダへ1枚ずつ PDF で保存する。
検出が間違っていた場合に備え、プレビュー上で枠を手動修正できる
（枠の追加・削除・移動・角のドラッグによる形の変更）。
"""
from pathlib import Path

import fitz
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
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
ORANGE = QColor(230, 81, 0)
SELECTED_COLOR = QColor(21, 101, 192)


class QuadEditorWidget(QWidget):
    """ページ画像の上に検出枠を表示し、マウスで修正できる部品。

    枠をクリックで選択、内側をドラッグで移動、角をドラッグで形を
    変える。「枠を追加」モードでは、ドラッグした範囲が新しい枠に
    なる。座標はすべて元画像の座標で保持する。
    """

    changed = pyqtSignal()

    def __init__(self, image, quads, parent=None):
        super().__init__(parent)
        self._image = image
        self._pixmap = QPixmap.fromImage(image)
        self._orig_quads = [
            [(float(x), float(y)) for (x, y) in q] for q in quads]
        self.quads = [
            [(float(x), float(y)) for (x, y) in q] for q in quads]
        self.seq_start = 0
        self.selected = -1
        self.add_mode = False
        self._drag = None  # ("move", idx, last_pt) / ("corner", idx, corner)
        self._add_start = None
        self._add_current = None
        scale = PREVIEW_WIDTH / max(1, image.width())
        self._scale = scale
        self.setFixedSize(
            PREVIEW_WIDTH, max(10, int(image.height() * scale)))
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # -- 座標変換 --
    def _to_img(self, pos):
        return QPointF(pos.x() / self._scale, pos.y() / self._scale)

    def _to_widget(self, x, y):
        return QPointF(x * self._scale, y * self._scale)

    # -- 操作 --
    def set_add_mode(self, on):
        self.add_mode = bool(on)
        self.setCursor(
            Qt.CursorShape.CrossCursor if on
            else Qt.CursorShape.ArrowCursor)
        self.update()

    def delete_selected(self):
        if 0 <= self.selected < len(self.quads):
            del self.quads[self.selected]
            self.selected = -1
            self.changed.emit()
            self.update()

    def reset_quads(self):
        self.quads = [
            [(float(x), float(y)) for (x, y) in q] for q in self._orig_quads]
        self.selected = -1
        self.changed.emit()
        self.update()

    def _corner_hit(self, img_pt):
        """選択中の枠の角に近い場合、その角の番号を返す。"""
        if not (0 <= self.selected < len(self.quads)):
            return None
        tol = 14 / self._scale
        for ci, (x, y) in enumerate(self.quads[self.selected]):
            if abs(img_pt.x() - x) <= tol and abs(img_pt.y() - y) <= tol:
                return ci
        return None

    def _quad_hit(self, img_pt):
        for idx in range(len(self.quads) - 1, -1, -1):
            poly = QPolygonF([QPointF(x, y) for (x, y) in self.quads[idx]])
            if poly.containsPoint(img_pt, Qt.FillRule.OddEvenFill):
                return idx
        return -1

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        img_pt = self._to_img(event.position())
        if self.add_mode:
            self._add_start = img_pt
            self._add_current = img_pt
            self.update()
            return
        ci = self._corner_hit(img_pt)
        if ci is not None:
            self._drag = ("corner", self.selected, ci)
            return
        idx = self._quad_hit(img_pt)
        self.selected = idx
        if idx >= 0:
            self._drag = ("move", idx, img_pt)
        self.update()

    def mouseMoveEvent(self, event):
        img_pt = self._to_img(event.position())
        if self._add_start is not None:
            self._add_current = img_pt
            self.update()
            return
        if self._drag is None:
            return
        kind, idx = self._drag[0], self._drag[1]
        if kind == "corner":
            ci = self._drag[2]
            x = min(max(img_pt.x(), 0.0), float(self._image.width()))
            y = min(max(img_pt.y(), 0.0), float(self._image.height()))
            self.quads[idx][ci] = (x, y)
        elif kind == "move":
            last = self._drag[2]
            dx = img_pt.x() - last.x()
            dy = img_pt.y() - last.y()
            self.quads[idx] = [
                (x + dx, y + dy) for (x, y) in self.quads[idx]]
            self._drag = ("move", idx, img_pt)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._add_start is not None:
            start, cur = self._add_start, self._add_current
            self._add_start = None
            self._add_current = None
            if start is not None and cur is not None:
                x0, x1 = sorted([start.x(), cur.x()])
                y0, y1 = sorted([start.y(), cur.y()])
                min_side = 24 / self._scale
                if x1 - x0 >= min_side and y1 - y0 >= min_side:
                    self.quads.append(
                        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
                    self.selected = len(self.quads) - 1
                    self.changed.emit()
            self.update()
            return
        if self._drag is not None:
            self._drag = None
            self.changed.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
            return
        super().keyPressEvent(event)

    # -- 描画 --
    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.drawPixmap(0, 0, self.width(), self.height(), self._pixmap)
            pen_w = max(2, self.width() // 300)
            font = QFont()
            font.setBold(True)
            font.setPixelSize(max(20, self.width() // 45))
            painter.setFont(font)
            for i, quad in enumerate(self.quads):
                is_sel = (i == self.selected)
                color = SELECTED_COLOR if is_sel else ORANGE
                painter.setPen(QPen(color, pen_w + (2 if is_sel else 0)))
                pts = [self._to_widget(x, y) for (x, y) in quad]
                for a in range(4):
                    b = (a + 1) % 4
                    painter.drawLine(pts[a], pts[b])
                label = str(self.seq_start + i + 1)
                tx = int(min(p.x() for p in pts)) + pen_w * 2
                ty = int(min(p.y() for p in pts)) + pen_w * 2
                r = max(22, self.width() // 44)
                painter.setBrush(color)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(tx, ty, r * 2, r * 2)
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(
                    tx, ty, r * 2, r * 2,
                    Qt.AlignmentFlag.AlignCenter, label)
                if is_sel:
                    painter.setBrush(QColor(255, 255, 255))
                    painter.setPen(QPen(color, 2))
                    hs = max(5, self.width() // 90)
                    for p in pts:
                        painter.drawRect(
                            int(p.x()) - hs, int(p.y()) - hs,
                            hs * 2, hs * 2)
            if self._add_start is not None and self._add_current is not None:
                rect = QRectF(
                    self._to_widget(self._add_start.x(), self._add_start.y()),
                    self._to_widget(
                        self._add_current.x(), self._add_current.y()))
                painter.setPen(QPen(SELECTED_COLOR, pen_w, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect.normalized())
        finally:
            painter.end()


class ReceiptSplitDialog(QDialog):
    """検出結果のプレビューと、フォルダへの一括保存を行うダイアログ。"""

    def __init__(self, parent, doc, source_stem=""):
        super().__init__(parent)
        self.setWindowTitle("レシート分割（多機能版）")
        self.resize(780, 860)
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
            "枠が間違っているときは、直せます。枠をクリックで選んで、"
            "ドラッグで移動、四角い角をドラッグで大きさや形の変更、"
            "「枠を追加」を押してからドラッグで新しい枠、"
            "「選択した枠を削除」で間違った枠を消せます。\n"
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
        for page_info in self._pages:
            header = QLabel()
            header.setStyleSheet("font-weight: bold;")
            page_info["header"] = header
            self._scroll_layout.insertWidget(
                self._scroll_layout.count() - 1, header)

            controls = QHBoxLayout()
            add_btn = QPushButton("＋ 枠を追加")
            add_btn.setCheckable(True)
            del_btn = QPushButton("選択した枠を削除")
            reset_btn = QPushButton("検出結果に戻す")
            controls.addWidget(add_btn)
            controls.addWidget(del_btn)
            controls.addWidget(reset_btn)
            controls.addStretch(1)
            controls_widget = QWidget()
            controls_widget.setLayout(controls)
            self._scroll_layout.insertWidget(
                self._scroll_layout.count() - 1, controls_widget)

            editor = QuadEditorWidget(page_info["image"], page_info["quads"])
            editor.changed.connect(self._refresh_counts)
            page_info["editor"] = editor
            add_btn.toggled.connect(editor.set_add_mode)
            del_btn.clicked.connect(editor.delete_selected)
            reset_btn.clicked.connect(editor.reset_quads)
            self._scroll_layout.insertWidget(
                self._scroll_layout.count() - 1, editor)
        self._refresh_counts()

    def _refresh_counts(self):
        seq = 0
        for page_info in self._pages:
            editor = page_info.get("editor")
            if editor is None:
                continue
            editor.seq_start = seq
            n = len(editor.quads)
            page_info["header"].setText(
                f"ページ {page_info['page_index'] + 1}：{n} 件")
            editor.update()
            seq += n
        self._total = seq
        if self._total:
            self._summary.setText(
                f"合計 {self._total} 件の書類を検出しました。"
                "枠の位置を確認してから保存してください。")
            self._save_btn.setEnabled(True)
        else:
            self._summary.setText(
                "書類がありません。「枠を追加」で枠を作るか、レシート"
                "同士の間隔を空けて取り込み直してお試しください。")
            self._save_btn.setEnabled(False)

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
                editor = page_info.get("editor")
                quads = editor.quads if editor is not None \
                    else page_info["quads"]
                for quad in quads:
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
