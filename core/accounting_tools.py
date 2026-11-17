# -*- coding: utf-8 -*-
"""会計向け P1 機能群（feature/trial-multi の第2バッチ）。

純粋関数中心のモジュール。UI からはここを呼ぶだけにする。
新しいライブラリは使わない（PyMuPDF + Python 標準ライブラリのみ）。
OCR は Windows 標準機能のみで、ここでは文字層からの抽出を主とし、
OCR 自体は既存の core/ocr_cleaner.py（Windows 標準 OCR）経由で UI 側が行う。

対象機能:
  8. 証憑 OCR→一覧 Excel/CSV（半自動・必ずユーザー確認をはさむ）
  9. 電帳法自動リネーム（提案→ユーザー確認後にリネーム）
 10. インボイス番号（T+13桁）の検出・形式チェック
 11. 複数 PDF 横断検索
 12. 2つの PDF のテキスト比較
 13. 個人情報パターンの一括墨消し候補（自動では消さない）
 14. 手書き署名・署名画像挿入
"""
import csv
import datetime
import difflib
import io
import os
import re
import unicodedata

import fitz

# ============================================================
# 共通ヘルパー
# ============================================================

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _norm(text: str) -> str:
    """全角数字・全角スペースなどを半角に正規化する。"""
    if not text:
        return ""
    return unicodedata.normalize("NFKC", str(text))


def _to_half_digits(text: str) -> str:
    return str(text).translate(_FULLWIDTH_DIGITS)


# ============================================================
# 8. 証憑（請求書・領収書）からの項目抽出
# ============================================================

_ERA_BASE = {"令和": 2018, "平成": 1988, "昭和": 1925, "大正": 1911, "明治": 1867}

