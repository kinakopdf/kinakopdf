# -*- coding: utf-8 -*-
"""レシート分割（多機能版 feature/trial-multi）。

複合機の原稿台いっぱいにレシートなどを並べて取り込んだページから、
1枚ずつ検出して個別の PDF に切り分ける。

新しいライブラリは使わず、PyQt6（QImage）と PyMuPDF だけで実装する。
検出は2系統を試し、妥当な検出数が多い方を採用する。

1. 暗い背景（フタを開けて取り込むなど、紙が背景より明るい場合）:
   明るい紙そのものの連結成分から四隅を推定する（写真をPDF化の
   書類検出と同じ考え方で、最大成分ではなく全成分を対象にする）。
2. 明るい背景（フタを閉めて取り込むなど、紙と背景が同化する場合）:
   文字などの暗い部分（インク）のマスクを作り、近くの文字同士が
   つながるよう太らせてから（モルフォロジーの閉処理＋膨張）、
   ひとかたまりになった塊から書類の範囲を推定する。レシートには
   文字の周りに余白があるため、四隅は外側へ広めに取る。

どちらの系統でも、レシート同士が重なっていたりくっついていたり
すると1枚に数えてしまう。呼び出し側はプレビューで件数と位置を
必ず確認できるようにすること。
"""
import math
from collections import deque

import fitz
from PyQt6.QtGui import QImage

from core.geometry import FITZ_LOCK, normalize_page_rotation
from core.photo_to_pdf import (
    _expand_quad,
    _gray_samples,
    _otsu_threshold,
    _polygon_area,
    page_render_dpi,
    qimage_to_jpeg_bytes,
    warp_perspective,
)

# 検出用に縮小する際の長辺ピクセル数。A3 いっぱいのレシートでも
# 1枚が十分な大きさで残る水準。
DETECT_MAX_DIM = 1100
# 書類とみなす最小面積（ページ面積に対する割合）と最大面積。
MIN_AREA_FRAC = 0.004
MAX_AREA_FRAC = 0.60
# インクのマスクを太らせて文字同士をつなげるときの半径（検出画像の
# 長辺に対する割合）。レシート内の文字の間隔（店名と明細の間や、
# 品名と金額の間の空白）より大きく、レシート同士の間隔より小さい
# 値にする。閉処理のあと、塊を少しだけ太らせる。
INK_CLOSE_RADIUS_FRAC = 0.016
INK_GROW_RADIUS_FRAC = 0.005
# インク系統で四隅を外側へ広げる割合（平均辺長に対する比率）。
# レシートの文字まわりの余白ぶんを見込む。
INK_EXPAND_RATIO = 0.055
# 紙系統で四隅を外側へ広げる割合（端の印刷が欠けないように）。
PAPER_EXPAND_RATIO = 0.015


# ============================================================
# 連結成分（全成分・統計だけを返す軽量版）
# ============================================================

class _Component:
    """連結成分の統計。ピクセル列は保持せず、極点と外接矩形だけを持つ。"""

    __slots__ = ("count", "min_x", "min_y", "max_x", "max_y",
                 "tl", "tr", "br", "bl")

    def __init__(self, x, y):
        self.count = 0
        self.min_x = self.max_x = x
        self.min_y = self.max_y = y
        self.tl = self.tr = self.br = self.bl = (x, y)

    def add(self, x, y):
        self.count += 1
        if x < self.min_x:
            self.min_x = x
        if x > self.max_x:
            self.max_x = x
        if y < self.min_y:
            self.min_y = y
        if y > self.max_y:
            self.max_y = y
        if x + y < self.tl[0] + self.tl[1]:
            self.tl = (x, y)
        if x + y > self.br[0] + self.br[1]:
            self.br = (x, y)
        if x - y > self.tr[0] - self.tr[1]:
            self.tr = (x, y)
        if x - y < self.bl[0] - self.bl[1]:
            self.bl = (x, y)

    @property
    def bbox(self):
        return (self.min_x, self.min_y, self.max_x, self.max_y)

    def quad(self):
        return [self.tl, self.tr, self.br, self.bl]


def _mask_from(data, w, h, stride, thr, bright):
    """二値マスク（詰めた bytearray、前景=1）を返す。"""
    mask = bytearray(w * h)
    if bright:
        for y in range(h):
            base = y * stride
            off = y * w
            for x in range(w):
                if data[base + x] > thr:
                    mask[off + x] = 1
    else:
        for y in range(h):
            base = y * stride
            off = y * w
            for x in range(w):
                if data[base + x] <= thr:
                    mask[off + x] = 1
    return mask


