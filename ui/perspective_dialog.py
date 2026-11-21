# -*- coding: utf-8 -*-
"""台形補正ダイアログ（多機能版）。

ページ上の書類部分が台形に歪んでいる場合に、書類の四隅をドラッグで
指定して、まっすぐな長方形に直すためのダイアログ。

- プレビューは注釈を焼き込んだ作業用コピーを画像化したもので、
  実行結果と同じ見た目になる（MergePagesDialog と同じ方式）。
- 四隅の初期値は core.photo_to_pdf.detect_document_quad で自動検出し、
  見つからなければページ全体を初期値にする。いつでも手で直せる。
- 補正したページは画像になり、文字データは残らない（ダイアログ内に明記）。
- このダイアログは四隅の収集と補正済み文書の生成までを担当し、
  実際のページ置き換え（Undo 可能なコマンド実行）は呼び出し側が行う。
"""
import fitz
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from core.geometry import FITZ_LOCK
from core.pdf_engine import PDFDocumentManager
from core.photo_to_pdf import (
    build_corrected_image_page_doc,
    detect_document_quad,
    page_render_dpi,
)
from ui.photo_to_pdf_dialog import QuadPreviewWidget


def _pt_to_mm(pt):
    return pt * 25.4 / 72.0


class PerspectiveCorrectionDialog(QDialog):
    """台形補正ダイアログ。

    doc は編集中の文書、page_idx は補正するページ。
    page_annotations を渡すと、プレビューと補正結果に注釈が焼き込まれる
    （通常保存や抽出と同じ扱い）。
    使い終わったら dispose() で作業用コピーを閉じること。
    """

    def __init__(self, parent, doc, page_idx, page_annotations=None):
        super().__init__(parent)
        self.setWindowTitle("🔶 台形補正")
        self.setMinimumSize(680, 620)
        self._page_idx = page_idx

        # 注釈を焼き込んだ作業用コピー（プレビューと補正の共通ソース）
        self._src_doc = PDFDocumentManager.extract_pages_as_doc(
            doc, [page_idx], page_annotations)
        with FITZ_LOCK:
            page = self._src_doc[0]
            if page.rotation:
                # 表示どおりの向きで画像化する（結合処理と同じ正規化）
                page.remove_rotation()
            rect = page.rect
            self._page_size_pt = (rect.width, rect.height)
            self._dpi = page_render_dpi(rect.width, rect.height)
            zoom = self._dpi / 72.0
            pix = page.get_pixmap(
                matrix=fitz.Matrix(zoom, zoom),
                colorspace=fitz.csRGB, alpha=False)
            self._image = QImage(
                pix.samples, pix.width, pix.height, pix.stride,
                QImage.Format.Format_RGB888).copy()

        self.detected = False
        quad = detect_document_quad(self._image)
        if quad is not None:
            self.detected = True
        else:
            w, h = self._image.width(), self._image.height()
            quad = [(0.0, 0.0), (float(w), 0.0),
                    (float(w), float(h)), (0.0, float(h))]

        layout = QVBoxLayout(self)

        intro = QLabel(
            f"{page_idx + 1}ページ目の書類部分を、まっすぐな長方形に直します。\n"
            "書類の四隅（オレンジの丸）をドラッグして、書類の角に合わせてください。\n"
            "※ 補正したページは画像になります。文字データは残らないので、"
            "コピーや検索はできなくなります。注釈は補正結果に焼き込まれます。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- プレビュー（四隅ドラッグ） ---
        self.preview = QuadPreviewWidget(self)
        self.preview.set_image(self._image, quad)
        self.preview.quadChanged.connect(self._update_result_label)
        layout.addWidget(self.preview, 1)

        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        row_ops = QHBoxLayout()
        btn_detect = QPushButton("🔍 四隅を自動検出", self)
        btn_detect.clicked.connect(self._redetect)
        row_ops.addWidget(btn_detect)
        row_ops.addStretch(1)
        self.lbl_result_size = QLabel()
        row_ops.addWidget(self.lbl_result_size)
        layout.addLayout(row_ops)

        # --- 下部ボタン ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("補正する")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._set_detected_message()
        self._update_result_label()

    # -- 内部処理 ------------------------------------------------------
    def _set_detected_message(self):
        if self.detected:
            self.status_label.setText(
                "書類の四隅を自動で見つけました。"
                "ずれている場合は、隅の丸をドラッグで直してください。")
        else:
            self.status_label.setText(
                "自動で見つからなかったので、四隅を手で調整してください。")

    def _redetect(self):
        quad = detect_document_quad(self._image)
        if quad is None:
            self.detected = False
            self.status_label.setText(
                "自動で見つからなかったので、四隅を手で調整してください。")
            return
        self.detected = True
        self.preview.set_image(self._image, quad)
        self._set_detected_message()
        self._update_result_label()

    def _update_result_label(self):
        quad = self.preview.quad
        if not quad:
            self.lbl_result_size.setText("")
            return
        # warp_perspective の出力寸法（対向辺の長い方）に合わせる
        def dist(a, b):
            return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

        tl, tr, br, bl = quad
        out_w_pt = max(dist(tl, tr), dist(bl, br)) * 72.0 / self._dpi
        out_h_pt = max(dist(tl, bl), dist(tr, br)) * 72.0 / self._dpi
        self.lbl_result_size.setText(
            f"できあがりの大きさ: 約 {_pt_to_mm(out_w_pt):.0f} × "
            f"{_pt_to_mm(out_h_pt):.0f} mm")

    # -- 公開API -------------------------------------------------------
    @property
    def quad(self):
        """現在の四隅（画像座標、左上・右上・右下・左下の順）。"""
        return self.preview.quad

    def build_corrected_doc(self):
        """現在の四隅で補正した1ページの文書を返す（呼び出し側が所有）。"""
        quad = self.preview.quad
        if not quad:
            raise ValueError("四隅が指定されていません。")
        return build_corrected_image_page_doc(self._image, quad, self._dpi)

    def dispose(self):
        if getattr(self, "_src_doc", None) is not None:
            try:
                with FITZ_LOCK:
                    self._src_doc.close()
            except Exception:
                pass
            self._src_doc = None
