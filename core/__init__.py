# -*- coding: utf-8 -*-
"""共通定数・幾何計算・注釈当たり判定ユーティリティ（純粋Python + PyMuPDF層）"""
import logging
import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import fitz

logger = logging.getLogger("SecureLocalPDFEditor")
if not logger.handlers:
    _handler = logging.NullHandler()
    logger.addHandler(_handler)

ERASE_WIDTH = 12.0
HIGHLIGHT_OPACITY = 0.35
TEXT_LINE_SPACING = 1.20
DEFAULT_DRAW_WIDTH = 1.5

COLOR_PRESETS = [
    ("赤", (0.90, 0.05, 0.05)),
    ("黒", (0.00, 0.00, 0.00)),
    ("青", (0.00, 0.28, 0.85)),
    ("緑", (0.00, 0.55, 0.18)),
    ("オレンジ", (0.95, 0.45, 0.00)),
    ("紫", (0.55, 0.05, 0.75)),
    ("黄（マーカー向き）", (0.98, 0.90, 0.05)),
]


def point_to_segment_distance(
    px: float, py: float, x0: float, y0: float, x1: float, y1: float
) -> float:
    """点 (px, py) と線分 (x0, y0)-(x1, y1) の最短距離を計算する"""
    dx = x1 - x0
    dy = y1 - y0
    len_sq = dx * dx + dy * dy
    if len_sq <= 1e-8:
        return math.hypot(px - x0, py - y0)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / len_sq))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


# ==========================================
# 消しゴム軌跡・ストローク分割・注釈当たり判定
# ==========================================

def stroke_to_erase_rects(
    points: Sequence[Tuple[float, float]], erase_width: float = ERASE_WIDTH
) -> List[fitz.Rect]:
    if not points:
        return []

    half = erase_width / 2.0
    step = max(erase_width / 4.0, 1.0)
    rects: List[fitz.Rect] = []

    def add_point_rect(px: float, py: float) -> None:
        rects.append(fitz.Rect(px - half, py - half, px + half, py + half))

    add_point_rect(points[0][0], points[0][1])

    for i in range(1, len(points)):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        dist = math.hypot(x1 - x0, y1 - y0)
        if dist > step:
            num_steps = int(math.ceil(dist / step))
            for s in range(1, num_steps + 1):
                t = s / num_steps
                ix = x0 + (x1 - x0) * t
                iy = y0 + (y1 - y0) * t
                add_point_rect(ix, iy)
        else:
            add_point_rect(x1, y1)

    return rects


def annotation_to_erase_rects(ann: Dict[str, Any]) -> List[fitz.Rect]:
    ann_type = ann.get("type")
    if ann_type == "erase":
        return stroke_to_erase_rects(ann.get("points", []), ERASE_WIDTH)
    if ann_type == "box_erase" and "rect" in ann:
        r = fitz.Rect(ann["rect"])
        r.normalize()
        return [r]
    return []


def adjust_updated_after_on_remove(ann_list: List[Dict[str, Any]], removed_idx: int) -> None:
    """注釈リストから removed_idx の要素が削除された際、updated_after のインデックスズレを補正する"""
    for ann in ann_list:
        if "updated_after" in ann and ann["updated_after"] > removed_idx:
            ann["updated_after"] = max(0, int(ann["updated_after"]) - 1)


def adjust_updated_after_on_insert(ann_list: List[Dict[str, Any]], inserted_idx: int) -> None:
    """注釈リストの inserted_idx に要素が復元挿入された際、updated_after のインデックスズレを補正する。

    挿入位置より後ろにある閾値だけを +1 する（``> inserted_idx``）。
    ``>=`` にすると、復元した消しゴム注釈そのものが閾値と同位置にあった場合に
    閾値が 1 つ進み、復元した消しゴムが「後からの消去」とみなされなくなる
    （audit #1 の再現例: updated_after=3 のテキストに対し index 3 の消去を
    復元すると updated_after が 4 になり、消去が get_later_user_erase_rects
    から漏れてテキストが焼き戻される）。
    """
    for i, ann in enumerate(ann_list):
        if i != inserted_idx and "updated_after" in ann and ann["updated_after"] > inserted_idx:
            ann["updated_after"] = int(ann["updated_after"]) + 1


def get_later_user_erase_rects(
    ann_list: Sequence[Dict[str, Any]], idx: int, ann: Dict[str, Any]
) -> List[fitz.Rect]:
    start_idx = max(idx + 1, int(ann.get("updated_after", idx + 1)))
    later_erases: List[fitz.Rect] = []
    for later_ann in ann_list[start_idx:]:
        if later_ann.get("type") in ("erase", "box_erase"):
            later_erases.extend(annotation_to_erase_rects(later_ann))
    return later_erases