_DATE_PATTERNS = [
    # 2026年11月18日 / 2026/11/18 / 2026-11-18 / 2026.11.18
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    re.compile(r"(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})"),
    # 令和8年11月18日 / 平成30年4月1日 など
    re.compile(r"(令和|平成|昭和|大正|明治)\s*(元|\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
]


def extract_date_from_text(text: str):
    """テキストから日付を1つ抽出する。見つかれば "YYYY-MM-DD"、なければ ""。

    和暦は西暦に換算する（令和元年=2019年）。存在しない日付は採用しない。
    """
    if not text:
        return ""
    s = _norm(text)
    for pat in _DATE_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        groups = m.groups()
        try:
            if groups[0] in _ERA_BASE:
                era = groups[0]
                year_num = 1 if groups[1] == "元" else int(groups[1])
                year = _ERA_BASE[era] + year_num
                month, day = int(groups[2]), int(groups[3])
            else:
                year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
            d = datetime.date(year, month, day)
            return d.isoformat()
        except ValueError:
            continue
    return ""


_AMOUNT_LABEL_RE = re.compile(
    r"(合計金額|ご請求金額|請求金額|お支払金額|お支払い金額|領収金額|"
    r"お買上金額|合計|小計|税込合計|金額|ご利用金額|請求合計)"
    r"[^0-9¥￥]{0,12}?[¥￥]?\s*([0-9][0-9,]{0,14})\s*(?:円)?"
)
_AMOUNT_YEN_RE = re.compile(r"[¥￥]\s*([0-9][0-9,]{0,14})\s*(?:円)?")
_AMOUNT_YEN_SUFFIX_RE = re.compile(r"([0-9][0-9,]{0,14})\s*円")


def _amount_to_int(num_str: str):
    try:
        value = int(_to_half_digits(num_str).replace(",", ""))
    except ValueError:
        return None
    if value <= 0 or value > 999_999_999_999:
        return None
    return value


def extract_amount_from_text(text: str):
    """テキストから金額（円）を抽出する。見つからなければ None。

    「合計」「請求金額」などのラベル付き金額を優先し、
    なければ ¥付き金額の中で最大のものを採用する。
    """
    if not text:
        return None
    s = _norm(text)
    labeled = []
    for m in _AMOUNT_LABEL_RE.finditer(s):
        value = _amount_to_int(m.group(2))
        if value is not None:
            # 「合計」系ラベルを最優先する
            priority = 0 if "合計" in m.group(1) or "請求" in m.group(1) else 1
            labeled.append((priority, value))
    if labeled:
        labeled.sort()
        return labeled[0][1]
    yen_amounts = [
        v for v in (_amount_to_int(m.group(1)) for m in _AMOUNT_YEN_RE.finditer(s))
        if v is not None
    ]
    if yen_amounts:
        return max(yen_amounts)
    suffix_amounts = [
        v for v in (_amount_to_int(m.group(1)) for m in _AMOUNT_YEN_SUFFIX_RE.finditer(s))
        if v is not None
    ]
    if suffix_amounts:
        return max(suffix_amounts)
    return None


_VENDOR_SUFFIX_RE = re.compile(
    r"([^\s\n]{1,40}(?:株式会社|有限会社|合同会社|合名会社|合資会社|"
    r"一般社団法人|公益社団法人|一般財団法人|公益財団法人|"
    r"医療法人|社会福祉法人|学校法人|宗教法人|協同組合|ＮＰＯ法人|NPO法人))"
)
_VENDOR_PREFIX_RE = re.compile(
    r"((?:株式会社|有限会社|合同会社|合名会社|合資会社|"
    r"一般社団法人|公益社団法人|一般財団法人|公益財団法人)[^\s\n]{1,40})"
)


def extract_vendor_from_text(text: str) -> str:
    """テキストから取引先（会社名らしき文字列）を抽出する。なければ ""。"""
    if not text:
        return ""
    s = _norm(text)
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _VENDOR_SUFFIX_RE.search(line) or _VENDOR_PREFIX_RE.search(line)
        if m:
            name = m.group(1).strip().strip("、。：:・")
            # 「御中」「様」などの宛名を除く
            name = re.sub(r"(御中|様|殿)$", "", name).strip()
            if name:
                return name[:60]
    return ""


def extract_voucher_fields(text: str) -> dict:
    """証憑テキストから日付・取引先・金額・インボイス番号の候補を抽出する。

    戻り値: {"date": "YYYY-MM-DD" or "", "vendor": "", "amount": int or None,
             "invoice_no": "T+13桁" or ""}
    いずれも「候補」であり、出力前にユーザーの確認・修正が必須の設計。
    """
    if not text:
        text = ""
    s = _norm(text)
    invoice = find_invoice_numbers(s)
    return {
        "date": extract_date_from_text(s),
        "vendor": extract_vendor_from_text(s),
        "amount": extract_amount_from_text(s),
        "invoice_no": invoice[0]["number"] if invoice else "",
    }


def extract_voucher_fields_from_doc(doc, page_limit: int = 3) -> dict:
    """fitz 文書の先頭 page_limit ページの文字層から証憑項目を抽出する。

    文字層がない（スキャン PDF など）場合は空欄を返す。
    その場合は UI 側で Windows 標準 OCR（core/ocr_cleaner.py）を
    使ってテキスト化してから extract_voucher_fields() に渡す。
    """
    parts = []
    for page in list(doc)[: max(0, page_limit)]:
        try:
            parts.append(page.get_text("text"))
        except Exception:
            pass
    return extract_voucher_fields("\n".join(parts))


def voucher_rows_to_csv(rows) -> str:
    """証憑一覧 rows（dict のリスト）を CSV 文字列にする。

    列: ファイル名, 日付, 取引先, 金額, インボイス番号, 備考
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["ファイル名", "日付", "取引先", "金額", "インボイス番号", "備考"])
    for r in rows:
        amount = r.get("amount")
        writer.writerow([
            r.get("file", ""),
            r.get("date", ""),
            r.get("vendor", ""),
            "" if amount is None else int(amount),
            r.get("invoice_no", ""),
            r.get("note", ""),
        ])
    return buf.getvalue()


def voucher_rows_to_xlsx(rows, output_path: str) -> None:
    """証憑一覧 rows を Excel（.xlsx）に書き出す。

    既存の core/excel_exporter.py の仕組みを流用する。
    """
    from core.excel_exporter import write_xlsx_from_sheets

    header = ["ファイル名", "日付", "取引先", "金額", "インボイス番号", "備考"]
    sheet_rows = [header]
    for r in rows:
        amount = r.get("amount")
        sheet_rows.append([
            str(r.get("file", "")),
            str(r.get("date", "")),
            str(r.get("vendor", "")),
            "" if amount is None else str(int(amount)),
            str(r.get("invoice_no", "")),
            str(r.get("note", "")),
        ])
    write_xlsx_from_sheets([("証憑一覧", sheet_rows)], output_path,
                           convert_numbers=True, bold_first_row=True)


# ============================================================
# 9. 電帳法自動リネーム（提案→ユーザー確認後に実行）
# ============================================================

_INVALID_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename_component(text: str, max_len: int = 60) -> str:
    """ファイル名に使えない文字を除去・置換し、空白を詰める。"""
    s = _INVALID_FILENAME_CHARS_RE.sub("", _norm(text or "")).strip()
    s = re.sub(r"\s+", "", s)
    s = s.strip(" .")
    return s[:max_len]


def suggest_densho_filename(date: str = "", vendor: str = "",
                            amount=None, original_name: str = "") -> str:
    """電帳法リネームのファイル名を提案する。

    形式: YYYY-MM-DD_取引先_金額.pdf
    日付・取引先・金額のいずれかが欠けている場合は、欠けた部分を
    「日付なし」「取引先なし」「金額なし」または元のファイル名で補う
    のではなく、その部分を空けたまま提案し、UI で修正を促す。
    （欠けたままではファイル名として不完全なため、呼び出し側で
    欠損を補うか、リネーム対象から外すこと）
    """
    date_part = (date or "").strip()
    if date_part:
        # YYYY/MM/DD などの入力も YYYY-MM-DD に整える
        try:
            parsed = datetime.date.fromisoformat(date_part)
            date_part = parsed.isoformat()
        except ValueError:
            m = re.match(r"(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})", date_part)
            if m:
                try:
                    date_part = datetime.date(
                        int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
                except ValueError:
                    date_part = ""
            else:
                date_part = ""
    vendor_part = sanitize_filename_component(vendor)
    amount_part = ""
    if amount is not None and str(amount).strip() != "":
        value = _amount_to_int(str(amount).replace("円", ""))
        if value is not None:
            amount_part = str(value)
    stem = "_".join(part for part in (date_part, vendor_part, amount_part) if part)
    if not stem:
        stem = sanitize_filename_component(
            os.path.splitext(os.path.basename(original_name or ""))[0]) or "名称未設定"
    return stem + ".pdf"


def rename_pdf_file(src_path: str, new_filename: str,
                    target_dir: str = None) -> str:
    """PDF ファイルを新しいファイル名にリネームする（確認済みのものだけ呼ぶ）。

    戻り値: リネーム後の絶対パス。
    同名ファイルが既にある場合や、元ファイルがない場合は ValueError。
    """
    if not src_path or not os.path.isfile(src_path):
        raise ValueError(f"リネーム元のファイルが見つかりません: {src_path}")
    new_filename = sanitize_filename_component(
        os.path.splitext(new_filename or "")[0]) + ".pdf"
    if not new_filename or new_filename == ".pdf":
        raise ValueError("新しいファイル名が空です。")
    dest_dir = os.path.abspath(target_dir) if target_dir else os.path.dirname(
        os.path.abspath(src_path))
    dest = os.path.join(dest_dir, new_filename)
    if os.path.abspath(src_path) == dest:
        return dest
    if os.path.exists(dest):
        raise ValueError(f"同名のファイルが既にあります: {dest}")
    os.rename(src_path, dest)
    return dest


# ============================================================
# 10. インボイス番号（T+13桁）の検出・形式チェック
# ============================================================

_INVOICE_RE = re.compile(r"[TtＴ][\s\-‐]*([0-9０-９][0-9０-９\-‐\s]{11,17}[0-9０-９])")


def compute_invoice_check_digit(base12: str) -> int:
    """法人番号（13桁）の検査用数字を計算する（国税庁の公式算式）。

    base12: 13桁のうち先頭1桁を除いた12桁（会社法人等番号）。
    算式: 基礎番号の最下位（右端）を1桁目として数え、奇数桁は1倍、
    偶数桁は2倍して合計し、合計を9で割った余りを9から引いた数が検査用数字。
    公式の計算例: 基礎番号 700110005901 → 検査用数字 8 → 法人番号 8700110005901。
    戻り値は 1〜9（9から余りを引くため 0 にはならない）。
    """
    digits = _to_half_digits(str(base12)).strip()
    if not re.fullmatch(r"\d{12}", digits):
        raise ValueError("基礎番号は12桁の数字で指定してください。")
    total = 0
    for i, ch in enumerate(reversed(digits)):
        weight = 1 if i % 2 == 0 else 2
        total += int(ch) * weight
    return 9 - (total % 9)


def validate_invoice_number(number: str) -> dict:
    """インボイス登録番号（T+13桁）の形式チェックを行う。

    戻り値: {"number": "T..." or "", "digits": "13桁" or "",
             "valid": bool, "reason": "判定の理由",
             "expected_check": int or None, "given_check": int or None}
    形式が正しくても、その番号の法人が実在するかの確認はできない
    （国税庁の公表サイトでの照会が必要）。その旨も reason に含める。
    """
    raw = _norm(number or "").strip().upper().replace(" ", "").replace("-", "")
    m = re.fullmatch(r"T(\d{13})", raw)
    if not m:
        return {
            "number": raw if raw.startswith("T") else "",
            "digits": "",
            "valid": False,
            "reason": "T+13桁の形式ではありません（例: T1234567890123）。",
            "expected_check": None,
            "given_check": None,
        }
    digits = m.group(1)
    given = int(digits[0])
    try:
        expected = compute_invoice_check_digit(digits[1:])
    except ValueError:
        return {
            "number": "T" + digits, "digits": digits, "valid": False,
            "reason": "基礎番号（後ろ12桁）が数字12桁ではありません。",
            "expected_check": None, "given_check": given,
        }
    if given == expected:
        return {
            "number": "T" + digits, "digits": digits, "valid": True,
            "reason": "形式・検査用数字ともに正しいです。ただし、実在する登録番号かどうかは"
                      "国税庁の公表サイトでの照会が必要です。",
            "expected_check": expected, "given_check": given,
        }
    return {
        "number": "T" + digits, "digits": digits, "valid": False,
        "reason": f"検査用数字が一致しません（先頭は {given} ですが、計算上は {expected} です）。"
                  "入力間違いの可能性があります。",
        "expected_check": expected, "given_check": given,
    }


def find_invoice_numbers(text: str) -> list:
    """テキスト中の T+13桁の登録番号をすべて検出し、形式チェック結果を付けて返す。

    戻り値: [{"number", "page_text": 見つかった表記, "valid", "reason",
              "expected_check", "given_check"}]（出現順・重複あり）
    """
    if not text:
        return []
    s = _norm(text)
    results = []
    for m in re.finditer(r"[TtＴ][\s\-‐]*[0-9０-９][0-9０-９\-‐\s]{11,17}[0-9０-９]", s):
        raw = m.group(0)
        digits_only = _to_half_digits(re.sub(r"[^0-9０-９]", "", raw))
        if len(digits_only) != 13:
            continue
        checked = validate_invoice_number("T" + digits_only)
        checked["found_text"] = raw.strip()
        results.append(checked)
    return results


def find_invoice_numbers_in_doc(doc) -> list:
    """文書全体からインボイス番号を検出する。1件ごとに page（1始まり）を付ける。"""
    results = []
    for page_idx, page in enumerate(doc):
        try:
            text = page.get_text("text")
        except Exception:
            continue
        for item in find_invoice_numbers(text):
            item = dict(item)
            item["page"] = page_idx + 1
            results.append(item)
    return results


# ============================================================
# 11. 複数 PDF 横断検索
# ============================================================

def search_text_in_doc(doc, keyword: str, max_hits_per_doc: int = 50) -> list:
    """1文書内でキーワードを検索し、[{page, snippet}] を返す。"""
    if not keyword:
        return []
    key = _norm(keyword)
    key_lower = key.lower()
    hits = []
    for page_idx, page in enumerate(doc):
        try:
            text = page.get_text("text")
        except Exception:
            continue
        text_norm = _norm(text)
        start = 0
        while len(hits) < max_hits_per_doc:
            pos = text_norm.lower().find(key_lower, start)
            if pos < 0:
                break
            snippet = text_norm[max(0, pos - 30):pos + len(key) + 30].replace("\n", " ")
            hits.append({"page": page_idx + 1, "snippet": snippet.strip()})
            start = pos + max(1, len(key))
    return hits


def search_pdfs_in_folder(folder: str, keyword: str,
                          max_hits_per_file: int = 20) -> dict:
    """フォルダ内の PDF（直下のみ）を横断検索する。

    戻り値: {"results": [{"file", "path", "page", "snippet"}], "files_searched": int,
             "files_skipped": [{"file", "reason"}]}
    暗号化などで開けない PDF はスキップして files_skipped に記録する。
    """
    if not keyword or not keyword.strip():
        raise ValueError("検索キーワードが空です。")
    if not folder or not os.path.isdir(folder):
        raise ValueError(f"フォルダが見つかりません: {folder}")
    results = []
    skipped = []
    searched = 0
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".pdf"):
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            doc = fitz.open(path)
        except Exception as e:
            skipped.append({"file": name, "reason": f"開けませんでした: {e}"})
            continue
        try:
            if doc.needs_pass:
                skipped.append({"file": name, "reason": "パスワード付きのため開けませんでした。"})
                continue
            searched += 1
            for hit in search_text_in_doc(doc, keyword, max_hits_per_doc=max_hits_per_file):
                results.append({
                    "file": name, "path": path,
                    "page": hit["page"], "snippet": hit["snippet"],
                })
        except Exception as e:
            skipped.append({"file": name, "reason": f"検索中にエラー: {e}"})
        finally:
            doc.close()
    return {"results": results, "files_searched": searched, "files_skipped": skipped}


# ============================================================
# 12. 2つの PDF のテキスト比較
# ============================================================

def compare_pdf_texts(doc_a, doc_b, max_diff_lines_per_page: int = 200) -> dict:
    """2つの文書のページごとのテキストを比較する。

    戻り値: {"pages": [{"page", "status", "added", "removed", "diff"}],
             "summary": {"total_pages": int, "same_pages": int,
                         "different_pages": int, "only_a_pages": int,
                         "only_b_pages": int}}
    status: "same"（同じ）/ "different"（違う）/ "only_a"（Aだけにあるページ）
            / "only_b"（Bだけにあるページ）
    diff は unified diff 形式の文字列（先頭行のみ）。
    """
    pages_a = []
    pages_b = []
    for doc, out in ((doc_a, pages_a), (doc_b, pages_b)):
        for page in doc:
            try:
                out.append(page.get_text("text"))
            except Exception:
                out.append("")
    total = max(len(pages_a), len(pages_b))
    pages = []
    same = different = only_a = only_b = 0
    for idx in range(total):
        page_no = idx + 1
        if idx >= len(pages_a):
            only_b += 1
            lines_b = pages_b[idx].splitlines()
            pages.append({
                "page": page_no, "status": "only_b",
                "added": lines_b[:max_diff_lines_per_page], "removed": [],
                "diff": "\n".join(f"+ {line}" for line in lines_b[:max_diff_lines_per_page]),
            })
            continue
        if idx >= len(pages_b):
            only_a += 1
            lines_a = pages_a[idx].splitlines()
            pages.append({
                "page": page_no, "status": "only_a",
                "added": [], "removed": lines_a[:max_diff_lines_per_page],
                "diff": "\n".join(f"- {line}" for line in lines_a[:max_diff_lines_per_page]),
            })
            continue
        lines_a = pages_a[idx].splitlines()
        lines_b = pages_b[idx].splitlines()
        if lines_a == lines_b:
            same += 1
            pages.append({"page": page_no, "status": "same",
                          "added": [], "removed": [], "diff": ""})
            continue
        different += 1
        diff_lines = list(difflib.unified_diff(
            lines_a, lines_b, lineterm="",
            fromfile=f"A {page_no}ページ", tofile=f"B {page_no}ページ"))
        added = [line[1:] for line in diff_lines
                 if line.startswith("+") and not line.startswith("+++")]
        removed = [line[1:] for line in diff_lines
                   if line.startswith("-") and not line.startswith("---")]
        pages.append({
            "page": page_no, "status": "different",
            "added": added[:max_diff_lines_per_page],
            "removed": removed[:max_diff_lines_per_page],
            "diff": "\n".join(diff_lines[:max_diff_lines_per_page]),
        })
    return {
        "pages": pages,
        "summary": {
            "total_pages": total, "same_pages": same,
            "different_pages": different,
            "only_a_pages": only_a, "only_b_pages": only_b,
        },
    }


# ============================================================
# 13. 個人情報パターンの一括墨消し候補
# ============================================================

PII_KINDS = {
    "email": "メールアドレス",
    "phone": "電話番号",
    "my_number": "マイナンバー（個人番号）",
    "bank_account": "口座番号",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_HYPHEN_RE = re.compile(r"0\d{1,4}-\d{1,4}-\d{4}")
_PHONE_MOBILE_RE = re.compile(r"(?<!\d)(?:090|080|070|050)\d{8}(?!\d)")
# マイナンバーは12桁。単独の12桁は他の番号と紛らわしいため、
# 「マイナンバー」「個人番号」の表示が近くにある場合か、
# 検査用数字（チェックディジット）が一致する場合にだけ候補にする。
_MY_NUMBER_RE = re.compile(r"(?<!\d)(\d{12})(?!\d)")
_MY_NUMBER_LABEL_RE = re.compile(r"(マイナンバー|個人番号)")
# 口座番号は7桁が一般的。ラベル付きのものだけ候補にする（単独の7桁は誤検出が多いため）。
_BANK_ACCOUNT_RE = re.compile(r"(?:口座番号|普通預金|当座預金|口座)[：:\s]*(\d{7})")


def compute_my_number_check_digit(base11: str) -> int:
    """マイナンバー（12桁）の検査用数字を計算する（行政手続における特定の個人を
    識別するための番号の利用等に関する法律の算式）。

    base11: 検査用数字を除いた11桁。右端から 2,3,4,5,6,7,2,3,4,5,6 を掛けて
    合計し、11で割った余りが0または1なら検査用数字は0、2以上なら11から余りを引く。
    """
    digits = _to_half_digits(str(base11)).strip()
    if not re.fullmatch(r"\d{11}", digits):
        raise ValueError("基礎番号は11桁の数字で指定してください。")
    weights = [2, 3, 4, 5, 6, 7, 2, 3, 4, 5, 6]
    total = sum(int(ch) * w for ch, w in zip(reversed(digits), weights))
    remainder = total % 11
    if remainder <= 1:
        return 0
    return 11 - remainder


def validate_my_number(number: str) -> dict:
    """12桁の数字がマイナンバーの検査用数字として正しいか確認する。"""
    digits = _to_half_digits(str(number or "")).strip()
    digits = re.sub(r"[^0-9]", "", digits)
    if not re.fullmatch(r"\d{12}", digits):
        return {"number": digits, "valid": False,
                "reason": "12桁の数字ではありません。"}
    expected = compute_my_number_check_digit(digits[:11])
    given = int(digits[11])
    if given == expected:
        return {"number": digits, "valid": True,
                "reason": "検査用数字が一致します。ただし、実在の番号かどうかは分かりません。"}
    return {"number": digits, "valid": False,
            "reason": f"検査用数字が一致しません（末尾は {given} ですが、計算上は {expected} です）。"}


def _search_rects(page, text: str):
    """ページ内で文字列を検索し、見つかった矩形のリストを返す。"""
    try:
        rects = page.search_for(text)
    except Exception:
        rects = []
    if rects:
        return [fitz.Rect(r) for r in rects]
    # ハイフン・スペースの有無で表記が揺れる場合の再試行
    for variant in (text.replace("-", ""), text.replace(" ", "")):
        if variant and variant != text:
            try:
                rects = page.search_for(variant)
            except Exception:
                rects = []
            if rects:
                return [fitz.Rect(r) for r in rects]
    return []


def find_pii_candidates(doc) -> list:
    """文書全体から個人情報パターンに一致する文字列を検出し、候補として返す。

    戻り値: [{"page", "kind", "kind_label", "text", "note", "rects": [fitz.Rect]}]
    自動では何も消さない。UI でユーザーが選んだものだけ墨消しする。
    rects が空の候補は位置が特定できなかったため墨消しできない（note に記載）。
    """
    candidates = []
    for page_idx, page in enumerate(doc):
        try:
            text = page.get_text("text")
        except Exception:
            continue
        text_norm = _norm(text)
        seen = set()

        def _add(kind, matched_text, note=""):
            key = (page_idx, kind, matched_text)
            if key in seen:
                return
            seen.add(key)
            rects = _search_rects(page, matched_text)
            if not rects:
                note = (note + "（位置が特定できなかったため墨消しできません）").strip()
            candidates.append({
                "page": page_idx + 1,
                "kind": kind,
                "kind_label": PII_KINDS[kind],
                "text": matched_text,
                "note": note,
                "rects": rects,
            })

        for m in _EMAIL_RE.finditer(text_norm):
            _add("email", m.group(0))
        for m in _PHONE_HYPHEN_RE.finditer(text_norm):
            _add("phone", m.group(0))
        for m in _PHONE_MOBILE_RE.finditer(text_norm):
            _add("phone", m.group(0))
        # 口座番号（ラベル付き7桁）
        for m in _BANK_ACCOUNT_RE.finditer(text_norm):
            _add("bank_account", m.group(1),
                 note="「口座番号」などの表示の後ろの7桁")
        # マイナンバー（12桁：ラベル付き、または検査用数字が一致するもの）
        has_label = bool(_MY_NUMBER_LABEL_RE.search(text_norm))
        for m in _MY_NUMBER_RE.finditer(text_norm):
            digits = m.group(1)
            check = validate_my_number(digits)
            if has_label or check["valid"]:
                _add("my_number", digits,
                     note="検査用数字が一致" if check["valid"] else "「マイナンバー」の表示あり")
    candidates.sort(key=lambda c: (c["page"], c["kind"], c["text"]))
    return candidates


def apply_pii_redactions(doc, selections) -> dict:
    """ユーザーが選んだ候補だけを物理的に墨消しする。

    selections: [{"page": 1始まり, "rects": [fitz.Rect, ...]}]
    戻り値: {"redacted": 墨消しした箇所数, "pages": 処理したページ数}
    墨消しは物理削除（復旧不可）。呼び出し前に必ずユーザー確認を取ること。
    """
    redacted = 0
    pages_done = set()
    by_page = {}
    for sel in selections:
        page_no = int(sel.get("page", 0))
        rects = sel.get("rects") or []
        if not (1 <= page_no <= len(doc)) or not rects:
            continue
        by_page.setdefault(page_no, []).extend(rects)
    for page_no, rects in sorted(by_page.items()):
        page = doc[page_no - 1]
        for rect in rects:
            r = fitz.Rect(rect)
            if r.is_empty:
                continue
            page.add_redact_annot(r, fill=(1, 1, 1))
            redacted += 1
        if redacted:
            pages_done.add(page_no)
        try:
            page.apply_redactions(
                images=getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0),
                graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0),
                text=getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0),
            )
        except TypeError:
            page.apply_redactions()
    return {"redacted": redacted, "pages": len(pages_done)}


# ============================================================
# 14. 手書き署名・署名画像挿入
# ============================================================

def insert_signature_image(doc, page_1based: int, image_bytes: bytes,
                           rect) -> fitz.Rect:
    """署名画像（PNG/JPG のバイト列）を指定ページの指定位置に挿入する。

    rect は fitz.Rect（PDF 座標）。戻り値は実際に挿入した矩形。
    画像として読めないバイト列の場合は ValueError。
    """
    if not (1 <= page_1based <= len(doc)):
        raise ValueError(f"ページ番号が範囲外です: {page_1based}")
    if not image_bytes:
        raise ValueError("署名画像が空です。")
    raw = bytes(image_bytes)
    is_png = raw[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpeg = raw[:3] == b"\xff\xd8\xff"
    if not (is_png or is_jpeg):
        raise ValueError("署名画像として読み込めませんでした（PNG/JPG を指定してください）。")
    try:
        pix = fitz.Pixmap(raw)
        pix = None
    except Exception:
        raise ValueError("署名画像として読み込めませんでした（PNG/JPG を指定してください）。")
    r = fitz.Rect(rect)
    r.normalize()
    if r.is_empty or r.width <= 0 or r.height <= 0:
        raise ValueError("署名の位置・サイズが正しくありません。")
    page = doc[page_1based - 1]
    page.insert_image(r, stream=bytes(image_bytes))
    return r


def draw_freehand_signature(page, points, width: float = 2.0,
                            color=(0, 0, 0)) -> int:
    """マウスで描いた手書き署名の軌跡（PDF 座標の点列）をページに描く。

    points: [(x, y), ...] のリスト。2点以上で1本の線になる。
    複数の筆跡（ストローク）は点列の中に None をはさんで区切る。
    戻り値: 描いたストローク数。
    """
    strokes = []
    current = []
    for pt in points or []:
        if pt is None:
            if len(current) >= 2:
                strokes.append(current)
            current = []
        else:
            current.append(fitz.Point(float(pt[0]), float(pt[1])))
    if len(current) >= 2:
        strokes.append(current)
    if not strokes:
        raise ValueError("署名の軌跡がありません。2点以上描いてください。")
    if width <= 0:
        raise ValueError("線の太さは 0 より大きくしてください。")
    shape = page.new_shape()
    for stroke in strokes:
        shape.draw_polyline(stroke)
        shape.finish(color=tuple(color), width=float(width))
    shape.commit()
    return len(strokes)


def mm_to_pt(mm: float) -> float:
    """ミリメートルを PDF ポイント（1pt = 1/72インチ）に換算する。"""
    return float(mm) * 72.0 / 25.4
