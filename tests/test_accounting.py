# -*- coding: utf-8 -*-
"""会計向け P1 機能（証憑一覧・電帳法リネーム・インボイス番号・
横断検索・PDF比較・個人情報墨消し候補・署名挿入）の単体テスト。"""
import csv
import io
import os
import tempfile
import unicodedata
import unittest

import fitz

from core.accounting_tools import (
    apply_pii_redactions,
    compare_pdf_texts,
    compute_invoice_check_digit,
    compute_my_number_check_digit,
    draw_freehand_signature,
    extract_amount_from_text,
    extract_date_from_text,
    extract_vendor_from_text,
    extract_voucher_fields,
    extract_voucher_fields_from_doc,
    find_invoice_numbers,
    find_invoice_numbers_in_doc,
    find_pii_candidates,
    insert_signature_image,
    mm_to_pt,
    rename_pdf_file,
    search_pdfs_in_folder,
    search_text_in_doc,
    suggest_densho_filename,
    validate_invoice_number,
    validate_my_number,
    voucher_rows_to_csv,
    voucher_rows_to_xlsx,
)


def norm(text):
    return unicodedata.normalize("NFKC", text).replace("\xa0", " ")


def make_doc_with_lines(lines, pages=1):
    """各行を日本語フォントで書き込んだ PDF を作る。"""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        y = 80
        for line in lines:
            page.insert_text((72, y), line, fontsize=12, fontname="japan")
            y += 22
    return doc


def make_invoice_pdf():
    return make_doc_with_lines([
        "請求書",
        "株式会社きなこ商事",
        "2026年11月10日",
        "合計金額 ¥12,345円",
        "登録番号 T8700110005901",
    ])


def make_signature_png_bytes(w=120, h=60):
    """zlib/struct のみで作る最小の PNG（署名画像の代わり）。"""
    import struct
    import zlib

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b""
    for _y in range(h):
        raw += b"\x00" + b"\x00\x00\x00" * w
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


