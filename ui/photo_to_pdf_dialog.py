# -*- coding: utf-8 -*-
"""写真をPDF化するダイアログ（多機能版 feature/trial-multi）。

複数枚の写真を選び、書類の四隅を自動検出（失敗時は手でドラッグ修正）して
透視変換で切り抜き・傾き補正し、1枚＝1ページの A4 PDF を作る。
処理本体は core/photo_to_pdf.py。
"""
from pathlib import Path

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.photo_to_pdf import (
    detect_document_quad,
    load_photo_image,
    photos_to_pdf_bytes,
    rotate_image_90,
    rotate_quad_90,
    warp_perspective,
)

_IMAGE_FILTER = "画像ファイル (*.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp)"


class QuadPreviewWidget(QWidget):
    """写真を表示し、書類の四隅をドラッグで調整できるプレビュー。

    四隅は画像座標（ピクセル）で保持し、表示倍率に合わせて描く。
    """

    quadChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 320)
        self._image = None
        self._pixmap = None
        self._quad = None  # [(x, y), ...] 画像座標、左上・右上・右下・左下
        self._drag_idx = None

    # --------------------------------------------------
    def set_image(self, image, quad):
        self._image = image
        self._pixmap = (
            QPixmap.fromImage(image)
            if image is not None and not image.isNull() else None)
        self._quad = [tuple(map(float, pt)) for pt in quad] if quad else None
        self._drag_idx = None
        self.update()

    @property
    def quad(self):
        return list(self._quad) if self._quad else None

    # --------------------------------------------------
    def _view_geometry(self):
        """(scale, offset_x, offset_y) を返す。画像が無ければ None。"""
        if self._pixmap is None:
            return None
        iw, ih = self._pixmap.width(), self._pixmap.height()
        if iw <= 0 or ih <= 0:
            return None
        margin = 16
        avail_w = max(1, self.width() - margin * 2)
        avail_h = max(1, self.height() - margin * 2)
        scale = min(avail_w / iw, avail_h / ih)
        ox = (self.width() - iw * scale) / 2.0
        oy = (self.height() - ih * scale) / 2.0
        return scale, ox, oy

    def _to_view(self, pt, geo):
        scale, ox, oy = geo
        return QPointF(ox + pt[0] * scale, oy + pt[1] * scale)

    def _to_image(self, pos, geo):
        scale, ox, oy = geo
        x = (pos.x() - ox) / scale
        y = (pos.y() - oy) / scale
        iw, ih = self._pixmap.width(), self._pixmap.height()
        return (min(max(x, 0.0), float(iw)), min(max(y, 0.0), float(ih)))

    # --------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(64, 64, 68))
        geo = self._view_geometry()
        if geo is None:
            painter.setPen(QColor(220, 220, 220))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter,
                "左のリストから写真を選んでください")
            return
        scale, ox, oy = geo
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        iw, ih = self._pixmap.width(), self._pixmap.height()
        painter.drawPixmap(
            int(ox), int(oy), int(iw * scale), int(ih * scale), self._pixmap)
        if self._quad:
            pts = [self._to_view(pt, geo) for pt in self._quad]
            pen = QPen(QColor(255, 120, 40), 2)
            painter.setPen(pen)
            for i in range(4):
                painter.drawLine(pts[i], pts[(i + 1) % 4])
            for i, pt in enumerate(pts):
                painter.setBrush(QColor(255, 120, 40))
                painter.setPen(QPen(QColor(255, 255, 255), 2))
                painter.drawEllipse(pt, 9, 9)
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(
                    int(pt.x()) - 4, int(pt.y()) - 14, str(i + 1))

    # --------------------------------------------------
    def mousePressEvent(self, event):
        geo = self._view_geometry()
        if geo is None or not self._quad:
            return
        pos = event.position()
        for i, pt in enumerate(self._quad):
            view_pt = self._to_view(pt, geo)
            if (pos - view_pt).manhattanLength() <= 22:
                self._drag_idx = i
                return

    def mouseMoveEvent(self, event):
        if self._drag_idx is None:
            return
        geo = self._view_geometry()
        if geo is None:
            return
        self._quad[self._drag_idx] = self._to_image(event.position(), geo)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._drag_idx is not None:
            self._drag_idx = None
            self.quadChanged.emit()


