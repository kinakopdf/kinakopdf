# -*- coding: utf-8 -*-
"""NDLOCR-Lite エンジン（core/ndl_ocr.py）のテスト。

実際のパイプライン（ONNXモデル）は使わず、行結果の変換と
バッチ処理の枠組みだけを検証する。実エンジンでの読み取り確認は
合成文書による手動検証で行う。
"""
import sys
import threading
import types
import unittest
from importlib.util import find_spec

from PyQt6.QtWidgets import QApplication

from core import ndl_ocr


def _line(text, x, y, w, h, vertical=False):
    return {
        "boundingBox": [[x, y], [x, y + h], [x + w, y], [x + w, y + h]],
        "text": text,
        "isVertical": "true" if vertical else "false",
        "confidence": 0.9,
    }


def _line_raw_vertical(text, x, y, w, h, raw_vertical):
    line = _line(text, x, y, w, h)
    line["isVertical"] = raw_vertical
    return line


class TestLinesToChunks(unittest.TestCase):
    def test_single_line_becomes_one_chunk(self):
        chunks = ndl_ocr._lines_to_chunks(
            [_line("*38,233", 100, 50, 120, 30)], fix_numeric=True, numeric_only=False
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["text"], "*38,233")
        self.assertEqual(chunks[0]["bbox"], (100.0, 50.0, 220.0, 80.0))

    def test_far_apart_cells_stay_separate(self):
        lines = [
            _line("09-28", 40, 50, 80, 30),
            _line("*13,971", 300, 50, 110, 30),
            _line("*4,194,516", 700, 50, 160, 30),
        ]
        chunks = ndl_ocr._lines_to_chunks(lines, fix_numeric=True, numeric_only=False)
        texts = [c["text"] for c in chunks]
        self.assertEqual(texts, ["09-28", "*13,971", "*4,194,516"])

    def test_empty_lines_are_skipped(self):
        lines = [_line("  ", 0, 0, 10, 10), _line("オリコ", 0, 40, 60, 30)]
        chunks = ndl_ocr._lines_to_chunks(lines, fix_numeric=True, numeric_only=False)
        self.assertEqual([c["text"] for c in chunks], ["オリコ"])

    def test_vertical_line_is_standalone_chunk(self):
        lines = [
            _line("合計", 300, 40, 30, 90, vertical=True),
            _line("12,300円", 40, 50, 120, 30),
        ]
        chunks = ndl_ocr._lines_to_chunks(lines, fix_numeric=True, numeric_only=False)
        texts = sorted(c["text"] for c in chunks)
        self.assertEqual(texts, ["12,300円", "合計"])
        vertical = [c for c in chunks if c["text"] == "合計"][0]
        self.assertEqual(vertical["bbox"], (300.0, 40.0, 330.0, 130.0))
        self.assertTrue(vertical.get("vertical"))
        horizontal = [c for c in chunks if c["text"] == "12,300円"][0]
        self.assertFalse(horizontal.get("vertical"))

    def test_vertical_flag_accepts_bool_true(self):
        lines = [_line_raw_vertical("縦書き", 10, 10, 30, 120, True)]
        chunks = ndl_ocr._lines_to_chunks(lines, fix_numeric=True, numeric_only=False)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].get("vertical"))

    def test_narrow_gap_cells_merge_like_word_engine(self):
        # 既存の単語結合と同じ挙動（間隔が狭い隣セルは1つの chunk になる）を記録する
        lines = [
            _line("あいう", 40, 50, 90, 30),
            _line("えお", 140, 50, 60, 30),
        ]
        chunks = ndl_ocr._lines_to_chunks(lines, fix_numeric=True, numeric_only=False)
        self.assertEqual([c["text"] for c in chunks], ["あいうえお"])


