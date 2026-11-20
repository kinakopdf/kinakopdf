# -*- coding: utf-8 -*-
"""PDFの文字データ（埋め込みテキスト）の取り出しと、文の連結ユーティリティ。

文字データ表示ダイアログ（ui/text_data_dialog.py）から使う。
- extract_page_texts: ページごとの埋め込みテキストをそのまま返す。
  注釈（墨消し等）がある場合は、保存・抽出と同じ焼き込みを行った複製から
  取り出すため、「保存したファイルに実際に残る文字」と一致する。
- extract_page_spans: 配置表示用に、位置・サイズ付きのスパン一覧を返す。
  取り出し元は extract_page_texts と同じ焼き込み済み複製なので、
  墨消しした文字はスパンにも現れない。
- join_fragments: OCRや抽出で改行ごとにバラバラになった文を1行につなげる。
"""
import contextlib
import math

import fitz

from core.geometry import (
    FITZ_LOCK,
    JAPANESE_FONT,
    MAX_FONT_SIZE,
    MIN_FONT_SIZE,
    TEXT_FONT_SIZE,
    srgb_int_to_rgb_tuple,
    view_point_to_raw_pdf,
    view_rect_to_raw_rect,
)
from core.pdf_engine import PDFDocumentManager


def _is_ascii_alnum(ch):
    return bool(ch) and ch.isascii() and ch.isalnum()


def join_fragments(text, remove_spaces=False):
    """改行でバラバラになった文を1行につなげる。

    - 改行コード（CRLF/CR）は LF にそろえ、各行の前後の空白を取り、空行は無視する。
    - 行と行の間は、行末と次の行頭が両方とも半角英数字のときだけ
      半角スペースを1つ入れる（日本語同士や日本語との境目はスペースなし）。
    - remove_spaces=True のときは、連結後に半角・全角スペースをすべて取り除く。
    """
    if not text:
        return ""
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if line:
            lines.append(line)
    if not lines:
        return ""

    result = lines[0]
    for line in lines[1:]:
        if _is_ascii_alnum(result[-1]) and _is_ascii_alnum(line[0]):
            result += " "
        result += line

    if remove_spaces:
        result = result.replace(" ", "").replace("　", "")
    return result


def join_span_texts(texts, remove_spaces=False):
    """選択したスパン群の文字を、抽出順のまま join_fragments の規則でつなげる。"""
    parts = [str(t) for t in (texts or [])]
    return join_fragments("\n".join(parts), remove_spaces=remove_spaces)


@contextlib.contextmanager
def _baked_source(doc, page_annotations):
    """抽出に使う文書を返す context manager。

    注釈（墨消し等）に中身がある場合は、抽出と同じ
    PDFDocumentManager.extract_pages_as_doc で焼き込み済みの複製を作って返し、
    抜けるときに必ず閉じる。呼び出し側の doc は変更しない。
    注釈が無ければ doc をそのまま返す。
    """
    has_annotations = bool(page_annotations) and any(
        bool(ann_list) for ann_list in page_annotations.values()
    )
    if not has_annotations:
        yield doc
        return
    baked = PDFDocumentManager.extract_pages_as_doc(
        doc, list(range(len(doc))), page_annotations
    )
    try:
        yield baked
    finally:
        baked.close()


def extract_page_texts(doc, page_annotations=None):
    """fitz.Document の各ページに埋め込まれた文字データをページ順に返す。

    page_annotations（viewer.page_annotations と同じ形式）に中身がある場合は、
    抽出と同じ PDFDocumentManager.extract_pages_as_doc で焼き込み済みの複製を
    作ってから取り出す（呼び出し側の doc は変更しない。複製は必ず閉じる）。
    注釈が無ければ doc から直接取り出す。
    """
    if doc is None:
        return []
    with FITZ_LOCK:
        if len(doc) == 0:
            return []
        with _baked_source(doc, page_annotations) as src:
            return [src[idx].get_text() for idx in range(len(src))]


