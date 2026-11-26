# -*- coding: utf-8 -*-
"""国会図書館 NDLOCR-Lite を使うOCRエンジン（OCR強化版専用）。

Windows標準OCR（core/ocr_cleaner.py）と同じ chunk 形式
{"text": str, "bbox": (x0, y0, x1, y1)}（画像ピクセル座標）を返すので、
呼び出し側はエンジンの違いを意識しなくてよい。

モデル（ONNXファイル）は、exe同梱 → ローカルキャッシュ →
国会図書館のサーバーから取得（SHA-256検証つき）の順で解決する。
初回だけモデル取得で時間がかかることがある。推論はすべてこのPCの中で行い、
読み取り対象の画像が外部に送られることはない。
"""
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PyQt6.QtGui import QImage

from core import logger
from core.ocr_cleaner import (
    _group_words_into_visual_rows,
    _merge_ocr_line_words_into_chunks,
    _normalize_numeric_ocr_misreads,
    normalize_strict_numeric_text,
)

_ENGINE_LOCK = threading.Lock()
_CALL_LOCK = threading.Lock()
_ENGINE: Optional[Tuple[Any, Any, Any, Any]] = None
_GLOBAL_CANCEL = threading.Event()


def engine_ready() -> bool:
    """エンジン（モデル読み込み済み）かどうか。"""
    return _ENGINE is not None


def request_shutdown_and_wait(timeout: float = 10.0) -> None:
    """ウインドウを閉じるとき用：実行中の推論を止め、終わるのを少し待つ。

    複数ウインドウのうち1つを閉じただけの場合に備え、待ち終わったら
    キャンセル合図は解除する（残ったウインドウのOCRが止まったままに
    ならないように）。
    """
    _GLOBAL_CANCEL.set()
    try:
        acquired = _CALL_LOCK.acquire(timeout=timeout)
        if acquired:
            _CALL_LOCK.release()
    finally:
        _GLOBAL_CANCEL.clear()


