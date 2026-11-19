# -*- coding: utf-8 -*-
"""PDFに埋め込まれた文字データをそのまま表示し、手直し・コピーするためのダイアログ。

AIや検索が読むのは、ページの見た目ではなくPDFに埋め込まれた文字データ。
その中身をページごとに確認し、OCRや抽出でバラバラになった文を「くっつける」で
1つの文に直してコピーできる。ここでの編集はPDF自体には反映されない
（確認・手直し・コピー専用）。
文字の取り出しは core/text_tools.extract_page_texts が担当し、このファイルは
fitz に依存しない（単体テストしやすくするため）。
"""
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core.text_tools import join_fragments

NO_TEXT_MESSAGE = "（このページに文字データはありません。画像だけのPDFです）"
NOTE_TEXT = (
    "PDFに埋め込まれている文字データそのものです。AIや検索が読むのはこの中身です。"
    "ここで直してもPDF自体は変わりません。直した文はコピーして使ってください。"
)
HINT_TEXT = (
    "OCRや文字の取り出しでバラバラになった文は、行を選んで「くっつける」を押すと"
    "改行が取れて1つの文になります（選んでいなければ全体が対象です）。"
)


class TextDataDialog(QDialog):
    """ページごとの文字データを表示・手直し・コピーするダイアログ"""

    def __init__(self, page_texts, parent=None):
        super().__init__(parent)
        self._page_texts = [str(t or "") for t in (page_texts or [])]

        self.setWindowTitle("文字データ")
        self.setMinimumSize(580, 480)
        self.setStyleSheet(
            "QDialog { background-color: #f8f9fa; color: #1a1a1a; }"
            "QLabel { background-color: transparent; color: #1a1a1a; }"
            "QCheckBox { background-color: transparent; color: #1a1a1a; }"
            "QPlainTextEdit, QComboBox { "
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

        note_lbl = QLabel(NOTE_TEXT)
        note_lbl.setWordWrap(True)
        note_lbl.setStyleSheet("color: #444; font-size: 11px; margin-bottom: 4px;")
        layout.addWidget(note_lbl)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("表示ページ:"))
        self.page_combo = QComboBox()
        self.page_combo.addItem("全ページ")
        for i in range(len(self._page_texts)):
            self.page_combo.addItem(f"{i + 1}ページ目")
        self.page_combo.currentIndexChanged.connect(self._reload_text)
        top_row.addWidget(self.page_combo)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setMinimumHeight(260)
        layout.addWidget(self.text_edit)

        hint_lbl = QLabel(HINT_TEXT)
        hint_lbl.setWordWrap(True)
        hint_lbl.setStyleSheet("color: #444; font-size: 11px;")
        layout.addWidget(hint_lbl)

        btn_row = QHBoxLayout()
        self.btn_join = QPushButton("くっつける")
        self.btn_join.setToolTip(
            "選択した行（なければ全体）の改行を取り除いて1行につなげます"
        )
        self.btn_join.clicked.connect(self._on_join_clicked)
        btn_row.addWidget(self.btn_join)

        self.chk_remove_spaces = QCheckBox("空白も取る")
        self.chk_remove_spaces.setToolTip(
            "つなげたあと、半角・全角のスペースもすべて取り除きます"
        )
        btn_row.addWidget(self.chk_remove_spaces)

        btn_row.addStretch(1)

        self.btn_copy = QPushButton("📋 コピー")
        self.btn_copy.setToolTip(
            "選択中の文字をコピーします（選択がなければ全体をコピーします）"
        )
        self.btn_copy.clicked.connect(self._on_copy_clicked)
        btn_row.addWidget(self.btn_copy)

        self.btn_close = QPushButton("閉じる")
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)
        layout.addLayout(btn_row)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #256d2a; font-size: 11px;")
        layout.addWidget(self.lbl_status)

        self._reload_text()

    def _page_display_text(self, page_idx):
        text = self._page_texts[page_idx]
        if text.strip():
            return text
        return NO_TEXT_MESSAGE

    def _current_display_text(self):
        idx = self.page_combo.currentIndex()
        if idx <= 0:
            if not self._page_texts:
                return NO_TEXT_MESSAGE
            blocks = []
            for i in range(len(self._page_texts)):
                body = self._page_display_text(i).rstrip()
                blocks.append(f"―― {i + 1}ページ目 ――\n{body}")
            return "\n\n".join(blocks)
        page_idx = idx - 1
        if page_idx >= len(self._page_texts):
            return NO_TEXT_MESSAGE
        return self._page_display_text(page_idx)

    def _reload_text(self):
        self.text_edit.setPlainText(self._current_display_text())
        self.lbl_status.setText("")

    def _on_join_clicked(self):
        remove_spaces = self.chk_remove_spaces.isChecked()
        cursor = self.text_edit.textCursor()
        if cursor.hasSelection():
            selected = cursor.selectedText().replace("\u2029", "\n")
            joined = join_fragments(selected, remove_spaces=remove_spaces)
            if joined != selected:
                cursor.insertText(joined)
                self.text_edit.setTextCursor(cursor)
            self.lbl_status.setText("選択した行をくっつけました")
        else:
            joined = join_fragments(
                self.text_edit.toPlainText(), remove_spaces=remove_spaces
            )
            self.text_edit.setPlainText(joined)
            self.lbl_status.setText("全体をくっつけました")

    def _on_copy_clicked(self):
        cursor = self.text_edit.textCursor()
        if cursor.hasSelection():
            text = cursor.selectedText().replace("\u2029", "\n")
        else:
            text = self.text_edit.toPlainText()
        QApplication.clipboard().setText(text)
        self.lbl_status.setText("コピーしました")