@unittest.skipUnless(
    find_spec("numpy") and find_spec("PIL"), "numpy/Pillow がない環境ではスキップ"
)
class TestBatchWithFakePipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtGui import QImage

        self.qimg = QImage(200, 100, QImage.Format.Format_RGB32)
        self.qimg.fill(0xFFFFFFFF)

        fake_ocr = types.ModuleType("ocr")

        def fake_run(detector, r30, r50, r100, inputname, img, outputpath, save_viz=False):
            return {
                "json_lines": [
                    _line("テスト", 10, 10, 80, 25),
                    _line("*1,017,520", 10, 50, 140, 25),
                ]
            }

        fake_ocr._run_ocr_on_image_array = fake_run
        self._orig_ocr_mod = sys.modules.get("ocr")
        sys.modules["ocr"] = fake_ocr
        self._orig_ensure = ndl_ocr._ensure_engine
        ndl_ocr._ensure_engine = lambda: (object(), object(), object(), object())

    def tearDown(self):
        ndl_ocr._ensure_engine = self._orig_ensure
        if self._orig_ocr_mod is None:
            sys.modules.pop("ocr", None)
        else:
            sys.modules["ocr"] = self._orig_ocr_mod

    def test_batch_returns_chunks_per_page_and_reports_progress(self):
        done = []
        results = ndl_ocr.run_ndl_ocr_batch_on_qimages(
            [self.qimg, self.qimg], progress_callback=done.append
        )
        self.assertEqual(len(results), 2)
        for page_chunks in results:
            texts = [c["text"] for c in page_chunks]
            self.assertIn("テスト", texts)
            self.assertIn("*1,017,520", texts)
        self.assertEqual(done, [0, 1])

    def test_batch_cancelled_returns_empty(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(
            ndl_ocr.run_ndl_ocr_batch_on_qimages([self.qimg], cancel_event=cancel),
            [],
        )

    def test_single_image_wrapper(self):
        chunks = ndl_ocr.run_ndl_ocr_on_qimage(self.qimg)
        self.assertTrue(any(c["text"] == "テスト" for c in chunks))

    def test_null_image_returns_empty(self):
        from PyQt6.QtGui import QImage

        self.assertEqual(ndl_ocr.run_ndl_ocr_on_qimage(QImage()), [])


class TestApplyInsertedCount(unittest.TestCase):
    """適用処理が「埋め込めたブロック数」を返すことの検証。

    認識したのに埋め込みが0件だったケースを、ダイアログ側で
    検出して警告するために使う戻り値。
    """

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_returns_inserted_count_and_text_is_searchable(self):
        import fitz

        from core.ocr_cleaner import apply_cleaned_image_and_ocr_to_page

        doc = fitz.open()
        doc.new_page(width=400, height=300)
        chunks = [
            {"text": "領収書", "bbox": (20.0, 30.0, 120.0, 60.0)},
            {"text": "合計3,140円", "bbox": (20.0, 100.0, 200.0, 130.0)},
            {"text": "縦書き", "bbox": (300.0, 30.0, 330.0, 150.0),
             "vertical": True},
        ]
        inserted = apply_cleaned_image_and_ocr_to_page(
            doc=doc, page_idx=0, cleaned_qimg=None, ocr_chunks=chunks,
            ocr_img_size=(400, 300), apply_cleaned_visual=False,
        )
        self.assertEqual(inserted, 3)
        extracted = doc[0].get_text("text")
        # フォントによっては抽出時に互換文字へ寄る字があるため、
        # 埋め込みの有無は合計の文字列と検索ヒットで確認する
        self.assertIn("合計3,140円", extracted)
        self.assertGreaterEqual(len(doc[0].search_for("合計3,140円")), 1)
        doc.close()

    def test_no_chunks_returns_zero(self):
        import fitz

        from core.ocr_cleaner import apply_cleaned_image_and_ocr_to_page

        doc = fitz.open()
        doc.new_page(width=200, height=200)
        inserted = apply_cleaned_image_and_ocr_to_page(
            doc=doc, page_idx=0, cleaned_qimg=None, ocr_chunks=[],
            ocr_img_size=(200, 200), apply_cleaned_visual=False,
        )
        self.assertEqual(inserted, 0)
        doc.close()


class TestDialogDefaultMode(unittest.TestCase):
    """ダイアログの標準の実行内容が「OCR文字も埋め込む」であること。

    以前は「色抜き画像のみ」が標準で、テスト実行で文字が見えても
    そのまま適用すると埋め込まれない、という落とし穴があった。
    """

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_default_action_mode_includes_ocr(self):
        import fitz
        from types import SimpleNamespace

        from ui.ocr_dialog import ScanCleanOcrDialog

        doc = fitz.open()
        doc.new_page(width=200, height=200)
        viewer = SimpleNamespace(current_pdf=doc, current_page_idx=0)
        dlg = ScanCleanOcrDialog(viewer)
        try:
            self.assertEqual(dlg.combo_action_mode.currentData(), "both")
            clean_item = dlg.combo_action_mode.findData("clean_only")
            self.assertGreaterEqual(clean_item, 0)
            self.assertIn(
                "埋め込まない",
                dlg.combo_action_mode.itemText(clean_item),
            )
        finally:
            dlg.close()
            doc.close()


@unittest.skipUnless(find_spec("numpy"), "numpy がない環境ではスキップ")
class TestQImageToArray(unittest.TestCase):
    def test_shape_and_pixel_values(self):
        from PyQt6.QtGui import QColor, QImage

        img = QImage(4, 2, QImage.Format.Format_RGB32)
        img.fill(QColor(10, 20, 30))
        img.setPixelColor(3, 1, QColor(200, 100, 50))
        arr = ndl_ocr._qimage_to_rgb_array(img)
        self.assertEqual(arr.shape, (2, 4, 3))
        self.assertEqual(list(arr[0, 0]), [10, 20, 30])
        self.assertEqual(list(arr[1, 3]), [200, 100, 50])


if __name__ == "__main__":
    unittest.main()
