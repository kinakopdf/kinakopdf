# -*- coding: utf-8 -*-
"""傾き補正（deskew）: 傾いたページの角度検出と、まっすぐに戻す補正。

スキャンや FAX で傾いてしまったページを対象にする。

- 角度検出は2系統:
  1. 文字データ入りページ: テキスト行の方向ベクトル（get_text の dir）
     から傾きを推定する。高速で高精度。
  2. 画像だけのページ: 低解像度に描画して二値化し、投影プロファイル法
     （角度を少しずつ変えて、行方向へ射影した濃淡ヒストグラムの
     二乗和が最大になる角度を探索）で推定する。純 Python で実装し、
     縮小画像＋インク画素のみを対象にして速度を確保する。
- 補正は show_pdf_page の任意角度 rotate（keep_proportion=True で
  角度が正確に出ることを実測済み）を使い、ページ内容を中心まわりに
  逆回転させて同じ大きさの新しいページに貼る。ベクタ・文字データは
  保たれる。端の内容が切れないよう、回転後の内容全体がページ内に
  収まる倍率（フィット倍率＝カバー倍率の逆数）まで縮小して収める。
  以前は白い三角を消すために拡大（カバー）していたが、小さい伝票
  など端まで印刷がある原稿で端が消えるため、縮小方式に変更した
  （2026-11-24、MIWA の実機報告）。周囲に白い余白が出るが、
  内容は一切欠けない。
- 角度の符号は「画面上で時計回りを正」とする PyMuPDF 座標系
  （get_text の行 dir の atan2）で統一する。検出値がそのまま
  show_pdf_page の rotate 引数になる（rotate=r で行の角度が -r
  変わるため、検出角 a に対して rotate=a で相殺される）。

前提: 対象ページは回転（/Rotate）が remove_rotation() で正規化
済みであること（呼び出し側の作業用コピーが担当する）。
"""
import math

import fitz

from core.geometry import FITZ_LOCK

# 画像検出で描画する横幅（px）。小さいほど速く、大きいほど精密。
_IMAGE_TARGET_WIDTH = 640
# 画像検出の探索範囲（度）と刻み。
_IMAGE_SEARCH_RANGE = 5.0
_IMAGE_COARSE_STEP = 0.25
_IMAGE_FINE_STEP = 0.05
_IMAGE_FINE_SPAN = 0.3
# これ未満のインク画素数では検出不能とみなす。
_IMAGE_MIN_INK = 300
# 投影計算に使うインク画素数の上限（間引き）。
_IMAGE_MAX_POINTS = 60000
# テキスト検出で「行」とみなす角度の上限（縦書き等を除外）。
_TEXT_MAX_LINE_ANGLE = 20.0


def detect_skew_from_text(page):
    """テキスト行の方向から傾き（度）を推定する。推定できなければ None。

    行ごとに dir の角度を取り、文字数で重み付けした中央値を返す。
    水平から大きく外れた行（縦書き・図中の文字など）は除外する。
    行が2行未満のページでは推定しない（画像検出に委ねる）。
    """
    try:
        data = page.get_text("dict")
    except Exception:
        return None
    samples = []  # (angle_deg, weight)
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            ang = math.degrees(math.atan2(dy, dx))
            if abs(ang) > _TEXT_MAX_LINE_ANGLE:
                continue
            text = "".join(
                span.get("text", "") for span in line.get("spans", []))
            weight = max(1.0, float(len(text.strip())))
            samples.append((ang, weight))
    if len(samples) < 2:
        return None
    samples.sort(key=lambda s: s[0])
    total = sum(w for _, w in samples)
    acc = 0.0
    for ang, w in samples:
        acc += w
        if acc >= total / 2.0:
            return round(ang, 2)
    return round(samples[-1][0], 2)


def _otsu_threshold(samples, step=7):
    """グレースケール画素列から大津法で二値化の閾値を求める。"""
    hist = [0] * 256
    for i in range(0, len(samples), step):
        hist[samples[i]] += 1
    total = sum(hist)
    if total == 0:
        return 128
    sum_all = sum(i * hist[i] for i in range(256))
    sum_b = 0
    w_b = 0
    max_var = -1.0
    thr = 128
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
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > max_var:
            max_var = var
            thr = i
    return thr


def _ink_points(page):
    """ページを低解像度グレースケールで描画し、インク画素の (x, y) を返す。"""
    rect = page.rect
    if rect.width <= 0 or rect.height <= 0:
        return []
    zoom = _IMAGE_TARGET_WIDTH / rect.width
    with FITZ_LOCK:
        pix = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            colorspace=fitz.csGRAY, alpha=False)
    w, h, samples = pix.width, pix.height, pix.samples
    thr = _otsu_threshold(samples)
    pts = []
    for y in range(h):
        base = y * w
        row = samples[base:base + w]
        for x in range(w):
            if row[x] < thr:
                pts.append((x, y))
    if len(pts) > _IMAGE_MAX_POINTS:
        stride = len(pts) // _IMAGE_MAX_POINTS + 1
        pts = pts[::stride]
    return pts


