# -*- coding: utf-8 -*-
"""
PDF -> Excel (.xlsx) 変換・書き出しエンジン (core/excel_exporter.py)
外部ライブラリ不要（PyMuPDF + Python標準ライブラリ zipfile / xml のみ）で
正規の Office Open XML (.xlsx) ファイルを生成します。
"""
import datetime
import os
import re
import tempfile
import unicodedata
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as xml_escape

import fitz

from core.geometry import FITZ_LOCK, get_effective_cropbox

# XML 1.0 で禁止されている制御文字（タブ・改行・復帰を除く）
_INVALID_XML_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]"
)

# 数値セルとして扱う金額・数値パターン（先頭ゼロコード 00123 や日付・電話番号は文字列のまま維持）
_STRICT_NUMBER_BODY_RE = re.compile(
    r"^[+-]?(?:0|[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)(?:\.\d+)?$"
)


def sanitize_xml_text(text: Any) -> str:
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return _INVALID_XML_CHARS_RE.sub("", s)


def parse_excel_numeric_cell(text: str) -> Optional[Tuple[float, bool]]:
    """
    セル文字列がExcelの数値セルとして安全に変換可能か判定する。
    戻り値: (数値float, パーセント表記か否か) または None（文字列のまま出力）
    """
    if not text:
        return None
    norm = unicodedata.normalize("NFKC", str(text)).strip()
    if not norm or "\n" in norm:
        return None

    is_percent = False
    if norm.endswith("%"):
        is_percent = True
        norm = norm[:-1].strip()

    is_paren_neg = norm.startswith("(") and norm.endswith(")") and len(norm) >= 3
    if is_paren_neg:
        norm = norm[1:-1].strip()

    # 末尾の「円」除去
    if norm.endswith("円"):
        norm = norm[:-1].strip()

    # 先頭のマイナス表記（▲, △, －, − 等）と通貨記号の整理
    neg = is_paren_neg
    if norm and norm[0] in "-－ー−﹣▲△":
        neg = not neg
        norm = norm[1:].strip()
    elif norm and norm[0] in "+＋":
        norm = norm[1:].strip()

    norm = norm.lstrip("¥￥$").strip()
    if norm and norm[0] in "-－ー−﹣▲△":
        neg = not neg
        norm = norm[1:].strip()

    if not norm or not _STRICT_NUMBER_BODY_RE.match(norm):
        return None

    try:
        val = float(norm.replace(",", ""))
    except ValueError:
        return None

    if neg and val > 0:
        val = -val
    if is_percent:
        val = val / 100.0
    if val == 0.0:
        # -0.0 になる経路を防ぐ（"-0.0%" が -0.0 として出力されないようにする）。
        val = 0.0
    return val, is_percent


def _col_index_to_letter(col_idx_1based: int) -> str:
    """1始まりの列番号をExcel列名 ('A', 'B', ..., 'Z', 'AA', ...) に変換する"""
    n = max(1, int(col_idx_1based))
    chars = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(65 + rem))
    return "".join(reversed(chars))


def _sanitize_sheet_name(raw_name: str, used_names: set) -> str:
    """Excelのシート名規約（31文字以内・禁止文字除去・重複回避）に適合させる"""
    cleaned = re.sub(r'[\\/?*\[\]:]', "_", str(raw_name or "Sheet")).strip().strip("'")
    if not cleaned:
        cleaned = "Sheet"
    base = cleaned[:31]
    candidate = base
    counter = 2
    while candidate.casefold() in used_names:
        suffix = f"_{counter}"
        candidate = base[: max(1, 31 - len(suffix))] + suffix
        counter += 1
    used_names.add(candidate.casefold())
    return candidate


def _estimate_display_width(text: str) -> float:
    """日本語全角文字を幅2.1、半角を1.1としてExcel列幅の目安を算出する"""
    if not text:
        return 0.0
    max_line_w = 0.0
    for line in str(text).split("\n"):
        w = sum(2.1 if ord(ch) > 127 else 1.1 for ch in line)
        if w > max_line_w:
            max_line_w = w
    return max_line_w


