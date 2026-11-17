# -*- coding: utf-8 -*-
"""表示だけの折りたたみ（ページの一部の帯を非表示にして詰めて表示）の純粋関数。

PDFファイル自体は一切変えず、画面表示だけを変えるための計算を置く。
GUI に依存しない純粋関数だけを置く。PyQt6 を import しないため、
GUI なしの単体テストから直接呼び出せる。

帯は「表示座標系」で持つ。表示座標系は page.rect を基準にした座標で、
回転・トリミングを反映した後の、画面に表示される方向の座標である。
帯は (向き, a0, a1) のタプルで表す。向き "h" は横帯（y0, y1 を隠して
上下を詰める）、向き "v" は縦帯（x0, x1 を隠して左右を詰める）。
1ページにつき帯は1つまで（横帯と縦帯は同時に存在しない）。
"""
from typing import Optional, Sequence, Tuple

# 折りたたむ帯として認める最小の幅・高さ（pt）。これより小さい帯は誤操作として無視する。
MIN_FOLD_BAND_HEIGHT_PT = 5.0
MIN_FOLD_BAND_WIDTH_PT = 5.0
# ページの高さ・幅に対してこれ以上の割合を折りたたむことは認めない。
MAX_FOLD_BAND_FRACTION = 0.9

# 折りたたみ帯の向き
FOLD_HORIZONTAL = "h"
FOLD_VERTICAL = "v"

# 折りたたみ帯: (向き, a0, a1) 表示座標系。a0 < a1 とする。
# 向き "h" の場合は a0=y0, a1=y1。向き "v" の場合は a0=x0, a1=x1。
FoldBand = Tuple[str, float, float]
# 旧形式（横帯だけの (y0, y1)）も、内部の軸方向の計算では (a0, a1) として扱う。
FoldAxisBand = Tuple[float, float]

# 縦帯とみなすための、ドラッグの高さが幅に対する倍率。
# これ以下の差はほぼ正方形とみなし、従来通り横帯として扱う。
VERTICAL_DRAG_RATIO = 1.1


def decide_fold_orientation(drag_width: float, drag_height: float) -> str:
    """ドラッグした矩形の形から折りたたみの向きを決める。

    縦長（高さが幅の1.1倍を超える）なら縦帯 "v"、それ以外（横長・
    ほぼ正方形を含む）は横帯 "h"。
    """
    if float(drag_height) > float(drag_width) * VERTICAL_DRAG_RATIO:
        return FOLD_VERTICAL
    return FOLD_HORIZONTAL


def clamp_fold_band_to_axis(
    page_start: float,
    page_size: float,
    a0: float,
    a1: float,
) -> FoldAxisBand:
    """帯の範囲をページ内に収まるよう切り詰め、a0 < a1 に正規化する。"""
    start = float(page_start)
    end = float(page_start) + float(page_size)
    lo = max(start, min(float(a0), float(a1)))
    hi = min(end, max(float(a0), float(a1)))
    return (lo, hi)


def clamp_fold_band_to_page(
    page_top: float,
    page_height: float,
    y0: float,
    y1: float,
) -> FoldAxisBand:
    """帯の y 範囲をページ内に収まるよう切り詰め、y0 < y1 に正規化する。"""
    return clamp_fold_band_to_axis(page_top, page_height, y0, y1)


def validate_fold_band_axis(
    page_start: float,
    page_size: float,
    a0: float,
    a1: float,
    unit_label: str = "高さ",
) -> Optional[str]:
    """折りたたみ帯が有効か判定する。有効なら None、無効なら理由の文言を返す。"""
    size = float(page_size)
    if size <= 0:
        return "ページのサイズが取得できませんでした。"
    lo, hi = clamp_fold_band_to_axis(page_start, page_size, a0, a1)
    band_size = hi - lo
    if band_size < MIN_FOLD_BAND_WIDTH_PT:
        return f"折りたたむ帯が小さすぎます（5pt以上の{unit_label}が必要です）。"
    if band_size >= size * MAX_FOLD_BAND_FRACTION:
        return "ページのほとんどを折りたたむことはできません。"
    return None


def validate_fold_band(
    page_top: float,
    page_height: float,
    y0: float,
    y1: float,
) -> Optional[str]:
    """折りたたみ帯（横帯）が有効か判定する。有効なら None、無効なら理由の文言を返す。"""
    err = validate_fold_band_axis(page_top, page_height, y0, y1, "高さ")
    if err == "ページのサイズが取得できませんでした。":
        return "ページの高さが取得できませんでした。"
    return err


def fold_display_size(page_size: float, band: Optional[FoldAxisBand]) -> float:
    """折りたたみ後の表示上のページの長さ（幅または高さ）を返す。帯がなければそのまま。"""
    if band is None:
        return float(page_size)
    return max(1.0, float(page_size) - (float(band[1]) - float(band[0])))


def fold_display_height(page_height: float, band: Optional[FoldAxisBand]) -> float:
    """折りたたみ後の表示上のページ高さを返す。帯がなければそのままの高さ。"""
    return fold_display_size(page_height, band)