def _span_record(page, line, span):
    """get_text("dict") の span 1件を、配置表示・書き戻し共用の辞書に変換する。

    座標系は PDF内文字トークン（PDFViewer._get_page_pdf_text_tokens）と同じ流儀:
    - rect / origin: 表示座標（ページの見た目どおり。配置表示の描画に使う）
    - raw_rect / pos / baseline_pos: 回転なしの生座標（注釈・墨消しに使う）

    注意: get_text の bbox / origin は「回転を適用する前」の座標系
    （CropBox の左上が原点）で返る一方、ページ矩形（page.rect）や
    描画した画像（pixmap）は回転後の表示座標になる。回転のあるページでは
    そのまま描くと画像と文字が系統的にずれるため、表示座標は
    page.rotation_matrix で回転後の座標にそろえる（回転0では恒等変換）。
    """
    text = str(span.get("text", ""))
    if not text.strip():
        return None
    bbox = fitz.Rect(span["bbox"])
    if bbox.is_empty:
        return None
    size = float(span.get("size", 0.0)) or TEXT_FONT_SIZE

    line_dir = line.get("dir", (1.0, 0.0))
    dir_len = math.hypot(line_dir[0], line_dir[1])
    if dir_len > 1e-4:
        dx, dy = line_dir[0] / dir_len, line_dir[1] / dir_len
    else:
        dx, dy = 1.0, 0.0
    view_deg = int(round(math.degrees(math.atan2(dy, dx)) / 90.0) * 90) % 360
    rotation = (page.rotation - view_deg) % 360

    origin = span.get("origin")
    if origin:
        text_base = fitz.Point(float(origin[0]), float(origin[1]))
    else:
        text_base = fitz.Point(
            bbox.x0 - dy * size * 0.88,
            bbox.y0 + dx * size * 0.88,
        )
    text_top = fitz.Point(
        text_base.x + dy * size * 0.88,
        text_base.y - dx * size * 0.88,
    )

    # 回転前の座標（text_base / text_top / bbox）を表示座標へそろえる
    rot_matrix = page.rotation_matrix
    view_bbox = bbox * rot_matrix
    view_base = text_base * rot_matrix
    view_top = text_top * rot_matrix

    raw_rect = view_rect_to_raw_rect(page, view_bbox)
    raw_base = view_point_to_raw_pdf(page, view_base)
    raw_top = view_point_to_raw_pdf(page, view_top)

    return {
        "text": text,
        "rect": (view_bbox.x0, view_bbox.y0, view_bbox.x1, view_bbox.y1),
        "raw_rect": (raw_rect.x0, raw_rect.y0, raw_rect.x1, raw_rect.y1),
        "origin": (view_base.x, view_base.y),
        "fontsize": round(max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size)), 1),
        "color": srgb_int_to_rgb_tuple(span.get("color", 0)),
        "pos": (raw_top.x, raw_top.y),
        "baseline_pos": (raw_base.x, raw_base.y),
        "rotation": rotation,
    }


def extract_page_spans(doc, page_annotations=None):
    """ページごとの文字スパンを、位置・サイズ付きでページ順に返す。

    取り出し元は extract_page_texts と同じ（注釈があれば焼き込み済み複製）。
    墨消しした文字はスパンに現れず、呼び出し側の doc は変更しない。
    各スパンは _span_record の形式の辞書。
    """
    if doc is None:
        return []
    with FITZ_LOCK:
        if len(doc) == 0:
            return []
        with _baked_source(doc, page_annotations) as src:
            result = []
            for page in src:
                spans = []
                try:
                    data = page.get_text("dict")
                except Exception:
                    data = {}
                for block in data.get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            record = _span_record(page, line, span)
                            if record is not None:
                                spans.append(record)
                result.append(spans)
            return result


def get_page_view_rects(doc):
    """各ページの表示座標系の矩形 (x0, y0, x1, y1) を返す（注釈の影響を受けない）。"""
    if doc is None:
        return []
    with FITZ_LOCK:
        return [tuple(float(v) for v in page.rect) for page in doc]


def render_page_png_bytes(doc, page_idx, page_annotations=None, zoom=2.0):
    """配置表示の「元の画像を薄く重ねる」用に、ページ画像の PNG バイト列を返す。

    取り出し元は extract_page_texts と同じ（注釈があれば焼き込み済み複製）で、
    呼び出し側の doc は変更しない。失敗時は None。
    """
    if doc is None:
        return None
    with FITZ_LOCK:
        if not (0 <= page_idx < len(doc)):
            return None
        with _baked_source(doc, page_annotations) as src:
            page = src[page_idx]
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return pix.tobytes("png")


_JP_FONT_OBJ = None


def _measure_text_width(text, fontsize):
    """焼き込み時と同じ日本語フォントで文字列の描画幅（pt）を測る。"""
    global _JP_FONT_OBJ
    if not text:
        return 0.0
    try:
        if JAPANESE_FONT:
            if _JP_FONT_OBJ is None:
                _JP_FONT_OBJ = fitz.Font(fontfile=JAPANESE_FONT)
            return float(_JP_FONT_OBJ.text_length(text, fontsize=fontsize))
    except Exception:
        pass
    try:
        return float(fitz.get_text_length(text, fontname="japan", fontsize=fontsize))
    except Exception:
        return float(fontsize) * sum(
            1.0 if ord(c) > 127 else 0.55 for c in text
        )


def fit_fontsize_to_width(text, base_fontsize, max_width):
    """text が max_width に収まるよう、base_fontsize を上限にフォントサイズを縮める。

    収まっている場合は base_fontsize をそのまま返す。最小は MIN_FONT_SIZE。
    """
    base = float(base_fontsize or 0.0)
    if base <= 0:
        base = TEXT_FONT_SIZE
    base = round(max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, base)), 1)
    if not text or not max_width or float(max_width) <= 0:
        return base
    with FITZ_LOCK:
        width = _measure_text_width(str(text), base)
    if width <= float(max_width) or width <= 0:
        return base
    size = base * (float(max_width) / width)
    return round(max(MIN_FONT_SIZE, min(base, size)), 1)
