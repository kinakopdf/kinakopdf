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
- strip_text_layer_to_doc: 見た目（画像）はそのままに、文字データだけを
  消した新しい文書を返す（AIが壊れた文字レイヤーを誤読するのを防ぐ用）。
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
    text_annotation_rotation,
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
    # 注釈の rotation はページ回転を含めない（text_annotation_rotation 参照）。
    # 以前は (page.rotation - view_deg) で、回転ページでは書き戻した文字が
    # ページ回転ぶんずれた向きになっていた。
    rotation = text_annotation_rotation(dx, dy)

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


def page_image_rects(page):
    """ページ内の画像ブロックの矩形（生座標）を返す。

    get_text("dict") の画像ブロック（type=1）の bbox で、
    同じ呼び出しで得られる文字スパンの bbox と同じ座標系になる
    （apply_text_only_redactions_to_page の画像重なり判定と同じ流儀）。
    """
    rects = []
    if page is None:
        return rects
    try:
        data = page.get_text("dict")
    except Exception:
        return rects
    for block in data.get("blocks", []):
        if block.get("type") != 1:
            continue
        r = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
        if not r.is_empty:
            rects.append(r)
    return rects


def rect_image_coverage(rect, image_rects):
    """rect の面積のうち image_rects に覆われる割合（0.0〜1.0）を返す。"""
    r = fitz.Rect(rect)
    r.normalize()
    area = r.width * r.height
    if area <= 0:
        return 0.0
    covered = 0.0
    for ir in image_rects:
        inter = r & ir
        if not inter.is_empty:
            covered += inter.width * inter.height
    return min(1.0, covered / area)


def spans_are_scan_overlay(page, raw_rects, coverage_threshold=0.5):
    """すべての矩形がページ内の画像に実質的に覆われていれば True。

    スキャン/写真の上に乗ったOCR文字（見た目は画像、文字は内部データ）
    かどうかの判定に使う。画像が1つもないページ（通常のPDF）では
    常に False になる。raw_rects は生座標系で渡すこと。
    """
    if page is None or not raw_rects:
        return False
    with FITZ_LOCK:
        image_rects = page_image_rects(page)
        if not image_rects:
            return False
        for rr in raw_rects:
            r = fitz.Rect(rr)
            r.normalize()
            if r.is_empty:
                return False
            if rect_image_coverage(r, image_rects) < coverage_threshold:
                return False
        return True


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


# ==========================================
# 画像だけのPDF（文字データの除去）
# ==========================================

# 画像のないページを画像化するときの解像度
_RASTERIZE_DPI = 200


def _remove_text_layer_keeping_images(page):
    """画像のあるページから、文字レイヤーだけを取り除く（ページを直接書き換える）。

    全面のリダクションを images=NONE / line art=NONE で適用するので、
    画像のピクセル（画像に焼き付いた字形も含む）とベクターの線画には
    一切触れず、埋め込み文字だけが消える。アプリの墨消し経路
    （apply_text_only_redactions_to_page は画像側の字形ピクセルも消す）
    とは目的が逆なので、そちらは通らない。
    ページ回転は一時的に外して作業し、終わったら元に戻す。
    """
    saved_rot = page.rotation % 360
    if saved_rot:
        page.set_rotation(0)
    try:
        page.add_redact_annot(page.rect, fill=False)
        img_none = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)
        gfx_none = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
        text_opt = getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0)
        try:
            page.apply_redactions(
                images=img_none,
                graphics=gfx_none,
                text=text_opt,
            )
        except TypeError:
            page.apply_redactions(images=img_none)
    finally:
        if saved_rot:
            page.set_rotation(saved_rot)


def _rasterize_page_via_copy(doc, page_idx, dpi=_RASTERIZE_DPI):
    """画像のないページを、回転なしの向きで描画した結果を返す。

    呼び出し側の文書には触れないよう、1ページだけの複製の上で回転を
    外して描画する。戻り値は (width, height, png_bytes, rotation) で、
    width / height は回転なしページの大きさ。
    """
    tmp = fitz.open()
    try:
        tmp.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
        page = tmp[0]
        saved_rot = page.rotation % 360
        if saved_rot:
            page.set_rotation(0)
        zoom = float(dpi) / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return page.rect.width, page.rect.height, pix.tobytes("png"), saved_rot
    finally:
        tmp.close()


def strip_text_layer_to_doc(doc):
    """文字データをすべて消し、見た目をそのまま残した新しい文書を返す。

    - 画像のあるページ: 文字レイヤーだけを消す。画像（写真・スキャン）も
      ベクターの線画もそのまま残るので、見た目は変わらない。
    - 画像のないページ: 文字を消すと真っ白になってしまうため、ページを
      200 DPI で画像化して差し替える（見た目は同じで、文字データはない）。

    ページの大きさ・回転・順序と、文書のメタデータは元どおりに保つ。
    呼び出し側の doc は変更しない。戻り値の文書は呼び出し側が閉じること。
    doc が None の場合は None を返す。
    """
    if doc is None:
        return None
    with FITZ_LOCK:
        out = fitz.open()
        try:
            try:
                allowed_keys = (
                    "title", "author", "subject", "keywords",
                    "creator", "producer", "creationDate", "modDate",
                )
                meta = {
                    k: (doc.metadata or {}).get(k)
                    for k in allowed_keys
                    if (doc.metadata or {}).get(k)
                }
                if meta:
                    out.set_metadata(meta)
            except Exception:
                pass
            for page in doc:
                if page.get_images():
                    out.insert_pdf(
                        doc, from_page=page.number, to_page=page.number
                    )
                    _remove_text_layer_keeping_images(out[len(out) - 1])
                else:
                    width, height, png, saved_rot = _rasterize_page_via_copy(
                        doc, page.number
                    )
                    new_page = out.new_page(width=width, height=height)
                    new_page.insert_image(new_page.rect, stream=png)
                    if saved_rot:
                        new_page.set_rotation(saved_rot)
            return out
        except Exception:
            out.close()
            raise


def save_image_only_pdf(doc, page_annotations, path):
    """現在の文書の「画像だけのPDF」を path に保存する。

    注釈（墨消し等）がある場合は、文字の取り出しと同じ焼き込み済み複製を
    元にするため、保存されるPDFの見た目は現在の表示と一致する。
    元の文書は変更しない。
    """
    with FITZ_LOCK:
        with _baked_source(doc, page_annotations) as src:
            stripped = strip_text_layer_to_doc(src)
            if stripped is None:
                raise ValueError("PDFが開かれていません。")
            try:
                stripped.save(path, garbage=4, deflate=True)
            finally:
                stripped.close()


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
