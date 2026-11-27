# -*- coding: utf-8 -*-
"""スキャン用紙カラー除去・網点ノイズ除去・Windows標準OCRエンジン"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import fitz
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QTransform

from core import logger
from core.geometry import (
    FITZ_LOCK,
    JAPANESE_FONT,
    MAX_RENDER_PIXELS,
    clamp_render_scales,
    get_effective_cropbox,
    view_point_to_raw_pdf_from_geom,
)

OCR_TEXT_MARKER_RGB: Tuple[float, float, float] = (2.0 / 255.0, 2.0 / 255.0, 2.0 / 255.0)
OCR_TEXT_MARKER_SRGB_INT: int = 0x020202
OCR_TEXT_MARKER_SRGB_INTS = frozenset({0x010101, 0x020202, 0x030303})

_ACTIVE_OCR_PROCS_LOCK = threading.Lock()
_ACTIVE_OCR_PROCS: set = set()


def terminate_all_active_ocr_processes() -> int:
    with _ACTIVE_OCR_PROCS_LOCK:
        procs = list(_ACTIVE_OCR_PROCS)
        _ACTIVE_OCR_PROCS.clear()
    terminated = 0
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.kill()
                terminated += 1
        except Exception:
            pass
    return terminated


def cleanup_stale_ocr_temp_dirs(max_age_sec: float = 300.0) -> int:
    tmp_root = tempfile.gettempdir()
    if not tmp_root or not os.path.isdir(tmp_root):
        return 0
    now = time.time()
    removed_count = 0
    try:
        for entry in os.scandir(tmp_root):
            if not entry.is_dir() or not entry.name.startswith("secure_pdf_ocr_"):
                continue
            try:
                if max_age_sec <= 0.0 or (now - entry.stat().st_mtime) >= max_age_sec:
                    shutil.rmtree(entry.path, ignore_errors=True)
                    if not os.path.exists(entry.path):
                        removed_count += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed_count


def cleanup_bytecode_cache(root_dir: Optional[str] = None) -> int:
    base = os.path.abspath(root_dir or os.path.join(os.path.dirname(__file__), ".."))
    if not os.path.isdir(base):
        return 0
    removed = 0
    for dirpath, dirnames, filenames in os.walk(base):
        for d in list(dirnames):
            if d == "__pycache__":
                full_d = os.path.join(dirpath, d)
                shutil.rmtree(full_d, ignore_errors=True)
                if not os.path.exists(full_d):
                    removed += 1
                dirnames.remove(d)
        for fname in filenames:
            if fname.endswith((".pyc", ".pyo")):
                try:
                    os.remove(os.path.join(dirpath, fname))
                    removed += 1
                except OSError:
                    pass
    return removed


def render_page_to_qimage(
    page: fitz.Page, dpi: int = 250, max_pixels: int = MAX_RENDER_PIXELS
) -> QImage:
    base_scale = max(72.0, float(dpi)) / 72.0
    with FITZ_LOCK:
        r = page.rect
        sx, sy = clamp_render_scales(
            r.width, r.height, base_scale, base_scale, max_pixels=max_pixels
        )
        pix = page.get_pixmap(
            matrix=fitz.Matrix(sx, sy), colorspace=fitz.csRGB, alpha=False
        )
        qimg = QImage(
            pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
        ).copy()
    return qimg.convertToFormat(QImage.Format.Format_RGB32)


def _gray_bytes_to_rgb32_qimage(gray_bytes: bytes, w: int, h: int) -> QImage:
    g_img = QImage(gray_bytes, w, h, w, QImage.Format.Format_Grayscale8)
    return g_img.convertToFormat(QImage.Format.Format_RGB32)


def _rgb32_qimage_to_gray_bytes(img_rgb32: QImage) -> bytes:
    w = img_rgb32.width()
    h = img_rgb32.height()
    bits = img_rgb32.constBits()
    bits.setsize(w * h * 4)
    return bytes(bits)[0::4]


def clean_scanned_qimage(
    src_img: QImage,
    color_drop_strength: float = 1.8,
    white_threshold: int = 185,
    black_threshold: int = 135,
    despeckle_level: int = 1,
    binarize: bool = True,
    keep_dark_color_text: bool = True,
) -> QImage:
    if src_img.isNull() or src_img.width() <= 0 or src_img.height() <= 0:
        return src_img

    img32 = src_img.convertToFormat(QImage.Format.Format_RGB32)
    w, h = img32.width(), img32.height()
    bits = img32.constBits()
    bits.setsize(w * h * 4)
    raw = bytes(bits)

    r_img = _gray_bytes_to_rgb32_qimage(raw[2::4], w, h)
    g_img = _gray_bytes_to_rgb32_qimage(raw[1::4], w, h)
    b_img = _gray_bytes_to_rgb32_qimage(raw[0::4], w, h)

    max_img = r_img.copy()
    p_max = QPainter(max_img)
    p_max.setCompositionMode(QPainter.CompositionMode.CompositionMode_Lighten)
    p_max.drawImage(0, 0, g_img)
    p_max.drawImage(0, 0, b_img)
    p_max.end()

    min_img = r_img.copy()
    p_min = QPainter(min_img)
    p_min.setCompositionMode(QPainter.CompositionMode.CompositionMode_Darken)
    p_min.drawImage(0, 0, g_img)
    p_min.drawImage(0, 0, b_img)
    p_min.end()

    chroma_img = max_img.copy()
    p_diff = QPainter(chroma_img)
    p_diff.setCompositionMode(QPainter.CompositionMode.CompositionMode_Difference)
    p_diff.drawImage(0, 0, min_img)
    p_diff.end()

    chroma_bytes = _rgb32_qimage_to_gray_bytes(chroma_img)
    strength = max(0.0, float(color_drop_strength))
    chroma_lut = bytes(
        0 if c <= 16 else min(255, int(round((c - 16) * strength * 1.85)))
        for c in range(256)
    )
    boosted_chroma_img = _gray_bytes_to_rgb32_qimage(
        chroma_bytes.translate(chroma_lut), w, h
    )

    if keep_dark_color_text:
        max_bytes = _rgb32_qimage_to_gray_bytes(max_img)
        min_bytes = _rgb32_qimage_to_gray_bytes(min_img)
        dark_mask_lut = bytes(0 if m < 150 else 255 for m in range(256))
        bright_mask_img = _gray_bytes_to_rgb32_qimage(
            max_bytes.translate(dark_mask_lut), w, h
        )
        p_mask = QPainter(boosted_chroma_img)
        p_mask.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        p_mask.drawImage(0, 0, bright_mask_img)
        p_mask.end()

        half_lut = bytes(v // 2 for v in range(256))
        base_img = _gray_bytes_to_rgb32_qimage(min_bytes.translate(half_lut), w, h)
        max_half_img = _gray_bytes_to_rgb32_qimage(max_bytes.translate(half_lut), w, h)
        p_base = QPainter(base_img)
        p_base.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        p_base.drawImage(0, 0, max_half_img)
        p_base.drawImage(0, 0, boosted_chroma_img)
        p_base.end()
        gray_after_color = _rgb32_qimage_to_gray_bytes(base_img)
    else:
        work_img = max_img.copy()
        p_add = QPainter(work_img)
        p_add.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        p_add.drawImage(0, 0, boosted_chroma_img)
        p_add.end()
        gray_after_color = _rgb32_qimage_to_gray_bytes(work_img)

    w_th = max(80, min(250, int(white_threshold)))
    b_th = max(20, min(w_th - 10, int(black_threshold)))

    if binarize:
        cutoff = int(round((w_th + b_th) * 0.5))
        tone_lut = bytes(255 if v >= cutoff else 0 for v in range(256))
    else:
        tone_lut = bytes(
            255
            if v >= w_th
            else (
                0
                if v <= b_th // 2
                else max(
                    0,
                    min(255, int(round((v - b_th * 0.4) * 255.0 / (w_th - b_th * 0.4)))),
                )
            )
            for v in range(256)
        )

    result_img = _gray_bytes_to_rgb32_qimage(gray_after_color.translate(tone_lut), w, h)
    if despeckle_level >= 1:
        result_img = _despeckle_isolated_dots(result_img, w, h, level=despeckle_level)
    return result_img


def _despeckle_isolated_dots(img_rgb32: QImage, w: int, h: int, level: int = 1) -> QImage:
    cur_img = img_rgb32
    radii = [1] if level == 1 else [1, 2, 1]
    bit_lut = bytes(32 if v >= 190 else 0 for v in range(256))
    erase_mask_lut = bytes(255 if v >= 224 else 0 for v in range(256))

    for radius in radii:
        cur_bytes = _rgb32_qimage_to_gray_bytes(cur_img)
        white_bit_img = _gray_bytes_to_rgb32_qimage(cur_bytes.translate(bit_lut), w, h)

        accum_img = QImage(w, h, QImage.Format.Format_RGB32)
        accum_img.fill(QColor(0, 0, 0))
        offsets = [
            (-radius, -radius), (0, -radius), (radius, -radius),
            (-radius, 0),                     (radius, 0),
            (-radius, radius),  (0, radius),  (radius, radius),
        ]
        p_acc = QPainter(accum_img)
        p_acc.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for dx, dy in offsets:
            p_acc.drawImage(QPoint(dx, dy), white_bit_img)
        p_acc.end()

        accum_bytes = _rgb32_qimage_to_gray_bytes(accum_img)
        erase_mask_img = _gray_bytes_to_rgb32_qimage(
            accum_bytes.translate(erase_mask_lut), w, h
        )
        next_img = cur_img.copy()
        p_clean = QPainter(next_img)
        p_clean.setCompositionMode(QPainter.CompositionMode.CompositionMode_Lighten)
        p_clean.drawImage(0, 0, erase_mask_img)
        p_clean.end()
        cur_img = next_img

    return cur_img


_POWERSHELL_BATCH_OCR_SCRIPT = r"""
param(
    [string]$ManifestJsonPath,
    [string]$OutJsonPath
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

Function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    return $netTask.Result
}

