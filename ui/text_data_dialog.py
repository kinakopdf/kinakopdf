# -*- coding: utf-8 -*-
"""PDFに埋め込まれた文字データを表示し、手直し・コピー・PDFへの反映をするダイアログ。

AIや検索が読むのは、ページの見た目ではなくPDFに埋め込まれた文字データ。
このダイアログには2つの見方がある。

- 配置表示: ページの元の配置のまま、画像の文字が文字データに入れ替わったように
スパンを並べて表示する。バラバラになった断片を選んで「くっつける」と1つの文に
なり、「PDFに反映」でPDFの文字データ自体を置き換えられる（反映は呼び出し側が
既存のテキスト置換と同じ注釈＋コマンド方式で行う）。
- テキスト表示: ページごとの文章として確認し、手直ししてコピーできる。

編集中の文章とくっつけプレビューは、呼び出し側（MainWindow）が持つ
session_state にページ単位で保持され、ダイアログを閉じても文書が同じ間は残る。
文字の取り出しは core/text_tools が担当し、このファイルは fitz に依存しない
（単体テストしやすくするため）。
"""
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
QApplication,
QCheckBox,
QComboBox,
QDialog,
QHBoxLayout,
QLabel,
QPlainTextEdit,
QPushButton,
QTabWidget,
QVBoxLayout,
QWidget,
)

from core.text_tools import join_fragments, join_span_texts

NO_TEXT_MESSAGE = "（このページに文字データはありません。画像だけのPDFです）"
NOTE_TEXT = (
"PDFに埋め込まれている文字データそのものです。AIや検索が読むのはこの中身です。"
"では元の場所のまま文字が見えます。バラバラになった部分を選んで"
"「くっつける」→確認して「PDFに反映」を押すと、PDFの文字データ自体を直せます。"
"では文章として確認・手直し・コピーができます。"
)
HINT_TEXT = (
"OCRや文字の取り出しでバラバラになった文は、行を選んで「くっつける」を押すと"
"改行が取れて1つの文になります（選んでいなければ全体が対象です）。"
"ここでの編集はPDF自体には反映されません。"
)
LAYOUT_HINT_TEXT = (
"クリックで文字のかたまり（スパン）を選んだり外したりできます。"
"バラバラになった文のかたまりを全部選んで「くっつける」を押してください。"
)
APPLY_NOTE_TEXT = (
"「PDFに反映」を押すと、選んだ部分の元の文字は消えて、つなげた文が同じ場所に"
"入ります。フォントは元のものと変わります。反映はメイン画面の「元に戻す」で"
"取り消せます。"
)

_DIALOG_STYLE = (
"QDialog { background-color: #f8f9fa; color: #1a1a1a;}"
"QLabel { background-color: transparent; color: #1a1a1a;}"
"QCheckBox { background-color: transparent; color: #1a1a1a;}"
"QPlainTextEdit, QComboBox { "
" background-color: #ffffff; color: #111111; "
" border: 1px solid #b0b5bd; border-radius: 4px; padding: 4px; "
" selection-background-color: #0066cc; selection-color: #ffffff; "
"}"
"QComboBox QAbstractItemView { "
" background-color: #ffffff; color: #111111; "
" selection-background-color: #e6f0fa; selection-color: #111111; "
"}"
"QPushButton { "
" background-color: #ffffff; color: #1a1a1a; "
" border: 1px solid #a0a5ad; border-radius: 4px; padding: 5px 12px; "
"}"
"QPushButton:hover { background-color: #eef2f7; border-color: #70757d;}"
"QPushButton:disabled { color: #9aa0a6; border-color: #ccd1d6;}"
"QTabWidget::pane { border: 1px solid #b0b5bd; border-radius: 4px; "
" background-color: #ffffff;}"
)


