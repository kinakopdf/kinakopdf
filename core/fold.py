# -*- coding: utf-8 -*-
"""表示だけの折りたたみ（ページの一部の横帯を非表示にして下を詰めて表示）の純粋関数。

PDFファイル自体は一切変えず、画面表示だけを変えるための計算を置く。
GUI に依存しない純粋関数だけを置く。PyQt6 を import しないため、
GUI なしの単体テストから直接呼び出せる。

帯は「表示座標系」で持つ。表示座標系は page.rect を基準にした座標で、
回転・トリミングを反映した後の、画面に表示される上下方向の座標である。
帯は (y0, y1) のタプルで表し、y0 < y1 とする。x 方向はページ全体が対象。
"""
from typing import Optional, Sequence, Tuple

# 折りたたむ帯として認める最小の高さ（pt）。これより小さい帯は誤操作として無視する。
MIN_FOLD_BAND_HEIGHT_PT = 5.0
# ページ高さに対してこれ以上の割合を折りたたむことは認めない。
MAX_FOLD_BAND_FRACTION = 0.9

# 折りたたみ帯: (y0, y1) 表示座標系
FoldBand = Tuple[float, float]


def clamp_fold_band_to_page(
    page_top: float,
    page_height: float,
    y0: float,
    y1: float,
) -> FoldBand:
    """帯の y 範囲をページ内に収まるよう切り詰め、y0 < y1 に正規化する。"""
    top = float(page_top)
    bottom = float(page_top) + float(page_height)
    lo = max(top, min(float(y0), float(y1)))
    hi = min(bottom, max(float(y0), float(y1)))
    return (lo, hi)


def validate_fold_band(
    page_top: float,
    page_height: float,
    y0: float,
    y1: float,
) -> Optional[str]:
    """折りたたみ帯が有効か判定する。有効なら None、無効なら理由の文言を返す。"""
    height = float(page_height)
    if height <= 0:
        return "ページの高さが取得できませんでした。"
    lo, hi = clamp_fold_band_to_page(page_top, page_height, y0, y1)
    band_h = hi - lo
    if band_h < MIN_FOLD_BAND_HEIGHT_PT:
        return "折りたたむ帯が小さすぎます（5pt以上の高さが必要です）。"
    if band_h >= height * MAX_FOLD_BAND_FRACTION:
        return "ページのほとんどを折りたたむことはできません。"
    return None


def fold_display_height(page_height: float, band: Optional[FoldBand]) -> float:
    """折りたたみ後の表示上のページ高さを返す。帯がなければそのままの高さ。"""
    if band is None:
        return float(page_height)
    return max(1.0, float(page_height) - (float(band[1]) - float(band[0])))


def view_y_to_folded_y(y: float, band: Optional[FoldBand]) -> float:
    """表示座標系の y を、折りたたみ後の表示 y に変換する。

    帯より上はそのまま、帯より下は帯の高さ分だけ上に詰める。
    帯の中の y は帯の上端（つなぎ目）に寄せる。
    """
    if band is None:
        return float(y)
    y0, y1 = float(band[0]), float(band[1])
    if y < y0:
        return float(y)
    if y >= y1:
        return float(y) - (y1 - y0)
    return y0


def folded_y_to_view_y(y: float, band: Optional[FoldBand]) -> float:
    """折りたたみ後の表示 y を、元の表示座標系の y に戻す。

    つなぎ目より上はそのまま、つなぎ目より下は帯の高さ分だけ下に戻す。
    """
    if band is None:
        return float(y)
    y0, y1 = float(band[0]), float(band[1])
    if y < y0:
        return float(y)
    return float(y) + (y1 - y0)


def view_point_in_band(y: float, band: Optional[FoldBand]) -> bool:
    """表示座標系の y が折りたたみ帯の中にあるか判定する。"""
    if band is None:
        return False
    return float(band[0]) <= float(y) < float(band[1])


def view_rect_intersects_band(
    rect: Sequence[float],
    band: Optional[FoldBand],
) -> bool:
    """表示座標系の矩形 (x0, y0, x1, y1) が折りたたみ帯と重なるか判定する。"""
    if band is None:
        return False
    y0 = min(float(rect[1]), float(rect[3]))
    y1 = max(float(rect[1]), float(rect[3]))
    return y0 < float(band[1]) and y1 > float(band[0])


def fold_band_pixel_rows(
    page_top: float,
    band: FoldBand,
    scale: float,
) -> Tuple[int, int]:
    """帯をピクセル行の範囲に変換する。

    戻り値は (切り取る先頭行, 切り取る末尾行の次の行)。行番号は
    ページ全体を描画したピクセル画像の上端を 0 とする。
    """
    top_row = int(round((float(band[0]) - float(page_top)) * float(scale)))
    bottom_row = int(round((float(band[1]) - float(page_top)) * float(scale)))
    if bottom_row < top_row:
        top_row, bottom_row = bottom_row, top_row
    return top_row, bottom_row
