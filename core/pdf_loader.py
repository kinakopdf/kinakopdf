# -*- coding: utf-8 -*-
import re
from pathlib import Path
from typing import List, Optional, Tuple
import fitz

from core import logger
from core.geometry import FITZ_LOCK


def natural_sort_key(text: str):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", str(text))]


def find_pdf_files_in_folder(folder_path: str) -> List[str]:
    p = Path(folder_path)
    if not p.is_dir():
        return []
    pdf_files = [
        str(f.resolve())
        for f in p.iterdir()
        if f.is_file() and f.suffix.lower() == ".pdf" and not f.name.startswith("._")
    ]
    pdf_files.sort(key=natural_sort_key)
    return pdf_files


def load_folder_as_merged_document(
    folder_path: str,
    password_callback=None
) -> Tuple[Optional[fitz.Document], List[Tuple[str, int, int]], List[Tuple[str, str]]]:
    """フォルダ内の PDF を 1 文書に結合して返す。

    戻り値は (結合文書, ファイル一覧, スキップしたファイル一覧) の 3 要素。
    スキップしたファイルは (ファイル名, 理由) の一覧で返す（audit #9）。
    理由は "encrypted (password required)" / "wrong password" /
    "password entry cancelled" / "0 pages" / "unreadable or corrupt file (...)"
    のいずれか。読み込めたファイルが 1 つも無い場合は文書は None。
    """
    pdf_paths = find_pdf_files_in_folder(folder_path)
    if not pdf_paths:
        return None, [], []

    with FITZ_LOCK:
        combined_doc = fitz.open()
    file_manifest = []
    skipped: List[Tuple[str, str]] = []

    current_page_offset = 0
    for fp in pdf_paths:
        name = Path(fp).name
        doc = None
        try:
            data = Path(fp).read_bytes()
            with FITZ_LOCK:
                doc = fitz.open(stream=data, filetype="pdf")
                needs_pass = doc.needs_pass

            if needs_pass:
                if password_callback:
                    res = password_callback(name)
                    if isinstance(res, tuple):
                        pwd, ok = res
                        if not ok:
                            skipped.append((name, "password entry cancelled"))
                            continue
                    else:
                        pwd = res
                    if not pwd:
                        skipped.append((name, "encrypted (password required)"))
                        continue
                    with FITZ_LOCK:
                        auth_ok = bool(doc.authenticate(pwd))
                    if not auth_ok:
                        skipped.append((name, "wrong password"))
                        continue
                else:
                    skipped.append((name, "encrypted (password required)"))
                    continue

            with FITZ_LOCK:
                page_count = len(doc)
                if page_count > 0:
                    combined_doc.insert_pdf(doc)
                    file_manifest.append(
                        (name, current_page_offset, current_page_offset + page_count - 1)
                    )
                    current_page_offset += page_count
                else:
                    skipped.append((name, "0 pages"))
        except Exception as exc:
            logger.debug("Failed to load PDF file in folder: %s", fp, exc_info=True)
            skipped.append((name, f"unreadable or corrupt file ({exc})"))
            continue
        finally:
            if doc is not None:
                try:
                    with FITZ_LOCK:
                        doc.close()
                except Exception:
                    logger.debug("Failed to close temporary PDF doc in loader", exc_info=True)

    with FITZ_LOCK:
        if len(combined_doc) == 0:
            combined_doc.close()
            return None, [], skipped

    return combined_doc, file_manifest, skipped