def _words_to_grid_rows(words: List[Tuple[Any, ...]]) -> List[List[str]]:
    """
    (x0, y0, x1, y1, word_text, ...) 形式の単語リストから、
    Y座標で行クラスタリングし、ページ全体のX座標クラスタから列位置を揃えて二次元配列へ変換する。
    """
    valid = [
        (float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4]).strip())
        for w in words
        if len(w) >= 5 and str(w[4]).strip()
    ]
    if not valid:
        return []

    # 背の高い文字から基準行を作り、高さの薄い「ー」等を同じ行へ吸着させる
    valid.sort(key=lambda item: (-(item[3] - item[1]), item[1], item[0]))
    row_clusters: List[Dict[str, Any]] = []
    for x0, y0, x1, y1, txt in valid:
        cy = (y0 + y1) * 0.5
        h = max(4.0, y1 - y0)
        placed = False
        for rc in row_clusters:
            rc_cy = (rc["y0"] + rc["y1"]) * 0.5
            rc_h = max(4.0, rc["y1"] - rc["y0"])
            overlap = min(rc["y1"], y1) - max(rc["y0"], y0)
            if overlap >= min(h, rc_h) * 0.35 or abs(cy - rc_cy) <= max(h, rc_h) * 0.55:
                rc["items"].append((x0, y0, x1, y1, txt))
                rc["y0"] = min(rc["y0"], y0)
                rc["y1"] = max(rc["y1"], y1)
                placed = True
                break
        if not placed:
            row_clusters.append({"y0": y0, "y1": y1, "items": [(x0, y0, x1, y1, txt)]})

    row_clusters.sort(key=lambda rc: rc["y0"])

    # 各行内で近接単語をセル（フレーズ）へ結合
    row_cells_list: List[List[Tuple[float, float, str]]] = []
    all_cell_x0s: List[float] = []

    for rc in row_clusters:
        items = sorted(rc["items"], key=lambda it: it[0])
        cells: List[Tuple[float, float, str]] = []
        cur_x0, _, cur_x1, _, cur_txt = items[0]
        cur_h = max(4.0, items[0][3] - items[0][1])

        for x0, y0, x1, y1, txt in items[1:]:
            gap = x0 - cur_x1
            h = max(4.0, y1 - y0)
            # 同じ列内の単語同士は結合し、大きく離れた列は別セルに分ける
            if gap <= max(8.0, min(cur_h, h) * 1.15):
                sep = " " if (cur_txt[-1].isascii() and txt[0].isascii() and cur_txt[-1].isalnum() and txt[0].isalnum()) else ""
                cur_txt = cur_txt + sep + txt
                cur_x1 = max(cur_x1, x1)
            else:
                cells.append((cur_x0, cur_x1, cur_txt))
                all_cell_x0s.append(cur_x0)
                cur_x0, cur_x1, cur_txt, cur_h = x0, x1, txt, h

        cells.append((cur_x0, cur_x1, cur_txt))
        all_cell_x0s.append(cur_x0)
        row_cells_list.append(cells)

    if not row_cells_list:
        return []

    # X開始位置をクラスタリングして列アンカーを決定
    sorted_xs = sorted(all_cell_x0s)
    col_anchors: List[float] = []
    for x_val in sorted_xs:
        if not col_anchors or abs(x_val - col_anchors[-1]) > 18.0:
            col_anchors.append(x_val)
        else:
            col_anchors[-1] = (col_anchors[-1] + x_val) * 0.5

    num_cols = max(1, len(col_anchors))
    grid: List[List[str]] = []

    for cells in row_cells_list:
        row_arr = [""] * num_cols
        last_col = -1
        for cx0, _, ctxt in cells:
            best_c = min(range(num_cols), key=lambda ci: abs(col_anchors[ci] - cx0))
            if best_c <= last_col:
                best_c = last_col + 1
            if best_c >= num_cols:
                # まれに同一行のセル数がアンカー数を超えた場合は列を拡張
                extra = best_c - num_cols + 1
                row_arr.extend([""] * extra)
                for r_prev in grid:
                    r_prev.extend([""] * extra)
                num_cols += extra
            row_arr[best_c] = ctxt
            last_col = best_c
        grid.append(row_arr)

    return grid