def _projection_score(pts, ang_deg):
    """角度 ang_deg の行方向へ射影したヒストグラムの二乗和。

    文字の行がその角度に揃っているほど、射影が特定の行へ集中して
    スコアが大きくなる。
    """
    a = math.radians(ang_deg)
    sa, ca = math.sin(a), math.cos(a)
    bins = {}
    for x, y in pts:
        t = int(round(y * ca - x * sa))
        bins[t] = bins.get(t, 0) + 1
    return sum(c * c for c in bins.values())


def detect_skew_from_image(page):
    """投影プロファイル法で傾き（度）を推定する。推定できなければ None。

    ±5° を粗く（0.25°刻み）探索し、最良角度の周辺を細かく
    （0.05°刻み）詰める二段探索。
    """
    pts = _ink_points(page)
    if len(pts) < _IMAGE_MIN_INK:
        return None
    best_ang, best_score = 0.0, -1
    ang = -_IMAGE_SEARCH_RANGE
    while ang <= _IMAGE_SEARCH_RANGE + 1e-9:
        score = _projection_score(pts, ang)
        if score > best_score:
            best_score, best_ang = score, ang
        ang += _IMAGE_COARSE_STEP
    ang = best_ang - _IMAGE_FINE_SPAN
    end = best_ang + _IMAGE_FINE_SPAN
    while ang <= end + 1e-9:
        score = _projection_score(pts, ang)
        if score > best_score:
            best_score, best_ang = score, ang
        ang += _IMAGE_FINE_STEP
    return round(best_ang, 2)


def detect_page_skew(page):
    """ページの傾きを検出する。(角度, 方式) を返し、検出不能なら (None, None)。

    方式は "text"（文字データから）または "image"（画像解析）。
    文字データで推定できるページはそちらを優先する。
    """
    angle = detect_skew_from_text(page)
    if angle is not None:
        return angle, "text"
    angle = detect_skew_from_image(page)
    if angle is not None:
        return angle, "image"
    return None, None


def cover_scale(width, height, angle_deg):
    """回転した内容がページ全体を覆うために必要な拡大倍率。

    中心まわりに angle_deg 回転させた (width, height) の矩形が、
    元と同じ大きさの矩形を完全に覆うための最小倍率。
    補正ではこの逆数をフィット倍率として使い、内容全体が
    ページ内に収まるよう縮小する（端を欠けさせないため）。
    """
    th = math.radians(abs(angle_deg))
    if th == 0.0:
        return 1.0
    return math.cos(th) + max(width / height, height / width) * math.sin(th)


def build_deskewed_pages_doc(doc, page_indices, angles):
    """指定ページを傾き補正した、新しい文書を返す（呼び出し側が所有）。

    page_indices と angles は同じ長さの並行リストで、返り値の文書は
    その順序で補正済みページを持つ。angles[i] は page_indices[i] の
    検出傾き（度）で、内容は逆向きに回転してまっすぐになる。
    ページの大きさは元のまま。ベクタ・文字データは保たれる
    （show_pdf_page で元ページをそのまま貼るため）。

    - 回転付きページは remove_rotation() で表示どおりに正規化して
      から貼る（結合処理と同じ扱い）。
    - 角度が 0 のページはそのままコピーする。
    - 元文書は変更しない。
    """
    if len(page_indices) != len(angles):
        raise ValueError("page_indices と angles の長さが一致しません")
    with FITZ_LOCK:
        for idx in page_indices:
            if not (0 <= idx < len(doc)):
                raise ValueError(f"ページ番号が範囲外です: {idx}")

        # 回転を正規化した作業用コピー（元文書は触らない）
        work = fitz.open()
        try:
            for idx in page_indices:
                work.insert_pdf(doc, from_page=idx, to_page=idx)
            for page in work:
                if page.rotation:
                    page.remove_rotation()

            out = fitz.open()
            try:
                for pos, angle in enumerate(angles):
                    src_page = work[pos]
                    rect = src_page.rect
                    w, h = rect.width, rect.height
                    angle = float(angle)
                    if abs(angle) < 1e-9:
                        out.insert_pdf(work, from_page=pos, to_page=pos)
                        continue
                    page = out.new_page(width=w, height=h)
                    th = math.radians(abs(angle))
                    cos_t, sin_t = math.cos(th), math.sin(th)
                    # 回転後の外接矩形をフィット倍率（カバー倍率の
                    # 逆数）で縮めた貼り付け先。keep_proportion=True
                    # のため、この矩形いっぱいに外接矩形がフィットし、
                    # 内容全体がページ内に収まって端が欠けない。
                    fit = 1.0 / cover_scale(w, h, angle)
                    bw = (w * cos_t + h * sin_t) * fit
                    bh = (w * sin_t + h * cos_t) * fit
                    cx, cy = w / 2.0, h / 2.0
                    target = fitz.Rect(
                        cx - bw / 2.0, cy - bh / 2.0,
                        cx + bw / 2.0, cy + bh / 2.0)
                    page.show_pdf_page(
                        target, work, pos,
                        rotate=angle, keep_proportion=True)
                return out
            except Exception:
                out.close()
                raise
        finally:
            work.close()
