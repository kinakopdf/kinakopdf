# -*- coding: utf-8 -*-
"""複数ページへの一括適用（トリミング・範囲墨消し）の対象決定ロジック。

GUI に依存しない純粋関数だけを置く。PyQt6 を import しないため、
GUI なしの単体テストから直接呼び出せる。
"""
from typing import List, Sequence, Tuple

SCOPE_CURRENT = "current"
SCOPE_SELECTED = "selected"
SCOPE_ALL = "all"
SCOPES = (SCOPE_CURRENT, SCOPE_SELECTED, SCOPE_ALL)

# mediabox の比較許容誤差（pt）。PDF によっては端数にわずかな差があるため。
GEOMETRY_TOLERANCE = 1.0

# ページ形状: (mediabox x0, y0, x1, y1, 回転角)
PageGeometry = Tuple[float, float, float, float, int]


def resolve_target_pages(
    scope: str,
    current_idx: int,
    selected_rows: Sequence[int],
    page_count: int,
) -> List[int]:
    """適用範囲の指定から対象ページ番号（0始まり）のソート済み一覧を返す。

    - "current": 今のページだけ
    - "selected": ページ一覧で選択中のページ（空なら今のページ）
    - "all": すべてのページ
    範囲外の行番号は無視する。ページが無い場合は空リストを返す。
    """
    if page_count <= 0:
        return []
    current = max(0, min(int(current_idx), page_count - 1))
    if scope == SCOPE_CURRENT:
        return [current]
    if scope == SCOPE_SELECTED:
        rows = sorted({int(r) for r in selected_rows if 0 <= int(r) < page_count})
        return rows if rows else [current]
    if scope == SCOPE_ALL:
        return list(range(page_count))
    raise ValueError(f"unknown scope: {scope!r}")


def same_page_geometry(
    a: PageGeometry,
    b: PageGeometry,
    tolerance: float = GEOMETRY_TOLERANCE,
) -> bool:
    """2ページの形状（mediabox と回転角）が同じ矩形を共有できるか判定する。

    mediabox の原点が違うページでは同じ PDF 座標の矩形が別の場所に
    なってしまうため、4辺すべてと回転角を比較する。
    """
    if int(a[4]) % 360 != int(b[4]) % 360:
        return False
    return all(abs(float(x) - float(y)) <= tolerance for x, y in zip(a[:4], b[:4]))


def partition_compatible_pages(
    page_geometries: Sequence[PageGeometry],
    ref_idx: int,
    target_indices: Sequence[int],
    tolerance: float = GEOMETRY_TOLERANCE,
) -> Tuple[List[int], List[int]]:
    """対象ページを「基準ページと同じ形状で適用できるもの」と
    「形状が違うためスキップするもの」に分ける。

    戻り値は (適用できるページ番号一覧, スキップするページ番号一覧)。
    基準ページ自体は常に適用対象に含まれる。
    """
    applicable: List[int] = []
    skipped: List[int] = []
    if not page_geometries or not (0 <= ref_idx < len(page_geometries)):
        return applicable, list(target_indices)
    ref = page_geometries[ref_idx]
    for idx in target_indices:
        if not (0 <= idx < len(page_geometries)):
            skipped.append(idx)
            continue
        if same_page_geometry(ref, page_geometries[idx], tolerance):
            applicable.append(idx)
        else:
            skipped.append(idx)
    return applicable, skipped


def format_batch_result(label: str, applied_count: int, skipped_count: int) -> str:
    """一括適用の結果をステータスバー向けの文言にする。"""
    msg = f"{label}を{applied_count}ページに適用しました"
    if skipped_count > 0:
        msg += f"（{skipped_count}ページはサイズや向きが違うためスキップしました）"
    return msg