def _extract_text_in_rect_ltr(page: fitz.Page, cell_rect: fitz.Rect) -> str:
    """
    指定セル矩形内の文字を、高さの薄い記号（「ー」「・」「、」等）による上下逆転を起こさずに
    行単位で必ず左から右（X座標順）に組み立てる。
    """
    expanded = fitz.Rect(
        cell_rect.x0 - 1.5,
        cell_rect.y0 - 1.5,
        cell_rect.x1 + 1.5,
        cell_rect.y1 + 1.5,
    )
    try:
        raw_dict = page.get_text("rawdict", clip=expanded)
    except Exception:
        return sanitize_xml_text(page.get_text("text", clip=cell_rect)).strip()

    chars: List[Dict[str, Any]] = []
    for blk in raw_dict.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    c_str = ch.get("c", "")
                    if not c_str or c_str.isspace():
                        continue
                    bbox = fitz.Rect(ch.get("bbox", (0, 0, 0, 0)))
                    cx = (bbox.x0 + bbox.x1) * 0.5
                    cy = (bbox.y0 + bbox.y1) * 0.5
                    if (
                        cell_rect.x0 - 1.0 <= cx <= cell_rect.x1 + 1.0
                        and cell_rect.y0 - 1.0 <= cy <= cell_rect.y1 + 1.0
                    ):
                        chars.append({
                            "c": c_str,
                            "x0": bbox.x0,
                            "y0": bbox.y0,
                            "x1": bbox.x1,
                            "y1": bbox.y1,
                            "h": max(2.0, bbox.height),
                        })

    if not chars:
        return ""

    # 背の高い文字から基準行を作り、高さの薄い「ー」「・」「、」等を同じ行へ吸着させる
    tall_first = sorted(chars, key=lambda item: (-item["h"], item["y0"], item["x0"]))
    row_groups: List[Dict[str, Any]] = []
    for item in tall_first:
        cy = (item["y0"] + item["y1"]) * 0.5
        ih = item["h"]
        placed = False
        for rg in row_groups:
            ry0, ry1 = rg["y0"], rg["y1"]
            rcy = (ry0 + ry1) * 0.5
            rh = max(4.0, ry1 - ry0)
            overlap = min(ry1, item["y1"]) - max(ry0, item["y0"])
            if overlap >= -max(1.5, min(ih, rh) * 0.3) or abs(cy - rcy) <= rh * 0.65:
                rg["chars"].append(item)
                rg["y0"] = min(ry0, item["y0"])
                rg["y1"] = max(ry1, item["y1"])
                placed = True
                break
        if not placed:
            row_groups.append({"y0": item["y0"], "y1": item["y1"], "chars": [item]})

    row_groups.sort(key=lambda rg: (rg["y0"] + rg["y1"]) * 0.5)

    lines_out: List[str] = []
    for rg in row_groups:
        # 同一行内は縦位置の微差を無視して 100% 左から右（X座標順）で並べる
        row_chars = sorted(rg["chars"], key=lambda item: (round(item["x0"], 2), round(item["x1"], 2)))
        parts = [row_chars[0]["c"]]
        for prev_c, cur_c in zip(row_chars, row_chars[1:]):
            gap = cur_c["x0"] - prev_c["x1"]
            avg_h = max(4.0, (prev_c["h"] + cur_c["h"]) * 0.5)
            # 同一座標に二重埋め込みされたOCR文字の重複を防止
            if (
                prev_c["c"] == cur_c["c"]
                and abs(cur_c["x0"] - prev_c["x0"]) < avg_h * 0.25
                and abs(cur_c["y0"] - prev_c["y0"]) < avg_h * 0.25
            ):
                continue
            if (
                gap > avg_h * 0.45
                and prev_c["c"].isascii()
                and cur_c["c"].isascii()
                and prev_c["c"].isalnum()
                and cur_c["c"].isalnum()
            ):
                parts.append(" " + cur_c["c"])
            else:
                parts.append(cur_c["c"])
        line_str = sanitize_xml_text("".join(parts)).strip()
        if line_str:
            lines_out.append(line_str)

    return "\n".join(lines_out)


