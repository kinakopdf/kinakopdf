# -*- coding: utf-8 -*-
"""会計向け P1 機能のダイアログ群（feature/trial-multi 第2バッチ）。

MainWindow から show_*_dialog(parent) として呼ばれる。
コア処理は core/accounting_tools.py にあり、ここは表示・確認・入出力のみ。
"""
import os
import sys
from pathlib import Path

import fitz
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.accounting_tools import (
    apply_pii_redactions,
    compare_pdf_texts,
    draw_freehand_signature,
    extract_voucher_fields,
    extract_voucher_fields_from_doc,
    find_invoice_numbers_in_doc,
    find_pii_candidates,
    insert_signature_image,
    mm_to_pt,
    rename_pdf_file,
    search_pdfs_in_folder,
    suggest_densho_filename,
    validate_invoice_number,
    voucher_rows_to_csv,
    voucher_rows_to_xlsx,
)

NOTE_OCR = ("※ 抽出結果は「候補」です。OCR の読み取り間違いがあります。"
            "出力前に一覧で必ず確認・修正してください。")


def _warn(parent, title, message):
    QMessageBox.warning(parent, title, message)


def _info(parent, title, message):
    QMessageBox.information(parent, title, message)


def _confirm(parent, title, message):
    return QMessageBox.question(
        parent, title, message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    ) == QMessageBox.StandardButton.Yes


def _pdf_files_in_folder(folder):
    return sorted(
        os.path.join(folder, name)
        for name in os.listdir(folder)
        if name.lower().endswith(".pdf")
        and os.path.isfile(os.path.join(folder, name))
    )


def _ocr_text_for_doc(doc, max_pages=2):
    """文字層がない文書を Windows 標準 OCR で読む（Windows のみ）。

    既存の core/ocr_cleaner.py の仕組みを使う。Windows 以外では "" を返す。
    """
    if sys.platform != "win32":
        return ""
    try:
        from core.ocr_cleaner import render_page_to_qimage, run_windows_ocr_batch_on_qimages
    except Exception:
        return ""
    images = []
    for page in list(doc)[:max_pages]:
        try:
            images.append(render_page_to_qimage(page, dpi=250))
        except Exception:
            pass
    if not images:
        return ""
    try:
        pages_chunks = run_windows_ocr_batch_on_qimages(images)
    except Exception:
        return ""
    texts = []
    for chunks in pages_chunks:
        texts.append(" ".join(str(c.get("text", "")) for c in chunks))
    return "\n".join(texts)


# ============================================================
# 8. 証憑 OCR→一覧 Excel/CSV（半自動）
# ============================================================