def is_rect_substantially_erased(
    target_rect: fitz.Rect,
    erase_rects: Sequence[fitz.Rect],
    ratio_threshold: float = 0.55,
) -> bool:
    """対象矩形の中心が消去領域に含まれるか、面積の一定割合以上が消去されたか判定する"""
    if not erase_rects:
        return False
    r = fitz.Rect(target_rect)
    r.normalize()
    if r.is_empty:
        return True
    center = fitz.Point((r.x0 + r.x1) / 2.0, (r.y0 + r.y1) / 2.0)
    if any(er.contains(center) or er.contains(r) for er in erase_rects):
        return True
    area = r.width * r.height
    if area <= 1e-4:
        return any(er.intersects(r) for er in erase_rects)
    # 重なり合う消去矩形を単純加算すると二重計上になるため（audit #12）、
    # 対象矩形との交差部分について和集合の面積を計算する。
    # x 座標の辺でスラブに分割し、各スラブ内で y 区間の和集合長を求める。
    clipped = []
    for er in erase_rects:
        inter = r & er
        if not inter.is_empty:
            clipped.append(inter)
    if not clipped:
        return False
    x_edges = sorted({x for rect in clipped for x in (rect.x0, rect.x1)})
    covered_area = 0.0
    for x0, x1 in zip(x_edges, x_edges[1:]):
        width = x1 - x0
        if width <= 0:
            continue
        y_intervals = sorted(
            (rect.y0, rect.y1)
            for rect in clipped
            if rect.x0 <= x0 and rect.x1 >= x1
        )
        y_covered = 0.0
        cur_y0 = None
        cur_y1 = None
        for y0, y1 in y_intervals:
            if cur_y0 is None:
                cur_y0, cur_y1 = y0, y1
            elif y0 <= cur_y1:
                cur_y1 = max(cur_y1, y1)
            else:
                y_covered += cur_y1 - cur_y0
                cur_y0, cur_y1 = y0, y1
        if cur_y0 is not None:
            y_covered += cur_y1 - cur_y0
        covered_area += width * y_covered
        if covered_area >= area * ratio_threshold:
            return True
    return False


def collect_text_only_redact_raw_rects(ann_list: Sequence[Dict[str, Any]]) -> List[fitz.Rect]:
    rects: List[fitz.Rect] = []
    for ann in ann_list:
        if ann.get("type") == "text_redact" and "rect" in ann:
            r = fitz.Rect(ann["rect"])
            r.normalize()
            rects.append(r)
        elif ann.get("type") == "text" and ann.get("redact_rect"):
            r = fitz.Rect(ann["redact_rect"])
            r.normalize()
            rects.append(r)
    return rects


def split_text_only_redact_raw_rects(
    ann_list: Sequence[Dict[str, Any]],
) -> Tuple[List[fitz.Rect], List[fitz.Rect]]:
    """テキストのみ墨消しの矩形を (通常, 画像を残す) の2群に分けて返す。

    「画像を残す」群は、スキャン画像の上に乗ったOCR文字の置換に伴う消去で、
    文字データだけを取り除き、背景画像のピクセルには触れない。
    - text_redact 注釈に keep_image が付いているもの
    - invisible な text 注釈の redact_rect
    が該当する。それ以外は通常群（従来どおり画像ピクセル消去の対象）。
    """
    normal: List[fitz.Rect] = []
    keep_image: List[fitz.Rect] = []
    for ann in ann_list:
        if ann.get("type") == "text_redact" and "rect" in ann:
            r = fitz.Rect(ann["rect"])
            r.normalize()
            (keep_image if ann.get("keep_image") else normal).append(r)
        elif ann.get("type") == "text" and ann.get("redact_rect"):
            r = fitz.Rect(ann["redact_rect"])
            r.normalize()
            (keep_image if ann.get("invisible") else normal).append(r)
    return normal, keep_image


def collect_all_erased_raw_rects(ann_list: Sequence[Dict[str, Any]]) -> List[fitz.Rect]:
    """テキスト消去領域（text_redact / redact_rect）と白塗り消去領域（erase / box_erase）を一括収集する"""
    rects = collect_text_only_redact_raw_rects(ann_list)
    for ann in ann_list:
        if ann.get("type") in ("erase", "box_erase"):
            rects.extend(annotation_to_erase_rects(ann))
    return rects