def extract_page_rows_for_excel(
    page: fitz.Page,
    mode: str = "auto",
) -> List[List[str]]:
    """
    1ページ分のPDF内容から Excel 出力用の行・列二次元リストを抽出する。
    mode:
      - "auto"       : 罫線テーブルを優先抽出し、テーブル外のテキスト行もY座標順に補完
      - "tables_only": 罫線テーブルのみ抽出（見つからない場合は座標抽出にフォールバック）
      - "layout"     : ページ全体の文字座標から行・列レイアウトを復元
    """
    crop_box = get_effective_cropbox(page)
    blocks_with_y: List[Tuple[float, List[List[str]]]] = []
    table_rects: List[fitz.Rect] = []

    if mode in ("auto", "tables_only") and hasattr(page, "find_tables"):
        try:
            finder = page.find_tables(clip=crop_box)
            tables = list(getattr(finder, "tables", None) or [])
            for tbl in tables:
                t_rect = fitz.Rect(tbl.bbox)
                clean_rows: List[List[str]] = []
                tbl_rows = getattr(tbl, "rows", None)
                if tbl_rows:
                    for row_obj in tbl_rows:
                        row_vals = [
                            _extract_text_in_rect_ltr(page, fitz.Rect(c_box))
                            if c_box is not None
                            else ""
                            for c_box in row_obj.cells
                        ]
                        if any(row_vals):
                            clean_rows.append(row_vals)
                else:
                    raw_rows = tbl.extract() or []
                    for r in raw_rows:
                        row_vals = [
                            sanitize_xml_text("" if cell is None else str(cell)).strip()
                            for cell in r
                        ]
                        if any(row_vals):
                            clean_rows.append(row_vals)
                if clean_rows:
                    table_rects.append(t_rect)
                    blocks_with_y.append((t_rect.y0, clean_rows))
        except Exception:
            pass

    if mode == "layout" or mode == "auto" or (mode == "tables_only" and not blocks_with_y):
        try:
            raw_words = page.get_text("words", clip=crop_box) or []
        except Exception:
            raw_words = []

        outside_words = []
        for w in raw_words:
            if len(w) < 5:
                continue
            cx = (float(w[0]) + float(w[2])) * 0.5
            cy = (float(w[1]) + float(w[3])) * 0.5
            pt = fitz.Point(cx, cy)
            if table_rects and any(tr.contains(pt) for tr in table_rects):
                continue
            outside_words.append(w)

        if outside_words:
            if not table_rects:
                grid = _words_to_grid_rows(outside_words)
                if grid:
                    blocks_with_y.append((crop_box.y0, grid))
            else:
                # テーブルとテーブルの間の非テーブル領域ごとにグループ化して順序を保つ
                sorted_trs = sorted(table_rects, key=lambda r: r.y0)
                bands: List[Tuple[float, float]] = []
                cur_top = crop_box.y0
                for tr in sorted_trs:
                    if tr.y0 > cur_top + 2.0:
                        bands.append((cur_top, tr.y0))
                    cur_top = max(cur_top, tr.y1)
                if crop_box.y1 > cur_top + 2.0:
                    bands.append((cur_top, crop_box.y1))

                for b_top, b_bot in bands:
                    band_words = [
                        w for w in outside_words
                        if b_top <= ((float(w[1]) + float(w[3])) * 0.5) <= b_bot
                    ]
                    if band_words:
                        grid = _words_to_grid_rows(band_words)
                        if grid:
                            blocks_with_y.append((b_top, grid))

    blocks_with_y.sort(key=lambda item: item[0])

    merged_rows: List[List[str]] = []
    for idx_b, (_, rows) in enumerate(blocks_with_y):
        if idx_b > 0 and merged_rows and rows:
            merged_rows.append([])
        merged_rows.extend(rows)

    return merged_rows


