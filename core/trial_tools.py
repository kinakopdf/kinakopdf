# -*- coding: utf-8 -*-
"""試作多機能版（feature/trial-multi）の P0 機能群。

純粋関数中心のモジュール。UI からはここを呼ぶだけにし、
fitz.Document / bytes を受け取って bytes や件数を返す。
新しいライブラリは使わない（PyMuPDF のみ）。
"""
import re

import fitz

from core.geometry import FITZ_LOCK, JAPANESE_FONT


def _with_fitz_lock(fn):
    """PyMuPDFはスレッド安全ではないため、このモジュールの公開関数は
    FITZ_LOCK の中で実行する（RLockなので入れ子でも安全）。"""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with FITZ_LOCK:
            return fn(*args, **kwargs)

    return wrapper


# ============================================================
# 共通ヘルパー
# ============================================================

def _insert_text_jp(page, point, text, fontsize=11.0, color=(0, 0, 0), rotate=0):
    """日本語対応のテキスト挿入。埋め込みフォントが使えなければ内蔵フォントに fallback。"""
    if JAPANESE_FONT:
        try:
            page.insert_text(
                point,
                text,
                fontsize=fontsize,
                color=color,
                rotate=rotate,
                fontfile=JAPANESE_FONT,
                fontname="trial_jp",
            )
            return
        except Exception:
            pass
    try:
        page.insert_text(
            point, text, fontsize=fontsize, color=color, rotate=rotate,
            fontname="japan",
        )
    except Exception:
        page.insert_text(
            point, text, fontsize=fontsize, color=color, rotate=rotate,
            fontname="helv",
        )


def _insert_textbox_jp(page, rect, text, fontsize=11.0, color=(0, 0, 0),
                       align=1, rotate=0):
    """日本語対応のテキストボックス挿入（中央揃えなどに使う）。"""
    if JAPANESE_FONT:
        try:
            page.insert_textbox(
                rect, text, fontsize=fontsize, color=color, align=align,
                rotate=rotate, fontfile=JAPANESE_FONT, fontname="trial_jp",
            )
            return
        except Exception:
            pass
    try:
        page.insert_textbox(
            rect, text, fontsize=fontsize, color=color, align=align,
            rotate=rotate, fontname="japan",
        )
    except Exception:
        page.insert_textbox(
            rect, text, fontsize=fontsize, color=color, align=align,
            rotate=rotate, fontname="helv",
        )


@_with_fitz_lock
def apply_toc_to_pdf_bytes(data: bytes, toc) -> bytes:
    """PDF バイト列にしおり（TOC）を付け直して返す。toc が空ならそのまま返す。"""
    if not toc:
        return data
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        doc.set_toc([list(e) for e in toc])
        return doc.tobytes(garbage=4, deflate=True)
    finally:
        doc.close()


# ============================================================
# 1. パスワード保護 / 解除
# ============================================================

@_with_fitz_lock
def pdf_needs_password(data: bytes) -> bool:
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        return bool(doc.needs_pass)
    finally:
        doc.close()


@_with_fitz_lock
def authenticate_pdf_bytes(data: bytes, password: str) -> bool:
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if not doc.needs_pass:
            return True
        return bool(doc.authenticate(password))
    finally:
        doc.close()


@_with_fitz_lock
def encrypt_pdf_bytes(data: bytes, user_password: str,
                      owner_password=None) -> bytes:
    """PDF バイト列にパスワードを設定して返す（AES-256）。

    既にパスワード付きの PDF には使えない（先に解除すること）。
    """
    if not user_password:
        raise ValueError("パスワードが空です。")
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if doc.needs_pass:
            raise ValueError("既にパスワードが設定された PDF です。先に解除してください。")
        return doc.tobytes(
            garbage=4,
            deflate=True,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            user_pw=user_password,
            owner_pw=owner_password or user_password,
        )
    finally:
        doc.close()


@_with_fitz_lock
def strip_password_pdf_bytes(data: bytes, password=None) -> bytes:
    """パスワード付き PDF の暗号化を解除したバイト列を返す。"""
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if doc.needs_pass:
            if password is None or not doc.authenticate(password):
                raise ValueError("パスワードが正しくありません。")
        return doc.tobytes(garbage=4, deflate=True)
    finally:
        doc.close()