def _label_mask(mask, w, h, min_count):
    """マスクの連結成分をすべて返す（min_count 未満は捨てる）。"""
    visited = bytearray(w * h)
    comps = []
    for sy in range(h):
        for sx in range(w):
            idx = sy * w + sx
            if visited[idx] or not mask[idx]:
                visited[idx] = 1
                continue
            visited[idx] = 1
            comp = _Component(sx, sy)
            queue = deque([(sx, sy)])
            while queue:
                x, y = queue.popleft()
                comp.add(x, y)
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        nidx = ny * w + nx
                        if not visited[nidx]:
                            visited[nidx] = 1
                            if mask[nidx]:
                                queue.append((nx, ny))
            if comp.count >= min_count:
                comps.append(comp)
    return comps


def _slide_window(mask, w, h, radius, want_any):
    """二値マスクの横→縦のスライディングウィンドウ処理。

    want_any=True で膨張（半径内に前景が1つでもあれば前景）、
    False で収縮（半径内がすべて前景なら前景）。二値マスクなので
    累積和で判定でき、純粋 Python でも実用的な速さになる。
    """
    if radius <= 0:
        return mask
    # 横方向
    rows = bytearray(w * h)
    for y in range(h):
        off = y * w
        prefix = [0] * (w + 1)
        s = 0
        for x in range(w):
            s += mask[off + x]
            prefix[x + 1] = s
        for x in range(w):
            lo = max(0, x - radius)
            hi = min(w, x + radius + 1)
            cnt = prefix[hi] - prefix[lo]
            if want_any:
                rows[off + x] = 1 if cnt > 0 else 0
            else:
                rows[off + x] = 1 if cnt == hi - lo else 0
    # 縦方向
    out = bytearray(w * h)
    for x in range(w):
        prefix = [0] * (h + 1)
        s = 0
        for y in range(h):
            s += rows[y * w + x]
            prefix[y + 1] = s
        for y in range(h):
            lo = max(0, y - radius)
            hi = min(h, y + radius + 1)
            cnt = prefix[hi] - prefix[lo]
            if want_any:
                out[y * w + x] = 1 if cnt > 0 else 0
            else:
                out[y * w + x] = 1 if cnt == hi - lo else 0
    return out


def _dilate(mask, w, h, radius):
    return _slide_window(mask, w, h, radius, True)


def _erode(mask, w, h, radius):
    return _slide_window(mask, w, h, radius, False)


# ============================================================
# 系統1: 紙そのものの成分から検出（暗い背景向け）
# ============================================================

def _quads_from_paper(data, w, h, stride, thr):
    page_area = float(w * h)
    mask = _mask_from(data, w, h, stride, thr, bright=True)
    comps = _label_mask(mask, w, h, int(page_area * MIN_AREA_FRAC))
    quads = []
    for comp in comps:
        frac = comp.count / page_area
        if frac > MAX_AREA_FRAC:
            continue
        # 四辺すべてに接する成分は背景（原稿台の明るい部分など）
        if (comp.min_x == 0 and comp.min_y == 0
                and comp.max_x == w - 1 and comp.max_y == h - 1):
            continue
        quad = comp.quad()
        area = _polygon_area(quad)
        if area <= 0:
            continue
        fill = comp.count / area
        if not (0.45 <= fill <= 1.05):
            continue
        min_edge = min(
            math.hypot(quad[i][0] - quad[(i + 1) % 4][0],
                       quad[i][1] - quad[(i + 1) % 4][1])
            for i in range(4))
        if min_edge < 0.05 * max(w, h):
            continue
        quads.append(quad)
    return quads


# ============================================================
# 系統2: インクの塊から検出（明るい背景向け）
# ============================================================

def _quads_from_ink(data, w, h, stride, thr):
    page_area = float(w * h)
    mask = _mask_from(data, w, h, stride, thr, bright=False)
    if not any(mask):
        return []
    # 近くの文字同士がつながるよう太らせ（閉処理）、少し膨らませる
    r_close = max(3, int(round(INK_CLOSE_RADIUS_FRAC * max(w, h))))
    r_grow = max(2, int(round(INK_GROW_RADIUS_FRAC * max(w, h))))
    blob = _erode(_dilate(mask, w, h, r_close), w, h, r_close)
    blob = _dilate(blob, w, h, r_grow)
    comps = _label_mask(blob, w, h, int(page_area * MIN_AREA_FRAC))
    quads = []
    for comp in comps:
        bw = comp.max_x - comp.min_x + 1
        bh = comp.max_y - comp.min_y + 1
        if bw * bh > page_area * MAX_AREA_FRAC:
            continue
        if bw < 0.05 * w or bh < 0.05 * h:
            continue
        quads.append(comp.quad())
    return quads


