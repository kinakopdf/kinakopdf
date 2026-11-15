# -*- coding: utf-8 -*-
"""
追加ライブラリ不要（PyQt6 + PyMuPDF + Windows標準PowerShell OCRのみ）の
スキャン用紙カラー除去（黄・赤・青の網掛け消去）・網点ノイズ除去・OCRエンジン
"""
import json
import math
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
from PyQt6.QtCore import QPoint, QRectF, Qt
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

# OCR透明テキスト識別用の内部カラー（画面上はrender_mode=3で完全透明）
OCR_TEXT_MARKER_RGB: Tuple[float, float, float] = (1.0 / 255.0, 1.0 / 255.0, 1.0 / 255.0)
OCR_TEXT_MARKER_SRGB_INT: int = 0x010101

_ACTIVE_OCR_PROCS_LOCK = threading.Lock()
_ACTIVE_OCR_PROCS: set = set()


def terminate_all_active_ocr_processes() -> int:
    """アプリ終了時などに実行中のPowerShell OCR子プロセスを即座に終了させる"""
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
    """一時ディレクトリ内に残存した古い secure_pdf_ocr_* フォルダを安全に掃除する"""
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
                mtime = entry.stat().st_mtime
                if max_age_sec <= 0.0 or (now - mtime) >= max_age_sec:
                    shutil.rmtree(entry.path, ignore_errors=True)
                    if not os.path.exists(entry.path):
                        removed_count += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed_count


def cleanup_bytecode_cache(root_dir: Optional[str] = None) -> int:
    """指定ディレクトリ配下の __pycache__ および .pyc ファイルを削除する"""
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
    """指定ページを指定DPI（大判時はmax_pixels上限で自動クランプ）のQImage(Format_RGB32)としてレンダリングする"""
    base_scale = max(72.0, float(dpi)) / 72.0
    with FITZ_LOCK:
        r = page.rect
        sx, sy = clamp_render_scales(
            r.width, r.height, base_scale, base_scale, max_pixels=max_pixels
        )
        pix = page.get_pixmap(
            matrix=fitz.Matrix(sx, sy),
            colorspace=fitz.csRGB,
            alpha=False,
        )
        qimg = QImage(
            pix.samples,
            pix.width,
            pix.height,
            pix.stride,
            QImage.Format.Format_RGB888,
        ).copy()
    return qimg.convertToFormat(QImage.Format.Format_RGB32)


def _gray_bytes_to_rgb32_qimage(gray_bytes: bytes, w: int, h: int) -> QImage:
    """長さ w*h のグレースケールbytesをストライド問題のないFormat_RGB32のQImageに変換する"""
    g_img = QImage(gray_bytes, w, h, w, QImage.Format.Format_Grayscale8)
    return g_img.convertToFormat(QImage.Format.Format_RGB32)


