# -*- coding: utf-8 -*-
"""2ページを1枚に結合するダイアログ（FAX結合）。

FAXで1枚の書類が途中で切れて2ページになって届いた場合に、見開きの
ように並べてプレビューしながら、真ん中の余白（内側）を削って
1ページにくっつけるためのダイアログ。

- 向きは「左右に並べる」「上下に並べる」を切り替えられる。
- 切り取り量はページごとにスライダーで調整し、プレビューへ即反映する。
- 2枚目の位置は上下・左右にずらして微調整できる（段差の補正）。
- 全画面表示（F11／ボタン、Escで戻る）に対応し、プレビューは
  高解像度で描くため全画面でも細部がつぶれにくい。
- 「元のページを残す」を選ぶと、元の2ページは消さず、結合した
  ページを追加する形になる。
- プレビューは注釈を焼き込んだ作業用コピーから描くため、実行結果と
  同じ見た目になる。結合自体は画像化せず、文字データは保たれる。
- このダイアログは設定の収集と結合済み文書の生成までを担当し、
  実際のページ置き換え（Undo 可能なコマンド実行）は呼び出し側が行う。
"""
import math

import fitz
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
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

# プレビューの描画解像度: 長辺のピクセル数の目安。
# 旧版は 1000px で、全画面にすると文字がぼやけたため 2400px に引き上げた。
_PREVIEW_MAX_PIXELS = 2400

# 位置の微調整スライダーの範囲（pt）。±100pt ≒ ±35mm。
_OFFSET_RANGE_PT = 100


def _pt_to_mm(pt):
    return pt * 25.4 / 72.0