# ============================================================
# 2. PDF 圧縮
# ============================================================

@_with_fitz_lock
def compress_pdf_bytes(data: bytes, max_image_dim: int = 1200,
                       jpeg_quality: int = 75) -> tuple:
    """画像のダウンサンプル＋JPEG再圧縮＋garbage/clean で圧縮する。

    戻り値: (圧縮後バイト列, 統計 dict)
    透明マスク付き画像や CMYK・アルファ付き画像は壊さないよう対象外とする。
    """
    if not (10 <= jpeg_quality <= 95):
        raise ValueError("jpeg_quality は 10〜95 の範囲で指定してください。")
    if max_image_dim < 100:
        raise ValueError("max_image_dim は 100 以上で指定してください。")

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        images_processed = 0
        images_skipped = 0
        seen_xrefs = set()
        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                smask = img[1]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                if smask:
                    images_skipped += 1
                    continue
                try:
                    pix = fitz.Pixmap(doc, xref)
                except Exception:
                    images_skipped += 1
                    continue
                try:
                    if pix.alpha or pix.n > 3:
                        images_skipped += 1
                        continue
                    w, h = pix.width, pix.height
                    if max(w, h) <= max_image_dim and pix.n == 3:
                        # 小さい画像はそのまま（再圧縮しても得が少ない）
                        # ただし PNG(Flate) のままでは大きいので JPEG 化は行う
                        pass
                    # 2の累乗で縮小（Pixmap.shrink は 2^n 分の1）
                    shrink = 0
                    while max(w, h) > max_image_dim and shrink < 4:
                        shrink += 1
                        w = (w + 1) // 2
                        h = (h + 1) // 2
                    if shrink:
                        pix.shrink(shrink)
                    jpg = pix.tobytes("jpg", jpg_quality=jpeg_quality)
                    if len(jpg) >= pix.n * pix.width * pix.height and shrink == 0:
                        # JPEG 化しても小さくならない場合は元のまま
                        images_skipped += 1
                        continue
                    doc.update_stream(xref, jpg)
                    colorspace = "/DeviceRGB" if pix.n == 3 else "/DeviceGray"
                    doc.xref_set_key(xref, "Filter", "/DCTDecode")
                    doc.xref_set_key(xref, "ColorSpace", colorspace)
                    doc.xref_set_key(xref, "BitsPerComponent", "8")
                    doc.xref_set_key(xref, "Width", str(pix.width))
                    doc.xref_set_key(xref, "Height", str(pix.height))
                    doc.xref_set_key(xref, "DecodeParms", "null")
                    images_processed += 1
                except Exception:
                    images_skipped += 1
                finally:
                    pix = None
            try:
                page.clean_contents()
            except Exception:
                pass
        try:
            doc.subset_fonts()
        except Exception:
            pass
        out = doc.tobytes(garbage=4, deflate=True, clean=True)
        stats = {
            "original_size": len(data),
            "compressed_size": len(out),
            "images_processed": images_processed,
            "images_skipped": images_skipped,
            "page_count": len(doc),
        }
        return out, stats
    finally:
        doc.close()


# ============================================================
# 3. PDF 分割
# ============================================================

_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


def parse_page_ranges(spec: str, page_count: int):
    """「1-3,5,7-9」のような指定（1始まり）を [(0始まり開始, 0始まり終了)] に変換する。

    空文字・範囲外・逆転範囲・不正な書式は ValueError。
    """
    if page_count <= 0:
        raise ValueError("ページが存在しません。")
    if not spec or not spec.strip():
        raise ValueError("ページ範囲が空です。例: 1-3,5")
    ranges = []
    for part in spec.split(","):
        m = _RANGE_RE.match(part)
        if not m:
            raise ValueError(f"ページ範囲の書式が正しくありません: {part!r}（例: 1-3,5）")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if start < 1 or end < 1 or start > page_count or end > page_count:
            raise ValueError(
                f"ページ範囲が全体のページ数（{page_count}）を超えています: {part!r}")
        if start > end:
            raise ValueError(f"開始ページが終了ページより大きいです: {part!r}")
        ranges.append((start - 1, end - 1))
    return ranges