def _rgb32_qimage_to_gray_bytes(img_rgb32: QImage) -> bytes:
    """Format_RGB32のQImageから1チャンネル分のbytes(長さ w*h)を高速抽出する"""
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
    """
    スキャン画像から有彩色（セゾンの黄色、JCBの赤・青など）と薄い網点（点々）を消去し、
    文字だけを鮮明に残したQImage(Format_RGB32)を返す。
    Qt6のC++ SIMD合成(QPainter)とbytes.translateのみを使用し、純粋Pythonループ無しで高速処理する。
    """
    if src_img.isNull() or src_img.width() <= 0 or src_img.height() <= 0:
        return src_img

    img32 = src_img.convertToFormat(QImage.Format.Format_RGB32)
    w = img32.width()
    h = img32.height()

    bits = img32.constBits()
    bits.setsize(w * h * 4)
    raw = bytes(bits)

    # BGRA順 (Little Endian Format_RGB32)
    b_bytes = raw[0::4]
    g_bytes = raw[1::4]
    r_bytes = raw[2::4]

    r_img = _gray_bytes_to_rgb32_qimage(r_bytes, w, h)
    g_img = _gray_bytes_to_rgb32_qimage(g_bytes, w, h)
    b_img = _gray_bytes_to_rgb32_qimage(b_bytes, w, h)

    # 1. max(R, G, B) と min(R, G, B) を QPainter の Lighten / Darken で一括算出
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

    # 2. 色差 Chroma = max(R, G, B) - min(R, G, B) を算出
    chroma_img = max_img.copy()
    p_diff = QPainter(chroma_img)
    p_diff.setCompositionMode(QPainter.CompositionMode.CompositionMode_Difference)
    p_diff.drawImage(0, 0, min_img)
    p_diff.end()

    chroma_bytes = _rgb32_qimage_to_gray_bytes(chroma_img)

    # 3. 有彩色を白(255)へ飛ばす量を計算
    strength = max(0.0, float(color_drop_strength))
    chroma_lut = bytes(
        0 if c <= 16 else min(255, int(round((c - 16) * strength * 1.85)))
        for c in range(256)
    )
    boosted_chroma = chroma_bytes.translate(chroma_lut)
    boosted_chroma_img = _gray_bytes_to_rgb32_qimage(boosted_chroma, w, h)

    if keep_dark_color_text:
        max_bytes = _rgb32_qimage_to_gray_bytes(max_img)
        min_bytes = _rgb32_qimage_to_gray_bytes(min_img)
        dark_mask_lut = bytes(0 if m < 150 else 255 for m in range(256))
        bright_mask = max_bytes.translate(dark_mask_lut)
        bright_mask_img = _gray_bytes_to_rgb32_qimage(bright_mask, w, h)

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

    # 4. 照明ムラ・紙の地色補正 + コントラスト強調 / 二値化
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
                    min(
                        255,
                        int(round((v - b_th * 0.4) * 255.0 / (w_th - b_th * 0.4))),
                    ),
                )
            )
            for v in range(256)
        )

    toned_bytes = gray_after_color.translate(tone_lut)
    result_img = _gray_bytes_to_rgb32_qimage(toned_bytes, w, h)

    # 5. うっすら残った点々（孤立ドット・網点・地紋）の除去
    if despeckle_level >= 1:
        result_img = _despeckle_isolated_dots(result_img, w, h, level=despeckle_level)

    return result_img


def _despeckle_isolated_dots(img_rgb32: QImage, w: int, h: int, level: int = 1) -> QImage:
    """
    8近傍の白ピクセル数をQPainterのシフト加算で高速カウントし、
    文字の線や句読点を傷つけずに1〜3pxの孤立した点々（網点・地紋）だけを白(255)で消去する。
    """
    cur_img = img_rgb32
    radii = [1] if level == 1 else [1, 2, 1]
    # NOTE: 全パスで閾値224を使う。192では1px細線のピクセルも
    # 白隣接6個 (6*32=192) で消去対象となり、レベル2が細線・句読点を消していた。
    thresholds = [224] * len(radii)
    bit_lut = bytes(32 if v >= 190 else 0 for v in range(256))

    for radius, count_th in zip(radii, thresholds):
        cur_bytes = _rgb32_qimage_to_gray_bytes(cur_img)
        white_bit_bytes = cur_bytes.translate(bit_lut)
        white_bit_img = _gray_bytes_to_rgb32_qimage(white_bit_bytes, w, h)

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
        erase_mask_lut = bytes(255 if v >= count_th else 0 for v in range(256))
        erase_mask_bytes = accum_bytes.translate(erase_mask_lut)
        erase_mask_img = _gray_bytes_to_rgb32_qimage(erase_mask_bytes, w, h)

        next_img = cur_img.copy()
        p_clean = QPainter(next_img)
        p_clean.setCompositionMode(QPainter.CompositionMode.CompositionMode_Lighten)
        p_clean.drawImage(0, 0, erase_mask_img)
        p_clean.end()
        cur_img = next_img

    return cur_img