def annotation_affects_base_pdf_text(ann: Optional[Dict[str, Any]]) -> bool:
    if not ann:
        return False
    return ann.get("type") == "text_redact" or bool(ann.get("redact_rect"))


def clip_stroke_by_rects(
    points: Sequence[Tuple[float, float]],
    erase_rects: Sequence[fitz.Rect],
    step: float = 1.5,
) -> List[List[Tuple[float, float]]]:
    if not points:
        return []
    if not erase_rects:
        return [list(points)]

    dense_pts: List[Tuple[float, float]] = [(points[0][0], points[0][1])]
    for i in range(1, len(points)):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        dist = math.hypot(x1 - x0, y1 - y0)
        if dist > step:
            num_steps = int(math.ceil(dist / step))
            for s in range(1, num_steps + 1):
                t = s / num_steps
                dense_pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
        else:
            dense_pts.append((x1, y1))

    segments: List[List[Tuple[float, float]]] = []
    current_seg: List[Tuple[float, float]] = []
    for px, py in dense_pts:
        pt = fitz.Point(px, py)
        if any(er.contains(pt) for er in erase_rects):
            if len(current_seg) == 1:
                current_seg.append(current_seg[0])
            if current_seg:
                segments.append(current_seg)
                current_seg = []
        else:
            current_seg.append((px, py))

    if len(current_seg) == 1:
        current_seg.append(current_seg[0])
    if current_seg:
        segments.append(current_seg)

    return segments


def is_highlight_annotation_visible(
    ann_list: Sequence[Dict[str, Any]], idx: int, ann: Dict[str, Any]
) -> bool:
    if not ann or ann.get("type") != "highlight" or "rect" not in ann:
        return False
    r = fitz.Rect(ann["rect"])
    r.normalize()
    if r.is_empty:
        return False
    later_erases = get_later_user_erase_rects(ann_list, idx, ann)
    return not is_rect_substantially_erased(r, later_erases)


def find_highlight_annotation_at_point(
    ann_list: Sequence[Dict[str, Any]],
    raw_pt: fitz.Point,
    padding: float = 1.8,
) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    for idx in range(len(ann_list) - 1, -1, -1):
        ann = ann_list[idx]
        if not is_highlight_annotation_visible(ann_list, idx, ann):
            continue
        r = fitz.Rect(ann["rect"])
        r.normalize()
        hit_r = fitz.Rect(r.x0 - padding, r.y0 - padding, r.x1 + padding, r.y1 + padding)
        if hit_r.contains(raw_pt):
            return idx, ann
    return None, None


def find_highlights_in_rect(
    ann_list: Sequence[Dict[str, Any]],
    target_raw_rect: fitz.Rect,
) -> List[Tuple[int, Dict[str, Any]]]:
    """指定されたPDF矩形領域と交差する可視マーカー注釈の (idx, ann) リストを返す"""
    tr = fitz.Rect(target_raw_rect)
    tr.normalize()
    if tr.is_empty:
        return []
    matched: List[Tuple[int, Dict[str, Any]]] = []
    for idx, ann in enumerate(ann_list):
        if not is_highlight_annotation_visible(ann_list, idx, ann):
            continue
        r = fitz.Rect(ann["rect"])
        r.normalize()
        if r.intersects(tr):
            matched.append((idx, ann))
    return matched


def get_visible_draw_segments(
    ann_list: Sequence[Dict[str, Any]], idx: int, ann: Dict[str, Any]
) -> List[List[Tuple[float, float]]]:
    if not ann or ann.get("type") != "draw":
        return []
    pts = ann.get("points", [])
    if not pts:
        return []
    later_erases = get_later_user_erase_rects(ann_list, idx, ann)
    return clip_stroke_by_rects(pts, later_erases)


def find_draw_annotation_at_point(
    ann_list: Sequence[Dict[str, Any]],
    raw_pt: fitz.Point,
    default_width: float = DEFAULT_DRAW_WIDTH,
) -> Tuple[Optional[int], Optional[Dict[str, Any]], Optional[List[List[Tuple[float, float]]]]]:
    for idx in range(len(ann_list) - 1, -1, -1):
        ann = ann_list[idx]
        segments = get_visible_draw_segments(ann_list, idx, ann)
        if not segments:
            continue
        hit_tol = max(4.5, float(ann.get("width", default_width)) * 0.5 + 3.5)
        for seg in segments:
            if len(seg) == 1:
                if math.hypot(raw_pt.x - seg[0][0], raw_pt.y - seg[0][1]) <= hit_tol:
                    return idx, ann, segments
            else:
                for i in range(1, len(seg)):
                    x0, y0 = seg[i - 1]
                    x1, y1 = seg[i]
                    if point_to_segment_distance(raw_pt.x, raw_pt.y, x0, y0, x1, y1) <= hit_tol:
                        return idx, ann, segments
    return None, None, None