def fold_display_width(page_width: float, band: Optional[FoldAxisBand]) -> float:
    """折りたたみ後の表示上のページ幅を返す。帯がなければそのままの幅。"""
    return fold_display_size(page_width, band)


def view_coord_to_folded_coord(v: float, band: Optional[FoldAxisBand]) -> float:
    """表示座標系の座標を、折りたたみ後の表示座標に変換する。

    帯より手前はそのまま、帯より先は帯の分だけ詰める。
    帯の中の座標は帯の手前端（つなぎ目）に寄せる。
    """
    if band is None:
        return float(v)
    a0, a1 = float(band[0]), float(band[1])
    if v < a0:
        return float(v)
    if v >= a1:
        return float(v) - (a1 - a0)
    return a0


def folded_coord_to_view_coord(v: float, band: Optional[FoldAxisBand]) -> float:
    """折りたたみ後の表示座標を、元の表示座標系の座標に戻す。

    つなぎ目より手前はそのまま、つなぎ目より先は帯の分だけ戻す。
    """
    if band is None:
        return float(v)
    a0, a1 = float(band[0]), float(band[1])
    if v < a0:
        return float(v)
    return float(v) + (a1 - a0)


def view_y_to_folded_y(y: float, band: Optional[FoldAxisBand]) -> float:
    """表示座標系の y を、折りたたみ後の表示 y に変換する。"""
    return view_coord_to_folded_coord(y, band)


def folded_y_to_view_y(y: float, band: Optional[FoldAxisBand]) -> float:
    """折りたたみ後の表示 y を、元の表示座標系の y に戻す。"""
    return folded_coord_to_view_coord(y, band)


def view_x_to_folded_x(x: float, band: Optional[FoldAxisBand]) -> float:
    """表示座標系の x を、折りたたみ後の表示 x に変換する。"""
    return view_coord_to_folded_coord(x, band)


def folded_x_to_view_x(x: float, band: Optional[FoldAxisBand]) -> float:
    """折りたたみ後の表示 x を、元の表示座標系の x に戻す。"""
    return folded_coord_to_view_coord(x, band)


def view_coord_in_band(v: float, band: Optional[FoldAxisBand]) -> bool:
    """表示座標系の座標が折りたたみ帯の中にあるか判定する。"""
    if band is None:
        return False
    return float(band[0]) <= float(v) < float(band[1])


def view_point_in_band(y: float, band: Optional[FoldAxisBand]) -> bool:
    """表示座標系の y が折りたたみ帯の中にあるか判定する。"""
    return view_coord_in_band(y, band)


def view_rect_intersects_band_axis(
    rect: Sequence[float],
    band: Optional[FoldAxisBand],
    orientation: str = FOLD_HORIZONTAL,
) -> bool:
    """表示座標系の矩形 (x0, y0, x1, y1) が折りたたみ帯と重なるか判定する。

    横帯は y 方向の重なり、縦帯は x 方向の重なりで判定する。
    """
    if band is None:
        return False
    if orientation == FOLD_VERTICAL:
        a0 = min(float(rect[0]), float(rect[2]))
        a1 = max(float(rect[0]), float(rect[2]))
    else:
        a0 = min(float(rect[1]), float(rect[3]))
        a1 = max(float(rect[1]), float(rect[3]))
    return a0 < float(band[1]) and a1 > float(band[0])


def view_rect_intersects_band(
    rect: Sequence[float],
    band: Optional[FoldAxisBand],
) -> bool:
    """表示座標系の矩形 (x0, y0, x1, y1) が横帯の折りたたみ帯と重なるか判定する。"""
    return view_rect_intersects_band_axis(rect, band, FOLD_HORIZONTAL)


def fold_band_pixel_range(
    page_start: float,
    band: FoldAxisBand,
    scale: float,
) -> Tuple[int, int]:
    """帯をピクセル範囲に変換する。

    戻り値は (切り取る先頭, 切り取る末尾の次)。番号はページ全体を
    描画したピクセル画像の上端・左端を 0 とする。
    """
    first = int(round((float(band[0]) - float(page_start)) * float(scale)))
    last = int(round((float(band[1]) - float(page_start)) * float(scale)))
    if last < first:
        first, last = last, first
    return first, last


def fold_band_pixel_rows(
    page_top: float,
    band: FoldAxisBand,
    scale: float,
) -> Tuple[int, int]:
    """帯をピクセル行の範囲に変換する。

    戻り値は (切り取る先頭行, 切り取る末尾行の次の行)。行番号は
    ページ全体を描画したピクセル画像の上端を 0 とする。
    """
    return fold_band_pixel_range(page_top, band, scale)


def fold_band_pixel_cols(
    page_left: float,
    band: FoldAxisBand,
    scale: float,
) -> Tuple[int, int]:
    """帯をピクセル列の範囲に変換する。

    戻り値は (切り取る先頭列, 切り取る末尾列の次の列)。列番号は
    ページ全体を描画したピクセル画像の左端を 0 とする。
    """
    return fold_band_pixel_range(page_left, band, scale)
