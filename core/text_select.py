# -*- coding: utf-8 -*-
"""テキスト選択：PyMuPDFの単語矩形から選択範囲内の単語を集めてテキスト化する純粋関数。

単語は PyMuPDF の page.get_text("words") が返すタプル
(x0, y0, x1, y1, text, block_no, line_no, word_no) の形で受け取る。
座標系は呼び出し側と単語で統一されていること（ビューア側では raw 座標に
変換してから渡す）。Qt や PyMuPDF のページには依存しない。
"""
from typing import List, Sequence, Tuple

# 単語タプル (x0, y0, x1, y1, text, block_no, line_no, word_no)
WordTuple = Tuple[float, float, float, float, str, int, int, int]
RectTuple = Tuple[float, float, float, float]

# 中心が選択範囲の外にある単語でも、重なり面積がこの割合以上あれば選択対象にする。
OVERLAP_RATIO = 0.35


def _normalize_rect(rect: Sequence[float]) -> RectTuple:
    x0, y0, x1, y1 = (float(v) for v in rect[:4])
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def word_matches_rect(
    word: Sequence,
    selection_rect: Sequence[float],
    overlap_ratio: float = OVERLAP_RATIO,
) -> bool:
    """単語矩形が選択矩形に当たるか判定する。

    単語の中心が選択範囲内にあるか、重なり面積が単語面積の一定割合以上
    あれば選択対象にする。幅・高さが 0 の単語は対象外。
    """
    wx0, wy0, wx1, wy1 = _normalize_rect(word[:4])
    if wx1 - wx0 <= 0 or wy1 - wy0 <= 0:
        return False
    sx0, sy0, sx1, sy1 = _normalize_rect(selection_rect)
    cx = (wx0 + wx1) * 0.5
    cy = (wy0 + wy1) * 0.5
    if sx0 <= cx <= sx1 and sy0 <= cy <= sy1:
        return True
    ix0, iy0 = max(wx0, sx0), max(wy0, sy0)
    ix1, iy1 = min(wx1, sx1), min(wy1, sy1)
    if ix1 <= ix0 or iy1 <= iy0:
        return False
    inter = (ix1 - ix0) * (iy1 - iy0)
    area = (wx1 - wx0) * (wy1 - wy0)
    return inter >= area * overlap_ratio


def _line_key(word: Sequence):
    """行の同一性判定キー。(block_no, line_no) が無ければ y 座標で代用する。"""
    if len(word) >= 8:
        try:
            return (int(word[5]), int(word[6]))
        except (TypeError, ValueError):
            pass
    return ("y", round(float(word[1]) / 4.0))


def select_words_in_rect(
    words: Sequence[Sequence],
    selection_rect: Sequence[float],
    overlap_ratio: float = OVERLAP_RATIO,
) -> List[Sequence]:
    """選択矩形と重なる単語を読み順（ブロック→行→単語番号）で返す。

    空の選択矩形や単語が無い場合は空リストを返す。空白だけの単語は除く。
    """
    sel = _normalize_rect(selection_rect)
    if sel[2] - sel[0] <= 0 or sel[3] - sel[1] <= 0:
        return []
    matched: List[Sequence] = []
    for word in words or []:
        if len(word) < 5:
            continue
        if not str(word[4]).strip():
            continue
        if word_matches_rect(word, sel, overlap_ratio):
            matched.append(word)
    if all(len(w) >= 8 for w in matched):
        matched.sort(key=lambda w: (int(w[5]), int(w[6]), int(w[7])))
    else:
        matched.sort(key=lambda w: (round(float(w[1]) / 4.0), round(float(w[0]), 1)))
    return matched


def words_to_text(selected_words: Sequence[Sequence]) -> str:
    """選択済み単語（読み順）をつなげてテキストにする。

    同じ行の単語は半角スペースでつなぎ、行またはブロックが変わると改行する。
    """
    if not selected_words:
        return ""
    parts: List[str] = []
    prev_key = None
    for word in selected_words:
        text = str(word[4]).strip()
        if not text:
            continue
        key = _line_key(word)
        if prev_key is None:
            parts.append(text)
        elif key == prev_key:
            parts.append(" ")
            parts.append(text)
        else:
            parts.append("\n")
            parts.append(text)
        prev_key = key
    return "".join(parts)


def extract_selected_text(
    words: Sequence[Sequence],
    selection_rect: Sequence[float],
    overlap_ratio: float = OVERLAP_RATIO,
) -> str:
    """単語一覧と選択矩形から、選択中のテキストを返す。"""
    return words_to_text(select_words_in_rect(words, selection_rect, overlap_ratio))