def _ndl_base_dir() -> Path:
    """NDLOCR-Lite の同梱 config/model の基準フォルダ。

    exe化（PyInstaller onefile）されている場合は _MEIPASS を基準にする
    （モジュールの __file__ は圧縮アーカイブ内の仮のパスになるため）。
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    import ocr as ndl_pipeline

    return Path(ndl_pipeline.__file__).resolve().parent


def _resolve_model_path(filename: str, expected_hash: str) -> str:
    from model_download import get_model_path

    return str(get_model_path(filename, expected_hash, _ndl_base_dir() / "model"))


def _ensure_engine() -> Tuple[Any, Any, Any, Any]:
    """検出器と認識器を読み込む（初回だけモデル取得と読み込みで時間がかかる）。"""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        try:
            import ocr as ndl
            from model_download import MODELS
        except Exception as exc:
            raise RuntimeError(
                "NDLOCR-Lite（国会図書館のOCR）を読み込めませんでした。"
            ) from exc

        base = _ndl_base_dir()
        paths: Dict[str, str] = {}
        try:
            for key, (filename, expected_hash) in MODELS.items():
                paths[key] = _resolve_model_path(filename, expected_hash)
        except Exception as exc:
            raise RuntimeError(
                "OCR用モデルの取得に失敗しました。"
                "初回は国会図書館のサーバーからモデルをダウンロードするため、"
                "インターネット接続が必要です。"
            ) from exc

        args = SimpleNamespace(
            det_weights=paths["det_weights"],
            det_classes=str(base / "config" / "ndl.yaml"),
            det_score_threshold=0.2,
            det_conf_threshold=0.25,
            det_iou_threshold=0.2,
            det_overlap_threshold=0.9,
            rec_weights=paths["rec_weights"],
            rec_weights30=paths["rec_weights30"],
            rec_weights50=paths["rec_weights50"],
            rec_classes=str(base / "config" / "NDLmoji.yaml"),
            device="cpu",
            enable_tcy=False,
            simple_mode=False,
        )
        logger.info("Loading NDLOCR-Lite models...")
        try:
            detector = ndl.get_detector(args)
            recognizer100 = ndl.get_recognizer(args=args)
            recognizer30 = ndl.get_recognizer(args=args, weights_path=args.rec_weights30)
            recognizer50 = ndl.get_recognizer(args=args, weights_path=args.rec_weights50)
        except Exception as exc:
            raise RuntimeError(
                "NDLOCR-Lite（国会図書館のOCR）の読み込みに失敗しました。"
            ) from exc
        _ENGINE = (detector, recognizer30, recognizer50, recognizer100)
        logger.info("NDLOCR-Lite models loaded.")
        return _ENGINE


def _qimage_to_rgb_array(qimg: QImage):
    """QImage を NDLOCR-Lite が受け取る RGB の numpy 配列に変換する。"""
    import numpy as np

    img = qimg.convertToFormat(QImage.Format.Format_RGB888)
    w, h = img.width(), img.height()
    bytes_per_line = img.bytesPerLine()
    ptr = img.bits()
    ptr.setsize(img.sizeInBytes())
    flat = np.frombuffer(bytes(ptr), dtype=np.uint8).reshape((h, bytes_per_line))
    return np.ascontiguousarray(flat[:, : w * 3].reshape((h, w, 3)))


def _lines_to_chunks(
    lines: Sequence[Dict[str, Any]], fix_numeric: bool, numeric_only: bool
) -> List[Dict[str, Any]]:
    """NDLOCR-Lite の行結果を、Windows標準OCRと同じ chunk 形式へ変換する。

    横書きの行は「単語」として扱い、既存の行グループ化・結合（数字の誤読補正
    を含む）をそのまま使う。縦書きの行は横書きの行と混ぜると並びが崩れるため、
    1行を1つの chunk としてそのまま返す。
    """
    words: List[Dict[str, Any]] = []
    vertical_chunks: List[Dict[str, Any]] = []
    for line in lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        box = line.get("boundingBox") or []
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except Exception:
            continue
        if not xs or not ys:
            continue
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        vertical_value = line.get("isVertical")
        if vertical_value is True or str(vertical_value).strip().lower() == "true":
            if numeric_only:
                text = normalize_strict_numeric_text(text, allow_standalone_dot=True)
            elif fix_numeric:
                text = _normalize_numeric_ocr_misreads(text)
            if text:
                vertical_chunks.append(
                    {"text": text, "bbox": (x0, y0, x1, y1), "vertical": True}
                )
        else:
            words.append(
                {"t": text, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
            )

    chunks: List[Dict[str, Any]] = []
    for row_words in _group_words_into_visual_rows(words):
        chunks.extend(
            _merge_ocr_line_words_into_chunks(
                row_words, fix_numeric=fix_numeric, numeric_only=numeric_only
            )
        )
    chunks.extend(vertical_chunks)
    return chunks


def run_ndl_ocr_batch_on_qimages(
    qimages: Sequence[QImage],
    fix_numeric: bool = True,
    cancel_event: Optional[threading.Event] = None,
    progress_callback: Optional[Callable[[int], None]] = None,
    numeric_only: bool = False,
) -> List[List[Dict[str, Any]]]:
    """複数ページ分の画像を NDLOCR-Lite で読み取り、ページごとの chunk 一覧を返す。

    呼び出し形式は run_windows_ocr_batch_on_qimages とそろえてある。
    キャンセルはページ単位で受け付ける（1ページの推論が終わるまでは待つ）。
    """
    if not qimages or _GLOBAL_CANCEL.is_set() or (
        cancel_event is not None and cancel_event.is_set()
    ):
        return []

    for idx, qimg in enumerate(qimages):
        if qimg.isNull() or qimg.width() <= 0 or qimg.height() <= 0:
            raise RuntimeError(f"{idx + 1}ページ目のOCR用画像が空です。")

    detector, recognizer30, recognizer50, recognizer100 = _ensure_engine()

    import ocr as ndl

    temp_dir = tempfile.mkdtemp(prefix="secure_pdf_ocr_")
    results: List[List[Dict[str, Any]]] = []
    try:
        for idx, qimg in enumerate(qimages):
            if _GLOBAL_CANCEL.is_set() or (
                cancel_event is not None and cancel_event.is_set()
            ):
                return []
            img = _qimage_to_rgb_array(qimg)
            with _CALL_LOCK:
                result = ndl._run_ocr_on_image_array(
                    detector,
                    recognizer30,
                    recognizer50,
                    recognizer100,
                    f"page_{idx + 1}",
                    img,
                    temp_dir,
                    save_viz=False,
                )
            results.append(
                _lines_to_chunks(
                    result.get("json_lines") or [],
                    fix_numeric=fix_numeric,
                    numeric_only=numeric_only,
                )
            )
            if progress_callback is not None:
                try:
                    progress_callback(idx)
                except Exception:
                    pass
    finally:
        try:
            for name in os.listdir(temp_dir):
                try:
                    os.remove(os.path.join(temp_dir, name))
                except OSError:
                    pass
            os.rmdir(temp_dir)
        except OSError:
            pass
    return results


def run_ndl_ocr_on_qimage(
    qimg: QImage,
    fix_numeric: bool = True,
    cancel_event: Optional[threading.Event] = None,
    numeric_only: bool = False,
) -> List[Dict[str, Any]]:
    if qimg.isNull() or qimg.width() <= 0 or qimg.height() <= 0:
        return []
    batch_res = run_ndl_ocr_batch_on_qimages(
        [qimg], fix_numeric=fix_numeric, cancel_event=cancel_event,
        numeric_only=numeric_only,
    )
    return batch_res[0] if batch_res else []