def show_voucher_dialog(parent):
    dlg = QDialog(parent)
    dlg.setWindowTitle("証憑OCR→一覧 Excel/CSV（半自動）")
    dlg.resize(900, 520)
    layout = QVBoxLayout(dlg)

    top = QHBoxLayout()
    folder_edit = QLineEdit(dlg)
    folder_edit.setPlaceholderText("請求書・領収書の PDF があるフォルダ")
    folder_btn = QPushButton("フォルダを選ぶ", dlg)
    top.addWidget(folder_edit)
    top.addWidget(folder_btn)
    layout.addLayout(top)

    ocr_check = QCheckBox("文字がないPDFは Windows 標準 OCR で読む（Windowsのみ）", dlg)
    ocr_check.setChecked(True)
    layout.addWidget(ocr_check)

    read_btn = QPushButton("フォルダ内の PDF を読み取る", dlg)
    layout.addWidget(read_btn)

    table = QTableWidget(0, 6, dlg)
    table.setHorizontalHeaderLabels(["ファイル名", "日付", "取引先", "金額", "インボイス番号", "備考"])
    table.horizontalHeader().setStretchLastSection(True)
    layout.addWidget(table)

    note = QLabel(NOTE_OCR + "\n一覧のセルは直接編集できます。修正してから出力してください。", dlg)
    note.setWordWrap(True)
    layout.addWidget(note)

    btn_row = QHBoxLayout()
    xlsx_btn = QPushButton("Excel（.xlsx）で出力", dlg)
    csv_btn = QPushButton("CSV で出力", dlg)
    close_btn = QPushButton("閉じる", dlg)
    btn_row.addWidget(xlsx_btn)
    btn_row.addWidget(csv_btn)
    btn_row.addStretch()
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)

    state = {"folder": ""}

    def pick_folder():
        folder = QFileDialog.getExistingDirectory(dlg, "証憑 PDF のフォルダを選ぶ")
        if folder:
            folder_edit.setText(folder)
            state["folder"] = folder

    folder_btn.clicked.connect(pick_folder)

    def read_folder():
        folder = folder_edit.text().strip() or state["folder"]
        if not folder or not os.path.isdir(folder):
            _warn(dlg, "エラー", "フォルダを選んでください。")
            return
        files = _pdf_files_in_folder(folder)
        if not files:
            _warn(dlg, "案内", "フォルダ内に PDF がありませんでした。")
            return
        table.setRowCount(0)
        for path in files:
            name = os.path.basename(path)
            try:
                doc = fitz.open(path)
            except Exception as e:
                row = table.rowCount()
                table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(name))
                table.setItem(row, 5, QTableWidgetItem(f"開けませんでした: {e}"))
                continue
            try:
                if doc.needs_pass:
                    row = table.rowCount()
                    table.insertRow(row)
                    table.setItem(row, 0, QTableWidgetItem(name))
                    table.setItem(row, 5, QTableWidgetItem("パスワード付きのため読めません"))
                    continue
                fields = extract_voucher_fields_from_doc(doc)
                note_text = ""
                if not any([fields["date"], fields["vendor"],
                            fields["amount"] is not None, fields["invoice_no"]]):
                    if ocr_check.isChecked():
                        ocr_text = _ocr_text_for_doc(doc)
                        if ocr_text.strip():
                            fields = extract_voucher_fields(ocr_text)
                            note_text = "OCRで読み取り"
                        else:
                            note_text = "文字が読み取れませんでした"
                    else:
                        note_text = "文字層がありません"
                row = table.rowCount()
                table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(name))
                table.setItem(row, 1, QTableWidgetItem(fields["date"]))
                table.setItem(row, 2, QTableWidgetItem(fields["vendor"]))
                table.setItem(row, 3, QTableWidgetItem(
                    "" if fields["amount"] is None else str(fields["amount"])))
                table.setItem(row, 4, QTableWidgetItem(fields["invoice_no"]))
                table.setItem(row, 5, QTableWidgetItem(note_text))
            finally:
                doc.close()
        parent.statusBar().showMessage(f"証憑を {table.rowCount()} 件読み取りました。")

    read_btn.clicked.connect(read_folder)

    def collect_rows():
        rows = []
        for r in range(table.rowCount()):
            def _cell(c):
                item = table.item(r, c)
                return item.text().strip() if item else ""
            amount_text = _cell(3).replace(",", "").replace("円", "")
            try:
                amount = int(amount_text) if amount_text else None
            except ValueError:
                amount = None
            rows.append({
                "file": _cell(0), "date": _cell(1), "vendor": _cell(2),
                "amount": amount, "invoice_no": _cell(4), "note": _cell(5),
            })
        return rows

    def export_xlsx():
        rows = collect_rows()
        if not rows:
            _warn(dlg, "案内", "先に「フォルダ内の PDF を読み取る」を実行してください。")
            return
        path, _ = QFileDialog.getSaveFileName(dlg, "Excel で保存", "証憑一覧.xlsx",
                                              "Excel Files (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            voucher_rows_to_xlsx(rows, path)
        except Exception as e:
            QMessageBox.critical(dlg, "保存エラー", f"保存に失敗しました。\n{e}")
            return
        _info(dlg, "完了", f"Excel に出力しました。\n{path}\n\n{NOTE_OCR}")

    def export_csv():
        rows = collect_rows()
        if not rows:
            _warn(dlg, "案内", "先に「フォルダ内の PDF を読み取る」を実行してください。")
            return
        path, _ = QFileDialog.getSaveFileName(dlg, "CSV で保存", "証憑一覧.csv",
                                              "CSV Files (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            Path(path).write_text(voucher_rows_to_csv(rows), encoding="utf-8-sig")
        except Exception as e:
            QMessageBox.critical(dlg, "保存エラー", f"保存に失敗しました。\n{e}")
            return
        _info(dlg, "完了", f"CSV に出力しました。\n{path}\n\n{NOTE_OCR}")

    xlsx_btn.clicked.connect(export_xlsx)
    csv_btn.clicked.connect(export_csv)
    close_btn.clicked.connect(dlg.accept)
    dlg.exec()


# ============================================================
# 9. 電帳法自動リネーム
# ============================================================

def show_densho_rename_dialog(parent):
    dlg = QDialog(parent)
    dlg.setWindowTitle("電帳法リネーム（YYYY-MM-DD_取引先_金額.pdf）")
    dlg.resize(900, 480)
    layout = QVBoxLayout(dlg)

    top = QHBoxLayout()
    folder_edit = QLineEdit(dlg)
    folder_edit.setPlaceholderText("リネームする PDF があるフォルダ")
    folder_btn = QPushButton("フォルダを選ぶ", dlg)
    top.addWidget(folder_edit)
    top.addWidget(folder_btn)
    layout.addLayout(top)

    read_btn = QPushButton("ファイル名の案を作る", dlg)
    layout.addWidget(read_btn)

    table = QTableWidget(0, 5, dlg)
    table.setHorizontalHeaderLabels(["リネームする", "現在の名前", "新しい名前（編集できます）", "日付", "取引先 / 金額"])
    table.horizontalHeader().setStretchLastSection(True)
    layout.addWidget(table)

    note = QLabel("新しい名前は「YYYY-MM-DD_取引先_金額.pdf」の形で提案します。"
                  "日付・取引先・金額が読み取れなかった行は空欄になります。"
                  "名前は表で直接修正できます。リネームは確認後に、チェックしたものだけ実行します。",
                  dlg)
    note.setWordWrap(True)
    layout.addWidget(note)

    btn_row = QHBoxLayout()
    rename_btn = QPushButton("チェックしたものをリネーム", dlg)
    close_btn = QPushButton("閉じる", dlg)
    btn_row.addWidget(rename_btn)
    btn_row.addStretch()
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)

    state = {"folder": ""}

    def pick_folder():
        folder = QFileDialog.getExistingDirectory(dlg, "PDF のフォルダを選ぶ")
        if folder:
            folder_edit.setText(folder)
            state["folder"] = folder

    folder_btn.clicked.connect(pick_folder)

    def build_proposals():
        folder = folder_edit.text().strip() or state["folder"]
        if not folder or not os.path.isdir(folder):
            _warn(dlg, "エラー", "フォルダを選んでください。")
            return
        files = _pdf_files_in_folder(folder)
        if not files:
            _warn(dlg, "案内", "フォルダ内に PDF がありませんでした。")
            return
        table.setRowCount(0)
        for path in files:
            name = os.path.basename(path)
            try:
                doc = fitz.open(path)
            except Exception:
                continue
            try:
                if doc.needs_pass:
                    continue
                fields = extract_voucher_fields_from_doc(doc)
            finally:
                doc.close()
            proposed = suggest_densho_filename(
                fields["date"], fields["vendor"], fields["amount"],
                original_name=name)
            row = table.rowCount()
            table.insertRow(row)
            check = QCheckBox(dlg)
            complete = bool(fields["date"] and fields["vendor"]
                            and fields["amount"] is not None)
            check.setChecked(complete)
            table.setCellWidget(row, 0, check)
            table.setItem(row, 1, QTableWidgetItem(name))
            table.setItem(row, 2, QTableWidgetItem(proposed))
            table.setItem(row, 3, QTableWidgetItem(fields["date"]))
            table.setItem(row, 4, QTableWidgetItem(
                f"{fields['vendor']} / "
                f"{'' if fields['amount'] is None else fields['amount']}"))
            table.item(row, 1).setData(Qt.ItemDataRole.UserRole, path)
        parent.statusBar().showMessage(f"ファイル名の案を {table.rowCount()} 件作りました。")

    read_btn.clicked.connect(build_proposals)

    def do_rename():
        targets = []
        for r in range(table.rowCount()):
            widget = table.cellWidget(r, 0)
            if widget is None or not widget.isChecked():
                continue
            src = table.item(r, 1).data(Qt.ItemDataRole.UserRole)
            new_name = table.item(r, 2).text().strip() if table.item(r, 2) else ""
            targets.append((src, new_name))
        if not targets:
            _warn(dlg, "案内", "リネームするファイルにチェックを入れてください。")
            return
        preview = "\n".join(
            f"{os.path.basename(s)} → {n}" for s, n in targets)
        if not _confirm(dlg, "確認",
                        f"次の {len(targets)} 件をリネームします。よろしいですか？\n\n{preview}"):
            return
        done, failed = [], []
        for src, new_name in targets:
            try:
                dest = rename_pdf_file(src, new_name)
                done.append(os.path.basename(dest))
            except Exception as e:
                failed.append(f"{os.path.basename(src)}: {e}")
        msg = f"リネームしました: {len(done)} 件"
        if done:
            msg += "\n" + "\n".join(done)
        if failed:
            msg += f"\n\n失敗: {len(failed)} 件\n" + "\n".join(failed)
        _info(dlg, "結果", msg)
        build_proposals()

    rename_btn.clicked.connect(do_rename)
    close_btn.clicked.connect(dlg.accept)
    dlg.exec()


# ============================================================
# 10. インボイス番号の検出・形式チェック
# ============================================================

def show_invoice_dialog(parent):
    doc = parent._trial_require_doc()
    if doc is None:
        return
    dlg = QDialog(parent)
    dlg.setWindowTitle("インボイス番号（T+13桁）の検出・形式チェック")
    dlg.resize(800, 480)
    layout = QVBoxLayout(dlg)

    table = QTableWidget(0, 4, dlg)
    table.setHorizontalHeaderLabels(["ページ", "登録番号", "判定", "理由"])
    table.horizontalHeader().setStretchLastSection(True)
    layout.addWidget(table)

    try:
        found = find_invoice_numbers_in_doc(doc)
    except Exception as e:
        _warn(dlg, "エラー", f"検出に失敗しました。\n{e}")
        return
    for item in found:
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(str(item["page"])))
        table.setItem(row, 1, QTableWidgetItem(item["number"]))
        table.setItem(row, 2, QTableWidgetItem("○ 正しい形式" if item["valid"] else "× 要確認"))
        table.setItem(row, 3, QTableWidgetItem(item["reason"]))
    if not found:
        layout.addWidget(QLabel("この PDF からは T+13桁の登録番号が見つかりませんでした。", dlg))

    manual = QHBoxLayout()
    manual_edit = QLineEdit(dlg)
    manual_edit.setPlaceholderText("例: T8700110005901（手入力で1件チェック）")
    manual_btn = QPushButton("この番号をチェック", dlg)
    manual.addWidget(manual_edit)
    manual.addWidget(manual_btn)
    layout.addLayout(manual)
    manual_result = QLabel("", dlg)
    manual_result.setWordWrap(True)
    layout.addWidget(manual_result)

    note = QLabel("形式（T+13桁）と検査用数字だけを確認します。"
                  "その番号の法人が実在するかどうかは分かりません。"
                  "実在確認は国税庁の「適格請求書発行事業者公表サイト」で照会してください。",
                  dlg)
    note.setWordWrap(True)
    layout.addWidget(note)

    def check_manual():
        res = validate_invoice_number(manual_edit.text())
        mark = "○ 正しい形式です。" if res["valid"] else "× 形式に問題があります。"
        manual_result.setText(f"{res['number'] or manual_edit.text().strip()} : {mark}\n{res['reason']}")

    manual_btn.clicked.connect(check_manual)

    close_btn = QPushButton("閉じる", dlg)
    close_btn.clicked.connect(dlg.accept)
    layout.addWidget(close_btn)
    dlg.exec()


