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
QFileDialog,
QHBoxLayout,
QLabel,
QMessageBox,
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
"配置表示では元の場所のまま文字が見えます。バラバラになった部分を選んで"
"「くっつける」→確認して「PDFに反映」を押すと、PDFの文字データ自体を直せます。"
"テキスト表示では文章として確認・手直し・コピーができます。"
)
HINT_TEXT = (
"OCRや文字の取り出しでバラバラになった文は、行を選んで「くっつける」を押すと"
"改行が取れて1つの文になります（選んでいなければ全体が対象です）。"
"ここでの編集はPDF自体には反映されません。"
)
LAYOUT_HINT_TEXT = (
"クリックで文字のかたまり（スパン）を選んだり外したりできます。"
"ドラッグで範囲選択（Shiftを押しながら離すと今の選択に追加）、"
"Space+ドラッグで表示位置を動かせます。"
"バラバラになった文のかたまりを全部選んで「くっつける」を押してください。"
"拡大・縮小ボタンや Ctrl+ホイールで大きくでき、ホイールで表示位置をスクロールできます。"
"「見開きで見比べ」では元の画像と文字データを並べて同じ倍率・位置で見比べられます。"
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
    各スパンには薄い枠を描くので、文字データがどこで区切れているか一目で分かる。
    左ドラッグは矩形の範囲選択（ラバーバンド）で、表示位置の移動（パン）は
    Space+左ドラッグ・中ボタンドラッグ・ホイールで行う。
    全体表示を基準に 0.5〜4 倍までズームでき、文字はベクタ描画なので、
    拡大してもぼやけない。
    """

    selectionChanged = pyqtSignal()
    viewChanged = pyqtSignal()

    MIN_ZOOM = 0.5
    MAX_ZOOM = 4.0

    # ドラッグとみなす移動量（px）。これ以下ならクリックとして扱う
    DRAG_THRESHOLD = 5.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page_rect = (0.0, 0.0, 1.0, 1.0)
        self._spans = []
        self._selected = set()
        self._underlay = None
        self._show_underlay = False
        # スパンごとの区切り枠を描くかどうか（既定は表示）
        self._show_span_outlines = True
        # 下敷き画像だけをずらす微調整量（ページ座標 pt、スパンには影響しない）
        self._underlay_offset = QPointF(0.0, 0.0)
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._press_pos = None
        self._press_span = None
        self._press_button = None
        # ドラッグ中の動作: None（未確定）/ "pan"（表示位置の移動）/ "band"（範囲選択）
        self._drag_mode = None
        self._band_current = None
        self._space_down = False
        self._last_pos = None
        # 左ドラッグで範囲選択を行うかどうか（画像ペインでは従来どおりパンにする）
        self._band_select_enabled = True
        self.setMinimumSize(440, 300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_page(self, page_rect, spans):
        if page_rect and len(page_rect) == 4:
            self._page_rect = tuple(float(v) for v in page_rect)
        self._spans = list(spans or [])
        self._selected = set()
        # ページが替わったら表示位置だけ戻す（ズーム倍率は維持する）
        self._pan = QPointF(0.0, 0.0)
        self._press_pos = None
        self._press_span = None
        self._press_button = None
        self._drag_mode = None
        self._band_current = None
        self._last_pos = None
        self.update()

    def set_underlay(self, pixmap):
        self._underlay = pixmap
        self.update()

    def set_show_underlay(self, show):
        self._show_underlay = bool(show)
        self.update()

    def set_underlay_offset(self, offset):
        """下敷き画像だけの表示ずれ（ページ座標 pt）を設定する。"""
        self._underlay_offset = QPointF(offset)
        self.update()

    def underlay_offset(self):
        return QPointF(self._underlay_offset)

    def set_show_span_outlines(self, show):
        """スパンごとの区切り枠を描くかどうかを設定する。"""
        self._show_span_outlines = bool(show)
        self.update()

    def show_span_outlines(self):
        return self._show_span_outlines

    def spans_in_rect(self, rect):
        """画面座標の矩形 rect と重なるスパンの番号一覧を返す（範囲選択用）。"""
        if rect is None or not self._spans:
            return []
        area, scale = self._page_area()
        band = QRectF(rect).normalized()
        return [
            idx
            for idx, span in enumerate(self._spans)
            if self._span_widget_rect(span, area, scale).intersects(band)
        ]

    def band_rect(self):
        """範囲選択中のラバーバンド矩形（画面座標）。選択中でなければ None。"""
        if self._drag_mode != "band" or self._press_pos is None:
            return None
        current = self._band_current if self._band_current is not None else self._press_pos
        return QRectF(self._press_pos, current).normalized()

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

    def set_view_state(self, zoom, pan):
        """ズームと表示位置を外部からそろえる（見比べモードの同期用）。

        viewChanged は出さない（同期のループを防ぐため）。呼び出し側が
        片方のキャンバスの viewChanged を受けて、もう片方へ反映する。
        """
        try:
            new_zoom = float(zoom)
        except (TypeError, ValueError):
            return
        self._zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, new_zoom))
        self._pan = QPointF(pan)
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
            # 微調整は下敷き画像だけに効かせる（スパンの位置は動かさない）
            target = QRectF(
                area.x() + self._underlay_offset.x() * scale,
                area.y() + self._underlay_offset.y() * scale,
                area.width(),
                area.height(),
            )
            painter.drawPixmap(target, self._underlay, QRectF(self._underlay.rect()))
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
            elif self._show_span_outlines:
                # 文字データの区切りが分かるよう、各スパンに薄い枠を描く
                painter.setPen(QPen(QColor(70, 130, 200, 150), 1))
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

        band = self.band_rect()
        if band is not None:
            painter.fillRect(band, QColor(0, 102, 204, 40))
            painter.setPen(
                QPen(QColor(0, 102, 204), 1, Qt.PenStyle.DashLine)
            )
            painter.drawRect(band)
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

    # --- ドラッグ操作（範囲選択とパン）と Space キーの追跡 ---

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            # Space を押している間の左ドラッグは、範囲選択ではなくパンにする
            self._space_down = True
            if self._band_select_enabled and self._drag_mode != "pan":
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = False
            if self._drag_mode != "pan":
                self.unsetCursor()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        # フォーカスを失ったら Space の押し下げ状態を取りこぼさないよう戻す
        self._space_down = False
        super().focusOutEvent(event)

    def _wants_pan(self):
        """今回のドラッグを表示位置の移動（パン）として扱うかどうか。"""
        if not self._band_select_enabled:
            return True
        if self._press_button == Qt.MouseButton.MiddleButton:
            return True
        return self._space_down

    def mousePressEvent(self, event):
        if event.button() not in (
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.MiddleButton,
        ):
            return
        if event.button() == Qt.MouseButton.LeftButton:
            # キャンバスにフォーカスを移し、Space+ドラッグのパンを効かせる
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        # 選択・パンの確定はリリース時。ここでは押し始めの状態だけ記録する
        self._press_pos = event.position()
        self._last_pos = event.position()
        self._press_span = self._hit_span(event.position())
        self._press_button = event.button()
        self._drag_mode = None
        self._band_current = None

    def mouseMoveEvent(self, event):
        if self._press_pos is None or self._press_button is None:
            return
        if not (event.buttons() & self._press_button):
            return
        pos = event.position()
        if self._drag_mode is None:
            if (pos - self._press_pos).manhattanLength() <= self.DRAG_THRESHOLD:
                self._last_pos = pos
                return
            # しきい値を超えたらドラッグ開始。Space/中ボタン/画像ペインではパン、
            # それ以外（文字ペインの左ドラッグ）は範囲選択にする
            self._drag_mode = "pan" if self._wants_pan() else "band"
            if self._drag_mode == "pan":
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            else:
                self._band_current = pos
        if self._drag_mode == "pan":
            self._pan += pos - self._last_pos
            self._last_pos = pos
            self._clamp_pan()
            self.update()
            # 見比べモードで相手ペインを追随させるため、パンでも通知する
            self.viewChanged.emit()
        else:
            self._band_current = pos
            self._last_pos = pos
            self.update()

    def mouseReleaseEvent(self, event):
        if self._press_button is None or event.button() != self._press_button:
            return
        mode = self._drag_mode
        press_span = self._press_span
        band = self.band_rect()
        self._press_pos = None
        self._press_span = None
        self._press_button = None
        self._last_pos = None
        self._drag_mode = None
        self._band_current = None
        self.unsetCursor()
        if mode == "band":
            if band is None:
                return
            indices = self.spans_in_rect(band)
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Shift を押しながら離した場合は、今の選択へ追加する
                indices = sorted(set(self._selected) | set(indices))
            self.set_selected(indices)
            return
        if mode == "pan" or press_span is None:
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
            self.viewChanged.emit()
            event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp_pan()
        # 全体表示の倍率が変わるので、下敷き解像度などの追随用に通知する
        self.viewChanged.emit()


class PageImageCanvas(LayoutCanvas):
    """見比べモードの左ペイン: 元のページ画像だけを表示するキャンバス。

    ズーム/パンの仕組みは LayoutCanvas と共通で、表示状態はダイアログ側が
    文字データ側と同期させる。スパンは持たないのでクリック選択は起きず、
    ドラッグでのパンと Ctrl+ホイールでのズームだけが効く。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = None
        # 画像ペインでは範囲選択をせず、左ドラッグは従来どおりパンにする
        self._band_select_enabled = False

    def set_image(self, pixmap):
        self._image = pixmap
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#e8eaed"))

        area, _scale = self._page_area()
        painter.fillRect(area, QColor("#ffffff"))
        painter.setPen(QPen(QColor("#9aa0a6"), 1))
        painter.drawRect(area)

        if self._image is not None and not self._image.isNull():
            painter.drawPixmap(area, self._image, QRectF(self._image.rect()))
        else:
            painter.setPen(QPen(QColor("#666666")))
            painter.drawText(
                area, Qt.AlignmentFlag.AlignCenter, "（ページ画像がありません）"
            )
        painter.end()


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
        strip_handler=None,
        strip_filename="",
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
        self._strip_handler = strip_handler
        self._strip_filename = str(strip_filename or "")
        self._image_cache = {}
        self._loading = False
        # 全画面表示の状態（Fキーとボタンで切り替える）
        self._is_fullscreen = False
        self._normal_geometry = None
        # 見比べモードで左右ペインの表示状態を同期する際のループ防止
        self._syncing_view = False
        # 重ね表示の下敷き画像だけをずらす微調整量（pt、ダイアログ内のみ保持）
        self._underlay_offset = QPointF(0.0, 0.0)
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
        self.btn_fullscreen = QPushButton("全画面")
        self.btn_fullscreen.setCheckable(True)
        self.btn_fullscreen.setToolTip(
            "全画面表示にします（Fキーでも切り替えられます。全画面中の Esc は"
            "通常表示に戻るだけで、画面は閉じません）"
        )
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        top_row.addWidget(self.btn_fullscreen)
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
        self.btn_strip = QPushButton("🖼️ 画像だけのPDFとして保存")
        self.btn_strip.setToolTip(
            "見た目（画像）はそのまま残して、文字データだけを消したPDFを"
            "別のファイルに保存します。元のファイルは変更しません"
        )
        self.btn_strip.setEnabled(self._strip_handler is not None)
        self.btn_strip.clicked.connect(self._on_strip_clicked)
        bottom_row.addWidget(self.btn_strip)
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
        # 見比べモードの左ペイン（元のページ画像）。通常は隠れている
        self.image_canvas = PageImageCanvas()
        self.image_canvas.viewChanged.connect(self._on_canvas_view_changed)
        self.image_canvas.setVisible(False)

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
        zoom_row.addWidget(QLabel("表示:"))
        self.cmb_view_mode = QComboBox()
        self.cmb_view_mode.addItem("文字データのみ", "text")
        self.cmb_view_mode.addItem("重ねて表示", "overlay")
        self.cmb_view_mode.addItem("見開きで見比べ", "compare")
        self.cmb_view_mode.setToolTip(
            "重ねて表示: ページの画像を薄く下に敷いて位置を見比べられます\n"
            "見開きで見比べ: 左に元の画像、右に文字データを並べ、"
            "拡大や移動が連動します"
        )
        self.cmb_view_mode.currentIndexChanged.connect(self._on_view_mode_changed)
        zoom_row.addWidget(self.cmb_view_mode)
        self.chk_span_outlines = QCheckBox("文字の区切りを表示")
        self.chk_span_outlines.setChecked(True)
        self.chk_span_outlines.setToolTip(
            "文字データのかたまり（スパン）ごとに薄い枠を表示します。"
            "文字がどこで途切れているか一目で分かります"
        )
        self.chk_span_outlines.toggled.connect(
            self.layout_canvas.set_show_span_outlines
        )
        zoom_row.addWidget(self.chk_span_outlines)
        v.addLayout(zoom_row)

        # 重ね表示のときだけ使う、下敷き画像の微調整行
        self.nudge_row_widget = QWidget()
        nudge_row = QHBoxLayout(self.nudge_row_widget)
        nudge_row.setContentsMargins(0, 0, 0, 0)
        nudge_row.addWidget(QLabel("重ね位置の微調整（画像だけをずらす）:"))
        self.btn_nudge_up = QPushButton("↑")
        self.btn_nudge_up.setToolTip("下敷き画像を 0.5pt 上へずらします")
        self.btn_nudge_up.clicked.connect(lambda: self._nudge_underlay(0.0, -0.5))
        nudge_row.addWidget(self.btn_nudge_up)
        self.btn_nudge_down = QPushButton("↓")
        self.btn_nudge_down.setToolTip("下敷き画像を 0.5pt 下へずらします")
        self.btn_nudge_down.clicked.connect(lambda: self._nudge_underlay(0.0, 0.5))
        nudge_row.addWidget(self.btn_nudge_down)
        self.btn_nudge_left = QPushButton("←")
        self.btn_nudge_left.setToolTip("下敷き画像を 0.5pt 左へずらします")
        self.btn_nudge_left.clicked.connect(lambda: self._nudge_underlay(-0.5, 0.0))
        nudge_row.addWidget(self.btn_nudge_left)
        self.btn_nudge_right = QPushButton("→")
        self.btn_nudge_right.setToolTip("下敷き画像を 0.5pt 右へずらします")
        self.btn_nudge_right.clicked.connect(lambda: self._nudge_underlay(0.5, 0.0))
        nudge_row.addWidget(self.btn_nudge_right)
        self.lbl_nudge = QLabel("(+0.0, +0.0) pt")
        nudge_row.addWidget(self.lbl_nudge)
        self.btn_nudge_reset = QPushButton("リセット")
        self.btn_nudge_reset.setToolTip("ずらした量を (0, 0) に戻します")
        self.btn_nudge_reset.clicked.connect(self._reset_underlay_offset)
        nudge_row.addWidget(self.btn_nudge_reset)
        nudge_row.addStretch(1)
        self.nudge_row_widget.setVisible(False)
        v.addWidget(self.nudge_row_widget)

        panes = QWidget()
        panes_layout = QHBoxLayout(panes)
        panes_layout.setContentsMargins(0, 0, 0, 0)
        panes_layout.addWidget(self.image_canvas, 1)
        panes_layout.addWidget(self.layout_canvas, 1)
        v.addWidget(panes, 1)

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
            self.image_canvas.set_page((0.0, 0.0, 1.0, 1.0), [])
            self._set_join_preview("")
            return
        self.layout_canvas.set_page(
            self._page_rects[page_idx], self._page_spans[page_idx]
        )
        # 見比べの左ペインも同じページに切り替える
        self.image_canvas.set_page(self._page_rects[page_idx], [])
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

    # --- 全画面表示 ---

    def _sync_fs_button(self):
        btn = getattr(self, "btn_fullscreen", None)
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(self._is_fullscreen)
            btn.blockSignals(False)

    def set_fullscreen(self, on):
        """全画面と通常表示を切り替える（見開きリーダーと同じ流儀）。

        通常表示に戻るときは、全画面にする前の位置と大きさに戻す。
        """
        on = bool(on)
        if on == self._is_fullscreen:
            self._sync_fs_button()
            return
        self._is_fullscreen = on
        if on:
            self._normal_geometry = self.geometry()
            self.showFullScreen()
            self.lbl_status.setText(
                "全画面表示です（Esc または Fキーで通常表示に戻ります）"
            )
        else:
            self.showNormal()
            if self._normal_geometry is not None:
                self.setGeometry(self._normal_geometry)
            self.lbl_status.setText("通常表示に戻りました")
        self._sync_fs_button()

    def toggle_fullscreen(self):
        self.set_fullscreen(not self._is_fullscreen)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()
        if key == Qt.Key.Key_Escape and self._is_fullscreen:
            # 全画面のときは Esc で全画面を抜けるだけ（ダイアログは閉じない）
            self.set_fullscreen(False)
            event.accept()
            return
        if key == Qt.Key.Key_F and not (
            mods
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            )
        ):
            self.toggle_fullscreen()
            event.accept()
            return
        super().keyPressEvent(event)

    # --- 配置表示のモード（文字のみ／重ね／見比べ）と下敷き画像 ---

    def _view_mode(self):
        """配置表示の見せ方: "text"（文字のみ）/ "overlay"（重ね）/ "compare"（見比べ）。"""
        cmb = getattr(self, "cmb_view_mode", None)
        if cmb is None:
            return "text"
        return cmb.currentData() or "text"

    def _on_view_mode_changed(self):
        mode = self._view_mode()
        self.image_canvas.setVisible(mode == "compare")
        self.nudge_row_widget.setVisible(mode == "overlay")
        if mode == "compare":
            # 見比べに入るときは、文字データ側の表示状態を画像側へ写す
            self.image_canvas.set_view_state(
                self.layout_canvas.zoom_factor(),
                self.layout_canvas.pan_offset(),
            )
        self._refresh_underlay()

    def _sync_peer_view(self, src, dst):
        """見比べモードで、片方の表示状態（ズームと位置）をもう片方へ写す。"""
        if self._syncing_view:
            return
        self._syncing_view = True
        try:
            dst.set_view_state(src.zoom_factor(), src.pan_offset())
        finally:
            self._syncing_view = False

    def _on_canvas_view_changed(self):
        # 見比べモードでは左右ペインの表示状態をそろえる。
        # 画像ペインが隠れている間はそちらの古い状態を写さない。
        src = self.sender()
        if src is self.layout_canvas:
            self._sync_peer_view(self.layout_canvas, self.image_canvas)
        elif src is self.image_canvas and self._view_mode() == "compare":
            self._sync_peer_view(self.image_canvas, self.layout_canvas)
        lbl = getattr(self, "lbl_zoom", None)
        if lbl is not None:
            percent = int(round(self.layout_canvas.zoom_factor() * 100))
            lbl.setText(f"{percent}%")
        if self._view_mode() in ("overlay", "compare"):
            # ズーム操作は連続するので、少し待ってから下敷き画像を取り直す
            self._underlay_timer.start(250)

    def _underlay_bucket(self, canvas=None):
        """表示倍率に見合う、ページ画像の描画ズーム（2/3/4/6）を返す。

        画面に出る大きさ × デバイスピクセル比ぶんの解像度が要るので、
        実効倍率を満たす一番小さい段階を選ぶ（上限は6倍）。
        """
        canvas = canvas if canvas is not None else self.layout_canvas
        need = canvas.effective_scale()
        need *= canvas.devicePixelRatioF() or 1.0
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

    def _fetch_page_image(self, page_idx, canvas):
        """指定ページの画像を、表示倍率に見合う解像度で取得する（キャッシュ共用）。"""
        if page_idx is None or self._image_provider is None:
            return None
        key = (page_idx, self._underlay_bucket(canvas))
        if key not in self._image_cache:
            self._image_cache[key] = self._request_underlay(key[0], key[1])
        return self._image_cache.get(key)

    def _refresh_underlay(self):
        mode = self._view_mode()
        page_idx = self._layout_page_idx()
        # 重ね表示のときだけ、文字データ側に薄い下敷きを敷く
        if mode == "overlay":
            pix = self._fetch_page_image(page_idx, self.layout_canvas)
            self.layout_canvas.set_underlay(pix)
        self.layout_canvas.set_show_underlay(mode == "overlay")
        # 見比べモードでは、左ペインに元の画像をそのまま出す
        if mode == "compare":
            pix = self._fetch_page_image(page_idx, self.image_canvas)
            self.image_canvas.set_image(pix)

    # --- 重ね表示の微調整（下敷き画像だけをずらす） ---

    NUDGE_LIMIT_PT = 30.0

    def underlay_offset(self):
        """下敷き画像の微調整量 (dx, dy)（ページ座標 pt）を返す。"""
        return (self._underlay_offset.x(), self._underlay_offset.y())

    def _nudge_underlay(self, dx, dy):
        """下敷き画像だけを (dx, dy) pt ずらす（±30pt までに制限）。"""
        limit = self.NUDGE_LIMIT_PT
        new_x = max(-limit, min(limit, self._underlay_offset.x() + float(dx)))
        new_y = max(-limit, min(limit, self._underlay_offset.y() + float(dy)))
        self._underlay_offset = QPointF(new_x, new_y)
        self.layout_canvas.set_underlay_offset(self._underlay_offset)
        self._update_nudge_label()

    def _reset_underlay_offset(self):
        self._underlay_offset = QPointF(0.0, 0.0)
        self.layout_canvas.set_underlay_offset(self._underlay_offset)
        self._update_nudge_label()

    def _update_nudge_label(self):
        lbl = getattr(self, "lbl_nudge", None)
        if lbl is not None:
            lbl.setText(
                f"({self._underlay_offset.x():+.1f}, "
                f"{self._underlay_offset.y():+.1f}) pt"
            )

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
        # 反映が不可視（OCRレイヤー型）だった場合は、その旨を伝える。
        # apply_handler の持ち主（MainWindow）が結果を属性に残す。
        handler_owner = getattr(self._apply_handler, "__self__", None)
        applied_invisible = bool(
            getattr(handler_owner, "_last_text_data_apply_invisible", False)
        )
        if applied_invisible:
            self.lbl_status.setText(
                "PDFに反映しました（画像の上には表示されず、文字データだけ直しました）。"
                "メイン画面で保存すると確定します"
            )
        else:
            self.lbl_status.setText("PDFに反映しました。メイン画面で保存すると確定します")

    # --- 画像だけのPDFとして保存（文字データの除去） ---

    def _on_strip_clicked(self):
        """見た目はそのままに、文字データだけを消したPDFを別ファイルへ保存する。

        実際の生成と保存は strip_handler（MainWindow）が行い、開いている
        文書や現在のファイルは変更されない。
        """
        if self._strip_handler is None:
            return
        has_text = any(t.strip() for t in self._page_texts)
        message = (
            "見た目（画像）はそのまま残して、文字データだけを消したPDFを、"
            "別のファイルに保存します。元のファイルは変更しません。"
        )
        if not has_text:
            message += (
                "\n\n（このPDFには文字データが見つかりませんでした。"
                "見た目をそのまま写したPDFを保存します）"
            )
        answer = QMessageBox.question(
            self,
            "画像だけのPDFとして保存",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "画像だけのPDFとして保存",
            self._strip_filename or "画像のみ.pdf",
            "PDFファイル (*.pdf)",
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        ok, result_message = self._strip_handler(path)
        if ok:
            self.lbl_status.setText(result_message)
            QMessageBox.information(self, "保存しました", result_message)
        else:
            QMessageBox.critical(self, "保存できませんでした", result_message)