class LayoutCanvas(QWidget):
    """1ページ分のスパンを元の配置のまま描くキャンバス。

    ページ画像の代わりに、抽出された文字データだけを同じ位置・大きさで描く。
    クリックでスパンの選択/解除ができ、選択中はハイライトする。
    """

    selectionChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page_rect = (0.0, 0.0, 1.0, 1.0)
        self._spans = []
        self._selected = set()
        self._underlay = None
        self._show_underlay = False
        self.setMinimumSize(440, 300)

    def set_page(self, page_rect, spans):
        if page_rect and len(page_rect) == 4:
            self._page_rect = tuple(float(v) for v in page_rect)
        self._spans = list(spans or [])
        self._selected = set()
        self.update()

    def set_underlay(self, pixmap):
        self._underlay = pixmap
        self.update()

    def set_show_underlay(self, show):
        self._show_underlay = bool(show)
        self.update()

    def selected_indices(self):
        return sorted(self._selected)

    def selected_spans(self):
        return [self._spans[i] for i in sorted(self._selected)]

    def set_selected(self, indices):
        self._selected = {
            int(i) for i in (indices or []) if 0 <= int(i) < len(self._spans)
        }
        self.update()
        self.selectionChanged.emit()

    def clear_selection(self):
        if self._selected:
            self._selected = set()
            self.update()
            self.selectionChanged.emit()

    def _page_area(self):
        """ウィジェット内に収まるページ表示矩形 (QRectF) と倍率を返す。"""
        x0, y0, x1, y1 = self._page_rect
        pw = max(1.0, x1 - x0)
        ph = max(1.0, y1 - y0)
        scale = min(self.width() / pw, self.height() / ph)
        disp_w = pw * scale
        disp_h = ph * scale
        ox = (self.width() - disp_w) / 2.0
        oy = (self.height() - disp_h) / 2.0
        return QRectF(ox, oy, disp_w, disp_h), scale

    def _span_widget_rect(self, span, area, scale):
        x0, y0, _, _ = self._page_rect
        r = span["rect"]
        return QRectF(
            area.x() + (r[0] - x0) * scale,
            area.y() + (r[1] - y0) * scale,
            max(1.0, (r[2] - r[0]) * scale),
            max(1.0, (r[3] - r[1]) * scale),
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#e8eaed"))

        area, scale = self._page_area()
        painter.fillRect(area, QColor("#ffffff"))
        painter.setPen(QPen(QColor("#9aa0a6"), 1))
        painter.drawRect(area)

        if (
            self._show_underlay
            and self._underlay is not None
            and not self._underlay.isNull()
        ):
            painter.save()
            painter.setOpacity(0.22)
            painter.drawPixmap(area.toRect(), self._underlay)
            painter.restore()

        if not self._spans:
            painter.setPen(QPen(QColor("#666666")))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, NO_TEXT_MESSAGE)
            painter.end()
            return

        x0, y0, _, _ = self._page_rect
        for idx, span in enumerate(self._spans):
            wrect = self._span_widget_rect(span, area, scale)
            if idx in self._selected:
                painter.fillRect(wrect, QColor(0, 102, 204, 55))
                painter.setPen(QPen(QColor(0, 102, 204), 1))
                painter.drawRect(wrect)

            font = QFont()
            font.setPixelSize(max(6, int(round(float(span["fontsize"]) * scale))))
            painter.setFont(font)
            painter.setPen(QPen(QColor("#1a1a1a")))
            origin = span.get("origin") or (span["rect"][0], span["rect"][3])
            painter.drawText(
                QPointF(
                    area.x() + (origin[0] - x0) * scale,
                    area.y() + (origin[1] - y0) * scale,
                ),
                str(span["text"]),
            )
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        area, scale = self._page_area()
        pos = event.position()
        # 後ろに描かれたスパンほど手前にあるものとして、逆順に判定する
        for idx in range(len(self._spans) - 1, -1, -1):
            wrect = self._span_widget_rect(self._spans[idx], area, scale)
            if wrect.adjusted(-2, -2, 2, 2).contains(pos):
                if idx in self._selected:
                    self._selected.discard(idx)
                else:
                    self._selected.add(idx)
                self.update()
                self.selectionChanged.emit()
                return