def find_draws_along_eraser_stroke(
    ann_list: Sequence[Dict[str, Any]],
    eraser_pts: Sequence[Tuple[float, float]],
    default_width: float = DEFAULT_DRAW_WIDTH,
) -> List[Tuple[int, Dict[str, Any]]]:
    if not eraser_pts:
        return []
    dense_eraser: List[Tuple[float, float]] = [(eraser_pts[0][0], eraser_pts[0][1])]
    for i in range(1, len(eraser_pts)):
        x0, y0 = eraser_pts[i - 1]
        x1, y1 = eraser_pts[i]
        dist = math.hypot(x1 - x0, y1 - y0)
        if dist > 2.5:
            steps = int(math.ceil(dist / 2.5))
            for s in range(1, steps + 1):
                t = s / steps
                dense_eraser.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
        else:
            dense_eraser.append((x1, y1))

    matched: List[Tuple[int, Dict[str, Any]]] = []
    for idx, ann in enumerate(ann_list):
        segments = get_visible_draw_segments(ann_list, idx, ann)
        if not segments:
            continue
        hit_tol = max(6.0, float(ann.get("width", default_width)) * 0.5 + 4.5)
        hit = False
        for seg in segments:
            if hit:
                break
            if len(seg) == 1:
                sx0, sy0 = seg[0]
                if any(math.hypot(ex - sx0, ey - sy0) <= hit_tol for ex, ey in dense_eraser):
                    hit = True
                    break
            else:
                for i in range(1, len(seg)):
                    sx0, sy0 = seg[i - 1]
                    sx1, sy1 = seg[i]
                    for ex, ey in dense_eraser:
                        if point_to_segment_distance(ex, ey, sx0, sy0, sx1, sy1) <= hit_tol:
                            hit = True
                            break
                    if hit:
                        break
        if hit:
            matched.append((idx, ann))
    return matched


def find_text_annotation_at_point(
    ann_list: Sequence[Dict[str, Any]],
    raw_pt: fitz.Point,
    estimate_bbox_fn: Callable[[Dict[str, Any]], fitz.Rect],
    min_pad: float = 3.5,
) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """PDF座標 raw_pt にヒットする可視テキスト注釈を末尾から検索して (idx, ann) を返す"""
    for idx in range(len(ann_list) - 1, -1, -1):
        ann = ann_list[idx]
        if ann.get("type") != "text":
            continue
        later_erases = get_later_user_erase_rects(ann_list, idx, ann)
        bbox = estimate_bbox_fn(ann)
        if is_rect_substantially_erased(bbox, later_erases):
            continue
        pad = max(min_pad, float(ann.get("fontsize", 14.0)) * 0.25)
        hit_r = fitz.Rect(bbox.x0 - pad, bbox.y0 - pad, bbox.x1 + pad, bbox.y1 + pad)
        if hit_r.contains(raw_pt):
            return idx, ann
    return None, None


def find_pdf_token_at_point(
    tokens: Sequence[Dict[str, Any]],
    ann_list: Sequence[Dict[str, Any]],
    raw_pt: fitz.Point,
    padding: float = 1.5,
) -> Optional[Dict[str, Any]]:
    """消去済み領域を除外した上で、PDF座標 raw_pt にヒットするPDF内文字トークンを返す"""
    if not tokens:
        return None
    erased_rects = collect_all_erased_raw_rects(ann_list)
    for token in tokens:
        r = fitz.Rect(token["raw_rect"])
        r.normalize()
        center = fitz.Point((r.x0 + r.x1) * 0.5, (r.y0 + r.y1) * 0.5)
        if any(er.contains(center) for er in erased_rects):
            continue
        hit_r = fitz.Rect(r.x0 - padding, r.y0 - padding, r.x1 + padding, r.y1 + padding)
        if hit_r.contains(raw_pt):
            return token
    return None