@_with_fitz_lock
def split_pdf_bytes(data: bytes, ranges) -> list:
    """指定範囲ごとに分割した PDF バイト列のリストを返す。

    ranges は parse_page_ranges() の戻り値（0始まり・両端含む）。
    各分割ファイルには範囲内のしおりだけを付け直す。
    """
    if not ranges:
        raise ValueError("分割するページ範囲が指定されていません。")
    src = fitz.open(stream=data, filetype="pdf")
    try:
        toc = src.get_toc()
        results = []
        for start, end in ranges:
            if start < 0 or end >= len(src) or start > end:
                raise ValueError(f"ページ範囲が不正です: {start + 1}-{end + 1}")
            sub = fitz.open()
            try:
                sub.insert_pdf(src, from_page=start, to_page=end)
                sub_toc = [
                    [lvl, title, pg - start]
                    for lvl, title, pg in toc
                    if start + 1 <= pg <= end + 1
                ]
                if sub_toc:
                    sub.set_toc(sub_toc)
                results.append(sub.tobytes(garbage=4, deflate=True))
            finally:
                sub.close()
        return results
    finally:
        src.close()


# ============================================================
# 4. フォーム入力＋フラット化
# ============================================================

_FIELD_TYPE_NAMES = {
    fitz.PDF_WIDGET_TYPE_TEXT: "テキスト",
    fitz.PDF_WIDGET_TYPE_CHECKBOX: "チェックボックス",
    fitz.PDF_WIDGET_TYPE_COMBOBOX: "コンボボックス",
    fitz.PDF_WIDGET_TYPE_LISTBOX: "リストボックス",
    fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "ラジオボタン",
    fitz.PDF_WIDGET_TYPE_BUTTON: "ボタン",
    fitz.PDF_WIDGET_TYPE_SIGNATURE: "署名",
    fitz.PDF_WIDGET_TYPE_UNKNOWN: "不明",
}


@_with_fitz_lock
def list_form_fields(doc) -> list:
    """全ページのフォーム欄を [{name, type, type_name, value, page}] で返す。"""
    fields = []
    for page_idx, page in enumerate(doc):
        for widget in page.widgets() or []:
            fields.append({
                "name": widget.field_name or "",
                "type": widget.field_type,
                "type_name": _FIELD_TYPE_NAMES.get(widget.field_type, "不明"),
                "value": widget.field_value,
                "page": page_idx + 1,
            })
    return fields


def _clear_widget_value(doc, widget) -> None:
    """欄の値を空にする。PyMuPDF は field_value = "" の代入を無視するため、
    値（/V）が入っている箇所（ウィジェット自身または親フィールド）を
    null にしてから update() で見た目を作り直す。Undo で空欄に戻す時に使う。
    """
    for target in (widget.xref,):
        try:
            doc.xref_set_key(target, "V", "null")
        except Exception:
            pass
    try:
        _type, ref = doc.xref_get_key(widget.xref, "Parent")
        parent_xref = int(str(ref).split()[0])
        doc.xref_set_key(parent_xref, "V", "null")
    except Exception:
        pass


@_with_fitz_lock
def fill_form_fields(doc, values: dict) -> dict:
    """values {欄名: 入力値} をフォームに反映する。

    戻り値: {"filled": 入力できた欄数, "missing": 見つからなかった欄名一覧}
    チェックボックスは True/False または "on"/"off"/"true"/"false"/"1"/"0" を受け付ける。
    """
    filled = 0
    found_names = set()
    for page in doc:
        for widget in page.widgets() or []:
            name = widget.field_name or ""
            if name in values:
                found_names.add(name)
                value = values[name]
                if widget.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                    if isinstance(value, str):
                        value = value.strip().lower() in ("on", "true", "1", "yes", "チェック")
                    widget.field_value = bool(value)
                else:
                    new_text = "" if value is None else str(value)
                    if new_text == "":
                        _clear_widget_value(doc, widget)
                    widget.field_value = new_text
                try:
                    widget.update()
                    filled += 1
                except Exception:
                    pass
    missing = [name for name in values if name not in found_names]
    return {"filled": filled, "missing": missing}