[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null

$engine = $null
foreach ($tag in @("ja-JP", "ja", "en-US", "en")) {
    try {
        $lang = New-Object Windows.Globalization.Language($tag)
        if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) {
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
            if ($null -ne $engine) { break }
        }
    } catch {}
}
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if ($null -eq $engine) {
    throw "Windows標準OCRエンジンを初期化できませんでした。"
}

$manifestRaw = [System.IO.File]::ReadAllText($ManifestJsonPath, [System.Text.Encoding]::UTF8)
$imagePaths = ConvertFrom-Json -InputObject $manifestRaw
if ($imagePaths -isnot [System.Array]) {
    $imagePaths = @($imagePaths)
}

$pagesResult = New-Object System.Collections.ArrayList
$idx = 0
foreach ($imgPath in $imagePaths) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync([string]$imgPath)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $ocrResult = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

        $linesList = New-Object System.Collections.ArrayList
        foreach ($line in $ocrResult.Lines) {
            $wordsList = New-Object System.Collections.ArrayList
            foreach ($w in $line.Words) {
                $r = $w.BoundingRect
                [void]$wordsList.Add(@{
                    t = [string]$w.Text
                    x = [double]$r.X
                    y = [double]$r.Y
                    w = [double]$r.Width
                    h = [double]$r.Height
                })
            }
            [void]$linesList.Add(@{
                words = $wordsList.ToArray()
            })
        }
        [void]$pagesResult.Add(@{
            lines = $linesList.ToArray()
        })
    } finally {
        $stream.Dispose()
    }
    [Console]::Out.WriteLine("@@OCR_PAGE_DONE@@:" + $idx)
    [Console]::Out.Flush()
    $idx += 1
}

