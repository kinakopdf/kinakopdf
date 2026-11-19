# -*- coding: utf-8 -*-
"""ベースライン一致テキストアイテムおよびテキスト編集・削除ダイアログ"""
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFontMetricsF,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGraphicsTextItem,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core import COLOR_PRESETS, TEXT_LINE_SPACING
from ui import (
    create_color_swatch_icon,
    qcolor_to_rgb_tuple,
    rgb_tuple_to_qcolor,
    sync_color_combo,
)
from core.geometry import (
    MAX_FONT_SIZE,
    MIN_FONT_SIZE,
    TEXT_FONT_SIZE,
    create_configured_preview_font,
)


class PreciseTextAnnotationItem(QGraphicsTextItem):
    """
    OSフォント固有のQtドキュメント余白やAscent差による位置ズレを排除し、
    PDF焼き込み時（bake_annotations_to_doc）およびサムネイル描画時と
    100%同じベースライン座標・行送り（0.88 + idx * TEXT_LINE_SPACING）で描画するテキストアイテム
    """

    def __init__(self, text, pixel_size, text_color, font_family):
        super().__init__()
        self._raw_text = str(text or "")
        self._lines = self._raw_text.split("\n") if self._raw_text else [""]
        self._pixel_size = max(1.0, float(pixel_size))
        self._text_color = QColor(text_color)

        font = create_configured_preview_font(font_family, self._pixel_size)
        self.setFont(font)
        self.setDefaultTextColor(self._text_color)
        self.document().setDocumentMargin(0)

    def boundingRect(self):
        fm = QFontMetricsF(self.font())
        max_w = max((fm.horizontalAdvance(line) for line in self._lines), default=0.0)
        max_w = max(max_w, self._pixel_size * 0.6)
        num_lines = max(1, len(self._lines))
        total_h = self._pixel_size * (1.15 + (num_lines - 1) * TEXT_LINE_SPACING)
        top_pad = self._pixel_size * 0.15
        return QRectF(-2.0, -top_pad, max_w + 6.0, total_h + top_pad)

    def contains(self, point: QPointF) -> bool:
        return self.boundingRect().adjusted(-4.0, -4.0, 4.0, 4.0).contains(point)

    def shape(self):
        path = QPainterPath()
        path.addRect(self.boundingRect().adjusted(-2.0, -2.0, 2.0, 2.0))
        return path

    def paint(self, painter, option, widget=None):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setFont(self.font())
        painter.setPen(QPen(self._text_color))
        for idx, line_str in enumerate(self._lines):
            if line_str:
                y_base = self._pixel_size * (0.88 + idx * TEXT_LINE_SPACING)
                painter.drawText(QPointF(0.0, y_base), line_str)
        painter.restore()