def _pdf_string_literal(text: str) -> str:
    """文字列を PDF の文字列オブジェクト表現にする。

    印字可能な ASCII だけならリテラル文字列 (...)、それ以外は
    UTF-16BE の16進文字列 <FEFF...> にする（日本語も安全に書ける）。
    """
    if all(0x20 <= ord(c) <= 0x7E for c in text):
        escaped = (
            text.replace("\\", "\\\\")
            .replace("(", "\\(")
            .replace(")", "\\)")
        )
        return f"({escaped})"
    return "<" + (b"\xfe\xff" + text.encode("utf-16-be")).hex() + ">"


@_with_fitz_lock
def snapshot_form_field_values(doc, names=None) -> list:
    """フォーム欄の値をウィジェット単位で記録する（Undo 用スナップショット）。

    同じ名前の欄でも、ウィジェット自身が /V を持っていると欄ごとに
    別の値になり得るため、名前単位ではなくウィジェット単位で記録する。

    names が与えられた場合は、その名前の欄だけを対象にする。
    戻り値: [{"page": int（0始まり）, "xref": int, "order": int,
              "name": str, "field_type": int, "value": object,
              "own_value": bool}, ...]
    order は同一ページ・同一名の中での出現順（xref が変わった場合の予備の手がかり）。
    own_value はウィジェット自身が /V を持っているかどうか。
    """
    entries = []
    counters = {}
    for page_idx, page in enumerate(doc):
        for widget in page.widgets() or []:
            name = widget.field_name or ""
            if names is not None and name not in names:
                continue
            key = (page_idx, name)
            order = counters.get(key, 0)
            counters[key] = order + 1
            try:
                vtype, _vraw = doc.xref_get_key(widget.xref, "V")
            except Exception:
                vtype = "null"
            entries.append({
                "page": page_idx,
                "xref": widget.xref,
                "order": order,
                "name": name,
                "field_type": widget.field_type,
                "value": widget.field_value,
                "own_value": vtype != "null",
            })
    return entries


def _match_widget(widgets, entry):
    """ウィジェット一覧からスナップショットの1件に対応するものを探す。

    xref が一致するものを優先し、無ければ（名前・出現順）で探す。
    """
    for widget in widgets:
        if widget.xref == entry.get("xref"):
            return widget
    same_name = [w for w in widgets
                 if (w.field_name or "") == entry.get("name", "")]
    order = entry.get("order", 0)
    if 0 <= order < len(same_name):
        return same_name[order]
    return None


def _restore_widget_value(doc, page, entry) -> bool:
    """スナップショットの1件分を、対応するウィジェットに戻す。

    注意: PyMuPDF の Widget は、取り出した Page が解放されると
    update() が失敗する（Annot is not bound to a page）ため、
    必ず page を保持した状態で呼ぶこと。
    """
    widgets = list(page.widgets() or [])
    widget = _match_widget(widgets, entry)
    if widget is None:
        return False
    value = entry.get("value")
    if widget.field_type in (
        fitz.PDF_WIDGET_TYPE_CHECKBOX,
        fitz.PDF_WIDGET_TYPE_RADIOBUTTON,
    ):
        if isinstance(value, str):
            value = value.strip().lower() in (
                "on", "true", "1", "yes", "チェック")
        widget.field_value = bool(value)
        widget.update()
        return True
    new_text = "" if value is None else str(value)
    if entry.get("own_value"):
        # 自前の /V を直接戻してから、ウィジェットを取り直して
        # 見た目（appearance）をその値で作り直す。
        doc.xref_set_key(widget.xref, "V", _pdf_string_literal(new_text))
        fresh = _match_widget(list(page.widgets() or []), entry)
        if fresh is not None:
            fresh.update()
        return True
    if new_text == "":
        _clear_widget_value(doc, widget)
    widget.field_value = new_text
    widget.update()
    return True


@_with_fitz_lock
def set_form_field_values_by_widget(doc, entries) -> dict:
    """snapshot_form_field_values で記録した値をウィジェット単位で復元する。

    戻り値: {"filled": 復元できた件数, "missing": 見つからなかった件数}

    復元の順序が重要: ウィジェット自身の /V を持つものを先に戻す。
    widget.update() は親フィールドの /V をそのウィジェットの値で書き換える
    副作用があるため、自前 /V を持たないウィジェット（親の値を共有する側）を
    後に戻すことで、親の値が最後に正しい共有値で確定する。
    """
    filled = 0
    missing = 0
    all_entries = list(entries or [])
    for own_pass in (True, False):
        targets = [e for e in all_entries
                   if bool(e.get("own_value")) == own_pass]
        if not targets:
            continue
        done = 0
        for page_no, page in enumerate(doc):
            page_targets = [e for e in targets if e.get("page") == page_no]
            for entry in page_targets:
                try:
                    if _restore_widget_value(doc, page, entry):
                        done += 1
                except Exception:
                    pass
        filled += done
        missing += len(targets) - done
    return {"filled": filled, "missing": missing}