# ==============================================================================
# Windows標準OCR (Windows.Media.Ocr via PowerShell) 実行＆チャンク構築
# ==============================================================================

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
foreach ($tag in @("ja-JP", "ja")) {
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
    throw "Windows標準OCRエンジンを初期化できませんでした。Windowsの言語設定に『光学式文字認識(日本語)』がインストールされているか確認してください。"
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

$payload = @{
    pages = $pagesResult.ToArray()
}
$json = ConvertTo-Json -InputObject $payload -Depth 8 -Compress
[System.IO.File]::WriteAllText($OutJsonPath, $json, [System.Text.Encoding]::UTF8)
"""


def _is_ascii_word(s: str) -> bool:
    return bool(s) and all(ord(c) < 128 and (c.isalnum() or c in "-_.") for c in s)


def _normalize_numeric_ocr_misreads(text: str) -> str:
    """
    金額・日付・番号などの数値列の中で起きやすい典型的なOCR誤認識
    (例: '12,34O' -> '12,340', '¥1O,OOO' -> '¥10,000', '¥I,200' -> '¥1,200') を文脈補正する
    """
    if not text:
        return text
    s = unicodedata.normalize("NFKC", text)
    for _ in range(4):
        prev = s
        s = re.sub(r"(?<=[0-9,¥￥+-])[Oo](?=[0-9OoIl|,円%)]|$)", "0", s)
        # NOTE: 2番目の文字クラスはハイフンをエスケープすること。
        # [¥￥+-▲△(] のままでは "+-▲" が文字範囲となりほぼ全ASCIIに一致し、
        # "hello" -> "he110" のように通常の英単語まで破壊する。
        # さらに文頭(^)の I/l は、後続に数字を含む数値列の場合にのみ 1 へ補正する
        # ("lollipop" の先頭 l を 1 にしないため)。
        s = re.sub(r"(?:^|(?<=[¥￥+\-▲△(]))[Il|](?=[0-9OoIl|,]*[0-9][0-9OoIl|,]*)", "1", s)
        s = re.sub(r"(?<=[0-9,])[Il|](?=[0-9OoIl|,円%)]|$)", "1", s)
        if s == prev:
            break
    return s


def _merge_ocr_line_words_into_chunks(
    words: List[Dict[str, Any]], fix_numeric: bool = True
) -> List[Dict[str, Any]]:
    """
    Windows.Media.Ocr が1文字単位で分割した日本語のWord群を、
    不要な空白を除去しつつ「店名」「日付」「金額」などの意味のある列チャンクへ結合する。
    """
    if not words:
        return []

    valid_words = [w for w in words if str(w.get("t", "")).strip() and float(w.get("w", 0)) > 0]
    if not valid_words:
        return []

    avg_h = sum(float(w["h"]) for w in valid_words) / len(valid_words)
    # NOTE: 結合前に (yバンド, x) で整列する。OCRの返却順は x 順とは限らず、
    # 未整列のままでは "B(x=100) A(x=10)" が "BA" になってしまう。
    # yバンド = y // avg_h (同一行の y ばらつきを吸収し、行またぎを分離する)。
    band_h = max(avg_h, 1.0)
    valid_words.sort(key=lambda w: (int(float(w["y"]) // band_h), float(w["x"])))
    # 結合可能な水平ギャップの上限。数値の継続は文字高の約0.35倍まで、
    # それ以外は約0.5倍までとし、離れた金額列 (例: 18px / 高さ24) の誤結合を防ぐ。
    num_gap_cap = max(2.0, avg_h * 0.35)
    text_gap_cap = max(4.0, avg_h * 0.5)

    chunks: List[Dict[str, Any]] = []
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
        is_num_continuation = (
            (prev_str[-1] in "0123456789,.$¥￥+-▲△(" and t[0] in "0123456789,.%円)")
            or (prev_str[-1].isdigit() and t[0].isdigit())
        )
        effective_max_gap = num_gap_cap if is_num_continuation else text_gap_cap

        # NOTE: 負の gap_x (重なったボックス) では結合しない。
        # 垂直方向の重なりが無い (= 別行) 場合も結合しない。
        if 0.0 <= gap_x <= effective_max_gap and vert_overlap >= 0.0:
            if (
                _is_ascii_word(prev_str)
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
            joined = "".join(cur_texts)
            if fix_numeric:
                joined = _normalize_numeric_ocr_misreads(joined)
            chunks.append({"text": joined, "bbox": (x0, y0, x1, y1)})
            cur_texts = [t]
            x0, y0, x1, y1 = wx0, wy0, wx1, wy1

    if cur_texts:
        joined = "".join(cur_texts)
        if fix_numeric:
            joined = _normalize_numeric_ocr_misreads(joined)
        chunks.append({"text": joined, "bbox": (x0, y0, x1, y1)})

    return chunks


def run_windows_ocr_batch_on_qimages(
    qimages: Sequence[QImage],
    fix_numeric: bool = True,
    cancel_event: Optional[threading.Event] = None,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> List[List[Dict[str, Any]]]:
    """
    複数のQImageに対して1回のPowerShellプロセス起動でWindows標準OCRを一括実行し、
    ページごとのチャンクリストの配列を返す。
    """
    if sys.platform != "win32":
        raise RuntimeError("Windows標準OCRはWindows環境専用です。")

    if not qimages:
        return []

    if cancel_event is not None and cancel_event.is_set():
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
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            ps1_path,
            "-ManifestJsonPath",
            manifest_path,
            "-OutJsonPath",
            json_path,
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
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
                        done_idx = int(s_line.split(":", 1)[1])
                        progress_callback(done_idx)
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
            page_chunks: List[Dict[str, Any]] = []
            for line_obj in lines:
                if not isinstance(line_obj, dict):
                    continue
                words = line_obj.get("words") or []
                if isinstance(words, dict):
                    words = [words]
                page_chunks.extend(_merge_ocr_line_words_into_chunks(words, fix_numeric=fix_numeric))
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
) -> List[Dict[str, Any]]:
    """
    単一のQImageに対してWindows標準OCRを実行し、
    [{"text": str, "bbox": (x0, y0, x1, y1)}, ...] (画像ピクセル座標) のリストを返す。
    """
    if qimg.isNull() or qimg.width() <= 0 or qimg.height() <= 0:
        return []
    batch_res = run_windows_ocr_batch_on_qimages(
        [qimg], fix_numeric=fix_numeric, cancel_event=cancel_event
    )
    return batch_res[0] if batch_res else []


def apply_cleaned_image_and_ocr_to_page(
    doc: fitz.Document,
    page_idx: int,
    cleaned_qimg: Optional[QImage],
    ocr_chunks: Optional[List[Dict[str, Any]]],
    ocr_img_size: Tuple[int, int],
    apply_cleaned_visual: bool = True,
) -> None:
    """
    指定したPDFページに、元のページの回転角(rotation)とトリミング枠(CropBox)を維持したまま
    1) 色抜き・点々除去済みの画像 (apply_cleaned_visual=True の場合)
    2) Windows標準OCRで抽出した透明テキスト (ocr_chunks が指定された場合)
    を反映する。
    """
    from PyQt6.QtCore import QBuffer, QIODevice

    with FITZ_LOCK:
        page = doc[page_idx]
        saved_rot = page.rotation % 360
        saved_crop = get_effective_cropbox(page)
        view_rect = fitz.Rect(page.rect)

        if saved_rot != 0:
            page.set_rotation(0)
        if saved_crop != page.mediabox:
            page.set_cropbox(page.mediabox)

        try:
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

                    est_fs = max(4.5, min(72.0, box_h * 0.88))
                    if fitz_font is not None:
                        try:
                            unit_w = fitz_font.text_length(text_str, fontsize=1.0)
                            if unit_w > 0.1:
                                width_fs = box_w / unit_w
                                est_fs = max(4.0, min(est_fs * 1.15, width_fs))
                        except Exception:
                            pass

                    view_baseline_pt = fitz.Point(vx0, vy0 + box_h * 0.84)
                    raw_baseline_pt = view_point_to_raw_pdf_from_geom(
                        saved_crop, view_rect, saved_rot, view_baseline_pt
                    )
                    inserted = False
                    if JAPANESE_FONT:
                        try:
                            page.insert_text(
                                raw_baseline_pt,
                                text_str,
                                fontsize=est_fs,
                                color=OCR_TEXT_MARKER_RGB,
                                rotate=saved_rot,
                                fontfile=JAPANESE_FONT,
                                fontname=font_alias,
                                render_mode=3,
                            )
                            inserted = True
                        except Exception:
                            inserted = False
                    if not inserted:
                        try:
                            page.insert_text(
                                raw_baseline_pt,
                                text_str,
                                fontsize=est_fs,
                                color=OCR_TEXT_MARKER_RGB,
                                rotate=saved_rot,
                                fontname="japan",
                                render_mode=3,
                            )
                        except Exception:
                            logger.debug("Failed to insert OCR text chunk", exc_info=True)
        finally:
            if page.rotation != saved_rot:
                page.set_rotation(saved_rot)
            if saved_crop != page.mediabox:
                page.set_cropbox(saved_crop)