class TextEditDeleteDialog(QDialog):
    """
    新規テキストボックスの挿入、または既存のPDF内文字・追加済みテキスト注釈の
    複数行編集・フォントサイズ・色の変更・削除を行うダイアログ
    """

    ACTION_NONE = None
    ACTION_UPDATE = "update"
    ACTION_DELETE = "delete"

    def __init__(self, text, fontsize, color, is_pdf_origin=False, is_new=False, parent=None):
        super().__init__(parent)
        self.result_action = self.ACTION_NONE
        self._is_new = bool(is_new)
        self._selected_color = (
            tuple(color[:3]) if (color and len(color) >= 3) else (0.0, 0.0, 0.0)
        )

        if self._is_new:
            self.setWindowTitle("新規テキストボックス入力（複数行対応）")
        elif is_pdf_origin:
            self.setWindowTitle("PDF内の文字を編集・削除（複数行対応）")
        else:
            self.setWindowTitle("テキストボックスの編集・削除（複数行対応）")
        self.setMinimumWidth(460)
        self.setStyleSheet(
            "QDialog { background-color: #f8f9fa; color: #1a1a1a; }"
            "QLabel { background-color: transparent; color: #1a1a1a; }"
            "QPlainTextEdit, QDoubleSpinBox, QComboBox { "
            "  background-color: #ffffff; color: #111111; "
            "  border: 1px solid #b0b5bd; border-radius: 4px; padding: 4px; "
            "  selection-background-color: #0066cc; selection-color: #ffffff; "
            "}"
            "QComboBox QAbstractItemView { "
            "  background-color: #ffffff; color: #111111; "
            "  selection-background-color: #e6f0fa; selection-color: #111111; "
            "}"
            "QPushButton { "
            "  background-color: #ffffff; color: #1a1a1a; "
            "  border: 1px solid #a0a5ad; border-radius: 4px; padding: 5px 12px; "
            "}"
            "QPushButton:hover { background-color: #eef2f7; border-color: #70757d; }"
        )

        layout = QVBoxLayout(self)

        if is_pdf_origin and not self._is_new:
            info_lbl = QLabel(
                "元のPDF内の文字を消去し、新しい文字・サイズ・色に置き換えます（表の罫線や背景は消えません）。"
            )
            info_lbl.setWordWrap(True)
            info_lbl.setStyleSheet("color: #444; font-size: 11px; margin-bottom: 4px;")
            layout.addWidget(info_lbl)

        if self._is_new:
            prompt_text = "挿入するテキストを入力してください（Enterで改行 / Ctrl+Enterで確定）:"
        else:
            prompt_text = "テキスト内容（Enterで改行 / Ctrl+Enterで確定 / 空欄で更新すると削除）:"
        layout.addWidget(QLabel(prompt_text))

        self.text_edit = QPlainTextEdit(str(text or ""))
        self.text_edit.setMinimumHeight(95)
        self.text_edit.setTabChangesFocus(True)
        self.text_edit.selectAll()
        layout.addWidget(self.text_edit)

        shortcut_submit = QShortcut(QKeySequence("Ctrl+Return"), self)
        shortcut_submit.activated.connect(self._on_update_clicked)
        shortcut_submit_enter = QShortcut(QKeySequence("Ctrl+Enter"), self)
        shortcut_submit_enter.activated.connect(self._on_update_clicked)

        prop_layout = QHBoxLayout()

        prop_layout.addWidget(QLabel("文字サイズ:"))
        self.spin_size = QDoubleSpinBox()
        self.spin_size.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self.spin_size.setDecimals(1)
        self.spin_size.setSingleStep(0.5)
        self.spin_size.setSuffix(" pt")
        self.spin_size.setValue(
            max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, float(fontsize or TEXT_FONT_SIZE)))
        )
        prop_layout.addWidget(self.spin_size)

        prop_layout.addSpacing(12)
        prop_layout.addWidget(QLabel("文字色:"))
        self.combo_color = QComboBox()
        self.combo_color.setMinimumWidth(140)
        for name, rgb in COLOR_PRESETS:
            self.combo_color.addItem(create_color_swatch_icon(rgb), name, rgb)
        self.combo_color.addItem("カスタム色...", "custom")
        self._sync_color_combo(self._selected_color)
        self.combo_color.activated.connect(self._on_color_activated)
        prop_layout.addWidget(self.combo_color)
        prop_layout.addStretch()

        layout.addLayout(prop_layout)
        layout.addSpacing(8)

        btn_layout = QHBoxLayout()
        if not self._is_new:
            self.btn_delete = QPushButton("🗑 この文字を削除")
            self.btn_delete.setStyleSheet("color: #b00020; font-weight: bold; padding: 5px 10px;")
            self.btn_delete.setAutoDefault(False)
            self.btn_delete.clicked.connect(self._on_delete_clicked)
            btn_layout.addWidget(self.btn_delete)

        self.btn_copy = QPushButton("📋 コピー")
        self.btn_copy.setAutoDefault(False)
        self.btn_copy.setToolTip("テキストをコピーしてこの画面を閉じます（変更はしません）")
        self.btn_copy.clicked.connect(self._on_copy_clicked)
        btn_layout.addWidget(self.btn_copy)

        self.btn_cancel = QPushButton("キャンセル")
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.clicked.connect(self.reject)

        submit_label = "✔ 挿入する (Ctrl+Enter)" if self._is_new else "✔ 更新する (Ctrl+Enter)"
        self.btn_update = QPushButton(submit_label)
        self.btn_update.setDefault(True)
        self.btn_update.setStyleSheet("font-weight: bold; padding: 5px 14px;")
        self.btn_update.clicked.connect(self._on_update_clicked)

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_update)
        layout.addLayout(btn_layout)

    def _sync_color_combo(self, rgb_tuple):
        sync_color_combo(self.combo_color, rgb_tuple, pen_eraser_active=False)

    def _on_color_activated(self, index):
        data = self.combo_color.itemData(index)
        if data == "custom":
            initial = rgb_tuple_to_qcolor(self._selected_color)
            chosen = QColorDialog.getColor(initial, self, "文字色を選択")
            if chosen.isValid():
                self._selected_color = qcolor_to_rgb_tuple(chosen)
            self._sync_color_combo(self._selected_color)
        elif isinstance(data, tuple):
            self._selected_color = data

    def _on_update_clicked(self):
        self.result_action = self.ACTION_UPDATE
        self.accept()

    def _on_delete_clicked(self):
        self.result_action = self.ACTION_DELETE
        self.accept()

    def _on_copy_clicked(self):
        cursor = self.text_edit.textCursor()
        if cursor.hasSelection():
            text = cursor.selectedText().replace("\u2029", "\n")
        else:
            text = self.text_edit.toPlainText()
        QApplication.clipboard().setText(text)
        self.result_action = self.ACTION_NONE
        self.reject()

    def get_clean_text(self):
        raw = self.text_edit.toPlainText()
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.strip():
            return ""
        lines = [line.rstrip() for line in normalized.split("\n")]
        return "\n".join(lines).strip("\n")

    def get_fontsize(self):
        return round(float(self.spin_size.value()), 1)

    def get_color(self):
        return tuple(self._selected_color)