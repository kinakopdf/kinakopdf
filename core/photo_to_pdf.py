# -*- coding: utf-8 -*-
"""写真をPDF化する試作機能のコア処理（多機能版 feature/trial-multi）。

新しいライブラリは使わず、PyQt6（QImage / QTransform）と PyMuPDF だけで、
写真の読み込み・書類の四隅の自動検出・透視変換による傾き補正・PDF化を行う。

自動検出は「縮小 → グレースケール → 大津の二値化 → 最大の連結成分 →
その成分の極点から四隅を推定」という純粋 Python の処理。
明るい書類が暗めの背景にある写真を想定している（逆の場合は反転も試す）。
検出できない写真も多いので、呼び出し側は必ず手動で四隅を直せる
フォールバックを用意すること。
"""
import math
from collections import deque

import fitz
from PyQt6.QtCore import QBuffer, QIODevice, QPointF, Qt
from PyQt6.QtGui import (
    QColor,
    QImage,
    QImageReader,
    QPainter,
    QPolygonF,
    QTransform,
)

A4_PORTRAIT_PT = (595.28, 841.89)
DEFAULT_PAGE_MARGIN_PT = 36.0
DETECT_MAX_DIM = 480


# ============================================================
# 画像の読み込み・変換
# ============================================================

def load_photo_image(path) -> QImage:
    """写真ファイルを QImage で読み込む。EXIF の向きも反映する。

    読めない場合は ValueError。
    """
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise ValueError(f"画像を読み込めませんでした: {path}")
    return image


def qimage_to_jpeg_bytes(image: QImage, quality: int = 92) -> bytes:
    """QImage を JPEG バイト列にする（PDF 埋め込み用）。"""
    img = image
    if img.format() != QImage.Format.Format_RGB32:
        img = img.convertToFormat(QImage.Format.Format_RGB32)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not img.save(buf, "JPEG", int(quality)):
            raise ValueError("画像の変換に失敗しました。")
        return bytes(buf.data())
    finally:
        buf.close()


def rotate_image_90(image: QImage, clockwise: bool) -> QImage:
    """画像を90度回転する（clockwise=True で時計回り）。"""
    transform = QTransform().rotate(90 if clockwise else -90)
    return image.transformed(
        transform, Qt.TransformationMode.SmoothTransformation)


def rotate_quad_90(quad, image_w: int, image_h: int, clockwise: bool) -> list:
    """rotate_image_90 に合わせて四隅（左上・右上・右下・左下の順）も回転する。

    image_w / image_h は回転前の画像サイズ。
    """
    if clockwise:
        pts = [(image_h - y, x) for (x, y) in quad]
        return [pts[3], pts[0], pts[1], pts[2]]
    pts = [(y, image_w - x) for (x, y) in quad]
    return [pts[1], pts[2], pts[3], pts[0]]


# ============================================================
# 透視変換（切り抜き＋傾き補正）
# ============================================================

def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def warp_perspective(image: QImage, quad) -> QImage:
    """四隅で指定した領域を透視変換で長方形に切り抜く。

    quad は [(x, y), ...] の4点で、左上・右上・右下・左下の順（画像座標）。
    出力サイズは、上下辺の平均ではなく長い方の辺に合わせる。
    四隅が不正（自己交差など）な場合は ValueError。
    """
    if image is None or image.isNull():
        raise ValueError("画像がありません。")
    if not quad or len(quad) != 4:
        raise ValueError("四隅が指定されていません。")
    tl, tr, br, bl = [(float(x), float(y)) for (x, y) in quad]
    out_w = max(1, int(round(max(_dist(tl, tr), _dist(bl, br)))))
    out_h = max(1, int(round(max(_dist(tl, bl), _dist(tr, br)))))
    src = QPolygonF([
        QPointF(*tl), QPointF(*tr), QPointF(*br), QPointF(*bl)])
    dst = QPolygonF([
        QPointF(0, 0), QPointF(out_w, 0),
        QPointF(out_w, out_h), QPointF(0, out_h)])
    transform = QTransform()
    if not QTransform.quadToQuad(src, dst, transform):
        raise ValueError("四隅の形が不正なため、補正できませんでした。")
    out = QImage(out_w, out_h, QImage.Format.Format_RGB32)
    out.fill(QColor(255, 255, 255))
    painter = QPainter(out)
    try:
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setTransform(transform)
        painter.drawImage(0, 0, image)
    finally:
        painter.end()
    return out