# ============================================================
# 11. 複数 PDF 横断検索
# ============================================================

def show_cross_search_dialog(parent):
    dlg = QDialog(parent)
    dlg.setWindowTitle("複数 PDF 横断検索")
    dlg.resize(900, 520)
    layout = QVBoxLayout(dlg)

    top = QHBoxLayout()
    folder_edit = QLineEdit(dlg)
    folder_edit.setPlaceholderText("検索する PDF があるフォルダ")
    folder_btn = QPushButton("フォルダを選ぶ", dlg)
    top.addWidget(folder_edit)
    top.addWidget(folder_btn)
    layout.addLayout(top)

    key_row = QHBoxLayout()
    key_edit = QLineEdit(dlg)
    key_edit.setPlaceholderText("キーワード（例: きなこ商事）")
    search_btn = QPushButton("検索", dlg)
    key_row.addWidget(key_edit)
    key_row.addWidget(search_btn)
    layout.addLayout(key_row)

    table = QTableWidget(0, 3, dlg)
    table.setHorizontalHeaderLabels(["ファイル", "ページ", "見つかった前後の文字"])
    table.horizontalHeader().setStretchLastSection(True)
    layout.addWidget(table)

    status_label = QLabel("", dlg)
    layout.addWidget(status_label)
    hint = QLabel("行をダブルクリックすると、その PDF を新しいウィンドウで開きます。", dlg)
    layout.addWidget(hint)

    close_btn = QPushButton("閉じる", dlg)
    close_btn.clicked.connect(dlg.accept)
    layout.addWidget(close_btn)

    def pick_folder():
        folder = QFileDialog.getExistingDirectory(dlg, "検索するフォルダを選ぶ")
        if folder:
            folder_edit.setText(folder)

    folder_btn.clicked.connect(pick_folder)

    def do_search():
        folder = folder_edit.text().strip()
        keyword = key_edit.text().strip()
        if not folder or not os.path.isdir(folder):
            _warn(dlg, "エラー", "フォルダを選んでください。")
            return
        if not keyword:
            _warn(dlg, "エラー", "キーワードを入力してください。")
            return
        try:
            res = search_pdfs_in_folder(folder, keyword)
        except Exception as e:
            QMessageBox.critical(dlg, "エラー", f"検索に失敗しました。\n{e}")
            return
        table.setRowCount(0)
        for hit in res["results"]:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(hit["file"]))
            table.setItem(row, 1, QTableWidgetItem(str(hit["page"])))
            table.setItem(row, 2, QTableWidgetItem(hit["snippet"]))
            table.item(row, 0).setData(Qt.ItemDataRole.UserRole, hit["path"])
        msg = (f"{res['files_searched']} ファイルを検索し、{len(res['results'])} 件ヒットしました。")
        if res["files_skipped"]:
            msg += "\n開けなかったファイル: " + ", ".join(
                s["file"] for s in res["files_skipped"])
        status_label.setText(msg)

    search_btn.clicked.connect(do_search)
    key_edit.returnPressed.connect(do_search)

    def open_hit(row, _col):
        item = table.item(row, 0)
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and os.path.isfile(path):
            try:
                parent.spawn_new_window(filepath=path)
            except Exception as e:
                _warn(dlg, "エラー", f"開けませんでした。\n{e}")

    table.cellDoubleClicked.connect(open_hit)
    dlg.exec()