class _PhotoItem:
    """ダイアログ内で保持する写真1枚分の状態。"""

    def __init__(self, path, image, quad, detected):
        self.path = path
        self.image = image
        self.quad = quad
        self.detected = detected
        self.apply_crop = True


class PhotoToPdfDialog(QDialog):
    """写真をPDF化するダイアログ。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("写真をPDF化")
        self.resize(880, 600)
        self._items = []
        self._current = -1

        main = QHBoxLayout(self)

        # 左: 一覧と並び替え
        left = QVBoxLayout()
        self.list_widget = QListWidget(self)
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        left.addWidget(self.list_widget)
        btn_add = QPushButton("＋ 画像を追加", self)
        btn_add.clicked.connect(self._add_images)
        left.addWidget(btn_add)
        btn_del = QPushButton("－ 削除", self)
        btn_del.clicked.connect(self._remove_current)
        left.addWidget(btn_del)
        row_move = QHBoxLayout()
        btn_up = QPushButton("↑ 上へ", self)
        btn_up.clicked.connect(lambda: self._move_current(-1))
        row_move.addWidget(btn_up)
        btn_down = QPushButton("↓ 下へ", self)
        btn_down.clicked.connect(lambda: self._move_current(1))
        row_move.addWidget(btn_down)
        left.addLayout(row_move)
        left.addWidget(QLabel(
            "写真を選ぶと、書類の四隅を\n自動で探します。", self))
        main.addLayout(left, 0)

        # 右: プレビューと操作
        right = QVBoxLayout()
        self.preview = QuadPreviewWidget(self)
        self.preview.quadChanged.connect(self._on_quad_changed)
        right.addWidget(self.preview, 1)
        self.status_label = QLabel("", self)
        right.addWidget(self.status_label)
        row_ops = QHBoxLayout()
        btn_detect = QPushButton("🔍 四隅を自動検出", self)
        btn_detect.clicked.connect(self._redetect_current)
        row_ops.addWidget(btn_detect)
        btn_rot_l = QPushButton("↩ 左へ90°回転", self)
        btn_rot_l.clicked.connect(lambda: self._rotate_current(False))
        row_ops.addWidget(btn_rot_l)
        btn_rot_r = QPushButton("↪ 右へ90°回転", self)
        btn_rot_r.clicked.connect(lambda: self._rotate_current(True))
        row_ops.addWidget(btn_rot_r)
        right.addLayout(row_ops)
        self.chk_crop = QCheckBox("切り抜き・傾き補正を適用する", self)
        self.chk_crop.setChecked(True)
        self.chk_crop.toggled.connect(self._on_crop_toggled)
        right.addWidget(self.chk_crop)
        row_bottom = QHBoxLayout()
        btn_save = QPushButton("📄 PDFを作成して保存", self)
        btn_save.clicked.connect(self._save_pdf)
        row_bottom.addWidget(btn_save)
        btn_close = QPushButton("閉じる", self)
        btn_close.clicked.connect(self.reject)
        row_bottom.addWidget(btn_close)
        right.addLayout(row_bottom)
        main.addLayout(right, 1)

    # --------------------------------------------------
    # 一覧の操作
    # --------------------------------------------------
    def _add_images(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "写真を選ぶ", "", _IMAGE_FILTER)
        errors = []
        first_new = len(self._items)
        for path in paths:
            try:
                image = load_photo_image(path)
            except Exception as e:
                errors.append(f"{Path(path).name}: {e}")
                continue
            quad = detect_document_quad(image)
            detected = quad is not None
            if quad is None:
                w, h = image.width(), image.height()
                quad = [(0.0, 0.0), (float(w), 0.0),
                        (float(w), float(h)), (0.0, float(h))]
            self._items.append(_PhotoItem(path, image, quad, detected))
            self.list_widget.addItem(Path(path).name)
        if errors:
            QMessageBox.warning(
                self, "読み込みエラー",
                "読み込めなかった画像があります。\n" + "\n".join(errors))
        if self._items and self.list_widget.currentRow() < 0:
            self.list_widget.setCurrentRow(first_new)

    def _remove_current(self):
        row = self.list_widget.currentRow()
        if row < 0:
            return
        del self._items[row]
        self.list_widget.takeItem(row)
        if not self._items:
            self._show_item(-1)

    def _move_current(self, delta):
        row = self.list_widget.currentRow()
        new_row = row + delta
        if row < 0 or not (0 <= new_row < len(self._items)):
            return
        self._items[row], self._items[new_row] = (
            self._items[new_row], self._items[row])
        text = self.list_widget.item(row).text()
        self.list_widget.item(row).setText(
            self.list_widget.item(new_row).text())
        self.list_widget.item(new_row).setText(text)
        self.list_widget.setCurrentRow(new_row)

    # --------------------------------------------------
    # プレビュー表示
    # --------------------------------------------------
    def _on_row_changed(self, row):
        self._show_item(row)

    def _show_item(self, row):
        self._current = row
        if not (0 <= row < len(self._items)):
            self.preview.set_image(None, None)
            self.status_label.setText("")
            return
        item = self._items[row]
        self.preview.set_image(item.image, item.quad)
        self.chk_crop.setChecked(item.apply_crop)
        if item.detected:
            self.status_label.setText(
                "書類の四隅を自動で見つけました。"
                "ずれている場合は、隅の丸をドラッグで直してください。")
        else:
            self.status_label.setText(
                "自動で見つからなかったので、四隅を手で調整してください。")

    def _current_item(self):
        if 0 <= self._current < len(self._items):
            return self._items[self._current]
        return None

    def _on_quad_changed(self):
        item = self._current_item()
        if item is not None and self.preview.quad is not None:
            item.quad = self.preview.quad

    def _on_crop_toggled(self, checked):
        item = self._current_item()
        if item is not None:
            item.apply_crop = bool(checked)

    # --------------------------------------------------
    # 検出・回転
    # --------------------------------------------------
    def _redetect_current(self):
        item = self._current_item()
        if item is None:
            return
        quad = detect_document_quad(item.image)
        if quad is None:
            self.status_label.setText(
                "自動で見つからなかったので、四隅を手で調整してください。")
            item.detected = False
            return
        item.quad = quad
        item.detected = True
        self.preview.set_image(item.image, item.quad)
        self.status_label.setText(
            "書類の四隅を自動で見つけました。"
            "ずれている場合は、隅の丸をドラッグで直してください。")

    def _rotate_current(self, clockwise):
        item = self._current_item()
        if item is None:
            return
        old_w, old_h = item.image.width(), item.image.height()
        item.image = rotate_image_90(item.image, clockwise)
        if item.quad:
            item.quad = rotate_quad_90(item.quad, old_w, old_h, clockwise)
        self.preview.set_image(item.image, item.quad)

    # --------------------------------------------------
    # PDF化
    # --------------------------------------------------
    def _save_pdf(self):
        if not self._items:
            QMessageBox.information(
                self, "案内", "先に「＋ 画像を追加」で写真を選んでください。")
            return
        # プレビューで編集中の四隅を反映
        self._on_quad_changed()
        try:
            images = []
            for item in self._items:
                img = item.image
                if item.apply_crop and item.quad:
                    img = warp_perspective(img, item.quad)
                images.append(img)
            data = photos_to_pdf_bytes(images)
        except Exception as e:
            QMessageBox.critical(
                self, "エラー", f"PDFの作成に失敗しました。\n{e}")
            return
        filepath, _ = QFileDialog.getSaveFileName(
            self, "写真PDFを保存", "写真.pdf", "PDF Files (*.pdf)")
        if not filepath:
            return
        if not filepath.lower().endswith(".pdf"):
            filepath += ".pdf"
        try:
            Path(filepath).write_bytes(data)
        except Exception as e:
            QMessageBox.critical(
                self, "保存エラー", f"保存に失敗しました。\n{e}")
            return
        QMessageBox.information(
            self, "完了",
            f"写真{len(self._items)}枚をPDFにして保存しました。\n{filepath}")
        self.accept()
