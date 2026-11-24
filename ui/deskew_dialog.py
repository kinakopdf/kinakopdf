# -*- coding: utf-8 -*-
"""傾き補正ダイアログ（多機能版）。

スキャンや FAX で傾いてしまったページについて、ページごとに検出した
傾きを一覧で提示し、微調整してから一括でまっすぐに戻すためのダイアログ。

- 検出は core.deskew.detect_page_skew（文字データ優先、なければ画像解析）。
  検出できなかったページはその旨を正直に表示し、手入力で直せる。
- 角度の調整はスライダーと数値入力の両方からでき、プレビューへ
  反映される。プレビューは補正済みページそのものを描くため、
  実行結果と同じ見た目になる。
- プレビューと補正のソースは、注釈を焼き込んだ作業用コピー
  （MergePagesDialog / PerspectiveCorrectionDialog と同じ方式）。
  補正しても文字データは残る。
- このダイアログは角度の収集と補正済み文書の生成までを担当し、
  実際のページ置き換え（Undo 可能なコマンド実行）は呼び出し側が行う。
"""
import fitz
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.deskew import build_deskewed_pages_doc, detect_page_skew
from core.geometry import FITZ_LOCK, normalize_page_rotation
from core.pdf_engine import PDFDocumentManager

# これ未満の角度は「まっすぐ」とみなして補正対象から外す（度）
_MIN_CORRECT_ANGLE = 0.05
# 角度調整コントロールの範囲（度）
_MAX_ANGLE = 20.0

_METHOD_TEXT = {
    "text": "文字データから検出",
    "image": "画像から検出",
    None: "手入力で指定できます",
}