def write_xlsx_from_sheets(
    sheets: Sequence[Tuple[str, List[List[str]]]],
    output_path: str,
    convert_numbers: bool = True,
    bold_first_row: bool = True,
) -> None:
    """
    (シート名, 行リスト) のシーケンスから正規の .xlsx ファイルを書き出す。
    """
    if not sheets:
        sheets = [("Sheet1", [[""]])]

    used_names = set()
    normalized_sheets: List[Tuple[str, List[List[str]]]] = []
    for raw_name, rows in sheets:
        sname = _sanitize_sheet_name(raw_name, used_names)
        normalized_sheets.append((sname, rows if rows else [[""]]))

    shared_strings: List[str] = []
    shared_index_map: Dict[str, int] = {}

    def get_shared_string_idx(val: str) -> int:
        clean = sanitize_xml_text(val)
        idx = shared_index_map.get(clean)
        if idx is None:
            idx = len(shared_strings)
            shared_strings.append(clean)
            shared_index_map[clean] = idx
        return idx

    worksheet_xmls: List[str] = []

    for _, rows in normalized_sheets:
        max_cols = max((len(r) for r in rows), default=1)
        max_cols = max(1, max_cols)
        col_widths = [10.0] * max_cols

        sheet_row_xmls: List[str] = []
        for r_idx_0, row in enumerate(rows):
            r_num = r_idx_0 + 1
            is_header = bold_first_row and (r_idx_0 == 0) and any(str(c).strip() for c in row)
            cell_xmls: List[str] = []

            for c_idx_0, raw_cell in enumerate(row):
                col_letter = _col_index_to_letter(c_idx_0 + 1)
                coord = f"{col_letter}{r_num}"
                cell_str = sanitize_xml_text(raw_cell).strip()

                est_w = _estimate_display_width(cell_str)
                if est_w + 3.0 > col_widths[c_idx_0]:
                    col_widths[c_idx_0] = min(65.0, est_w + 3.0)

                if not cell_str:
                    style_id = 1 if is_header else 0
                    cell_xmls.append(f'<c r="{coord}" s="{style_id}"/>')
                    continue

                num_parsed = parse_excel_numeric_cell(cell_str) if (convert_numbers and not is_header) else None
                if num_parsed is not None:
                    num_val, is_pct = num_parsed
                    style_id = 3 if is_pct else 2
                    val_repr = str(int(num_val)) if (not is_pct and abs(num_val - round(num_val)) < 1e-9) else repr(num_val)
                    cell_xmls.append(f'<c r="{coord}" s="{style_id}"><v>{val_repr}</v></c>')
                else:
                    s_idx = get_shared_string_idx(cell_str)
                    style_id = 1 if is_header else 0
                    cell_xmls.append(f'<c r="{coord}" t="s" s="{style_id}"><v>{s_idx}</v></c>')

            sheet_row_xmls.append(f'<row r="{r_num}">{"".join(cell_xmls)}</row>')

        cols_xml_parts = [
            f'<col min="{i + 1}" max="{i + 1}" width="{col_widths[i]:.2f}" customWidth="1"/>'
            for i in range(max_cols)
        ]
        last_col_letter = _col_index_to_letter(max_cols)
        last_row_num = max(1, len(rows))

        ws_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<dimension ref="A1:{last_col_letter}{last_row_num}"/>'
            '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
            '<sheetFormatPr defaultRowHeight="18"/>'
            f'<cols>{"".join(cols_xml_parts)}</cols>'
            f'<sheetData>{"".join(sheet_row_xmls)}</sheetData>'
            '</worksheet>'
        )
        worksheet_xmls.append(ws_xml)

    # sharedStrings.xml
    si_parts = []
    for s in shared_strings:
        preserve = ' xml:space="preserve"' if (s.startswith(" ") or s.endswith(" ") or "\n" in s) else ""
        si_parts.append(f"<si><t{preserve}>{xml_escape(s)}</t></si>")
    shared_strings_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">'
        f'{"".join(si_parts)}'
        '</sst>'
    )

    # styles.xml (0: 通常文字列折り返し, 1: 見出し太字+薄背景+下線, 2: カンマ数値, 3: パーセント数値)
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="2">'
        '<numFmt numFmtId="164" formatCode="#,##0;-#,##0;0"/>'
        '<numFmt numFmtId="165" formatCode="0.0%"/>'
        '</numFmts>'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Yu Gothic"/></font>'
        '<font><b/><sz val="11"/><name val="Yu Gothic"/></font>'
        '</fonts>'
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF2F4F8"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="2">'
        '<border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left style="thin"><color rgb="FFD0D7DE"/></left><right style="thin"><color rgb="FFD0D7DE"/></right>'
        '<top style="thin"><color rgb="FFD0D7DE"/></top><bottom style="medium"><color rgb="FF8C959F"/></bottom><diagonal/></border>'
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="4">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="center" horizontal="right"/></xf>'
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="center" horizontal="right"/></xf>'
        '</cellXfs>'
        '</styleSheet>'
    )

    # workbook.xml & workbook.xml.rels & [Content_Types].xml
    sheet_tags = []
    rel_tags = []
    ct_tags = []
    for i, (sname, _) in enumerate(normalized_sheets, start=1):
        sheet_tags.append(f'<sheet name="{xml_escape(sname)}" sheetId="{i}" r:id="rId{i}"/>')
        rel_tags.append(
            f'<Relationship Id="rId{i}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
        )
        ct_tags.append(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    next_rid = len(normalized_sheets) + 1
    rel_tags.append(
        f'<Relationship Id="rId{next_rid}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    rel_tags.append(
        f'<Relationship Id="rId{next_rid + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
    )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{"".join(sheet_tags)}</sheets>'
        '</workbook>'
    )

    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(rel_tags)}'
        '</Relationships>'
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'{"".join(ct_tags)}'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>'
    )

    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
    )

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:creator>KinakoPDF</dc:creator>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now_iso}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now_iso}</dcterms:modified>'
        '</cp:coreProperties>'
    )

    app_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Application>KinakoPDF</Application>'
        '</Properties>'
    )

    # 既存ファイルを壊さないよう、同じフォルダの一時ファイルに書いてから置き換える
    _target_dir = os.path.dirname(os.path.abspath(output_path))
    _fd, _tmp_path = tempfile.mkstemp(dir=_target_dir, suffix=".tmp.xlsx")
    os.close(_fd)
    try:
        with zipfile.ZipFile(_tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types_xml.encode("utf-8"))
            zf.writestr("_rels/.rels", root_rels_xml.encode("utf-8"))
            zf.writestr("docProps/core.xml", core_xml.encode("utf-8"))
            zf.writestr("docProps/app.xml", app_xml.encode("utf-8"))
            zf.writestr("xl/workbook.xml", workbook_xml.encode("utf-8"))
            zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml.encode("utf-8"))
            zf.writestr("xl/styles.xml", styles_xml.encode("utf-8"))
            zf.writestr("xl/sharedStrings.xml", shared_strings_xml.encode("utf-8"))
            for idx_s, ws_xml in enumerate(worksheet_xmls, start=1):
                zf.writestr(f"xl/worksheets/sheet{idx_s}.xml", ws_xml.encode("utf-8"))
        os.replace(_tmp_path, output_path)
    except Exception:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        raise