# ============================================================
# 検出の入口
# ============================================================

def _reading_order(quads):
    """四隅のリストを行（上から）→列（左から）の読み順に並べ替える。"""
    if not quads:
        return []
    heights = sorted(
        max(p[1] for p in q) - min(p[1] for p in q) for q in quads)
    row_h = max(1.0, heights[len(heights) // 2] * 0.6)

    def key(q):
        cx = sum(p[0] for p in q) / 4.0
        cy = sum(p[1] for p in q) / 4.0
        return (int(cy // row_h), cx)

    return sorted(quads, key=key)


def detect_receipt_quads(image: QImage, max_dim: int = DETECT_MAX_DIM):
    """ページ画像から書類（レシート）の四隅をすべて検出する。

    戻り値: 四隅のリスト。各四隅は [(x, y) × 4]（左上・右上・右下・
    左下の順、元の画像座標）で、読み順に並んでいる。
    検出できない場合は空リスト。
    """
    if image is None or image.isNull():
        return []
    if image.width() < 16 or image.height() < 16:
        return []
    data, gw, gh, stride, scale = _gray_samples(image, max_dim)
    if gw < 16 or gh < 16:
        return []
    thr = _otsu_threshold(data, gw, gh, stride)
    paper = _quads_from_paper(data, gw, gh, stride, thr)
    if len(paper) >= 2:
        # 紙の形が取れているなら、そちらの方が正確なので採用する
        chosen, ratio = paper, PAPER_EXPAND_RATIO
    else:
        ink = _quads_from_ink(data, gw, gh, stride, thr)
        if len(ink) > len(paper):
            chosen, ratio = ink, INK_EXPAND_RATIO
        else:
            chosen, ratio = paper, PAPER_EXPAND_RATIO
    inv = 1.0 / scale if scale else 1.0
    out = []
    for quad in chosen:
        scaled = [(x * inv, y * inv) for (x, y) in quad]
        out.append(_expand_quad(
            scaled, image.width(), image.height(), ratio=ratio))
    return _reading_order(out)


# ============================================================
# ページ画像化と切り分け
# ============================================================

def render_page_image(page):
    """ページを画像化する。戻り値: (QImage, dpi)。

    台形補正と同じ基準（200 DPI・長辺 3600px まで）で描画する。
    呼び出し前に normalize_page_rotation 済みであること。
    """
    dpi = page_render_dpi(page.rect.width, page.rect.height)
    zoom = dpi / 72.0
    with FITZ_LOCK:
        pix = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            colorspace=fitz.csRGB, alpha=False)
        samples = pix.samples
        w, h, stride = pix.width, pix.height, pix.stride
        image = QImage(
            samples, w, h, stride, QImage.Format.Format_RGB888).copy()
    if image.format() != QImage.Format.Format_RGB32:
        image = image.convertToFormat(QImage.Format.Format_RGB32)
    return image, dpi


def build_receipt_pdf_bytes(image: QImage, quad, dpi: float,
                            jpeg_quality: int = 92) -> bytes:
    """四隅で切り抜いて傾きを直した、レシート1枚分の PDF バイト列。

    出力ページの大きさは、切り抜き後画像のピクセル寸法を dpi で
    pt に換算したもの（おおよその実寸を保つ）。画像だけのページに
    なり、文字データは含まれない。
    """
    warped = warp_perspective(image, quad)
    data = qimage_to_jpeg_bytes(warped, quality=jpeg_quality)
    width_pt = warped.width() * 72.0 / float(dpi)
    height_pt = warped.height() * 72.0 / float(dpi)
    doc = fitz.open()
    try:
        page = doc.new_page(width=width_pt, height=height_pt)
        page.insert_image(page.rect, stream=data)
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()


def analyze_document(doc):
    """文書の全ページを解析する。

    戻り値: ページごとの dict のリスト。
    {"page_index", "image", "dpi", "quads"}
    回転（/Rotate）のあるページは作業用コピー上で正規化してから
    解析する（doc 自体は変更しない）。
    """
    results = []
    work = fitz.open()
    try:
        with FITZ_LOCK:
            work.insert_pdf(doc)
        for idx in range(len(work)):
            page = work.load_page(idx)
            with FITZ_LOCK:
                normalize_page_rotation(page)
            image, dpi = render_page_image(page)
            quads = detect_receipt_quads(image)
            results.append({
                "page_index": idx,
                "image": image,
                "dpi": dpi,
                "quads": quads,
            })
        return results
    finally:
        work.close()
