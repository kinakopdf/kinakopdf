# -*- coding: utf-8 -*-
"""試作多機能版 P0（パスワード・圧縮・分割・フォーム・印鑑・しおり・
ヘッダー/フッター/透かし）の単体テスト。"""
import unicodedata
import unittest

import fitz

from core.trial_tools import (
    add_bookmark,
    apply_header_footer,
    apply_stamp_to_doc,
    apply_toc_to_pdf_bytes,
    authenticate_pdf_bytes,
    compress_pdf_bytes,
    delete_bookmark,
    draw_round_seal,
    draw_text_stamp,
    encrypt_pdf_bytes,
    fill_form_fields,
    flatten_form_fields,
    get_bookmarks,
    list_form_fields,
    parse_page_ranges,
    pdf_needs_password,
    rename_bookmark,
    set_bookmarks,
    split_pdf_bytes,
    strip_password_pdf_bytes,
)


def norm(text):
    """抽出テキストのゆらぎ（互換漢字・NBSP）を吸収する。"""
    return unicodedata.normalize("NFKC", text).replace("\xa0", " ")


def make_doc(pages=3, with_text=True):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        if with_text:
            page.insert_text((72, 100), f"Sample page {i + 1}",
                             fontsize=14, fontname="helv")
    return doc


def doc_bytes(doc):
    return doc.tobytes(garbage=4, deflate=True)


def make_photo_doc():
    """大きな画像を含む PDF（圧縮テスト用）。ノイズ画像は PNG だと大きい。"""
    import random
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Photo page", fontsize=14, fontname="helv")
    rnd = random.Random(42)
    w, h = 1600, 1200
    raw = bytearray()
    for _y in range(h):
        raw.append(0)  # フィルタなし
        raw.extend(rnd.getrandbits(8) for _ in range(w * 3))
    png = _png_bytes(w, h, bytes(raw))
    page.insert_image(fitz.Rect(50, 150, 550, 525), stream=png)
    return doc


def _png_bytes(w, h, raw_scanlines):
    """zlib/struct のみで作る最小の PNG（RGB・フィルタなし）。"""
    import struct
    import zlib

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw_scanlines))
            + chunk(b"IEND", b""))


def make_form_doc():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    w = fitz.Widget()
    w.field_name = "name"
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(100, 100, 300, 125)
    w.field_value = ""
    page.add_widget(w)
    w2 = fitz.Widget()
    w2.field_name = "agree"
    w2.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
    w2.rect = fitz.Rect(100, 150, 120, 170)
    w2.field_value = False
    page.add_widget(w2)
    return doc


class TestPassword(unittest.TestCase):
    def test_encrypt_and_open(self):
        data = doc_bytes(make_doc(2))
        self.assertFalse(pdf_needs_password(data))
        enc = encrypt_pdf_bytes(data, "secret123")
        self.assertTrue(pdf_needs_password(enc))
        self.assertTrue(authenticate_pdf_bytes(enc, "secret123"))
        self.assertFalse(authenticate_pdf_bytes(enc, "wrong"))
        doc = fitz.open(stream=enc, filetype="pdf")
        try:
            self.assertTrue(doc.authenticate("secret123"))
            self.assertEqual(len(doc), 2)
            self.assertIn("Sample page 1", doc[0].get_text())
        finally:
            doc.close()

    def test_encrypt_empty_password_rejected(self):
        with self.assertRaises(ValueError):
            encrypt_pdf_bytes(doc_bytes(make_doc(1)), "")

    def test_strip_password(self):
        enc = encrypt_pdf_bytes(doc_bytes(make_doc(1)), "pw")
        plain = strip_password_pdf_bytes(enc, "pw")
        self.assertFalse(pdf_needs_password(plain))
        doc = fitz.open(stream=plain, filetype="pdf")
        try:
            self.assertEqual(len(doc), 1)
        finally:
            doc.close()

    def test_strip_wrong_password_rejected(self):
        enc = encrypt_pdf_bytes(doc_bytes(make_doc(1)), "pw")
        with self.assertRaises(ValueError):
            strip_password_pdf_bytes(enc, "nope")