def export_pdf_document_to_xlsx(
    doc: fitz.Document,
    page_indices: Sequence[int],
    output_path: str,
    page_annotations: Optional[Dict[int, List[Dict[str, Any]]]] = None,
    mode: str = "auto",
    split_sheets: bool = True,
    convert_numbers: bool = True,
) -> Tuple[int, int]:
    """
    PDFドキュメントの指定ページ群を .xlsx ファイルとして書き出す。
    戻り値: (出力シート数, 総出力行数)
    """
    from core.pdf_engine import PDFDocumentManager

    valid_indices = [i for i in page_indices if 0 <= i < len(doc)]
    if not valid_indices:
        raise ValueError("Excel出力対象の有効なページがありません。")

    work_doc = PDFDocumentManager.extract_pages_as_doc(
        doc, valid_indices, page_annotations or {}
    )
    try:
        sheets: List[Tuple[str, List[List[str]]]] = []
        combined_rows: List[List[str]] = []
        total_rows = 0

        with FITZ_LOCK:
            for local_i, orig_page_idx in enumerate(valid_indices):
                page = work_doc[local_i]
                page_rows = extract_page_rows_for_excel(page, mode=mode)
                if not page_rows:
                    page_rows = [[f"（{orig_page_idx + 1}ページ目: 抽出可能なテキストがありません）"]]

                if split_sheets:
                    sheets.append((f"{orig_page_idx + 1}ページ", page_rows))
                    total_rows += len(page_rows)
                else:
                    if combined_rows:
                        combined_rows.append([])
                    combined_rows.append([f"=== {orig_page_idx + 1}ページ目 ==="])
                    combined_rows.extend(page_rows)

        if not split_sheets:
            sheets.append(("PDF抽出データ", combined_rows))
            total_rows = len(combined_rows)

        write_xlsx_from_sheets(
            sheets=sheets,
            output_path=output_path,
            convert_numbers=convert_numbers,
            bold_first_row=True,
        )
        return len(sheets), total_rows
    finally:
        with FITZ_LOCK:
            work_doc.close()