@_with_fitz_lock
def flatten_form_fields(doc) -> dict:
    """フォーム欄を通常の表示に固定（フラット化）する。

    テキスト欄は値をページ上に文字として描画してから欄を削除する。
    チェックボックスはオンなら ✓ を描画する。
    戻り値: {"flattened": 固定した欄数}
    """
    count = 0
    for page in doc:
        widgets = list(page.widgets() or [])
        for widget in widgets:
            rect = fitz.Rect(widget.rect)
            value = widget.field_value
            try:
                if widget.field_type == fitz.PDF_WIDGET_TYPE_TEXT:
                    text = "" if value is None else str(value)
                    if text:
                        fontsize = max(6.0, min(14.0, rect.height * 0.7))
                        _insert_textbox_jp(
                            page,
                            fitz.Rect(rect.x0 + 1, rect.y0, rect.x1 - 1, rect.y1),
                            text, fontsize=fontsize, color=(0, 0, 0), align=0,
                        )
                elif widget.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                    if value:
                        fontsize = max(8.0, min(16.0, rect.height * 0.8))
                        _insert_textbox_jp(
                            page, rect, "✓", fontsize=fontsize,
                            color=(0, 0, 0), align=1,
                        )
                elif widget.field_type in (
                    fitz.PDF_WIDGET_TYPE_COMBOBOX,
                    fitz.PDF_WIDGET_TYPE_LISTBOX,
                    fitz.PDF_WIDGET_TYPE_RADIOBUTTON,
                ):
                    text = "" if value is None else str(value)
                    if text:
                        fontsize = max(6.0, min(14.0, rect.height * 0.7))
                        _insert_textbox_jp(
                            page, rect, text, fontsize=fontsize,
                            color=(0, 0, 0), align=0,
                        )
            except Exception:
                pass
            try:
                page.delete_widget(widget)
                count += 1
            except Exception:
                pass
    return {"flattened": count}


# ============================================================
# 5. 電子印鑑・スタンプ
# ============================================================

SEAL_RED = (0.78, 0.05, 0.05)
STAMP_PRESETS = ["済", "確認", "承認", "受領", "検印", "回覧", "コピー", "原本"]


def draw_round_seal(page, rect, name: str, date_text=None,
                    color=SEAL_RED, line_width: float = 2.0) -> fitz.Rect:
    """丸い印鑑をページに描く。rect は外接する正方形（中心・直径を決める）。

    名字を中央に、日付があればその下に小さく入れる。
    """
    r = fitz.Rect(rect)
    r.normalize()
    if r.is_empty:
        raise ValueError("印鑑の位置・サイズが正しくありません。")
    cx = (r.x0 + r.x1) / 2.0
    cy = (r.y0 + r.y1) / 2.0
    radius = min(r.width, r.height) / 2.0
    if radius <= 0:
        raise ValueError("印鑑のサイズが小さすぎます。")
    shape = page.new_shape()
    shape.draw_circle(fitz.Point(cx, cy), radius)
    shape.finish(color=color, width=line_width)
    shape.draw_circle(fitz.Point(cx, cy), radius * 0.82)
    shape.finish(color=color, width=line_width * 0.6)
    shape.commit()

    name = (name or "").strip()
    if name:
        fontsize = min(radius * 0.5, radius * 1.2 / max(1, len(name)))
        _insert_textbox_jp(
            page,
            fitz.Rect(cx - radius * 0.8, cy - radius * 0.75,
                      cx + radius * 0.8, cy + radius * 0.35),
            name, fontsize=max(6.0, fontsize), color=color, align=1,
        )
    if date_text:
        fontsize = max(5.0, radius * 0.22)
        _insert_textbox_jp(
            page,
            fitz.Rect(cx - radius * 0.8, cy + radius * 0.25,
                      cx + radius * 0.8, cy + radius * 0.75),
            str(date_text), fontsize=fontsize, color=color, align=1,
        )
    return fitz.Rect(cx - radius, cy - radius, cx + radius, cy + radius)


