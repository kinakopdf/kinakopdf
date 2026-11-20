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
from PyQt6.QtCore import QPointF, QRectF, QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
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
"拡大・縮小ボタンや Ctrl+ホイールで大きくでき、ドラッグで表示位置を動かせます。"
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


def large_dialog_size(fraction=0.92):
    """ダイアログの初期サイズとして、画面の利用可能領域の fraction 倍を返す。

    配置表示でページ全体を小さく押し込むと文字が読めなくなるため、
    画面いっぱいに近く大きく開く。小さい画面では画面サイズまでに抑える。
    """
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QSize(1100, 800)
    avail = screen.availableGeometry()
    width = min(avail.width(), max(1024, int(avail.width() * fraction)))
    height = min(avail.height(), max(720, int(avail.height() * fraction)))
    return QSize(width, height)


class LayoutCanvas(QWidget):
    """1ページ分のスパンを元の配置のまま描くキャンバス。

    ページ画像の代わりに、抽出された文字データだけを同じ位置・大きさで描く。
    クリックでスパンの選択/解除ができ、選択中はハイライトする。
    全体表示を基準に 0.5〜4 倍までズームでき、はみ出した部分はドラッグで
    表示位置を動かして見られる（パン）。文字はベクタ描画なので、
    拡大してもぼやけない。
    """

    selectionChanged = pyqtSignal()
    viewChanged = pyqtSignal()

    MIN_ZOOM = 0.5
    MAX_ZOOM = 4.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page_rect = (0.0, 0.0, 1.0, 1.0)
        self._spans = []
        self._selected = set()
        self._underlay = None
        self._show_underlay = False
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._press_pos = None
        self._press_span = None
        self._panning = False
        self._last_pos = None
        self.setMinimumSize(440, 300)

    def set_page(self, page_rect, spans):
        if page_rect and len(page_rect) == 4:
            self._page_rect = tuple(float(v) for v in page_rect)
        self._spans = list(spans or [])
        self._selected = set()
        # ページが替わったら表示位置だけ戻す（ズーム倍率は維持する）
        self._pan = QPointF(0.0, 0.0)
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

    # --- ズームとパン ---

    def zoom_factor(self):
        return self._zoom

    def pan_offset(self):
        return QPointF(self._pan)

    def set_pan_offset(self, offset):
        self._pan = QPointF(offset)
        self._clamp_pan()
        self.update()

    def _fit_scale(self):
        """全体表示（ズーム1倍）のときの、ページ座標→画面ピクセルの倍率。"""
        x0, y0, x1, y1 = self._page_rect
        pw = max(1.0, x1 - x0)
        ph = max(1.0, y1 - y0)
        return min(self.width() / pw, self.height() / ph)

    def effective_scale(self):
        """実効倍率（全体表示の倍率 × ズーム）。下敷き画像の解像度決定に使う。"""
        return self._fit_scale() * self._zoom

    def set_zoom(self, zoom, center=None):
        """ズーム倍率を変える。center（画面座標）の下にある場所を中心に拡縮する。"""
        try:
            new_zoom = float(zoom)
        except (TypeError, ValueError):
            return
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, new_zoom))
        if center is None:
            center = QPointF(self.width() / 2.0, self.height() / 2.0)
        old_area, old_scale = self._page_area()
        page_x = page_y = 0.0
        if old_scale > 0:
            page_x = (center.x() - old_area.x()) / old_scale
            page_y = (center.y() - old_area.y()) / old_scale
        self._zoom = new_zoom
        new_scale = self.effective_scale()
        x0, y0, x1, y1 = self._page_rect
        pw = max(1.0, x1 - x0)
        ph = max(1.0, y1 - y0)
        base_ox = (self.width() - pw * new_scale) / 2.0
        base_oy = (self.height() - ph * new_scale) / 2.0
        self._pan = QPointF(
            center.x() - page_x * new_scale - base_ox,
            center.y() - page_y * new_scale - base_oy,
        )
        self._clamp_pan()
        self.update()
        self.viewChanged.emit()

    def zoom_in(self):
        self.set_zoom(self._zoom * 1.25)

    def zoom_out(self):
        self.set_zoom(self._zoom / 1.25)

    def zoom_fit(self):
        """全体表示に戻し、表示位置も中央に戻す。"""
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()
        self.viewChanged.emit()

    def _clamp_pan(self):
        """ページが画面から消え切らない範囲に表示位置を収める。"""
        if self.width() <= 0 or self.height() <= 0:
            return
        scale = self.effective_scale()
        x0, y0, x1, y1 = self._page_rect
        pw = max(1.0, x1 - x0)
        ph = max(1.0, y1 - y0)
        disp_w = pw * scale
        disp_h = ph * scale
        margin = 48.0
        base_ox = (self.width() - disp_w) / 2.0
        base_oy = (self.height() - disp_h) / 2.0
        min_px = (margin - disp_w) - base_ox
        max_px = (self.width() - margin) - base_ox
        min_py = (margin - disp_h) - base_oy
        max_py = (self.height() - margin) - base_oy
        self._pan = QPointF(
            max(min_px, min(max_px, self._pan.x())),
            max(min_py, min(max_py, self._pan.y())),
        )

    def _page_area(self):
        """ウィジェット内のページ表示矩形 (QRectF) と実効倍率を返す。

        ズームとパンを込みで計算するので、描画とクリック判定は
        常にこの結果を共有してずれない。
        """
        x0, y0, x1, y1 = self._page_rect
        pw = max(1.0, x1 - x0)
        ph = max(1.0, y1 - y0)
        scale = self._fit_scale() * self._zoom
        disp_w = pw * scale
        disp_h = ph * scale
        ox = (self.width() - disp_w) / 2.0 + self._pan.x()
        oy = (self.height() - disp_h) / 2.0 + self._pan.y()
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

    def _hit_span(self, pos):
        """画面座標 pos の下にあるスパンの番号を返す（なければ None）。"""
        area, scale = self._page_area()
        # 後ろに描かれたスパンほど手前にあるものとして、逆順に判定する
        for idx in range(len(self._spans) - 1, -1, -1):
            wrect = self._span_widget_rect(self._spans[idx], area, scale)
            if wrect.adjusted(-2, -2, 2, 2).contains(pos):
                return idx
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # 選択の確定はリリース時。ドラッグしたらパンに切り替える
        self._press_pos = event.position()
        self._last_pos = event.position()
        self._press_span = self._hit_span(event.position())
        self._panning = False

    def mouseMoveEvent(self, event):
        if self._press_pos is None:
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        pos = event.position()
        if not self._panning:
            if (pos - self._press_pos).manhattanLength() <= 5.0:
                self._last_pos = pos
                return
            self._panning = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self._pan += pos - self._last_pos
        self._last_pos = pos
        self._clamp_pan()
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        was_panning = self._panning
        press_span = self._press_span
        self._press_pos = None
        self._press_span = None
        self._last_pos = None
        self._panning = False
        self.unsetCursor()
        if was_panning or press_span is None:
            return
        # 押し始めと離した場所が同じスパンの上ならクリックとみなす
        if self._hit_span(event.position()) != press_span:
            return
        if press_span in self._selected:
            self._selected.discard(press_span)
        else:
            self._selected.add(press_span)
        self.update()
        self.selectionChanged.emit()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                self.set_zoom(
                    self._zoom * (1.2 ** steps), center=event.position()
                )
            event.accept()
            return
        delta = event.angleDelta()
        if delta.x() or delta.y():
            # 通常のホイールは表示位置のスクロール（パン）として扱う
            self._pan += QPointF(delta.x() * 0.5, delta.y() * 0.5)
            self._clamp_pan()
            self.update()
            event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp_pan()
        # 全体表示の倍率が変わるので、下敷き解像度などの追随用に通知する
        self.viewChanged.emit()


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
        # ズーム変更が連続したときに、下敷き画像の取り直しをまとめるタイマー
        self._underlay_timer = QTimer(self)
        self._underlay_timer.setSingleShot(True)
        self._underlay_timer.timeout.connect(self._refresh_underlay)

        self.setWindowTitle("文字データ")
        default_size = large_dialog_size()
        self.resize(default_size)
        self.setMinimumSize(
            min(1024, default_size.width()), min(720, default_size.height())
        )
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
        layout.addWidget(self.tabs, 1)

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
        self.layout_canvas.viewChanged.connect(self._on_canvas_view_changed)

        zoom_row = QHBoxLayout()
        self.btn_zoom_in = QPushButton("拡大")
        self.btn_zoom_in.setToolTip(
            "文字を大きく表示します（キャンバス上で Ctrl+ホイールでも拡大できます）"
        )
        self.btn_zoom_in.clicked.connect(self.layout_canvas.zoom_in)
        zoom_row.addWidget(self.btn_zoom_in)
        self.btn_zoom_out = QPushButton("縮小")
        self.btn_zoom_out.setToolTip("表示を小さくします")
        self.btn_zoom_out.clicked.connect(self.layout_canvas.zoom_out)
        zoom_row.addWidget(self.btn_zoom_out)
        self.btn_zoom_fit = QPushButton("全体表示")
        self.btn_zoom_fit.setToolTip("ページ全体が見える大きさと位置に戻します")
        self.btn_zoom_fit.clicked.connect(self.layout_canvas.zoom_fit)
        zoom_row.addWidget(self.btn_zoom_fit)
        self.lbl_zoom = QLabel("100%")
        zoom_row.addWidget(self.lbl_zoom)
        zoom_row.addStretch(1)
        self.chk_underlay = QCheckBox("元の画像を薄く重ねる")
        self.chk_underlay.setToolTip(
            "ページの画像を薄く下に敷いて、文字データの位置と見比べられます"
        )
        self.chk_underlay.toggled.connect(self._on_underlay_toggled)
        zoom_row.addWidget(self.chk_underlay)
        v.addLayout(zoom_row)

        v.addWidget(self.layout_canvas, 1)

        opt_row = QHBoxLayout()
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

    def _on_canvas_view_changed(self):
        lbl = getattr(self, "lbl_zoom", None)
        if lbl is not None:
            percent = int(round(self.layout_canvas.zoom_factor() * 100))
            lbl.setText(f"{percent}%")
        chk = getattr(self, "chk_underlay", None)
        if chk is not None and chk.isChecked():
            # ズーム操作は連続するので、少し待ってから下敷きを取り直す
            self._underlay_timer.start(250)

    def _underlay_bucket(self):
        """現在の表示倍率に見合う、下敷き画像の描画ズーム（2/3/4/6）を返す。

        画面に出る大きさ × デバイスピクセル比ぶんの解像度が要るので、
        実効倍率を満たす一番小さい段階を選ぶ（上限は6倍）。
        """
        need = self.layout_canvas.effective_scale()
        need *= self.layout_canvas.devicePixelRatioF() or 1.0
        for bucket in (2.0, 3.0, 4.0, 6.0):
            if need <= bucket:
                return bucket
        return 6.0

    def _request_underlay(self, page_idx, zoom):
        try:
            return self._image_provider(page_idx, zoom)
        except TypeError:
            # ズーム引数に未対応の提供側（従来形式）でも動くようにする
            try:
                return self._image_provider(page_idx)
            except Exception:
                return None
        except Exception:
            return None

    def _refresh_underlay(self):
        show = self.chk_underlay.isChecked()
        page_idx = self._layout_page_idx()
        if show and page_idx is not None and self._image_provider is not None:
            key = (page_idx, self._underlay_bucket())
            if key not in self._image_cache:
                self._image_cache[key] = self._request_underlay(key[0], key[1])
            self.layout_canvas.set_underlay(self._image_cache.get(key))
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
        for key in [k for k in self._image_cache if k[0] == page_idx]:
            del self._image_cache[key]
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