# ============================================================
# 12. 2つの PDF のテキスト比較
# ============================================================

def show_compare_dialog(parent):
    dlg = QDialog(parent)
    dlg.setWindowTitle("2つの PDF のテキスト比較")
    dlg.resize(900, 560)
    layout = QVBoxLayout(dlg)

    def file_row(label_text):
        row = QHBoxLayout()
        edit = QLineEdit(dlg)
        edit.setPlaceholderText("PDF ファイルを選んでください")
        edit.setReadOnly(True)
        btn = QPushButton("ファイルを選ぶ", dlg)
        row.addWidget(QLabel(label_text, dlg))
        row.addWidget(edit)
        row.addWidget(btn)
        layout.addLayout(row)
        return edit, btn

    edit_a, btn_a = file_row("A:")
    edit_b, btn_b = file_row("B:")
    compare_btn = QPushButton("比較する", dlg)
    layout.addWidget(compare_btn)

    summary_label = QLabel("", dlg)
    layout.addWidget(summary_label)

    table = QTableWidget(0, 2, dlg)
    table.setHorizontalHeaderLabels(["ページ", "結果"])
    table.horizontalHeader().setStretchLastSection(True)
    layout.addWidget(table)

    detail = QPlainTextEdit(dlg)
    detail.setReadOnly(True)
    detail.setPlaceholderText("上の行を選ぶと、ここに差分が表示されます。")
    layout.addWidget(detail)

    close_btn = QPushButton("閉じる", dlg)
    close_btn.clicked.connect(dlg.accept)
    layout.addWidget(close_btn)

    state = {"result": None}

    def pick(edit):
        path, _ = QFileDialog.getOpenFileName(dlg, "PDF を選ぶ", "", "PDF Files (*.pdf)")
        if path:
            edit.setText(path)

    btn_a.clicked.connect(lambda: pick(edit_a))
    btn_b.clicked.connect(lambda: pick(edit_b))

    def do_compare():
        path_a, path_b = edit_a.text().strip(), edit_b.text().strip()
        if not path_a or not path_b:
            _warn(dlg, "エラー", "比較する PDF を2つ選んでください。")
            return
        if os.path.abspath(path_a) == os.path.abspath(path_b):
            _warn(dlg, "エラー", "同じファイル同士は比較できません。")
            return
        try:
            doc_a = fitz.open(path_a)
            doc_b = fitz.open(path_b)
        except Exception as e:
            QMessageBox.critical(dlg, "エラー", f"PDF を開けませんでした。\n{e}")
            return
        try:
            res = compare_pdf_texts(doc_a, doc_b)
        except Exception as e:
            QMessageBox.critical(dlg, "エラー", f"比較に失敗しました。\n{e}")
            return
        finally:
            doc_a.close()
            doc_b.close()
        state["result"] = res
        s = res["summary"]
        summary_label.setText(
            f"全 {s['total_pages']} ページ中、同じ {s['same_pages']} ページ、"
            f"違う {s['different_pages']} ページ、"
            f"Aだけ {s['only_a_pages']} ページ、Bだけ {s['only_b_pages']} ページ")
        table.setRowCount(0)
        status_text = {"same": "同じ", "different": "違う",
                       "only_a": "Aだけにある", "only_b": "Bだけにある"}
        for page_info in res["pages"]:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(str(page_info["page"])))
            table.setItem(row, 1, QTableWidgetItem(status_text[page_info["status"]]))
        detail.clear()

    compare_btn.clicked.connect(do_compare)

    def show_detail(row, _col):
        res = state["result"]
        if res is None:
            return
        page_info = res["pages"][row]
        if page_info["status"] == "same":
            detail.setPlainText(f"{page_info['page']}ページ目: テキストは同じです。")
        elif page_info["diff"]:
            detail.setPlainText(page_info["diff"])
        else:
            detail.setPlainText(f"{page_info['page']}ページ目: 差分はありません。")

    table.cellClicked.connect(show_detail)
    dlg.exec()