def find_text_targets_in_rect(
    ann_list: Sequence[Dict[str, Any]],
    tokens: Sequence[Dict[str, Any]],
    target_raw_rect: fitz.Rect,
    estimate_bbox_fn: Callable[[Dict[str, Any]], fitz.Rect],
) -> Tuple[List[int], List[Dict[str, Any]]]:
    """指定されたPDF矩形領域と交差する可視テキスト注釈のインデックス一覧とPDF内トークン一覧を返す"""
    tr = fitz.Rect(target_raw_rect)
    tr.normalize()
    if tr.is_empty:
        return [], []

    matched_ann_indices: List[int] = []
    for idx, ann in enumerate(ann_list):
        if ann.get("type") != "text":
            continue
        later_erases = get_later_user_erase_rects(ann_list, idx, ann)
        bbox = estimate_bbox_fn(ann)
        if is_rect_substantially_erased(bbox, later_erases):
            continue
        if bbox.intersects(tr):
            matched_ann_indices.append(idx)

    erased_rects = collect_all_erased_raw_rects(ann_list)
    matched_tokens: List[Dict[str, Any]] = []
    for token in (tokens or []):
        r = fitz.Rect(token["raw_rect"])
        r.normalize()
        center = fitz.Point((r.x0 + r.x1) * 0.5, (r.y0 + r.y1) * 0.5)
        if any(er.contains(center) for er in erased_rects):
            continue
        if r.intersects(tr):
            matched_tokens.append(token)

    return matched_ann_indices, matched_tokens


def build_text_annotation(
    text: str,
    pos: Tuple[float, float],
    baseline_pos: Tuple[float, float],
    fontsize: float,
    color: Sequence[float],
    rotation: int,
    updated_after: int = 0,
    redact_rect: Optional[Sequence[float]] = None,
    invisible: bool = False,
) -> Dict[str, Any]:
    """新規または置換用のテキスト注釈辞書を統一フォーマットで生成する

    invisible=True の場合、焼き込み時にレンダリングモード3（不可視）で
    挿入される。スキャン画像の上に乗ったOCR文字の置換用で、見た目は
    元の画像のままにし、内部の文字データだけを差し替える。
    """
    annot: Dict[str, Any] = {
        "type": "text",
        "text": str(text),
        "pos": (float(pos[0]), float(pos[1])),
        "baseline_pos": (float(baseline_pos[0]), float(baseline_pos[1])),
        "fontsize": float(fontsize),
        "color": tuple(float(c) for c in color[:3]),
        "rotation": int(rotation) % 360,
        "updated_after": int(updated_after),
    }
    if redact_rect is not None:
        annot["redact_rect"] = tuple(float(v) for v in redact_rect)
    if invisible:
        annot["invisible"] = True
    return annot


def build_pdf_token_replacement_annotation(
    pdf_token: Dict[str, Any],
    new_text: Optional[str] = None,
    new_fontsize: Optional[float] = None,
    new_color: Optional[Sequence[float]] = None,
    new_pos: Optional[Tuple[float, float]] = None,
    updated_after: int = 0,
    invisible: bool = False,
) -> Dict[str, Any]:
    """既存のPDF内文字トークンを編集・色変更する際のテキスト注釈辞書を生成する"""
    return build_text_annotation(
        text=pdf_token["text"] if new_text is None else new_text,
        pos=pdf_token["pos"] if new_pos is None else new_pos,
        baseline_pos=pdf_token["baseline_pos"],
        fontsize=float(pdf_token["fontsize"] if new_fontsize is None else new_fontsize),
        color=pdf_token["color"] if new_color is None else new_color,
        rotation=int(pdf_token.get("rotation", 0)),
        updated_after=updated_after,
        redact_rect=pdf_token["raw_rect"],
        invisible=invisible,
    )


def plan_text_annotation_removal(
    ann_list: Sequence[Dict[str, Any]], ann_idx: int
) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    テキスト注釈削除時の操作プランを返す。
    - 元PDF文字の置換注釈(redact_rectあり)の場合: ("replace_with_redact", old_ann, redact_ann)
    - 通常の追加テキスト注釈の場合: ("pop", old_ann, None)
    - 無効なインデックスの場合: ("noop", None, None)
    """
    if not (0 <= ann_idx < len(ann_list)):
        return "noop", None, None
    target = ann_list[ann_idx]
    if target.get("type") != "text":
        return "noop", None, None
    if target.get("redact_rect"):
        redact_ann = {"type": "text_redact", "rect": tuple(target["redact_rect"])}
        if target.get("invisible"):
            redact_ann["keep_image"] = True
        return "replace_with_redact", dict(target), redact_ann
    return "pop", dict(target), None


def __getattr__(name: str):
    """後方互換性のため、旧core所属のQtカラー/アイコンヘルパーが要求された場合はui層へ委譲する"""
    if name in (
        "rgb_tuple_to_qcolor",
        "qcolor_to_rgb_tuple",
        "create_color_swatch_icon",
        "create_pen_eraser_icon",
        "sync_color_combo",
    ):
        import ui
        return getattr(ui, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