class TestVoucherExtraction(unittest.TestCase):
    def test_date_japanese(self):
        self.assertEqual(extract_date_from_text("発行日 2026年11月18日"), "2026-11-18")
        self.assertEqual(extract_date_from_text("2026/11/18"), "2026-11-18")
        self.assertEqual(extract_date_from_text("2026-11-18"), "2026-11-18")
        self.assertEqual(extract_date_from_text("令和8年11月18日"), "2026-11-18")
        self.assertEqual(extract_date_from_text("日付なし"), "")

    def test_date_invalid_rejected(self):
        # 存在しない日付は採用しない
        self.assertEqual(extract_date_from_text("2026年13月40日"), "")

    def test_amount_labeled(self):
        self.assertEqual(extract_amount_from_text("合計金額 ¥12,345円"), 12345)
        self.assertEqual(extract_amount_from_text("ご請求金額 98,765円"), 98765)
        self.assertEqual(extract_amount_from_text("小計 1,000円 合計 5,000円"), 5000)

    def test_amount_yen_only(self):
        self.assertEqual(extract_amount_from_text("お支払い ¥3,300"), 3300)
        self.assertEqual(extract_amount_from_text("金額の記載なし"), None)

    def test_vendor(self):
        self.assertEqual(extract_vendor_from_text("株式会社きなこ商事\n請求書"), "株式会社きなこ商事")
        self.assertEqual(extract_vendor_from_text("きなこ商事株式会社 御中"), "きなこ商事株式会社")
        self.assertEqual(extract_vendor_from_text("取引先の記載なし"), "")

    def test_voucher_fields_from_doc(self):
        doc = make_invoice_pdf()
        fields = extract_voucher_fields_from_doc(doc)
        self.assertEqual(fields["date"], "2026-11-10")
        self.assertEqual(fields["vendor"], "株式会社きなこ商事")
        self.assertEqual(fields["amount"], 12345)
        self.assertEqual(fields["invoice_no"], "T8700110005901")

    def test_voucher_fields_empty_doc(self):
        doc = fitz.open()
        doc.new_page(width=595, height=842)
        fields = extract_voucher_fields_from_doc(doc)
        self.assertEqual(fields, {"date": "", "vendor": "", "amount": None,
                                  "invoice_no": ""})

    def test_csv_output(self):
        rows = [{"file": "a.pdf", "date": "2026-11-10", "vendor": "株式会社きなこ商事",
                 "amount": 12345, "invoice_no": "T8700110005901", "note": ""}]
        text = voucher_rows_to_csv(rows)
        parsed = list(csv.reader(io.StringIO(text)))
        self.assertEqual(parsed[0], ["ファイル名", "日付", "取引先", "金額", "インボイス番号", "備考"])
        self.assertEqual(parsed[1], ["a.pdf", "2026-11-10", "株式会社きなこ商事",
                                     "12345", "T8700110005901", ""])

    def test_xlsx_output(self):
        rows = [{"file": "a.pdf", "date": "2026-11-10", "vendor": "株式会社きなこ商事",
                 "amount": 12345, "invoice_no": "T8700110005901", "note": "要確認"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.xlsx")
            voucher_rows_to_xlsx(rows, path)
            self.assertTrue(os.path.getsize(path) > 0)
            import zipfile
            with zipfile.ZipFile(path) as zf:
                self.assertIn("xl/workbook.xml", zf.namelist())
                shared = zf.read("xl/sharedStrings.xml").decode("utf-8")
                self.assertIn("株式会社きなこ商事", shared)


class TestDenshoRename(unittest.TestCase):
    def test_suggest_filename(self):
        self.assertEqual(
            suggest_densho_filename("2026-11-10", "株式会社きなこ商事", 12345),
            "2026-11-10_株式会社きなこ商事_12345.pdf")

    def test_suggest_filename_slash_date(self):
        self.assertEqual(
            suggest_densho_filename("2026/11/10", "ABC商事", "9,800円"),
            "2026-11-10_ABC商事_9800.pdf")

    def test_suggest_filename_missing_parts(self):
        name = suggest_densho_filename("", "ABC商事", None)
        self.assertEqual(name, "ABC商事.pdf")
        name = suggest_densho_filename("", "", None, original_name="請求書.pdf")
        self.assertEqual(name, "請求書.pdf")

    def test_suggest_filename_illegal_chars_removed(self):
        name = suggest_densho_filename("2026-11-10", "A/B商事：テスト", 100)
        self.assertNotIn("/", name)
        self.assertNotIn("：", name)
        self.assertTrue(name.endswith(".pdf"))

    def test_rename_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "old.pdf")
            with open(src, "wb") as f:
                f.write(b"%PDF-1.4 fake")
            dest = rename_pdf_file(src, "2026-11-10_ABC商事_100.pdf")
            self.assertTrue(os.path.isfile(dest))
            self.assertFalse(os.path.exists(src))

    def test_rename_collision_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "old.pdf")
            other = os.path.join(tmp, "2026-11-10_ABC商事_100.pdf")
            for p in (src, other):
                with open(p, "wb") as f:
                    f.write(b"x")
            with self.assertRaises(ValueError):
                rename_pdf_file(src, "2026-11-10_ABC商事_100.pdf")

    def test_rename_missing_rejected(self):
        with self.assertRaises(ValueError):
            rename_pdf_file("/nonexistent/no.pdf", "a.pdf")