# ============================================================
# 書類の四隅の自動検出
# ============================================================

def _gray_samples(image: QImage, max_dim: int):
    """検出用に縮小したグレースケール画像の生バイト列を返す。

    戻り値: (data, width, height, stride, scale)
    """
    w, h = image.width(), image.height()
    scale = min(1.0, max_dim / max(w, h)) if max(w, h) > 0 else 1.0
    if scale < 1.0:
        small = image.scaled(
            max(1, int(round(w * scale))), max(1, int(round(h * scale))),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
    else:
        small = image
    gray = small.convertToFormat(QImage.Format.Format_Grayscale8)
    gw, gh = gray.width(), gray.height()
    stride = gray.bytesPerLine()
    data = bytes(gray.constBits().asstring(gray.sizeInBytes()))
    return data, gw, gh, stride, scale


def _otsu_threshold(data, w, h, stride) -> int:
    """大津の方法で二値化のしきい値を求める。"""
    hist = [0] * 256
    for y in range(h):
        base = y * stride
        for x in range(w):
            hist[data[base + x]] += 1
    total = w * h
    sum_all = sum(i * hist[i] for i in range(256))
    sum_b = 0
    w_b = 0
    best = -1.0
    thr = 0
    for i in range(256):
        w_b += hist[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * hist[i]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best = between
            thr = i
    return thr


def _largest_component(data, w, h, stride, thr, bright: bool) -> list:
    """二値化した画像で最大の連結成分（ピクセル座標のリスト）を返す。

    bright=True なら「しきい値より明るい」側、False なら暗い側を前景とする。
    """
    def is_fg(x, y):
        v = data[y * stride + x]
        return v > thr if bright else v <= thr

    visited = bytearray(w * h)
    best = []
    for sy in range(h):
        for sx in range(w):
            idx = sy * w + sx
            if visited[idx]:
                continue
            visited[idx] = 1
            if not is_fg(sx, sy):
                continue
            comp = []
            queue = deque([(sx, sy)])
            while queue:
                x, y = queue.popleft()
                comp.append((x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        nidx = ny * w + nx
                        if not visited[nidx]:
                            visited[nidx] = 1
                            if is_fg(nx, ny):
                                queue.append((nx, ny))
            if len(comp) > len(best):
                best = comp
    return best


def _polygon_area(points) -> float:
    area = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _quad_from_component(comp, img_w, img_h):
    """連結成分の極点から四隅（左上・右上・右下・左下）を推定する。

    戻り値: (quad, fill_ratio) または None。
    fill_ratio は「成分の面積 / 四角形の面積」で、四角形らしい形ほど 1 に近い。
    成分の形が四角形からかけ離れている場合は None。
    """
    tl = min(comp, key=lambda p: p[0] + p[1])
    br = max(comp, key=lambda p: p[0] + p[1])
    tr = max(comp, key=lambda p: p[0] - p[1])
    bl = min(comp, key=lambda p: p[0] - p[1])
    quad = [tl, tr, br, bl]
    area = _polygon_area(quad)
    if area <= 0:
        return None
    # 成分が四角形の内側をある程度埋めているか（四角形らしい形か）
    fill_ratio = len(comp) / area
    if not (0.5 <= fill_ratio <= 1.05):
        return None
    min_edge = min(_dist(quad[i], quad[(i + 1) % 4]) for i in range(4))
    if min_edge < 0.08 * max(img_w, img_h):
        return None
    return quad, fill_ratio


def _touches_all_borders(comp, w, h) -> bool:
    """成分が画像の四辺すべてに接しているか（＝背景の可能性が高い）。"""
    xs = [p[0] for p in comp]
    ys = [p[1] for p in comp]
    return min(xs) == 0 and min(ys) == 0 and max(xs) == w - 1 and max(ys) == h - 1


def detect_document_quad(image: QImage, max_dim: int = DETECT_MAX_DIM):
    """写真の中の書類の四隅を自動検出する。

    戻り値: [(x, y), ...] の4点（左上・右上・右下・左下の順、元の画像座標）。
    検出できない場合は None。
    """
    if image is None or image.isNull():
        return None
    if image.width() < 8 or image.height() < 8:
        return None
    data, gw, gh, stride, scale = _gray_samples(image, max_dim)
    if gw < 8 or gh < 8:
        return None
    thr = _otsu_threshold(data, gw, gh, stride)
    best = None
    for bright in (True, False):
        comp = _largest_component(data, gw, gh, stride, thr, bright)
        frac = len(comp) / (gw * gh) if gw * gh else 0.0
        if not (0.05 <= frac <= 0.95):
            continue
        # 四辺すべてに接する成分は書類ではなく背景とみなす
        if _touches_all_borders(comp, gw, gh):
            continue
        found = _quad_from_component(comp, gw, gh)
        if found is None:
            continue
        quad, fill_ratio = found
        # 四角形の充填率が高い候補を優先する（書類はほぼ長方形なので
        # 充填率が1に近く、背景は書類の分だけ欠けて低くなる）
        score = (fill_ratio, len(comp))
        if best is None or score > best[0]:
            best = (score, quad)
    if best is None:
        return None
    inv = 1.0 / scale if scale else 1.0
    quad = [(x * inv, y * inv) for (x, y) in best[1]]
    return _expand_quad(quad, image.width(), image.height())


def _expand_quad(quad, img_w, img_h, ratio=0.015):
    """検出した四隅を、中心から外側へ少し広げて返す。

    二値化の境界は原稿の実物の端より内側に寄りやすく、そのまま
    切り抜くと端の印刷が欠ける。平均辺長の ratio 倍だけ各頂点を
    重心から遠ざけ、画像の範囲で丸める（2026-11-24、MIWA の
    実機報告：小さい伝票で台形補正すると端が消える）。
    """
    cx = sum(p[0] for p in quad) / 4.0
    cy = sum(p[1] for p in quad) / 4.0
    mean_edge = sum(
        _dist(quad[i], quad[(i + 1) % 4]) for i in range(4)) / 4.0
    push = mean_edge * ratio
    out = []
    for (x, y) in quad:
        dx, dy = x - cx, y - cy
        norm = math.hypot(dx, dy)
        if norm <= 0:
            out.append((x, y))
            continue
        nx = x + dx / norm * push
        ny = y + dy / norm * push
        out.append((min(max(nx, 0.0), float(img_w - 1)),
                    min(max(ny, 0.0), float(img_h - 1))))
    return out


# ============================================================
# PDF化
# ============================================================

def create_pdf_bytes_from_images(image_bytes_list,
                                 margin_pt: float = DEFAULT_PAGE_MARGIN_PT) -> bytes:
    """画像バイト列のリストを、1枚＝1ページの A4 PDF にする。

    画像の向きに合わせて A4 の縦/横を選び、余白を付けて中央に収める。
    """
    items = [bytes(b) for b in (image_bytes_list or []) if b]
    if not items:
        raise ValueError("PDFにする画像がありません。")
    doc = fitz.open()
    try:
        for data in items:
            pix = fitz.Pixmap(data)
            iw, ih = pix.width, pix.height
            if iw <= 0 or ih <= 0:
                raise ValueError("画像のサイズが取得できませんでした。")
            if ih >= iw:
                page_w, page_h = A4_PORTRAIT_PT
            else:
                page_w, page_h = A4_PORTRAIT_PT[1], A4_PORTRAIT_PT[0]
            page = doc.new_page(width=page_w, height=page_h)
            avail_w = max(10.0, page_w - margin_pt * 2)
            avail_h = max(10.0, page_h - margin_pt * 2)
            scale = min(avail_w / iw, avail_h / ih)
            draw_w, draw_h = iw * scale, ih * scale
            rect = fitz.Rect(
                (page_w - draw_w) / 2, (page_h - draw_h) / 2,
                (page_w + draw_w) / 2, (page_h + draw_h) / 2)
            page.insert_image(rect, stream=data)
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()


def photos_to_pdf_bytes(images, jpeg_quality: int = 92) -> bytes:
    """QImage のリストを1枚＝1ページの A4 PDF バイト列にする。"""
    return create_pdf_bytes_from_images(
        [qimage_to_jpeg_bytes(img, quality=jpeg_quality) for img in images])


# ============================================================
# 既存ページの台形補正（多機能版）
# ============================================================

# ページを画像化する際の基準解像度。小さな文字でも読める水準として
# 200 DPI を使い、巨大なページでは長辺のピクセル数を抑える。
PAGE_RENDER_DPI = 200.0
PAGE_RENDER_MAX_DIM = 3600


def page_render_dpi(width_pt: float, height_pt: float,
                    target_dpi: float = PAGE_RENDER_DPI,
                    max_dim: int = PAGE_RENDER_MAX_DIM) -> float:
    """ページサイズ（pt）に対して使うレンダリング DPI を返す。

    target_dpi を基準に、長辺が max_dim ピクセルを超えないよう下げる。
    """
    longest_pt = max(float(width_pt), float(height_pt))
    if longest_pt <= 0:
        return float(target_dpi)
    dpi = min(float(target_dpi), max_dim * 72.0 / longest_pt)
    return max(36.0, dpi)


def _segments_cross(a, b, c, d) -> bool:
    """線分 ab と線分 cd が交差するか（端点の共有は除く）。"""
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1 = orient(a, b, c)
    o2 = orient(a, b, d)
    o3 = orient(c, d, a)
    o4 = orient(c, d, b)
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0) and o1 and o2 and o3 and o4


def build_corrected_image_page_doc(image: QImage, quad, dpi: float,
                                   jpeg_quality: int = 92):
    """ページを写した画像と四隅から、台形補正した1ページだけの文書を作る。

    quad は画像座標（ピクセル）の4点で、左上・右上・右下・左下の順。
    出力ページの大きさは、補正後画像のピクセル寸法を dpi で pt に
    換算したものになり、元の書類部分のおおよその実寸を保つ。
    補正後のページは画像だけになり、文字データは残らない。
    戻り値の fitz.Document は呼び出し側が所有する。
    """
    if quad and len(quad) == 4:
        # 向かい合う辺同士が交わる四隅（蝶ネクタイ型）は、透視変換が
        # 成功しても意味のない結果になるため、ここで弾く
        pts = [(float(x), float(y)) for (x, y) in quad]
        if (_segments_cross(pts[0], pts[1], pts[3], pts[2])
                or _segments_cross(pts[1], pts[2], pts[0], pts[3])):
            raise ValueError("四隅が交差しています。書類の角に合わせ直してください。")
    warped = warp_perspective(image, quad)
    data = qimage_to_jpeg_bytes(warped, quality=jpeg_quality)
    width_pt = warped.width() * 72.0 / float(dpi)
    height_pt = warped.height() * 72.0 / float(dpi)
    doc = fitz.open()
    try:
        page = doc.new_page(width=width_pt, height=height_pt)
        page.insert_image(page.rect, stream=data)
    except Exception:
        doc.close()
        raise
    return doc