# ============================================================
# 13. 個人情報パターンの一括墨消し候補
# ============================================================

def show_pii_dialog(parent):
    doc = parent._trial_require_doc()
    if doc is None:
        return
    dlg = QDialog(parent)
    dlg.setWindowTitle("個人情報パターンの墨消し候補")
    dlg.resize(900, 520)
    layout = QVBoxLayout(dlg)

    table = QTableWidget(0, 5, dlg)
    table.setHorizontalHeaderLabels(["墨消しする", "種類", "ページ", "内容", "備考"])
    table.horizontalHeader().setStretchLastSection(True)
    layout.addWidget(table)

    try:
        candidates = find_pii_candidates(doc)
    except Exception as e:
        _warn(dlg, "エラー", f"検出に失敗しました。\n{e}")
        return
    for cand in candidates:
        row = table.rowCount()
        table.insertRow(row)
        check = QCheckBox(dlg)
        check.setChecked(False)
        if not cand["rects"]:
            check.setEnabled(False)
        table.setCellWidget(row, 0, check)
        table.setItem(row, 1, QTableWidgetItem(cand["kind_label"]))
        table.setItem(row, 2, QTableWidgetItem(str(cand["page"])))
        table.setItem(row, 3, QTableWidgetItem(cand["text"]))
        table.setItem(row, 4, QTableWidgetItem(cand["note"]))
        table.item(row, 1).setData(Qt.ItemDataRole.UserRole, cand)
    if not candidates:
        layout.addWidget(QLabel("個人情報らしいパターンは見つかりませんでした。", dlg))

    note = QLabel("候補は自動では消えません。チェックしたものだけ、確認後に墨消しします。"
                  "墨消しは物理削除で、保存後は復旧できません。"
                  "口座番号は「口座番号」などの表示があるもの、マイナンバーは表示があるものか"
                  "検査用数字が一致するものだけを候補にしています。", dlg)
    note.setWordWrap(True)
    layout.addWidget(note)

    btn_row = QHBoxLayout()
    redact_btn = QPushButton("チェックしたものを墨消しする", dlg)
    close_btn = QPushButton("閉じる", dlg)
    btn_row.addWidget(redact_btn)
    btn_row.addStretch()
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)

    def do_redact():
        selections = []
        for r in range(table.rowCount()):
            widget = table.cellWidget(r, 0)
            if widget is None or not widget.isChecked():
                continue
            cand = table.item(r, 1).data(Qt.ItemDataRole.UserRole)
            if cand:
                selections.append(cand)
        if not selections:
            _warn(dlg, "案内", "墨消しする候補にチェックを入れてください。")
            return
        preview = "\n".join(f"{c['kind_label']}（{c['page']}ページ）: {c['text']}" for c in selections)
        if not _confirm(dlg, "確認",
                        f"次の {len(selections)} 件を墨消し（物理削除）します。"
                        "保存後は復旧できません。よろしいですか？\n\n" + preview):
            return
        try:
            # 対象ページだけを一時文書に写し、そこで墨消ししてから
            # ReplacePagesContentCommand で Undo 可能に適用する（監査 #1）。
            # 一時文書のページ順に合わせて選択中のページ番号を付け直す。
            target_pages = sorted({int(c["page"]) for c in selections if c.get("rects")})
            page_map = {orig: pos + 1 for pos, orig in enumerate(target_pages)}
            remapped = []
            for sel in selections:
                new_sel = dict(sel)
                new_sel["page"] = page_map[int(sel["page"])]
                remapped.append(new_sel)
            res = parent._trial_apply_to_pages(
                [p - 1 for p in target_pages],
                lambda tmp: apply_pii_redactions(tmp, remapped))
        except Exception as e:
            QMessageBox.critical(dlg, "エラー", f"墨消しに失敗しました。\n{e}")
            return
        _info(dlg, "完了",
              f"{res['redacted']} 件を墨消ししました（{res['pages']} ページ）。"
              "\n保存すると PDF に反映されます。")
        dlg.accept()

    redact_btn.clicked.connect(do_redact)
    close_btn.clicked.connect(dlg.accept)
    dlg.exec()


