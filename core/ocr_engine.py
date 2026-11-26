# -*- coding: utf-8 -*-
"""OCRエンジンの入口。

この版（OCR強化版）では、国会図書館の NDLOCR-Lite を使う。
呼び出し側はここを経由することで、エンジンの実装を意識しない。
"""
from core.ndl_ocr import (
    engine_ready,
    request_shutdown_and_wait,
    run_ndl_ocr_batch_on_qimages,
    run_ndl_ocr_on_qimage,
)

OCR_ENGINE_LABEL = "国会図書館 NDLOCR-Lite"

run_ocr_on_qimage = run_ndl_ocr_on_qimage
run_ocr_batch_on_qimages = run_ndl_ocr_batch_on_qimages

__all__ = [
    "OCR_ENGINE_LABEL",
    "engine_ready",
    "request_shutdown_and_wait",
    "run_ocr_batch_on_qimages",
    "run_ocr_on_qimage",
]