class DeskewDialog(QDialog):
    """傾き補正ダイアログ。

    doc は編集中の文書、page_indices は補正候補のページ番号リスト。
    page_annotations を渡すと、プレビューと補正結果に注釈が焼き込まれる
    （通常保存や抽出と同じ扱い）。
    使い終わったら dispose() で作業用コピーを閉じること。
    """

    def __init__(self, parent, doc, page_indices, page_annotations=None):
        super().__init__(parent)
        self.setWindowTitle("📐 傾き補正")
        self.setMinimumSize(720, 640)
        self._page_indices = [int(i) for i in page_indices]

        # 注釈を焼き込んだ作業用コピー（プレビューと補正の共通ソース）
        self._src_doc = PDFDocumentManager.extract_pages_as_doc(
            doc, self._page_indices, page_annotations)
        with FITZ_LOCK:
            for page in self._src_doc:
                # 表示どおりの向きで検出・補正する（台形補正と同じ正規化）
                normalize_page_rotation(page)

        # --- 傾き検出 ---
        self._detected = []   # [(angle|None, method|None), ...]
        self._angles = []     # 補正に使う角度（検出値 or 手入力）
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with FITZ_LOCK:
                for pos in range(len(self._src_doc)):
                    angle, method = detect_page_skew(self._src_doc[pos])
                    self._detected.append((angle, method))
                    self._angles.append(angle if angle is not None else 0.0)
        finally:
            QApplication.restoreOverrideCursor()

        layout = QVBoxLayout(self)

        intro = QLabel(
            "傾いてしまったページを、まっすぐに戻します。\n"
            "検出した傾きはページごとに自動で入っています。ずれている場合は、"
            "ページを選んで下のスライダーや数値で調整してください。\n"
            "※ 補正しても文字データはそのまま残ります。注釈は補正結果に焼き込まれます。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- ページ一覧 ---
        self.table = QTableWidget(len(self._page_indices), 3, self)
        self.table.setHorizontalHeaderLabels(
            ["ページ", "検出した傾き", "検出方法"])
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        for row, page_idx in enumerate(self._page_indices):
            angle, method = self._detected[row]
            angle_text = (
                f"{angle:+.2f}°" if angle is not None
                else "検出できませんでした")
            values = [f"{page_idx + 1}ページ目", angle_text,
                      _METHOD_TEXT.get(method, "")]
            for col, text in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        self.table.currentCellChanged.connect(self._on_row_changed)
        layout.addWidget(self.table)

        # --- 角度の調整 ---
        row_angle = QHBoxLayout()
        row_angle.addWidget(QLabel("補正する角度:"))
        self.spin_angle = QDoubleSpinBox(self)
        self.spin_angle.setRange(-_MAX_ANGLE, _MAX_ANGLE)
        self.spin_angle.setSingleStep(0.1)
        self.spin_angle.setDecimals(2)
        self.spin_angle.setSuffix("°")
        self.spin_angle.setMinimumWidth(110)
        self.spin_angle.valueChanged.connect(self._on_angle_changed)
        row_angle.addWidget(self.spin_angle)
        self.slider_angle = QSlider(Qt.Orientation.Horizontal, self)
        self.slider_angle.setRange(
            int(-_MAX_ANGLE * 10), int(_MAX_ANGLE * 10))
        self.slider_angle.setSingleStep(1)
        self.slider_angle.valueChanged.connect(self._on_slider_changed)
        row_angle.addWidget(self.slider_angle, 1)
        layout.addLayout(row_angle)

        # --- プレビュー ---
        self.lbl_preview = QLabel(self)
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview.setMinimumHeight(300)
        self.lbl_preview.setStyleSheet("background-color: #e1e1e1;")
        layout.addWidget(self.lbl_preview, 1)

        # --- 下部ボタン ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "まっすぐにする")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._update_preview)

        if self._page_indices:
            self.table.setCurrentCell(0, 0)
            self._sync_angle_controls()
            self._update_preview()

    # -- 内部処理 ------------------------------------------------------
    def _current_row(self):
        row = self.table.currentRow()
        if 0 <= row < len(self._page_indices):
            return row
        return 0 if self._page_indices else -1

    def _on_row_changed(self, *_args):
        self._sync_angle_controls()
        self._update_preview()

    def _sync_angle_controls(self):
        row = self._current_row()
        if row < 0:
            return
        angle = self._angles[row]
        self.spin_angle.blockSignals(True)
        self.spin_angle.setValue(angle)
        self.spin_angle.blockSignals(False)
        self.slider_angle.blockSignals(True)
        self.slider_angle.setValue(int(round(angle * 10)))
        self.slider_angle.blockSignals(False)

    def _on_angle_changed(self, value):
        row = self._current_row()
        if row < 0:
            return
        self._angles[row] = float(value)
        self.slider_angle.blockSignals(True)
        self.slider_angle.setValue(int(round(float(value) * 10)))
        self.slider_angle.blockSignals(False)
        self._preview_timer.start(120)

    def _on_slider_changed(self, value):
        row = self._current_row()
        if row < 0:
            return
        angle = value / 10.0
        self._angles[row] = angle
        self.spin_angle.blockSignals(True)
        self.spin_angle.setValue(angle)
        self.spin_angle.blockSignals(False)
        self._preview_timer.start(120)

    def _update_preview(self):
        row = self._current_row()
        if row < 0 or self._src_doc is None:
            return
        angle = self._angles[row]
        tmp = None
        try:
            tmp = build_deskewed_pages_doc(self._src_doc, [row], [angle])
            page = tmp[0]
            rect = page.rect
            zoom = min(560.0 / rect.width, 400.0 / rect.height)
            with FITZ_LOCK:
                pix = page.get_pixmap(
                    matrix=fitz.Matrix(zoom, zoom),
                    colorspace=fitz.csRGB, alpha=False)
            image = QImage(
                pix.samples, pix.width, pix.height, pix.stride,
                QImage.Format.Format_RGB888).copy()
            self.lbl_preview.setPixmap(QPixmap.fromImage(image))
        except Exception:
            self.lbl_preview.setText("プレビューを表示できませんでした")
        finally:
            if tmp is not None:
                try:
                    with FITZ_LOCK:
                        tmp.close()
                except Exception:
                    pass

    # -- 公開API -------------------------------------------------------
    @property
    def detected_angles(self):
        """ページごとの検出角度（検出できなかったページは None）。"""
        return [angle for angle, _ in self._detected]

    def angle_for_row(self, row):
        """row 行目のページに使う補正角度（検出値＋手調整）。"""
        return self._angles[row]

    def set_angle(self, row, angle):
        """row 行目のページの補正角度を設定する。"""
        self._angles[row] = float(angle)
        if row == self._current_row():
            self._sync_angle_controls()
            self._update_preview()

    def correction_items(self):
        """実際に補正する (ページ番号, 角度) のリスト。

        角度がほぼ 0° のページは対象から外す。
        """
        return [
            (page_idx, self._angles[row])
            for row, page_idx in enumerate(self._page_indices)
            if abs(self._angles[row]) >= _MIN_CORRECT_ANGLE
        ]

    def build_deskewed_doc(self):
        """補正済み文書と対象ページ番号を (indices, doc) で返す。

        doc は correction_items() と同じ順序で補正済みページを持ち、
        呼び出し側が所有する。補正対象が無ければ ValueError。
        """
        items = self.correction_items()
        if not items:
            raise ValueError("補正するページがありません。")
        rows = [self._page_indices.index(page_idx) for page_idx, _ in items]
        angles = [angle for _, angle in items]
        doc = build_deskewed_pages_doc(self._src_doc, rows, angles)
        return [page_idx for page_idx, _ in items], doc

    def dispose(self):
        if getattr(self, "_src_doc", None) is not None:
            try:
                with FITZ_LOCK:
                    self._src_doc.close()
            except Exception:
                pass
            self._src_doc = None