class TestCompress(unittest.TestCase):
    def test_photo_pdf_gets_smaller(self):
        data = doc_bytes(make_photo_doc())
        out, stats = compress_pdf_bytes(data, max_image_dim=800, jpeg_quality=70)
        self.assertLess(stats["compressed_size"], stats["original_size"])
        self.assertGreater(stats["images_processed"], 0)
        doc = fitz.open(stream=out, filetype="pdf")
        try:
            self.assertEqual(len(doc), 1)
            self.assertIn("Photo page", doc[0].get_text())
            self.assertGreater(len(doc[0].get_images()), 0)
            # 描画できること
            pix = doc[0].get_pixmap(dpi=72)
            self.assertGreater(pix.width, 0)
        finally:
            doc.close()

    def test_text_only_pdf_unchanged_content(self):
        data = doc_bytes(make_doc(2))
        out, stats = compress_pdf_bytes(data)
        self.assertEqual(stats["images_processed"], 0)
        doc = fitz.open(stream=out, filetype="pdf")
        try:
            self.assertEqual(len(doc), 2)
            self.assertIn("Sample page 2", doc[1].get_text())
        finally:
            doc.close()

    def test_invalid_quality_rejected(self):
        with self.assertRaises(ValueError):
            compress_pdf_bytes(doc_bytes(make_doc(1)), jpeg_quality=5)


class TestSplit(unittest.TestCase):
    def test_parse_ranges(self):
        self.assertEqual(parse_page_ranges("1-3,5", 5), [(0, 2), (4, 4)])
        self.assertEqual(parse_page_ranges("2", 5), [(1, 1)])
        self.assertEqual(parse_page_ranges(" 1 - 2 , 4 ", 5), [(0, 1), (3, 3)])

    def test_parse_ranges_errors(self):
        for bad in ("", "0", "6", "3-2", "a-b", "1-6"):
            with self.assertRaises(ValueError, msg=bad):
                parse_page_ranges(bad, 5)

    def test_split_pages_and_text(self):
        data = doc_bytes(make_doc(5))
        parts = split_pdf_bytes(data, parse_page_ranges("1-2,4-5", 5))
        self.assertEqual(len(parts), 2)
        for part, expected in zip(parts, (2, 2)):
            doc = fitz.open(stream=part, filetype="pdf")
            try:
                self.assertEqual(len(doc), expected)
            finally:
                doc.close()
        doc = fitz.open(stream=parts[1], filetype="pdf")
        try:
            self.assertIn("Sample page 4", doc[0].get_text())
            self.assertIn("Sample page 5", doc[1].get_text())
        finally:
            doc.close()

    def test_split_keeps_bookmarks_in_range(self):
        doc = make_doc(4)
        doc.set_toc([[1, "First", 1], [1, "Third", 3]])
        data = doc_bytes(doc)
        parts = split_pdf_bytes(data, [(2, 3)])
        out = fitz.open(stream=parts[0], filetype="pdf")
        try:
            self.assertEqual(out.get_toc(), [[1, "Third", 1]])
        finally:
            out.close()


class TestForm(unittest.TestCase):
    def test_list_and_fill(self):
        doc = make_form_doc()
        fields = list_form_fields(doc)
        self.assertEqual({f["name"] for f in fields}, {"name", "agree"})
        res = fill_form_fields(doc, {"name": "山田太郎", "agree": True,
                                     "missing_field": "x"})
        self.assertEqual(res["filled"], 2)
        self.assertEqual(res["missing"], ["missing_field"])
        fields = {f["name"]: f for f in list_form_fields(doc)}
        self.assertEqual(fields["name"]["value"], "山田太郎")
        self.assertTrue(fields["agree"]["value"])

    def test_flatten(self):
        doc = make_form_doc()
        fill_form_fields(doc, {"name": "山田太郎", "agree": True})
        res = flatten_form_fields(doc)
        self.assertEqual(res["flattened"], 2)
        self.assertEqual(list_form_fields(doc), [])
        self.assertIn("山田太郎", norm(doc[0].get_text()))

    def test_no_fields(self):
        doc = make_doc(1)
        self.assertEqual(list_form_fields(doc), [])
        self.assertEqual(flatten_form_fields(doc), {"flattened": 0})