# ============================================================
# 14. 手書き署名・署名画像挿入
# ============================================================

class SignaturePad(QWidget):
    """マウスで署名を描くパッド。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 160)
        self._pixmap = QPixmap(420, 160)
        self._pixmap.fill(Qt.GlobalColor.white)
        self._drawing = False
        self._last = None
        self._strokes = []  # [[(x, y), ...], ...]
        self._current = []

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drawing = True
            self._last = event.position().toPoint()
            self._current = [(self._last.x(), self._last.y())]

    def mouseMoveEvent(self, event):
        if not self._drawing or self._last is None:
            return
        pos = event.position().toPoint()
        painter = QPainter(self._pixmap)
        painter.setPen(QPen(Qt.GlobalColor.black, 2))
        painter.drawLine(self._last, pos)
        painter.end()
        self._last = pos
        self._current.append((pos.x(), pos.y()))
        self.update()

    def mouseReleaseEvent(self, event):
        if self._drawing:
            self._drawing = False
            if len(self._current) >= 2:
                self._strokes.append(self._current)
            self._current = []
            self._last = None

    def clear(self):
        self._pixmap.fill(Qt.GlobalColor.white)
        self._strokes = []
        self._current = []
        self.update()

    def is_empty(self):
        return not self._strokes

    def to_png_bytes(self):
        """パッドの内容を PNG バイト列にする（白背景のまま）。"""
        from PyQt6.QtCore import QBuffer, QIODevice
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        self._pixmap.save(buf, "PNG")
        data = bytes(buf.data())
        buf.close()
        return data


def show_signature_dialog(parent):
    doc = parent._trial_require_doc()
    if doc is None:
        return
    dlg = QDialog(parent)
    dlg.setWindowTitle("署名の挿入（画像 / 手書き）")
    dlg.resize(560, 640)
    layout = QVBoxLayout(dlg)

    # 配置
    form = QFormLayout()
    page_spin = QSpinBox(dlg)
    page_spin.setRange(1, len(doc))
    page_spin.setValue(1)
    form.addRow("ページ:", page_spin)
    x_spin = QSpinBox(dlg)
    x_spin.setRange(0, 500)
    x_spin.setValue(120)
    x_spin.setSuffix(" mm")
    form.addRow("左端（ページ左上から）:", x_spin)
    y_spin = QSpinBox(dlg)
    y_spin.setRange(0, 500)
    y_spin.setValue(240)
    y_spin.setSuffix(" mm")
    form.addRow("上端（ページ左上から）:", y_spin)
    w_spin = QSpinBox(dlg)
    w_spin.setRange(10, 200)
    w_spin.setValue(50)
    w_spin.setSuffix(" mm")
    form.addRow("幅:", w_spin)
    h_spin = QSpinBox(dlg)
    h_spin.setRange(5, 200)
    h_spin.setValue(20)
    h_spin.setSuffix(" mm")
    form.addRow("高さ:", h_spin)
    layout.addLayout(form)

    # 画像ファイル
    img_row = QHBoxLayout()
    img_edit = QLineEdit(dlg)
    img_edit.setPlaceholderText("署名の画像ファイル（PNG/JPG）")
    img_edit.setReadOnly(True)
    img_btn = QPushButton("画像ファイルを選ぶ", dlg)
    img_row.addWidget(img_edit)
    img_row.addWidget(img_btn)
    layout.addLayout(img_row)
    img_insert_btn = QPushButton("選んだ画像を挿入する", dlg)
    layout.addWidget(img_insert_btn)

    layout.addWidget(QLabel("— または、下のパッドにマウスで署名を描く —", dlg))
    pad = SignaturePad(dlg)
    layout.addWidget(pad)
    pad_row = QHBoxLayout()
    pad_clear_btn = QPushButton("パッドを消す", dlg)
    pad_insert_btn = QPushButton("描いた署名を挿入する", dlg)
    pad_row.addWidget(pad_clear_btn)
    pad_row.addWidget(pad_insert_btn)
    layout.addLayout(pad_row)

    note = QLabel("署名はページに描き込まれます（Undo には未対応です）。"
                  "保存すると PDF に反映されます。", dlg)
    note.setWordWrap(True)
    layout.addWidget(note)

    close_btn = QPushButton("閉じる", dlg)
    close_btn.clicked.connect(dlg.accept)
    layout.addWidget(close_btn)

    def target_rect():
        x0 = mm_to_pt(x_spin.value())
        y0 = mm_to_pt(y_spin.value())
        return fitz.Rect(x0, y0, x0 + mm_to_pt(w_spin.value()),
                         y0 + mm_to_pt(h_spin.value()))

    def pick_image():
        path, _ = QFileDialog.getOpenFileName(
            dlg, "署名画像を選ぶ", "", "Images (*.png *.jpg *.jpeg)")
        if path:
            img_edit.setText(path)

    img_btn.clicked.connect(pick_image)

    def insert_image():
        path = img_edit.text().strip()
        if not path or not os.path.isfile(path):
            _warn(dlg, "エラー", "署名の画像ファイルを選んでください。")
            return
        try:
            image_bytes = Path(path).read_bytes()
            parent._trial_apply_to_pages(
                [page_spin.value() - 1],
                lambda tmp: insert_signature_image(tmp, 1, image_bytes, target_rect()))
        except Exception as e:
            QMessageBox.critical(dlg, "エラー", f"挿入に失敗しました。\n{e}")
            return
        _info(dlg, "完了", f"{page_spin.value()}ページ目に署名画像を挿入しました。")

    img_insert_btn.clicked.connect(insert_image)

    def insert_handwritten():
        if pad.is_empty():
            _warn(dlg, "案内", "先にパッドに署名を描いてください。")
            return
        try:
            image_bytes = pad.to_png_bytes()
            parent._trial_apply_to_pages(
                [page_spin.value() - 1],
                lambda tmp: insert_signature_image(tmp, 1, image_bytes, target_rect()))
        except Exception as e:
            QMessageBox.critical(dlg, "エラー", f"挿入に失敗しました。\n{e}")
            return
        _info(dlg, "完了", f"{page_spin.value()}ページ目に手書き署名を挿入しました。")

    pad_insert_btn.clicked.connect(insert_handwritten)
    pad_clear_btn.clicked.connect(pad.clear)
    dlg.exec()