class TestInvoiceNumber(unittest.TestCase):
    def test_check_digit_official_example(self):
        # 国税庁の公式計算例: 基礎番号 700110005901 → 検査用数字 8
        self.assertEqual(compute_invoice_check_digit("700110005901"), 8)

    def test_check_digit_invalid_base(self):
        with self.assertRaises(ValueError):
            compute_invoice_check_digit("123")
        with self.assertRaises(ValueError):
            compute_invoice_check_digit("70011000590A")

    def test_validate_ok(self):
        res = validate_invoice_number("T8700110005901")
        self.assertTrue(res["valid"])
        self.assertEqual(res["expected_check"], 8)
        self.assertEqual(res["given_check"], 8)

    def test_validate_wrong_check(self):
        res = validate_invoice_number("T1700110005901")
        self.assertFalse(res["valid"])
        self.assertEqual(res["expected_check"], 8)
        self.assertEqual(res["given_check"], 1)

    def test_validate_bad_format(self):
        for bad in ("", "T123", "T870011000590", "8700110005901", "T870011000590A"):
            res = validate_invoice_number(bad)
            self.assertFalse(res["valid"], msg=bad)

    def test_validate_fullwidth_and_hyphen(self):
        res = validate_invoice_number("Ｔ８７００１１０００５９０１")
        self.assertTrue(res["valid"])

    def test_find_in_text(self):
        found = find_invoice_numbers("登録番号：T8700110005901 です。")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["valid"])
        found_bad = find_invoice_numbers("T1700110005901")
        self.assertEqual(len(found_bad), 1)
        self.assertFalse(found_bad[0]["valid"])

    def test_find_in_doc(self):
        doc = make_invoice_pdf()
        found = find_invoice_numbers_in_doc(doc)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["page"], 1)
        self.assertTrue(found[0]["valid"])


class TestCrossPdfSearch(unittest.TestCase):
    def test_search_in_doc(self):
        doc = make_doc_with_lines(["請求書", "合計金額 5,000円", "備考なし"], pages=2)
        hits = search_text_in_doc(doc, "合計金額")
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["page"], 1)
        self.assertIn("合計金額", hits[0]["snippet"])
        self.assertEqual(search_text_in_doc(doc, "存在しない語"), [])

    def test_search_in_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, lines in (("a.pdf", ["請求書", "きなこ商事"]),
                                ("b.pdf", ["領収書", "きなこ商事"]),
                                ("c.pdf", ["見積書", "他社"])):
                doc = make_doc_with_lines(lines)
                doc.save(os.path.join(tmp, name))
                doc.close()
            res = search_pdfs_in_folder(tmp, "きなこ商事")
            self.assertEqual(res["files_searched"], 3)
            self.assertEqual(res["files_skipped"], [])
            found_files = {r["file"] for r in res["results"]}
            self.assertEqual(found_files, {"a.pdf", "b.pdf"})
            with self.assertRaises(ValueError):
                search_pdfs_in_folder(tmp, "  ")
            with self.assertRaises(ValueError):
                search_pdfs_in_folder(os.path.join(tmp, "nope"), "x")


class TestCompare(unittest.TestCase):
    def test_same(self):
        a = make_doc_with_lines(["同じ行", "二行目"])
        b = make_doc_with_lines(["同じ行", "二行目"])
        res = compare_pdf_texts(a, b)
        self.assertEqual(res["summary"]["same_pages"], 1)
        self.assertEqual(res["pages"][0]["status"], "same")

    def test_different(self):
        a = make_doc_with_lines(["同じ行", "旧い行"])
        b = make_doc_with_lines(["同じ行", "新しい行"])
        res = compare_pdf_texts(a, b)
        self.assertEqual(res["summary"]["different_pages"], 1)
        page = res["pages"][0]
        self.assertEqual(page["status"], "different")
        self.assertIn("旧い行", page["removed"])
        self.assertIn("新しい行", page["added"])
        self.assertIn("旧い行", page["diff"])

    def test_page_count_diff(self):
        a = make_doc_with_lines(["1ページ目"], pages=1)
        b = make_doc_with_lines(["1ページ目"], pages=2)
        res = compare_pdf_texts(a, b)
        self.assertEqual(res["summary"]["only_b_pages"], 1)
        self.assertEqual(res["pages"][1]["status"], "only_b")