def draw_text_stamp(page, rect, text: str, color=SEAL_RED,
                    line_width: float = 1.5) -> fitz.Rect:
    """「済」「確認」などの角いスタンプをページに描く。二重枠＋中央の文字。"""
    r = fitz.Rect(rect)
    r.normalize()
    if r.is_empty:
        raise ValueError("スタンプの位置・サイズが正しくありません。")
    text = (text or "").strip()
    if not text:
        raise ValueError("スタンプの文字が空です。")
    shape = page.new_shape()
    shape.draw_rect(r)
    shape.finish(color=color, width=line_width)
    inner = fitz.Rect(r.x0 + 2.5, r.y0 + 2.5, r.x1 - 2.5, r.y1 - 2.5)
    if not inner.is_empty:
        shape.draw_rect(inner)
        shape.finish(color=color, width=line_width * 0.6)
    shape.commit()
    fontsize = min(r.height * 0.55, r.width * 0.8 / max(1, len(text)))
    _insert_textbox_jp(
        page, r, text, fontsize=max(6.0, fontsize), color=color, align=1,
    )
    return r


@_with_fitz_lock
def apply_stamp_to_doc(doc, page_1based: int, kind: str, rect,
                       text="", date_text=None, color=SEAL_RED) -> fitz.Rect:
    """doc の指定ページに印鑑（kind="seal"）またはスタンプ（kind="stamp"）を押す。"""
    if not (1 <= page_1based <= len(doc)):
        raise ValueError(f"ページ番号が範囲外です: {page_1based}")
    page = doc[page_1based - 1]
    if kind == "seal":
        return draw_round_seal(page, rect, text, date_text=date_text, color=color)
    if kind == "stamp":
        return draw_text_stamp(page, rect, text, color=color)
    raise ValueError(f"不明なスタンプ種別です: {kind!r}")


# ============================================================
# 6. しおり表示・編集
# ============================================================

@_with_fitz_lock
def get_bookmarks(doc) -> list:
    """しおりを [{level, title, page}]（page は1始まり）で返す。"""
    return [
        {"level": int(lvl), "title": str(title), "page": int(pg)}
        for lvl, title, pg in doc.get_toc()
    ]


def normalize_toc_entries(entries, page_count: int) -> list:
    """entries [{level, title, page}] を set_toc 用の [[階層, タイトル, ページ]] に正規化する。

    純粋関数（文書を変更しない）。先頭は階層1に、以降は直前より1段深いまでに
    正規化する（削除後に先頭が階層2になる等、set_toc が拒否する形を防ぐ）。
    """
    toc = []
    prev_level = 0
    for e in entries:
        level = int(e.get("level", 1))
        title = str(e.get("title", "")).strip()
        page = int(e.get("page", 1))
        if not title:
            raise ValueError("しおりのタイトルが空です。")
        if level < 1:
            raise ValueError("しおりの階層は 1 以上で指定してください。")
        if not (1 <= page <= page_count):
            raise ValueError(f"しおりのページ番号が範囲外です: {page}")
        level = 1 if prev_level == 0 else min(level, prev_level + 1)
        prev_level = level
        toc.append([level, title, page])
    return toc


@_with_fitz_lock
def set_bookmarks(doc, entries) -> int:
    """entries [{level, title, page}] をしおりとして設定する。件数を返す。"""
    toc = normalize_toc_entries(entries, len(doc))
    doc.set_toc(toc)
    return len(toc)


@_with_fitz_lock
def add_bookmark(doc, title: str, page_1based: int, level: int = 1) -> int:
    """末尾にしおりを追加する。追加後の件数を返す。"""
    entries = get_bookmarks(doc)
    entries.append({"level": level, "title": title, "page": page_1based})
    return set_bookmarks(doc, entries)


@_with_fitz_lock
def rename_bookmark(doc, index: int, new_title: str) -> None:
    """index 番目（0始まり）のしおりの名前を変更する。"""
    entries = get_bookmarks(doc)
    if not (0 <= index < len(entries)):
        raise IndexError(f"しおりの番号が範囲外です: {index}")
    if not (new_title or "").strip():
        raise ValueError("しおりの新しい名前が空です。")
    entries[index]["title"] = new_title.strip()
    set_bookmarks(doc, entries)


