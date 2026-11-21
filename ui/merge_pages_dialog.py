# -*- coding: utf-8 -*-
"""2ページを1枚に結合するダイアログ（FAX結合）。

FAXで1枚の書類が途中で切れて2ページになって届いた場合に、見開きの
ように並べてプレビューしながら、真ん中の余白（内側）を削って
1ページにくっつけるためのダイアログ。

- 向きは「左右に並べる」「上下に並べる」を切り替えられる。
- 切り取り量はページごとにスライダーで調整し、プレビューへ即反映する。
- プレビューは注釈を焼き込んだ作業用コピーから描くため、実行結果と
  同じ見た目になる。結合自体は画像化せず、文字データは保たれる。
- このダイアログは設定の収集と結合済み文書の生成までを担当し、
  実際のページ置き換え（Undo 可能なコマンド実行）は呼び出し側が行う。
"""
import math

import fitz
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core.geometry import FITZ_LOCK
from core.pdf_engine import PDFDocumentManager

# プレビュー描画で使う、内側の辺に最低限残す長さ（pt）。
# core 側のクランプ（20pt）より大きい側に倒し、スライダー上限を 50% にする。
_MIN_KEEP_RATIO = 0.5


def _pt_to_mm(pt):
    return pt * 25.4 / 72.0