class TestPii(unittest.TestCase):
    def test_my_number_check_digit(self):
        # 11桁から検査用数字を計算し、12桁にして検証が通ること・改ざんは通らないこと
        base = "12345678901"
        check = compute_my_number_check_digit(base)
        self.assertTrue(validate_my_number(base + str(check))["valid"])
        wrong = (check + 1) % 10
        self.assertFalse(validate_my_number(base + str(wrong))["valid"])
        with self.assertRaises(ValueError):
            compute_my_number_check_digit("123")

    def test_find_candidates(self):
        base = "12345678901"
        check = compute_my_number_check_digit(base)
        my_number = base + str(check)
        doc = make_doc_with_lines([
            "連絡先 tanaka@example.co.jp まで",
            "電話 03-1234-5678 携帯 09012345678",
            "口座番号 1234567",
            f"マイナンバー {my_number}",
        ])
        cands = find_pii_candidates(doc)
        by_kind = {}
        for c in cands:
            by_kind.setdefault(c["kind"], []).append(c["text"])
        self.assertIn("tanaka@example.co.jp", by_kind.get("email", []))
        self.assertIn("03-1234-5678", by_kind.get("phone", []))
        self.assertIn("09012345678", by_kind.get("phone", []))
        self.assertIn("1234567", by_kind.get("bank_account", []))
        self.assertIn(my_number, by_kind.get("my_number", []))
        # 全候補に墨消し用の矩形があること
        for c in cands:
            self.assertTrue(c["rects"], msg=c)

    def test_bare_7digit_not_flagged(self):
        # ラベルのない7桁だけでは口座番号と誤検出しないこと
        doc = make_doc_with_lines(["金額 1234567円 です"])
        cands = find_pii_candidates(doc)
        self.assertEqual([c for c in cands if c["kind"] == "bank_account"], [])

    def test_apply_redactions_only_selected(self):
        doc = make_doc_with_lines([
            "tanaka@example.co.jp",
            "残す文字",
        ])
        cands = find_pii_candidates(doc)
        self.assertEqual(len(cands), 1)
        res = apply_pii_redactions(doc, [cands[0]])
        self.assertEqual(res["redacted"], 1)
        text = norm(doc[0].get_text())
        self.assertNotIn("tanaka@example.co.jp", text)
        self.assertIn("残す文字", text)

    def test_apply_empty_selection(self):
        doc = make_doc_with_lines(["何もない"])
        self.assertEqual(apply_pii_redactions(doc, []), {"redacted": 0, "pages": 0})


class TestSignature(unittest.TestCase):
    def test_insert_image(self):
        doc = make_doc_with_lines(["署名欄"], pages=1)
        rect = insert_signature_image(
            doc, 1, make_signature_png_bytes(),
            fitz.Rect(400, 700, 520, 760))
        self.assertAlmostEqual(rect.width, 120, delta=1)
        self.assertEqual(len(doc[0].get_images()), 1)

    def test_insert_image_invalid(self):
        doc = make_doc_with_lines(["署名欄"], pages=1)
        with self.assertRaises(ValueError):
            insert_signature_image(doc, 1, b"not an image", fitz.Rect(0, 0, 50, 50))
        with self.assertRaises(ValueError):
            insert_signature_image(doc, 5, make_signature_png_bytes(),
                                   fitz.Rect(0, 0, 50, 50))

    def test_freehand_signature(self):
        doc = make_doc_with_lines(["署名欄"], pages=1)
        n = draw_freehand_signature(
            doc[0],
            [(100, 700), (120, 690), (140, 700), None, (100, 720), (140, 720)],
            width=2.0)
        self.assertEqual(n, 2)
        self.assertGreater(len(doc[0].get_drawings()), 0)

    def test_freehand_signature_too_few_points(self):
        doc = make_doc_with_lines(["署名欄"], pages=1)
        with self.assertRaises(ValueError):
            draw_freehand_signature(doc[0], [(100, 700)])

    def test_mm_to_pt(self):
        self.assertAlmostEqual(mm_to_pt(25.4), 72.0, places=3)


if __name__ == "__main__":
    unittest.main()