@_with_fitz_lock
def delete_bookmark(doc, index: int) -> None:
    """index 番目（0始まり）のしおりを削除する。"""
    entries = get_bookmarks(doc)
    if not (0 <= index < len(entries)):
        raise IndexError(f"しおりの番号が範囲外です: {index}")
    del entries[index]
    set_bookmarks(doc, entries)


# ============================================================
# 7. ヘッダー / フッター・ページ番号・透かし
# ============================================================

def _format_template(template: str, page_1based: int, total: int) -> str:
    return template.replace("{page}", str(page_1based)).replace("{total}", str(total))


@_with_fitz_lock
def apply_header_footer(doc, header: str = "", footer: str = "",
                        show_page_number: bool = False,
                        page_number_template: str = "{page} / {total}",
                        watermark: str = "", watermark_fontsize: float = 48.0,
                        watermark_color=(0.85, 0.85, 0.85),
                        watermark_rotation: int = 45,
                        page_indices=None) -> dict:
    """各ページにヘッダー/フッター/ページ番号/透かしを描き込んで保存対象にする。

    header/footer/page_number_template は {page} {total} を使える。
    page_indices は 0始まりの対象ページ一覧（None なら全ページ）。
    戻り値: {"pages": 処理ページ数}
    """
    if not any([header, footer, show_page_number, watermark]):
        raise ValueError("ヘッダー・フッター・ページ番号・透かしのいずれかを指定してください。")
    total = len(doc)
    if total == 0:
        raise ValueError("ページが存在しません。")
    if page_indices is None:
        page_indices = list(range(total))
    count = 0
    for idx in page_indices:
        if not (0 <= idx < total):
            raise ValueError(f"ページ番号が範囲外です: {idx + 1}")
        page = doc[idx]
        rect = page.rect
        page_no = idx + 1
        margin = 36
        if header:
            text = _format_template(header, page_no, total)
            _insert_textbox_jp(
                page,
                fitz.Rect(margin, 10, rect.width - margin, 30),
                text, fontsize=10, color=(0.25, 0.25, 0.25), align=1,
            )
        if footer:
            text = _format_template(footer, page_no, total)
            _insert_textbox_jp(
                page,
                fitz.Rect(margin, rect.height - 30, rect.width - margin, rect.height - 10),
                text, fontsize=10, color=(0.25, 0.25, 0.25), align=1,
            )
        if show_page_number:
            text = _format_template(page_number_template, page_no, total)
            _insert_textbox_jp(
                page,
                fitz.Rect(margin, rect.height - 30, rect.width - margin, rect.height - 10),
                text, fontsize=10, color=(0.25, 0.25, 0.25), align=2,
            )
        if watermark:
            # insert_textbox の rotate は 90 度単位しか扱えないため、
            # 任意角度の透かしは morph（回転行列）付きの insert_text で描く。
            fontsize = max(12.0, float(watermark_fontsize))
            center = fitz.Point(rect.width / 2.0, rect.height / 2.0)
            try:
                text_len = fitz.get_text_length(
                    watermark, fontname="helv", fontsize=fontsize)
            except Exception:
                text_len = fontsize * len(watermark) * 0.6
            start = fitz.Point(center.x - text_len / 2.0, center.y)
            if JAPANESE_FONT:
                try:
                    page.insert_text(
                        start, watermark, fontsize=fontsize,
                        color=tuple(watermark_color),
                        fontfile=JAPANESE_FONT, fontname="trial_jp",
                        morph=(center, fitz.Matrix(int(watermark_rotation))),
                    )
                except Exception:
                    page.insert_text(
                        start, watermark, fontsize=fontsize,
                        color=tuple(watermark_color), fontname="helv",
                        morph=(center, fitz.Matrix(int(watermark_rotation))),
                    )
            else:
                page.insert_text(
                    start, watermark, fontsize=fontsize,
                    color=tuple(watermark_color), fontname="helv",
                    morph=(center, fitz.Matrix(int(watermark_rotation))),
                )
        count += 1
    return {"pages": count}