class TextDataDialog(QDialog):
    """ページごとの文字データを表示・手直し・コピーし、PDFへ反映するダイアログ"""

    def __init__(
        self,
        page_texts,
        parent=None,
        *,
        page_spans=None,
        page_rects=None,
        session_state=None,
        apply_handler=None,
        refresh_handler=None,
        image_provider=None,
    ):
        super().__init__(parent)
        self._page_texts = [str(t or "") for t in (page_texts or [])]
        page_count = len(self._page_texts)
        self._page_spans = [list(spans or []) for spans in (page_spans or [])]
        while len(self._page_spans) < page_count:
            self._page_spans.append([])
        self._page_rects = [
            tuple(float(v) for v in rect) for rect in (page_rects or [])
        ]
        while len(self._page_rects) < page_count:
            self._page_rects.append((0.0, 0.0, 595.0, 842.0))

        if session_state is None:
            session_state = {}
        session_state.setdefault("pages", {})
        session_state.setdefault("all", None)
        session_state.setdefault("join", {})
        self._session = session_state
        self._apply_handler = apply_handler
        self._refresh_handler = refresh_handler
        self._image_provider = image_provider
        self._image_cache = {}
        self._loading = False

        self.setWindowTitle("文字データ")
        self.setMinimumSize(640, 560)
        self.setStyleSheet(_DIALOG_STYLE)

        layout = QVBoxLayout(self)

        note_lbl = QLabel(NOTE_TEXT)
        note_lbl.setWordWrap(True)
        note_lbl.setStyleSheet("color: #444; font-size: 11px; margin-bottom: 4px;")
        layout.addWidget(note_lbl)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("表示ページ:"))
        self.page_combo = QComboBox()
        self.page_combo.addItem("全ページ")
        for i in range(page_count):
            self.page_combo.addItem(f"{i + 1}ページ目")
        self.page_combo.currentIndexChanged.connect(self._on_page_changed)
        top_row.addWidget(self.page_combo)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_layout_tab(), "配置表示")
        self.tabs.addTab(self._build_text_tab(), "テキスト表示")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs)

        bottom_row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #256d2a; font-size: 11px;")
        bottom_row.addWidget(self.lbl_status)
        bottom_row.addStretch(1)
        self.btn_close = QPushButton("閉じる")
        self.btn_close.clicked.connect(self.accept)
        bottom_row.addWidget(self.btn_close)
        layout.addLayout(bottom_row)

        self._reload_all()

    def _build_layout_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)

        hint = QLabel(LAYOUT_HINT_TEXT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #444; font-size: 11px;")
        v.addWidget(hint)

        self.layout_canvas = LayoutCanvas()
        v.addWidget(self.layout_canvas, 1)

        opt_row = QHBoxLayout()
        self.chk_underlay = QCheckBox("元の画像を薄く重ねる")
        self.chk_underlay.setToolTip(
            "ページの画像を薄く下に敷いて、文字データの位置と見比べられます"
        )
        self.chk_underlay.toggled.connect(self._on_underlay_toggled)
        opt_row.addWidget(self.chk_underlay)
        opt_row.addStretch(1)
        self.btn_layout_join = QPushButton("くっつける")
        self.btn_layout_join.setToolTip(
            "選んだかたまりの文字を、並んでいる順に1つの文につなげます"
        )
        self.btn_layout_join.clicked.connect(self._on_layout_join_clicked)
        opt_row.addWidget(self.btn_layout_join)
        v.addLayout(opt_row)

        v.addWidget(QLabel("つなげた文（ここで直してから反映できます）:"))
        self.join_preview = QPlainTextEdit()
        self.join_preview.setMinimumHeight(44)
        self.join_preview.setMaximumHeight(64)
        self.join_preview.textChanged.connect(self._on_join_preview_changed)
        v.addWidget(self.join_preview)

        apply_note = QLabel(APPLY_NOTE_TEXT)
        apply_note.setWordWrap(True)
        apply_note.setStyleSheet("color: #444; font-size: 11px;")
        v.addWidget(apply_note)

        apply_row = QHBoxLayout()
        apply_row.addStretch(1)
        self.btn_apply = QPushButton("PDFに反映")
        self.btn_apply.setToolTip(
            "選んだ部分の元の文字を消して、つなげた文を同じ場所に入れます"
        )
        self.btn_apply.setEnabled(self._apply_handler is not None)
        self.btn_apply.clicked.connect(self._on_apply_clicked)
        apply_row.addWidget(self.btn_apply)
        v.addLayout(apply_row)
        return tab

    def _build_text_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setMinimumHeight(260)
        self.text_edit.textChanged.connect(self._on_text_edited)
        v.addWidget(self.text_edit, 1)

        hint_lbl = QLabel(HINT_TEXT)
        hint_lbl.setWordWrap(True)
        hint_lbl.setStyleSheet("color: #444; font-size: 11px;")
        v.addWidget(hint_lbl)

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
        v.addLayout(btn_row)
        return tab


    def _layout_page_idx(self):
        """配置表示の対象ページ。全ページ選択時は先頭ページに寄せる。"""
        idx = self.page_combo.currentIndex()
        if idx <= 0:
            return 0 if self._page_texts else None
        page_idx = idx - 1
        return page_idx if page_idx < len(self._page_texts) else None

    def _on_page_changed(self):
        # 配置表示タブでは「全ページ」を選んでも先頭ページに寄せる
        tabs = getattr(self, "tabs", None)
        if (
            tabs is not None
            and tabs.currentIndex() == 0
            and self.page_combo.currentIndex() == 0
            and self._page_texts
        ):
            self.page_combo.setCurrentIndex(1)
            return  # コンボ変更側で _reload_all が呼ばれる
        self._reload_all()

    def _on_tab_changed(self, tab_idx):
        # 配置表示は1ページ単位なので、「全ページ」なら先頭ページに切り替える
        if tab_idx == 0 and self.page_combo.currentIndex() == 0 and self._page_texts:
            self.page_combo.setCurrentIndex(1)
            return  # コンボ変更側で _reload_all が呼ばれる
        self._reload_layout()

    def _reload_all(self):
        self._reload_text()
        self._reload_layout()
        self.lbl_status.setText("")

    def _page_display_text(self, page_idx):
        text = self._page_texts[page_idx]
        if text.strip():
            return text
        return NO_TEXT_MESSAGE

    def _current_display_text(self):
        idx = self.page_combo.currentIndex()
        if idx <= 0:
            saved_all = self._session.get("all")
            if saved_all is not None:
                return saved_all
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
        saved = self._session.get("pages", {}).get(page_idx)
        if saved is not None:
            return saved
        return self._page_display_text(page_idx)

    def _reload_text(self):
        self._loading = True
        try:
            self.text_edit.setPlainText(self._current_display_text())
        finally:
            self._loading = False

    def _on_text_edited(self):
        if self._loading:
            return
        idx = self.page_combo.currentIndex()
        text = self.text_edit.toPlainText()
        if idx <= 0:
            self._session["all"] = text
        else:
            self._session["pages"][idx - 1] = text

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

    def _reload_layout(self):
        page_idx = self._layout_page_idx()
        if page_idx is None:
            self.layout_canvas.set_page((0.0, 0.0, 1.0, 1.0), [])
            self._set_join_preview("")
            return
        self.layout_canvas.set_page(
            self._page_rects[page_idx], self._page_spans[page_idx]
        )
        saved = self._session.get("join", {}).get(page_idx)
        self._set_join_preview(saved if saved is not None else "")
        self._refresh_underlay()

    def _set_join_preview(self, text):
        self._loading = True
        try:
            self.join_preview.setPlainText(text)
        finally:
            self._loading = False

    def _on_join_preview_changed(self):
        if self._loading:
            return
        page_idx = self._layout_page_idx()
        if page_idx is not None:
            self._session["join"][page_idx] = self.join_preview.toPlainText()

    def _on_underlay_toggled(self, checked):
        self._refresh_underlay()

    def _refresh_underlay(self):
        show = self.chk_underlay.isChecked()
        page_idx = self._layout_page_idx()
        if show and page_idx is not None and self._image_provider is not None:
            if page_idx not in self._image_cache:
                try:
                    self._image_cache[page_idx] = self._image_provider(page_idx)
                except Exception:
                    self._image_cache[page_idx] = None
            self.layout_canvas.set_underlay(self._image_cache.get(page_idx))
        self.layout_canvas.set_show_underlay(show)

    def _on_layout_join_clicked(self):
        spans = self.layout_canvas.selected_spans()
        if not spans:
            self.lbl_status.setText("先に、くっつけたい文字を選んでください")
            return
        joined = join_span_texts([sp["text"] for sp in spans])
        self._set_join_preview(joined)
        # プレビュー欄への反映も作業状態として保持する
        page_idx = self._layout_page_idx()
        if page_idx is not None:
            self._session["join"][page_idx] = joined
        self.lbl_status.setText("選んだ部分をくっつけました")

    def _on_apply_clicked(self):
        if self._apply_handler is None:
            return
        page_idx = self._layout_page_idx()
        spans = self.layout_canvas.selected_spans()
        joined = self.join_preview.toPlainText().strip()
        if page_idx is None or not spans:
            self.lbl_status.setText("先に、直したい文字を選んでください")
            return
        if not joined:
            self.lbl_status.setText("つなげた文が空です。「くっつける」を押してください")
            return
        ok = self._apply_handler(page_idx, spans, joined)
        if not ok:
            self.lbl_status.setText("PDFに反映できませんでした")
            return

        # 反映で変わったページの作業状態は破棄し、最新データを取り直す
        self._session["pages"].pop(page_idx, None)
        self._session["join"].pop(page_idx, None)
        self._session["all"] = None
        self._image_cache.pop(page_idx, None)
        if self._refresh_handler is not None:
            try:
                page_texts, page_spans = self._refresh_handler()
            except Exception:
                page_texts, page_spans = None, None
            if page_texts is not None:
                self._page_texts = [str(t or "") for t in page_texts]
            if page_spans is not None:
                self._page_spans = [list(s or []) for s in page_spans]
                while len(self._page_spans) < len(self._page_texts):
                    self._page_spans.append([])
        self._reload_all()
        self.lbl_status.setText("PDFに反映しました。メイン画面で保存すると確定します")