class TestStamp(unittest.TestCase):
    def test_round_seal(self):
        doc = make_doc(1)
        rect = draw_round_seal(doc[0], fitz.Rect(400, 100, 480, 180),
                               "山田", date_text="2026.11.18")
        self.assertAlmostEqual(rect.width, 80, delta=1)
        # 描画が増えていること（円のパス）
        self.assertGreater(len(doc[0].get_drawings()), 0)
        self.assertIn("山田", norm(doc[0].get_text()))

    def test_text_stamp(self):
        doc = make_doc(1)
        draw_text_stamp(doc[0], fitz.Rect(400, 100, 470, 140), "済")
        self.assertIn("済", doc[0].get_text())
        self.assertGreater(len(doc[0].get_drawings()), 0)

    def test_apply_stamp_to_doc(self):
        doc = make_doc(2)
        apply_stamp_to_doc(doc, 2, "stamp", fitz.Rect(400, 100, 470, 140), text="確認")
        self.assertIn("確認", doc[1].get_text())
        self.assertNotIn("確認", doc[0].get_text())
        with self.assertRaises(ValueError):
            apply_stamp_to_doc(doc, 3, "stamp", fitz.Rect(0, 0, 50, 50), text="済")
        with self.assertRaises(ValueError):
            apply_stamp_to_doc(doc, 1, "unknown", fitz.Rect(0, 0, 50, 50), text="x")

    def test_empty_stamp_text_rejected(self):
        doc = make_doc(1)
        with self.assertRaises(ValueError):
            draw_text_stamp(doc[0], fitz.Rect(0, 0, 50, 50), "  ")


class TestBookmarks(unittest.TestCase):
    def test_add_rename_delete(self):
        doc = make_doc(5)
        self.assertEqual(get_bookmarks(doc), [])
        add_bookmark(doc, "はじめに", 1)
        add_bookmark(doc, "第2章", 3, level=2)
        marks = get_bookmarks(doc)
        self.assertEqual(len(marks), 2)
        self.assertEqual(marks[0], {"level": 1, "title": "はじめに", "page": 1})
        rename_bookmark(doc, 1, "第二章")
        self.assertEqual(get_bookmarks(doc)[1]["title"], "第二章")
        delete_bookmark(doc, 0)
        # 先頭になったしおりは階層1に正規化される
        self.assertEqual(get_bookmarks(doc), [
            {"level": 1, "title": "第二章", "page": 3}])

    def test_set_bookmarks_validation(self):
        doc = make_doc(3)
        with self.assertRaises(ValueError):
            set_bookmarks(doc, [{"level": 1, "title": "", "page": 1}])
        with self.assertRaises(ValueError):
            set_bookmarks(doc, [{"level": 1, "title": "x", "page": 9}])
        with self.assertRaises(IndexError):
            rename_bookmark(doc, 0, "x")
        with self.assertRaises(IndexError):
            delete_bookmark(doc, 0)

    def test_bookmarks_survive_save(self):
        doc = make_doc(3)
        add_bookmark(doc, "表紙", 1)
        data = doc_bytes(doc)
        out = fitz.open(stream=data, filetype="pdf")
        try:
            self.assertEqual(out.get_toc(), [[1, "表紙", 1]])
        finally:
            out.close()

    def test_apply_toc_to_bytes(self):
        data = doc_bytes(make_doc(2))
        out = apply_toc_to_pdf_bytes(data, [[1, "目次", 2]])
        doc = fitz.open(stream=out, filetype="pdf")
        try:
            self.assertEqual(doc.get_toc(), [[1, "目次", 2]])
        finally:
            doc.close()


class TestHeaderFooter(unittest.TestCase):
    def test_header_footer_page_number(self):
        doc = make_doc(3)
        res = apply_header_footer(
            doc, header="きなこPDF 試作", footer="社外秘",
            show_page_number=True, page_number_template="{page} / {total}")
        self.assertEqual(res, {"pages": 3})
        for i, page in enumerate(doc):
            text = norm(page.get_text())
            self.assertIn("きなこPDF", text)
            self.assertIn("社外秘", text)
            self.assertIn(f"{i + 1} / 3", text)

    def test_watermark(self):
        doc = make_doc(1)
        apply_header_footer(doc, watermark="DRAFT", watermark_fontsize=60)
        self.assertIn("DRAFT", doc[0].get_text())
        self.assertIn("Sample page 1", doc[0].get_text())

    def test_partial_pages(self):
        doc = make_doc(3)
        apply_header_footer(doc, header="H", page_indices=[1])
        self.assertNotIn("H", doc[0].get_text())
        self.assertIn("H", doc[1].get_text())
        self.assertNotIn("H", doc[2].get_text())

    def test_nothing_specified_rejected(self):
        doc = make_doc(1)
        with self.assertRaises(ValueError):
            apply_header_footer(doc)


if __name__ == "__main__":
    unittest.main()