$payload = @{ pages = $pagesResult.ToArray() }
$json = ConvertTo-Json -InputObject $payload -Depth 8 -Compress
[System.IO.File]::WriteAllText($OutJsonPath, $json, [System.Text.Encoding]::UTF8)
"""


def _is_ascii_word(s: str) -> bool:
    return bool(s) and all(ord(c) < 128 and (c.isalnum() or c in "-_.") for c in s)


def _normalize_numeric_ocr_misreads(text: str) -> str:
    if not text:
        return text
    s = unicodedata.normalize("NFKC", text)
    for _ in range(4):
        prev = s
        s = re.sub(r"(?<=[0-9,¥￥+-])[Oo](?=[0-9OoIl|,円%)]|$)", "0", s)
        s = re.sub(r"(?:^|(?<=[¥￥+\-▲△(]))[Il|](?=[0-9OoIl|,]*[0-9][0-9OoIl|,]*)", "1", s)
        s = re.sub(r"(?<=[0-9,])[Il|](?=[0-9OoIl|,円%)]|$)", "1", s)
        if s == prev:
            break
    return s


# 日本語OCRが小数点「.」を「、」「。」「・」等と認識した文字をすべて「.」へ変換するマップ
_STRICT_NUMERIC_CHAR_MAP = str.maketrans({
    "O": "0", "o": "0", "Q": "0", "D": "0", "〇": "0", "○": "0", "ロ": "0", "口": "0",
    "I": "1", "l": "1", "|": "1", "!": "1", "i": "1", "j": "1", "丨": "1", "｜": "1",
    "Z": "2", "z": "2", "乙": "2",
    "S": "5", "s": "5",
    "G": "6", "b": "6",
    "T": "7", "Y": "7", "フ": "7", "ワ": "7",
    "B": "8", "&": "8",
    "g": "9", "q": "9",
    "ー": "-", "―": "-", "‐": "-", "–": "-", "—": "-", "−": "-", "﹣": "-",
    "、": ".", "。": ".", "．": ".", "・": ".", "･": ".", "·": ".", "•": ".", "∙": ".", "⋅": ".", "‚": ".", "､": ".", "｡": ".",
    "，": ",",
    "￥": "¥",
})

_DOT_LIKE_CHARS = frozenset({
    ".", "．", "、", "。", "・", "･", "·", "•", "∙", "⋅", "‚", "､", "｡", ",", "，"
})


def normalize_strict_numeric_text(text: str, allow_standalone_dot: bool = False) -> str:
    """
    範囲OCR「🔢 数字特化」用の数値・小数正規化フィルタ。
    「、」「。」「・」等をすべて小数点「.」へ変換し、3桁区切り以外のカンマも小数点へ復元する。
    """
    if not text:
        return ""
    raw_strip = str(text).strip()
    if allow_standalone_dot and raw_strip in _DOT_LIKE_CHARS:
        return "." if raw_strip not in (",", "，") else ","

    s = unicodedata.normalize("NFKC", raw_strip)
    if not s:
        return ""
    if allow_standalone_dot and s in _DOT_LIKE_CHARS:
        return "." if s not in (",", "，") else ","

    s = s.translate(_STRICT_NUMERIC_CHAR_MAP)
    s = re.sub(r"[^0-9,.\+\-¥\$%\(\)▲△/: ]+", "", s)
    s = re.sub(r"\s*\.\s*", ".", s)
    s = re.sub(r"\s*,\s*", ",", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"(?<=[0-9,.\+\-¥\$▲△/:])\s+(?=[0-9,.%\)])", "", s)

    # 0,125 や 12,5 のように3桁区切りではないカンマを小数点「.」へ変換
    s = re.sub(r"(?<!\d)0,(?=\d)", "0.", s)
    s = re.sub(r"(?<=\d),(?=\d{1,2}(?!\d))", ".", s)
    s = re.sub(r"(?<=\d),(?=\d{4,}(?!\d))", ".", s)
    s = re.sub(r"\.{2,}", ".", s)

    if not any(ch.isdigit() for ch in s):
        if allow_standalone_dot and s in (".", ","):
            return s
        return ""
    return s


def _group_words_into_visual_rows(words: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """
    ページ内のWord群を垂直方向の重なりに基づいて行ごとに束ね、各行内をX座標（左→右）順に整列する。
    これにより、下端にある小さな「、」や「.」が別バンドや別行に飛ばされるのを完全に防ぐ。
    """
    valid = [w for w in words if str(w.get("t", "")).strip() and float(w.get("w", 0)) > 0]
    if not valid:
        return []

    # まず高さが大きい通常の文字から先に行の基準を作り、小さな点（、や.）を後から吸着させる
    tall_sorted = sorted(valid, key=lambda w: (-float(w["h"]), float(w["y"]), float(w["x"])))
    rows: List[Dict[str, Any]] = []

    for w in tall_sorted:
        wy0 = float(w["y"])
        wy1 = wy0 + float(w["h"])
        wcy = (wy0 + wy1) * 0.5
        wh = max(2.0, float(w["h"]))

        best_row = None
        for r in rows:
            ry0, ry1 = r["y0"], r["y1"]
            rcy = (ry0 + ry1) * 0.5
            rh = max(4.0, ry1 - ry0)
            overlap = min(ry1, wy1) - max(ry0, wy0)
            # 垂直範囲が重なっているか、行の下端付近（小数点・読点の位置）にある場合は同じ行とみなす
            if overlap >= -max(2.0, min(wh, rh) * 0.35) or abs(wcy - rcy) <= rh * 0.65:
                best_row = r
                break

        if best_row is not None:
            best_row["words"].append(w)
            best_row["y0"] = min(best_row["y0"], wy0)
            best_row["y1"] = max(best_row["y1"], wy1)
        else:
            rows.append({"y0": wy0, "y1": wy1, "words": [w]})

    rows.sort(key=lambda r: (r["y0"] + r["y1"]) * 0.5)
    result_rows: List[List[Dict[str, Any]]] = []
    for r in rows:
        rw = r["words"]
        rw.sort(key=lambda w: float(w["x"]))
        result_rows.append(rw)
    return result_rows


def _merge_ocr_line_words_into_chunks(
    words: List[Dict[str, Any]], fix_numeric: bool = True, numeric_only: bool = False
) -> List[Dict[str, Any]]:
    """
    行内のWord群をX座標順に走査してチャンクへ結合する。
    「、」「。」「.」などの小数点候補が数字の隣にある場合は確実に連結する。
    """
    if not words:
        return []

    valid_words = [w for w in words if str(w.get("t", "")).strip() and float(w.get("w", 0)) > 0]
    if not valid_words:
        return []

    # 同一行内のWordは必ずX座標（左から右）の順に並べる（Y座標によるバンド分割は小点を末尾に飛ばすため行わない）
    valid_words.sort(key=lambda w: float(w["x"]))
    avg_h = max(4.0, sum(float(w["h"]) for w in valid_words) / len(valid_words))
    num_gap_cap = max(2.5, avg_h * 0.38)
    text_gap_cap = max(4.0, avg_h * 0.5)

    raw_groups: List[Dict[str, Any]] = []
    cur_texts: List[str] = []
    x0 = y0 = x1 = y1 = 0.0

    for w in valid_words:
        t = str(w["t"]).strip()
        wx0 = float(w["x"])
        wy0 = float(w["y"])
        wx1 = wx0 + float(w["w"])
        wy1 = wy0 + float(w["h"])

        if not cur_texts:
            cur_texts = [t]
            x0, y0, x1, y1 = wx0, wy0, wx1, wy1
            continue

        gap_x = wx0 - x1
        vert_overlap = min(y1, wy1) - max(y0, wy0)
        prev_str = cur_texts[-1]

        is_dot_adjacent = (
            t in _DOT_LIKE_CHARS
            or prev_str in _DOT_LIKE_CHARS
            or prev_str[-1] in _DOT_LIKE_CHARS
            or t[0] in _DOT_LIKE_CHARS
        )
        is_num_continuation = (
            is_dot_adjacent
            or (prev_str[-1] in "0123456789,.$¥￥+-▲△(" and t[0] in "0123456789,.%円)")
            or (prev_str[-1].isdigit() and t[0].isdigit())
        )

        if is_dot_adjacent:
            effective_max_gap = max(8.0, avg_h * 0.95)
        elif is_num_continuation:
            effective_max_gap = num_gap_cap
        else:
            effective_max_gap = text_gap_cap

        if -3.0 <= gap_x <= effective_max_gap and (vert_overlap >= -avg_h * 0.45 or is_dot_adjacent):
            if (
                not numeric_only
                and not is_dot_adjacent
                and _is_ascii_word(prev_str)
                and _is_ascii_word(t)
                and not is_num_continuation
                and gap_x > avg_h * 0.18
            ):
                cur_texts.append(" " + t)
            else:
                cur_texts.append(t)
            x0 = min(x0, wx0)
            y0 = min(y0, wy0)
            x1 = max(x1, wx1)
            y1 = max(y1, wy1)
        else:
            raw_groups.append({"text": "".join(cur_texts), "bbox": (x0, y0, x1, y1)})
            cur_texts = [t]
            x0, y0, x1, y1 = wx0, wy0, wx1, wy1

    if cur_texts:
        raw_groups.append({"text": "".join(cur_texts), "bbox": (x0, y0, x1, y1)})

    # 単独の「、」「.」グループが数字グループの間に挟まっている場合は前後の数字と結合する
    if len(raw_groups) >= 2:
        stitched: List[Dict[str, Any]] = []
        i = 0
        while i < len(raw_groups):
            cur_g = raw_groups[i]
            cur_t = cur_g["text"].strip()
            if (
                cur_t in _DOT_LIKE_CHARS
                and stitched
                and any(c.isdigit() for c in stitched[-1]["text"])
            ):
                prev_g = stitched[-1]
                dot_ch = "." if (numeric_only or cur_t not in (",", "，")) else ","
                if i + 1 < len(raw_groups) and any(c.isdigit() for c in raw_groups[i + 1]["text"]):
                    next_g = raw_groups[i + 1]
                    stitched[-1] = {
                        "text": prev_g["text"] + dot_ch + next_g["text"],
                        "bbox": (
                            min(prev_g["bbox"][0], cur_g["bbox"][0], next_g["bbox"][0]),
                            min(prev_g["bbox"][1], cur_g["bbox"][1], next_g["bbox"][1]),
                            max(prev_g["bbox"][2], cur_g["bbox"][2], next_g["bbox"][2]),
                            max(prev_g["bbox"][3], cur_g["bbox"][3], next_g["bbox"][3]),
                        ),
                    }
                    i += 2
                    continue
                else:
                    stitched[-1] = {
                        "text": prev_g["text"] + dot_ch,
                        "bbox": (
                            min(prev_g["bbox"][0], cur_g["bbox"][0]),
                            min(prev_g["bbox"][1], cur_g["bbox"][1]),
                            max(prev_g["bbox"][2], cur_g["bbox"][2]),
                            max(prev_g["bbox"][3], cur_g["bbox"][3]),
                        ),
                    }
                    i += 1
                    continue
            stitched.append(cur_g)
            i += 1
        raw_groups = stitched

    chunks: List[Dict[str, Any]] = []
    for grp in raw_groups:
        joined = grp["text"]
        if numeric_only:
            joined = normalize_strict_numeric_text(joined, allow_standalone_dot=True)
        elif fix_numeric:
            joined = _normalize_numeric_ocr_misreads(joined)
        if joined:
            chunks.append({"text": joined, "bbox": grp["bbox"]})

    return chunks


def run_windows_ocr_batch_on_qimages(
    qimages: Sequence[QImage],
    fix_numeric: bool = True,
    cancel_event: Optional[threading.Event] = None,
    progress_callback: Optional[Callable[[int], None]] = None,
    numeric_only: bool = False,
) -> List[List[Dict[str, Any]]]:
    if sys.platform != "win32":
        raise RuntimeError("Windows標準OCRはWindows環境専用です。")
    if not qimages or (cancel_event is not None and cancel_event.is_set()):
        return []

    temp_dir = tempfile.mkdtemp(prefix="secure_pdf_ocr_")
    ps1_path = os.path.join(temp_dir, "run_batch_ocr.ps1")
    manifest_path = os.path.join(temp_dir, "manifest.json")
    json_path = os.path.join(temp_dir, "ocr_out.json")
    img_paths: List[str] = []

    try:
        for idx, qimg in enumerate(qimages):
            if cancel_event is not None and cancel_event.is_set():
                return []
            if qimg.isNull() or qimg.width() <= 0 or qimg.height() <= 0:
                raise RuntimeError(f"{idx + 1}ページ目のOCR用画像が空です。")
            p = os.path.join(temp_dir, f"page_{idx}.png")
            if not qimg.save(p, "PNG"):
                raise RuntimeError("OCR用の一時画像ファイルの保存に失敗しました。")
            img_paths.append(p)

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(img_paths, f, ensure_ascii=False)
        with open(ps1_path, "w", encoding="utf-8-sig") as f:
            f.write(_POWERSHELL_BATCH_OCR_SCRIPT)

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        cmd = [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", ps1_path,
            "-ManifestJsonPath", manifest_path, "-OutJsonPath", json_path,
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", creationflags=creation_flags,
        )
        with _ACTIVE_OCR_PROCS_LOCK:
            _ACTIVE_OCR_PROCS.add(proc)

        stdout_lines: List[str] = []

        def _stdout_reader():
            if proc.stdout is None:
                return
            for line in proc.stdout:
                s_line = line.strip()
                stdout_lines.append(s_line)
                if s_line.startswith("@@OCR_PAGE_DONE@@:") and progress_callback is not None:
                    try:
                        progress_callback(int(s_line.split(":", 1)[1]))
                    except Exception:
                        pass

        reader_thread = threading.Thread(target=_stdout_reader, daemon=True)
        reader_thread.start()

        timeout_sec = max(45.0, 25.0 * len(qimages))
        start_time = time.monotonic()
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    proc.kill()
                except Exception:
                    pass
                return []
            if time.monotonic() - start_time > timeout_sec:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise RuntimeError("Windows標準OCRの実行がタイムアウトしました。")
            time.sleep(0.04)

        reader_thread.join(timeout=1.0)
        if proc.returncode != 0 or not os.path.isfile(json_path):
            err_detail = "\n".join(stdout_lines).strip()
            raise RuntimeError(f"Windows標準OCRの実行に失敗しました。\n{err_detail}")

        with open(json_path, "r", encoding="utf-8-sig") as f:
            raw_json = f.read().strip()
        if not raw_json:
            return [[] for _ in qimages]

        data = json.loads(raw_json)
        pages_data = data.get("pages") or []
        if isinstance(pages_data, dict):
            pages_data = [pages_data]

        results_per_page: List[List[Dict[str, Any]]] = []
        for page_obj in pages_data:
            if not isinstance(page_obj, dict):
                results_per_page.append([])
                continue
            lines = page_obj.get("lines") or []
            if isinstance(lines, dict):
                lines = [lines]

            # ページ内の全Wordを収集し、垂直重なりで行ごとに再グループ化してからX順結合する
            all_page_words: List[Dict[str, Any]] = []
            for line_obj in lines:
                if not isinstance(line_obj, dict):
                    continue
                words = line_obj.get("words") or []
                if isinstance(words, dict):
                    words = [words]
                all_page_words.extend(words)

            page_chunks: List[Dict[str, Any]] = []
            for visual_row_words in _group_words_into_visual_rows(all_page_words):
                page_chunks.extend(
                    _merge_ocr_line_words_into_chunks(
                        visual_row_words, fix_numeric=fix_numeric, numeric_only=numeric_only
                    )
                )
            results_per_page.append(page_chunks)

        while len(results_per_page) < len(qimages):
            results_per_page.append([])
        return results_per_page

    finally:
        if "proc" in locals() and proc is not None:
            with _ACTIVE_OCR_PROCS_LOCK:
                _ACTIVE_OCR_PROCS.discard(proc)
        for p in [*img_paths, ps1_path, manifest_path, json_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass


def run_windows_ocr_on_qimage(
    qimg: QImage,
    fix_numeric: bool = True,
    cancel_event: Optional[threading.Event] = None,
    numeric_only: bool = False,
) -> List[Dict[str, Any]]:
    if qimg.isNull() or qimg.width() <= 0 or qimg.height() <= 0:
        return []
    batch_res = run_windows_ocr_batch_on_qimages(
        [qimg], fix_numeric=fix_numeric, cancel_event=cancel_event, numeric_only=numeric_only
    )
    return batch_res[0] if batch_res else []


def apply_cleaned_image_and_ocr_to_page(
    doc: fitz.Document,
    page_idx: int,
    cleaned_qimg: Optional[QImage],
    ocr_chunks: Optional[List[Dict[str, Any]]],
    ocr_img_size: Tuple[int, int],
    apply_cleaned_visual: bool = True,
) -> int:
    """色抜き画像とOCR文字をページへ反映し、埋め込めたブロック数を返す。

    埋め込みは例外を握りつぶして続行する作りのため、呼び出し側が
    「認識したのに1件も埋め込めなかった」ことを検出できるよう、
    実際に挿入できた chunk の数を返す。
    """
    from PyQt6.QtCore import QBuffer, QIODevice

    inserted_count = 0
    with FITZ_LOCK:
        page = doc[page_idx]
        saved_rot = page.rotation % 360
        saved_crop = get_effective_cropbox(page)
        view_rect = fitz.Rect(page.rect)

        try:
            if saved_rot != 0:
                page.set_rotation(0)
            if saved_crop != page.mediabox:
                # 原点がずれた MediaBox などでは、cropbox を mediabox へ
                # 広げる操作自体が例外になることがある。その場合は
                # 今の cropbox のまま処理を続ける（座標計算は saved_crop
                # 基準なので、そのままでも位置は合う）。
                try:
                    page.set_cropbox(page.mediabox)
                except Exception:
                    logger.debug(
                        "Failed to widen cropbox to mediabox; continuing",
                        exc_info=True,
                    )
            if apply_cleaned_visual and cleaned_qimg is not None and not cleaned_qimg.isNull():
                img_to_insert = cleaned_qimg
                if saved_rot != 0:
                    tf = QTransform()
                    tf.rotate(-float(saved_rot))
                    img_to_insert = cleaned_qimg.transformed(
                        tf, Qt.TransformationMode.SmoothTransformation
                    )
                buf = QBuffer()
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                img_to_insert.save(buf, "PNG")
                png_bytes = bytes(buf.data())
                buf.close()

                page.add_redact_annot(saved_crop, fill=(1, 1, 1))
                try:
                    page.apply_redactions(
                        images=getattr(fitz, "PDF_REDACT_IMAGE_REMOVE", 1),
                        graphics=getattr(fitz, "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", 1),
                        text=getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0),
                    )
                except TypeError:
                    page.apply_redactions()
                page.insert_image(saved_crop, stream=png_bytes)

            if ocr_chunks and not apply_cleaned_visual:
                try:
                    raw_d = page.get_text("rawdict")
                    old_ocr_rects = []
                    for blk in raw_d.get("blocks", []):
                        if blk.get("type") != 0:
                            continue
                        for ln in blk.get("lines", []):
                            for sp in ln.get("spans", []):
                                if sp.get("color") in OCR_TEXT_MARKER_SRGB_INTS:
                                    old_ocr_rects.append(fitz.Rect(sp.get("bbox", (0, 0, 0, 0))))
                    if old_ocr_rects:
                        for rr in old_ocr_rects:
                            page.add_redact_annot(rr, fill=False)
                        img_none = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)
                        gfx_none = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
                        txt_rem = getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0)
                        try:
                            page.apply_redactions(images=img_none, graphics=gfx_none, text=txt_rem)
                        except TypeError:
                            page.apply_redactions(images=img_none)
                except Exception:
                    logger.debug("Failed to clear previous OCR text layer", exc_info=True)

            if ocr_chunks:
                img_w = max(1.0, float(ocr_img_size[0]))
                img_h = max(1.0, float(ocr_img_size[1]))
                sx = view_rect.width / img_w
                sy = view_rect.height / img_h

                existing_fonts = {f[4] for f in (page.get_fonts() or []) if len(f) > 4}
                font_alias = "jp_ocr_font"
                idx_f = 1
                while font_alias in existing_fonts:
                    idx_f += 1
                    font_alias = f"jp_ocr_font_{idx_f}"

                fitz_font = None
                if JAPANESE_FONT:
                    try:
                        fitz_font = fitz.Font(fontfile=JAPANESE_FONT)
                    except Exception:
                        fitz_font = None

                def _insert_chunk_text(one_text, view_pt, fs):
                    raw_pt = view_point_to_raw_pdf_from_geom(
                        saved_crop, view_rect, saved_rot, view_pt
                    )
                    if JAPANESE_FONT:
                        try:
                            page.insert_text(
                                raw_pt, one_text, fontsize=fs,
                                color=OCR_TEXT_MARKER_RGB, rotate=saved_rot,
                                fontfile=JAPANESE_FONT, fontname=font_alias, render_mode=3,
                            )
                            return True
                        except Exception:
                            pass
                    try:
                        page.insert_text(
                            raw_pt, one_text, fontsize=fs,
                            color=OCR_TEXT_MARKER_RGB, rotate=saved_rot,
                            fontname="japan", render_mode=3,
                        )
                        return True
                    except Exception:
                        logger.debug("Failed to insert OCR text chunk", exc_info=True)
                    return False

                for chunk in ocr_chunks:
                    text_str = str(chunk.get("text", "")).strip()
                    if not text_str:
                        continue
                    bx0, by0, bx1, by1 = chunk["bbox"]
                    vx0 = view_rect.x0 + bx0 * sx
                    vy0 = view_rect.y0 + by0 * sy
                    vx1 = view_rect.x0 + bx1 * sx
                    vy1 = view_rect.y0 + by1 * sy

                    box_w = max(2.0, vx1 - vx0)
                    box_h = max(4.0, vy1 - vy0)

                    if chunk.get("vertical"):
                        # 縦書きは1文字ずつ縦に並べて埋め込む（横書きとして
                        # 1行で入れると、位置も大きさも合わなくなるため）
                        char_count = max(1, len(text_str))
                        cell_h = box_h / char_count
                        fs_v = max(4.0, min(72.0, box_w * 0.88, cell_h * 0.95))
                        char_x = vx0 + max(0.0, (box_w - fs_v) / 2.0)
                        any_ok = False
                        for i, ch in enumerate(text_str):
                            baseline_y = vy0 + cell_h * i + cell_h * 0.5 + fs_v * 0.36
                            if _insert_chunk_text(ch, fitz.Point(char_x, baseline_y), fs_v):
                                any_ok = True
                        if any_ok:
                            inserted_count += 1
                        continue

                    est_fs = max(4.5, min(72.0, box_h * 0.88))
                    if fitz_font is not None:
                        try:
                            unit_w = fitz_font.text_length(text_str, fontsize=1.0)
                            if unit_w > 0.1:
                                est_fs = max(4.0, min(est_fs * 1.15, box_w / unit_w))
                        except Exception:
                            pass

                    view_baseline_pt = fitz.Point(vx0, vy0 + box_h * 0.84)
                    if _insert_chunk_text(text_str, view_baseline_pt, est_fs):
                        inserted_count += 1
        finally:
            # 復元は必ず試みる（片方が失敗しても、もう片方は戻す）
            try:
                if page.rotation != saved_rot:
                    page.set_rotation(saved_rot)
            except Exception:
                logger.debug("Failed to restore page rotation", exc_info=True)
            try:
                if fitz.Rect(page.cropbox) != saved_crop:
                    page.set_cropbox(saved_crop)
            except Exception:
                logger.debug("Failed to restore cropbox", exc_info=True)
    return inserted_count