class _MergePreviewWidget(QWidget):
    """2ページを並べた結合結果のプレビュー。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 340)
        self._images = [None, None]      # [QImage, QImage]（pt 基準で等倍レンダリング済み想定ではなく、ピクセル実寸）
        self._sizes = [(0.0, 0.0), (0.0, 0.0)]  # [(w_pt, h_pt), ...]
        self._orientation = "horizontal"
        self._trims = [0.0, 0.0]

    def set_content(self, images, sizes):
        self._images = list(images)
        self._sizes = [(float(w), float(h)) for w, h in sizes]
        self.update()

    def set_layout_params(self, orientation, trim_first, trim_second):
        self._orientation = orientation
        self._trims = [float(trim_first), float(trim_second)]
        self.update()

    def result_size_pt(self):
        """結合後のページサイズ (w_pt, h_pt)。"""
        (wa, ha), (wb, hb) = self._sizes
        ta, tb = self._trims
        if self._orientation == "horizontal":
            return (wa - ta) + (wb - tb), max(ha, hb)
        return max(wa, wb), (ha - ta) + (hb - tb)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(225, 225, 225))
        (wa, ha), (wb, hb) = self._sizes
        if wa <= 0 or wb <= 0:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "プレビューを準備できませんでした")
            return
        ta, tb = self._trims
        total_w, total_h = self.result_size_pt()
        margin = 14
        scale = min((self.width() - margin * 2) / total_w,
                    (self.height() - margin * 2) / total_h)
        ox = (self.width() - total_w * scale) / 2
        oy = (self.height() - total_h * scale) / 2

        # 結合後のページの輪郭
        page_rect = QRectF(ox, oy, total_w * scale, total_h * scale)
        painter.fillRect(page_rect, QColor(255, 255, 255))
        painter.setPen(QPen(QColor(120, 120, 120), 1))
        painter.drawRect(page_rect)

        # 各ページの貼り付け先（pt）と、元画像から切り出す割合
        if self._orientation == "horizontal":
            targets = [
                QRectF(ox, oy + (total_h - ha) * scale / 2,
                       (wa - ta) * scale, ha * scale),
                QRectF(ox + (wa - ta) * scale, oy + (total_h - hb) * scale / 2,
                       (wb - tb) * scale, hb * scale),
            ]
            src_fracs = [(0.0, (wa - ta) / wa), (tb / wb, 1.0)]
            seam_x = ox + (wa - ta) * scale
            seam = (QRectF(seam_x, oy, 0, total_h * scale), True)
        else:
            targets = [
                QRectF(ox + (total_w - wa) * scale / 2, oy,
                       wa * scale, (ha - ta) * scale),
                QRectF(ox + (total_w - wb) * scale / 2, oy + (ha - ta) * scale,
                       wb * scale, (hb - tb) * scale),
            ]
            src_fracs = [(0.0, (ha - ta) / ha), (tb / hb, 1.0)]
            seam_y = oy + (ha - ta) * scale
            seam = (QRectF(ox, seam_y, total_w * scale, 0), False)

        for img, target, (f0, f1) in zip(self._images, targets, src_fracs):
            if img is None:
                continue
            if self._orientation == "horizontal":
                src = QRectF(img.width() * f0, 0,
                             img.width() * (f1 - f0), img.height())
            else:
                src = QRectF(0, img.height() * f0,
                             img.width(), img.height() * (f1 - f0))
            painter.drawImage(target, img, src)

        # 真ん中の継ぎ目（破線）
        seam_rect, is_vertical = seam
        painter.setPen(QPen(QColor(200, 60, 60), 1, Qt.PenStyle.DashLine))
        if is_vertical:
            painter.drawLine(int(seam_rect.x()), int(seam_rect.y()),
                             int(seam_rect.x()), int(seam_rect.y() + seam_rect.height()))
        else:
            painter.drawLine(int(seam_rect.x()), int(seam_rect.y()),
                             int(seam_rect.x() + seam_rect.width()), int(seam_rect.y()))


class MergePagesDialog(QDialog):
    """2ページ結合ダイアログ。

    doc は編集中の文書、first_idx / second_idx は結合する2ページ。
    page_annotations を渡すと、プレビューと結合結果に注釈が焼き込まれる
    （通常保存や抽出と同じ扱い）。
    使い終わったら dispose() で作業用コピーを閉じること。
    """

    def __init__(self, parent, doc, first_idx, second_idx, page_annotations=None):
        super().__init__(parent)
        self.setWindowTitle("🧩 2ページを1枚に結合")
        self.setMinimumSize(660, 600)
        self._first_idx = first_idx
        self._second_idx = second_idx

        # 注釈を焼き込んだ作業用コピー（プレビューと結合の共通ソース）
        self._src_doc = PDFDocumentManager.extract_pages_as_doc(
            doc, [first_idx, second_idx], page_annotations)
        self._sizes = []
        images = []
        with FITZ_LOCK:
            max_dim = 1.0
            rects = []
            for pos in (0, 1):
                rect = self._src_doc[pos].rect
                rects.append(rect)
                self._sizes.append((rect.width, rect.height))
                max_dim = max(max_dim, rect.width, rect.height)
            zoom = max(0.5, min(3.0, 1000.0 / max_dim))
            for pos in (0, 1):
                pix = self._src_doc[pos].get_pixmap(
                    matrix=fitz.Matrix(zoom, zoom),
                    colorspace=fitz.csRGB, alpha=False)
                images.append(QImage(
                    pix.samples, pix.width, pix.height, pix.stride,
                    QImage.Format.Format_RGB888).copy())

        layout = QVBoxLayout(self)

        intro = QLabel(
            f"{first_idx + 1}ページ目と{second_idx + 1}ページ目を、1枚のページにくっつけます。\n"
            "FAXで2枚に分かれて届いた書類などに使えます。文字データはそのまま残ります。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- 向き ---
        dir_box = QGroupBox("並べる向き")
        dir_layout = QHBoxLayout(dir_box)
        self.radio_horizontal = QRadioButton("左右に並べる（見開き）")
        self.radio_vertical = QRadioButton("上下に並べる")
        self.radio_horizontal.setChecked(True)
        self.radio_horizontal.toggled.connect(self._on_layout_changed)
        dir_layout.addWidget(self.radio_horizontal)
        dir_layout.addWidget(self.radio_vertical)
        dir_layout.addStretch(1)
        layout.addWidget(dir_box)

        # --- プレビュー ---
        self.preview = _MergePreviewWidget(self)
        self.preview.set_content(images, self._sizes)
        layout.addWidget(self.preview, 1)

        self.lbl_result_size = QLabel()
        layout.addWidget(self.lbl_result_size)

        # --- 切り取り量 ---
        trim_box = QGroupBox("真ん中の切り取り量（内側の余白を削ります）")
        trim_layout = QVBoxLayout(trim_box)
        self._trim_sliders = []
        self._trim_labels = []
        for pos in (0, 1):
            row = QHBoxLayout()
            label = QLabel()
            label.setMinimumWidth(250)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, self._slider_max(pos))
            slider.setValue(0)
            slider.valueChanged.connect(self._on_layout_changed)
            value_label = QLabel("0.0 mm")
            value_label.setMinimumWidth(60)
            row.addWidget(label)
            row.addWidget(slider, 1)
            row.addWidget(value_label)
            trim_layout.addLayout(row)
            self._trim_labels.append((label, value_label))
            self._trim_sliders.append(slider)
        layout.addWidget(trim_box)

        # --- 下部ボタン ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("結合する")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._on_layout_changed()

    # -- 内部処理 ------------------------------------------------------
    def _slider_max(self, pos):
        w, h = self._sizes[pos]
        inner = w if self._orientation() == "horizontal" else h
        return max(0, int(math.floor(inner * _MIN_KEEP_RATIO)))

    def _orientation(self):
        return "horizontal" if self.radio_horizontal.isChecked() else "vertical"

    def _trims_pt(self):
        return [float(s.value()) for s in self._trim_sliders]

    def _on_layout_changed(self, *_args):
        orientation = self._orientation()
        # 向きが変わると内側の辺が変わるため、スライダー上限を作り直す
        for pos, slider in enumerate(self._trim_sliders):
            new_max = self._slider_max(pos)
            if slider.maximum() != new_max:
                slider.blockSignals(True)
                slider.setMaximum(new_max)
                if slider.value() > new_max:
                    slider.setValue(new_max)
                slider.blockSignals(False)
        trim_first, trim_second = self._trims_pt()
        self.preview.set_layout_params(orientation, trim_first, trim_second)

        first_no = self._first_idx + 1
        second_no = self._second_idx + 1
        if orientation == "horizontal":
            texts = [
                f"{first_no}ページ目（左）の右端を削る",
                f"{second_no}ページ目（右）の左端を削る",
            ]
        else:
            texts = [
                f"{first_no}ページ目（上）の下端を削る",
                f"{second_no}ページ目（下）の上端を削る",
            ]
        for (label, value_label), text, trim in zip(
                self._trim_labels, texts, (trim_first, trim_second)):
            label.setText(text)
            value_label.setText(f"{_pt_to_mm(trim):.1f} mm")

        total_w, total_h = self.preview.result_size_pt()
        self.lbl_result_size.setText(
            f"できあがりの大きさ: 約 {_pt_to_mm(total_w):.0f} × {_pt_to_mm(total_h):.0f} mm"
        )

    # -- 公開API -------------------------------------------------------
    def build_merged_doc(self):
        """現在の設定で結合した1ページの文書を返す（呼び出し側が所有）。"""
        trim_first, trim_second = self._trims_pt()
        return PDFDocumentManager.build_merged_two_pages_doc(
            self._src_doc, 0, 1,
            orientation=self._orientation(),
            trim_first=trim_first, trim_second=trim_second)

    def dispose(self):
        if getattr(self, "_src_doc", None) is not None:
            try:
                with FITZ_LOCK:
                    self._src_doc.close()
            except Exception:
                pass
            self._src_doc = None