class _MergePreviewWidget(QWidget):
    """2ページを並べた結合結果のプレビュー。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 340)
        self._images = [None, None]
        self._sizes = [(0.0, 0.0), (0.0, 0.0)]
        self._orientation = "horizontal"
        self._trims = [0.0, 0.0]
        self._offsets = [0.0, 0.0]  # 2枚目の (x, y) ずらし量 pt

    def set_content(self, images, sizes):
        self._images = list(images)
        self._sizes = [(float(w), float(h)) for w, h in sizes]
        self.update()

    def set_layout_params(self, orientation, trim_first, trim_second,
                          offset_x=0.0, offset_y=0.0):
        self._orientation = orientation
        self._trims = [float(trim_first), float(trim_second)]
        self._offsets = [float(offset_x), float(offset_y)]
        self.update()

    def _positions(self):
        """(全体w, 全体h, [(x, y, w, h), ...]) を pt で返す。core と同じ計算。"""
        (wa, ha), (wb, hb) = self._sizes
        ta, tb = self._trims
        off_x, off_y = self._offsets
        if self._orientation == "horizontal":
            kept = [(wa - ta, ha), (wb - tb, hb)]
            base_h = max(ha, hb)
            origins = [
                (0.0, (base_h - ha) / 2),
                (kept[0][0] + off_x, (base_h - hb) / 2 + off_y),
            ]
        else:
            kept = [(wa, ha - ta), (wb, hb - tb)]
            base_w = max(wa, wb)
            origins = [
                ((base_w - wa) / 2, 0.0),
                ((base_w - wb) / 2 + off_x, kept[0][1] + off_y),
            ]
        min_x = min(o[0] for o in origins)
        min_y = min(o[1] for o in origins)
        max_x = max(o[0] + d[0] for o, d in zip(origins, kept))
        max_y = max(o[1] + d[1] for o, d in zip(origins, kept))
        rects = [
            (ox - min_x, oy - min_y, dw, dh)
            for (ox, oy), (dw, dh) in zip(origins, kept)
        ]
        return (max_x - min_x, max_y - min_y, rects)

    def result_size_pt(self):
        """結合後のページサイズ (w_pt, h_pt)。"""
        total_w, total_h, _rects = self._positions()
        return (total_w, total_h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor(225, 225, 225))
        (wa, ha), (wb, hb) = self._sizes
        if wa <= 0 or wb <= 0:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "プレビューを準備できませんでした")
            return
        ta, tb = self._trims
        total_w, total_h, pos_rects = self._positions()
        margin = 14
        scale = min((self.width() - margin * 2) / total_w,
                    (self.height() - margin * 2) / total_h)
        ox = (self.width() - total_w * scale) / 2
        oy = (self.height() - total_h * scale) / 2

        page_rect = QRectF(ox, oy, total_w * scale, total_h * scale)
        painter.fillRect(page_rect, QColor(255, 255, 255))
        painter.setPen(QPen(QColor(120, 120, 120), 1))
        painter.drawRect(page_rect)

        targets = [
            QRectF(ox + x * scale, oy + y * scale, w * scale, h * scale)
            for (x, y, w, h) in pos_rects
        ]
        if self._orientation == "horizontal":
            src_fracs = [(0.0, (wa - ta) / wa), (tb / wb, 1.0)]
        else:
            src_fracs = [(0.0, (ha - ta) / ha), (tb / hb, 1.0)]

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

        # 真ん中の継ぎ目（破線）: 1枚目の内側の辺の位置
        painter.setPen(QPen(QColor(200, 60, 60), 1, Qt.PenStyle.DashLine))
        x0, y0, w0, h0 = pos_rects[0]
        if self._orientation == "horizontal":
            seam_x = ox + (x0 + w0) * scale
            painter.drawLine(int(seam_x), int(oy),
                             int(seam_x), int(oy + total_h * scale))
        else:
            seam_y = oy + (y0 + h0) * scale
            painter.drawLine(int(ox), int(seam_y),
                             int(ox + total_w * scale), int(seam_y))


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
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setMinimumSize(760, 640)
        self.resize(1040, 800)
        self._first_idx = first_idx
        self._second_idx = second_idx
        self._is_fullscreen = False

        # 注釈を焼き込んだ作業用コピー（プレビューと結合の共通ソース）
        self._src_doc = PDFDocumentManager.extract_pages_as_doc(
            doc, [first_idx, second_idx], page_annotations)
        self._sizes = []
        images = []
        with FITZ_LOCK:
            max_dim = 1.0
            for pos in (0, 1):
                rect = self._src_doc[pos].rect
                self._sizes.append((rect.width, rect.height))
                max_dim = max(max_dim, rect.width, rect.height)
            # 画質アップ: 長辺 2400px 目安で描く（旧版は 1000px）
            zoom = max(0.5, min(6.0, _PREVIEW_MAX_PIXELS / max_dim))
            self._preview_zoom = zoom
            for pos in (0, 1):
                pix = self._src_doc[pos].get_pixmap(
                    matrix=fitz.Matrix(zoom, zoom),
                    colorspace=fitz.csRGB, alpha=False)
                images.append(QImage(
                    pix.samples, pix.width, pix.height, pix.stride,
                    QImage.Format.Format_RGB888).copy())

        layout = QVBoxLayout(self)

        intro_row = QHBoxLayout()
        intro = QLabel(
            f"{first_idx + 1}ページ目と{second_idx + 1}ページ目を、1枚のページにくっつけます。\n"
            "FAXで2枚に分かれて届いた書類などに使えます。文字データはそのまま残ります。"
        )
        intro.setWordWrap(True)
        intro_row.addWidget(intro, 1)
        self.btn_fullscreen = QPushButton("⛶ 全画面 (F11)")
        self.btn_fullscreen.setCheckable(True)
        self.btn_fullscreen.setToolTip("全画面表示に切り替えます (F11 / Escで戻る)")
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        intro_row.addWidget(self.btn_fullscreen)
        layout.addLayout(intro_row)

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

        # --- 位置の微調整（2枚目のずらし） ---
        offset_box = QGroupBox("位置の微調整（2枚目をずらして段差を直します）")
        offset_layout = QVBoxLayout(offset_box)
        self._offset_sliders = []
        self._offset_labels = []
        for text in ("上下にずらす", "左右にずらす"):
            row = QHBoxLayout()
            label = QLabel(f"{second_idx + 1}ページ目を{text}")
            label.setMinimumWidth(250)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(-_OFFSET_RANGE_PT, _OFFSET_RANGE_PT)
            slider.setValue(0)
            slider.valueChanged.connect(self._on_layout_changed)
            value_label = QLabel("0.0 mm")
            value_label.setMinimumWidth(60)
            row.addWidget(label)
            row.addWidget(slider, 1)
            row.addWidget(value_label)
            offset_layout.addLayout(row)
            self._offset_labels.append(value_label)
            self._offset_sliders.append(slider)
        layout.addWidget(offset_box)

        # --- 元のページを残す ---
        self.check_keep_originals = QCheckBox(
            "元のページを残す（元の2ページは消さず、結合したページを追加します）")
        self.check_keep_originals.setToolTip(
            "チェックすると、結合したページが元の位置に追加され、"
            "元の2ページもそのまま残ります。取り消し（Undo）もできます。")
        layout.addWidget(self.check_keep_originals)

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

        self._shortcut_fullscreen = QShortcut(QKeySequence("F11"), self)
        self._shortcut_fullscreen.activated.connect(self.toggle_fullscreen)

        self._on_layout_changed()

    # -- 全画面 ---------------------------------------------------------
    def toggle_fullscreen(self):
        self.set_fullscreen(not self._is_fullscreen)

    def set_fullscreen(self, on):
        on = bool(on)
        if on == self._is_fullscreen:
            return
        self._is_fullscreen = on
        if on:
            self.showFullScreen()
            self.btn_fullscreen.setText("🗗 通常画面に戻す (F11)")
        else:
            self.showNormal()
            self.btn_fullscreen.setText("⛶ 全画面 (F11)")
        self.btn_fullscreen.blockSignals(True)
        self.btn_fullscreen.setChecked(on)
        self.btn_fullscreen.blockSignals(False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._is_fullscreen:
            # 全画面中の Esc は全画面を抜けるだけ（ダイアログは閉じない）
            self.set_fullscreen(False)
            event.accept()
            return
        super().keyPressEvent(event)

    def reject(self):
        if self._is_fullscreen:
            self.set_fullscreen(False)
            return
        super().reject()

    # -- 内部処理 ------------------------------------------------------
    def _slider_max(self, pos):
        w, h = self._sizes[pos]
        inner = w if self._orientation() == "horizontal" else h
        return max(0, int(math.floor(inner * _MIN_KEEP_RATIO)))

    def _orientation(self):
        return "horizontal" if self.radio_horizontal.isChecked() else "vertical"

    def _trims_pt(self):
        return [float(s.value()) for s in self._trim_sliders]

    def _offsets_pt(self):
        """(上下, 左右) ではなく (x, y) の順で返す: slider[0]=上下(y), slider[1]=左右(x)"""
        return (float(self._offset_sliders[1].value()),
                float(self._offset_sliders[0].value()))

    def _on_layout_changed(self, *_args):
        orientation = self._orientation()
        for pos, slider in enumerate(self._trim_sliders):
            new_max = self._slider_max(pos)
            if slider.maximum() != new_max:
                slider.blockSignals(True)
                slider.setMaximum(new_max)
                if slider.value() > new_max:
                    slider.setValue(new_max)
                slider.blockSignals(False)
        trim_first, trim_second = self._trims_pt()
        offset_x, offset_y = self._offsets_pt()
        self.preview.set_layout_params(
            orientation, trim_first, trim_second, offset_x, offset_y)

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

        # 微調整の表示（上下が slider[0]、左右が slider[1]）
        self._offset_labels[0].setText(
            f"{_pt_to_mm(float(self._offset_sliders[0].value())):+.1f} mm")
        self._offset_labels[1].setText(
            f"{_pt_to_mm(float(self._offset_sliders[1].value())):+.1f} mm")

        total_w, total_h = self.preview.result_size_pt()
        self.lbl_result_size.setText(
            f"できあがりの大きさ: 約 {_pt_to_mm(total_w):.0f} × {_pt_to_mm(total_h):.0f} mm"
        )

    # -- 公開API -------------------------------------------------------
    def keep_originals(self):
        return bool(self.check_keep_originals.isChecked())

    def build_merged_doc(self):
        """現在の設定で結合した1ページの文書を返す（呼び出し側が所有）。"""
        trim_first, trim_second = self._trims_pt()
        offset_x, offset_y = self._offsets_pt()
        return PDFDocumentManager.build_merged_two_pages_doc(
            self._src_doc, 0, 1,
            orientation=self._orientation(),
            trim_first=trim_first, trim_second=trim_second,
            offset_second_x=offset_x, offset_second_y=offset_y)

    def dispose(self):
        if getattr(self, "_src_doc", None) is not None:
            try:
                with FITZ_LOCK:
                    self._src_doc.close()
            except Exception:
                pass
            self._src_doc = None
