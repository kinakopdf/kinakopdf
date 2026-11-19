# -*- coding: utf-8 -*-
"""PDFの文字データ（埋め込みテキスト）の取り出しと、文の連結ユーティリティ。

文字データ表示ダイアログ（ui/text_data_dialog.py）から使う。
- extract_page_texts: ページごとの埋め込みテキストをそのまま返す。
  注釈（墨消し等）がある場合は、保存・抽出と同じ焼き込みを行った複製から
  取り出すため、「保存したファイルに実際に残る文字」と一致する。
- join_fragments: OCRや抽出で改行ごとにバラバラになった文を1行につなげる。
"""
from core.geometry import FITZ_LOCK
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
        has_annotations = bool(page_annotations) and any(
            bool(ann_list) for ann_list in page_annotations.values()
        )
        if not has_annotations:
            return [doc[idx].get_text() for idx in range(len(doc))]

        baked = PDFDocumentManager.extract_pages_as_doc(
            doc, list(range(len(doc))), page_annotations
        )
        try:
            return [baked[idx].get_text() for idx in range(len(baked))]
        finally:
            baked.close()
