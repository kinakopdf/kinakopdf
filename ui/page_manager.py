# -*- coding: utf-8 -*-
"""ページ管理ドック (PageManager)、非同期サムネイル生成タスク、D&Dリストウィジェット群"""
import copy
import math
import sys
import traceback
from pathlib import Path
from typing import List

import fitz
from PyQt6.QtCore import (
    QByteArray,
    QMimeData,
    QObject,
    QPointF,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QDrag,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core import (
    ERASE_WIDTH,
    HIGHLIGHT_OPACITY,
    TEXT_LINE_SPACING,
    clip_stroke_by_rects,
    split_text_only_redact_raw_rects,
    get_later_user_erase_rects,
    is_rect_substantially_erased,
    logger,
)
from ui import rgb_tuple_to_qcolor
from core.commands import (
    DeletePagesCommand,
    InsertPagesCommand,
    ReorderPagesCommand,
    RotatePagesCommand,
)
from core.geometry import (
    APP_NAME,
    DEFAULT_COLOR,
    DRAW_WIDTH,
    FITZ_LOCK,
    MIME_PDF_PAGES,
    TEXT_FONT_SIZE,
    THUMBNAIL_RENDER_SCALE,
    AnnotationDict,
    create_configured_preview_font,
    estimate_text_bbox_in_pdf,
    get_effective_cropbox,
    raw_pdf_to_view_point_from_geom,
    raw_rect_to_view_rect_from_geom,
)
from core.pdf_engine import PDFDocumentManager


class BasePageListWidget(QListWidget):
    _in_process_drag_doc = None

    @classmethod
    def take_in_process_drag_doc(cls):
        doc = cls._in_process_drag_doc
        cls._in_process_drag_doc = None
        return doc

    @classmethod
    def clear_in_process_drag_doc(cls):
        if cls._in_process_drag_doc is not None:
            try:
                with FITZ_LOCK:
                    cls._in_process_drag_doc.close()
            except Exception:
                logger.debug("Failed to close in-process drag doc", exc_info=True)
            cls._in_process_drag_doc = None

    def __init__(self, doc_provider, annot_provider=None, parent=None):
        super().__init__(parent)
        self.doc_provider = doc_provider
        self.annot_provider = annot_provider

        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setIconSize(QSize(95, 130))
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setSpacing(6)
        self.setDragEnabled(True)

    def startDrag(self, supportedActions):
        doc = self.doc_provider()
        if not doc:
            return

        selected_items = self.selectedItems()
        if not selected_items and self.currentItem() is not None:
            selected_items = [self.currentItem()]
        if not selected_items:
            return

        rows = sorted(self.row(item) for item in selected_items)
        annots = self.annot_provider() if self.annot_provider else None

        self.clear_in_process_drag_doc()
        payload = b"IN_PROCESS_DOC"
        try:
            extracted = PDFDocumentManager.extract_pages_as_doc(doc, rows, annots)
            BasePageListWidget._in_process_drag_doc = extracted
            with FITZ_LOCK:
                payload = extracted.tobytes(garbage=1, deflate=True)
        except Exception:
            logger.debug("Failed to extract drag pages", exc_info=True)
            self.clear_in_process_drag_doc()

        mime = QMimeData()
        mime.setData(MIME_PDF_PAGES, QByteArray(payload))

        drag = QDrag(self)
        drag.setMimeData(mime)
        if selected_items[0].icon():
            drag.setPixmap(selected_items[0].icon().pixmap(65, 90))
        try:
            drag.exec(
                Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                Qt.DropAction.MoveAction,
            )
        finally:
            QTimer.singleShot(500, self.clear_in_process_drag_doc)


class MainPageListWidget(BasePageListWidget):
    reorderRequested = pyqtSignal(list, int)
    pagesDropped = pyqtSignal(int, bytes)
    filesDropped = pyqtSignal(int, list)

    def __init__(self, doc_provider, annot_provider, parent=None):
        super().__init__(doc_provider, annot_provider, parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._drop_indicator_line = None

    def _is_multi_column(self):
        if self.count() >= 2:
            r0 = self.visualItemRect(self.item(0))
            r1 = self.visualItemRect(self.item(1))
            if abs(r0.center().y() - r1.center().y()) < r0.height() * 0.5:
                return True
        return False

    def _get_drop_index(self, pos, is_internal=False):
        count = self.count()
        if count == 0:
            return 0

        multi_column = self._is_multi_column()
        best_idx = count
        closest_item_idx = -1
        min_dist = float("inf")

        for i in range(count):
            rect = self.visualItemRect(self.item(i))
            cx = rect.center().x()
            cy = rect.center().y()
            dist = math.hypot(pos.x() - cx, pos.y() - cy)
            if dist < min_dist:
                min_dist = dist
                closest_item_idx = i
                if not multi_column:
                    best_idx = i + 1 if pos.y() > cy else i
                else:
                    if pos.y() > rect.bottom():
                        best_idx = i + 1
                    elif pos.y() < rect.top():
                        best_idx = i
                    else:
                        best_idx = i + 1 if pos.x() > cx else i

        if is_internal and closest_item_idx >= 0:
            selected_rows = sorted(self.row(item) for item in self.selectedItems())
            if not selected_rows and self.currentRow() >= 0:
                selected_rows = [self.currentRow()]
            if selected_rows:
                first_sel = selected_rows[0]
                last_sel = selected_rows[-1]
                if closest_item_idx < first_sel and best_idx == first_sel:
                    best_idx = closest_item_idx
                elif closest_item_idx > last_sel and best_idx == last_sel + 1:
                    best_idx = closest_item_idx + 1

        return max(0, min(best_idx, count))

    def _update_drop_indicator(self, target_idx):
        count = self.count()
        if count == 0 or target_idx is None:
            self._drop_indicator_line = None
            self.viewport().update()
            return

        multi_column = self._is_multi_column()
        if target_idx < count:
            rect = self.visualItemRect(self.item(target_idx))
            if multi_column:
                x = rect.left() - 3
                self._drop_indicator_line = (x, rect.top() + 2, x, rect.bottom() - 2)
            else:
                y = rect.top() - 3
                self._drop_indicator_line = (rect.left() + 4, y, rect.right() - 4, y)
        else:
            rect = self.visualItemRect(self.item(count - 1))
            if multi_column:
                x = rect.right() + 3
                self._drop_indicator_line = (x, rect.top() + 2, x, rect.bottom() - 2)
            else:
                y = rect.bottom() + 3
                self._drop_indicator_line = (rect.left() + 4, y, rect.right() - 4, y)

        self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop_indicator_line is not None:
            x1, y1, x2, y2 = self._drop_indicator_line
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor(0, 110, 230, 230), 3.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(x1, y1, x2, y2)
            painter.end()

    def _accept_drag_event(self, event):
        if event.source() is self:
            if event.possibleActions() & Qt.DropAction.MoveAction:
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()
            else:
                event.acceptProposedAction()
            return True
        if event.mimeData().hasFormat(MIME_PDF_PAGES):
            if event.possibleActions() & Qt.DropAction.CopyAction:
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
            else:
                event.acceptProposedAction()
            return True
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return True
        return False

    def dragEnterEvent(self, event):
        if not self._accept_drag_event(event):
            event.ignore()

    def dragMoveEvent(self, event):
        if self._accept_drag_event(event):
            pos = event.position().toPoint()
            target_idx = self._get_drop_index(pos, is_internal=(event.source() is self))
            self._update_drop_indicator(target_idx)
        else:
            self._update_drop_indicator(None)
            event.ignore()

    def dragLeaveEvent(self, event):
        self._update_drop_indicator(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._update_drop_indicator(None)
        pos = event.position().toPoint()
        target_idx = self._get_drop_index(pos, is_internal=(event.source() is self))

        if event.source() is self:
            selected_rows = sorted(self.row(item) for item in self.selectedItems())
            if not selected_rows and self.currentRow() >= 0:
                selected_rows = [self.currentRow()]

            if not selected_rows or self.count() <= 1:
                event.ignore()
                return

            selected_set = set(selected_rows)
            prefix = [i for i in range(target_idx) if i not in selected_set]
            suffix = [i for i in range(target_idx, self.count()) if i not in selected_set]
            order = prefix + selected_rows + suffix
            insert_at = len(prefix)

            self._accept_drag_event(event)
            if order != list(range(self.count())):
                QTimer.singleShot(
                    0,
                    lambda o=order, idx=insert_at: self.reorderRequested.emit(o, idx),
                )
            return

        if event.mimeData().hasFormat(MIME_PDF_PAGES):
            pdf_bytes = bytes(event.mimeData().data(MIME_PDF_PAGES))
            if pdf_bytes:
                self._accept_drag_event(event)
                payload = pdf_bytes
                if pdf_bytes == b"IN_PROCESS_DOC":
                    # startDrag の drag.exec() 終了後 500ms で _in_process_drag_doc
                    # が破棄されるため、QTimer コールバックを待たず同期処理中に
                    # 所有権を取得する（遅延するとドキュメントが閉じられる）。
                    in_proc_doc = BasePageListWidget.take_in_process_drag_doc()
                    if in_proc_doc is not None:
                        payload = in_proc_doc
                QTimer.singleShot(
                    0,
                    lambda t=target_idx, b=payload: self.pagesDropped.emit(t, b),
                )
            return

        if event.mimeData().hasUrls():
            paths = []
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    p = url.toLocalFile()
                    if p.lower().endswith(".pdf"):
                        paths.append(p)
            if paths:
                self._accept_drag_event(event)
                QTimer.singleShot(
                    0,
                    lambda t=target_idx, p=paths: self.filesDropped.emit(t, p),
                )
            return

        event.ignore()


class SubPageListWidget(BasePageListWidget):
    fileDroppedToSub = pyqtSignal(str)

    def __init__(self, doc_provider, parent=None):
        super().__init__(doc_provider, None, parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    path = url.toLocalFile()
                    event.acceptProposedAction()
                    QTimer.singleShot(0, lambda p=path: self.fileDroppedToSub.emit(p))
                    return
        event.ignore()


# ==========================================
# ページ管理
# ==========================================

class ThumbnailSignals(QObject):
    rendered = pyqtSignal(str, int, int, QImage, str)


class ThumbnailTask(QRunnable):
    def __init__(
        self,
        list_type: str,
        page_idx: int,
        doc: fitz.Document,
        gen_id: int,
        ann_list: List[AnnotationDict],
        thumb_scale: float,
        font_family: str,
    ) -> None:
        super().__init__()
        self.list_type = list_type
        self.page_idx = page_idx
        self.doc = doc
        self.gen_id = gen_id
        self.ann_list = copy.deepcopy(ann_list) if ann_list else []
        self.thumb_scale = thumb_scale
        self.font_family = font_family
        self.signals = ThumbnailSignals()

    def run(self) -> None:
        redacted_cleanly = False
        with FITZ_LOCK:
            try:
                if not self.doc or getattr(self.doc, "is_closed", False) or self.page_idx >= len(self.doc):
                    return
                page = self.doc[self.page_idx]
                page_rect = fitz.Rect(page.rect)
                page_rot = page.rotation % 360
                crop_rect = get_effective_cropbox(page)
                text_redact_rects, keep_image_rects = (
                    split_text_only_redact_raw_rects(self.ann_list)
                )
                if text_redact_rects or keep_image_rects:
                    temp_doc = fitz.open()
                    try:
                        temp_doc.insert_pdf(self.doc, from_page=self.page_idx, to_page=self.page_idx)
                        temp_page = temp_doc[0]
                        temp_page.set_rotation(0)
                        temp_page.set_cropbox(temp_page.mediabox)
                        PDFDocumentManager.apply_text_only_redactions_to_page(temp_page, text_redact_rects, keep_image_rects)
                        if temp_page.rotation != page_rot:
                            temp_page.set_rotation(page_rot)
                        if crop_rect != temp_page.mediabox:
                            temp_page.set_cropbox(crop_rect)
                        pix = temp_page.get_pixmap(
                            matrix=fitz.Matrix(self.thumb_scale, self.thumb_scale),
                            colorspace=fitz.csRGB,
                            alpha=False,
                        )
                        redacted_cleanly = True
                    finally:
                        temp_doc.close()
                else:
                    pix = page.get_pixmap(
                        matrix=fitz.Matrix(self.thumb_scale, self.thumb_scale),
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
            except Exception:
                logger.debug("Thumbnail rendering aborted", exc_info=True)
                return

        if self.ann_list and page_rect.width > 0 and page_rect.height > 0:
            PageManager.draw_annotations_on_image(
                qimg, page_rect, crop_rect, page_rot, self.ann_list, self.font_family, redacted_cleanly
            )

        label = f"{self.page_idx + 1} *" if self.ann_list else str(self.page_idx + 1)
        self.signals.rendered.emit(self.list_type, self.page_idx, self.gen_id, qimg, label)


class PageManager(QDockWidget):
    def __init__(self, main_window):
        super().__init__("ページ管理・D&D取り込み", main_window)

        self.main = main_window
        self.sub_pdf = None
        self.sub_filepath = None
        self._placeholder_icon = None
        self._pending_main_thumbs = set()
        self._pending_sub_thumbs = set()
        self._in_flight_main_thumbs = set()
        self._in_flight_sub_thumbs = set()

        self._main_generation_id = 0
        self._sub_generation_id = 0
        self.thread_pool = QThreadPool.globalInstance()

        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(20)
        self._thumb_timer.timeout.connect(self._dispatch_pending_thumbnails)

        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )

        splitter = QSplitter(Qt.Orientation.Vertical)

        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(4, 4, 4, 4)

        lbl_main = QLabel("📄 編集中のPDF（ドラッグまたはボタンで並び替え）")
        lbl_main.setStyleSheet("font-weight: bold;")
        top_layout.addWidget(lbl_main)

        self.list_widget = MainPageListWidget(
            doc_provider=lambda: self.main.viewer.current_pdf,
            annot_provider=lambda: self.main.viewer.page_annotations,
        )
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        self.list_widget.reorderRequested.connect(self.on_reorder_requested)
        self.list_widget.pagesDropped.connect(self.on_pages_dropped)
        self.list_widget.filesDropped.connect(self.on_files_dropped)
        top_layout.addWidget(self.list_widget)

        move_buttons = QHBoxLayout()
        self.btn_move_up = QPushButton("⬆ 前へ移動")
        self.btn_move_down = QPushButton("⬇ 次へ移動")
        self.btn_move_up.setToolTip("選択中のページを1つ前へ移動します")
        self.btn_move_down.setToolTip("選択中のページを1つ次へ移動します")
        self.btn_move_up.clicked.connect(lambda: self.move_selected_pages(-1))
        self.btn_move_down.clicked.connect(lambda: self.move_selected_pages(1))
        move_buttons.addWidget(self.btn_move_up)
        move_buttons.addWidget(self.btn_move_down)
        top_layout.addLayout(move_buttons)

        reverse_buttons = QHBoxLayout()
        self.btn_reverse = QPushButton("🔃 逆順にする")
        self.btn_reverse.setToolTip(
            "全ページの並びを逆順にします（元に戻す操作で戻せます）")
        self.btn_reverse.clicked.connect(self.reverse_all_pages)
        reverse_buttons.addWidget(self.btn_reverse)
        top_layout.addLayout(reverse_buttons)

        rot_buttons = QHBoxLayout()
        self.btn_rot_left = QPushButton("↺ 選択ページを左90°回転")
        self.btn_rot_right = QPushButton("↻ 選択ページを右90°回転")
        self.btn_rot_left.clicked.connect(lambda: self.rotate_selected_main_pages(-90))
        self.btn_rot_right.clicked.connect(lambda: self.rotate_selected_main_pages(90))
        rot_buttons.addWidget(self.btn_rot_left)
        rot_buttons.addWidget(self.btn_rot_right)
        top_layout.addLayout(rot_buttons)

        buttons = QHBoxLayout()
        self.btn_insert = QPushButton("➕ ファイル挿入")
        self.btn_delete = QPushButton("🗑 ページ削除")

        self.btn_insert.clicked.connect(self.insert_pdf)
        self.btn_delete.clicked.connect(self.delete_page)

        buttons.addWidget(self.btn_insert)
        buttons.addWidget(self.btn_delete)
        top_layout.addLayout(buttons)

        extract_buttons = QHBoxLayout()
        self.btn_extract = QPushButton("📄 選択ページを抽出")
        self.btn_extract.setToolTip(
            "選択中のページだけを、注釈・墨消しを反映した新しいPDFとして保存します"
        )
        self.btn_extract.clicked.connect(self.extract_selected_pages)
        extract_buttons.addWidget(self.btn_extract)
        top_layout.addLayout(extract_buttons)

        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(4, 8, 4, 4)

        sub_title_row = QHBoxLayout()
        self.lbl_sub = QLabel("📥 取り込み元PDF（ドラッグして上へ挿入）")
        self.lbl_sub.setStyleSheet("font-weight: bold;")
        sub_title_row.addWidget(self.lbl_sub)
        self.btn_open_sub_new_window = QPushButton("🗔 新規ウインドウで開く")
        self.btn_open_sub_new_window.setToolTip(
            "取り込み元のPDFを、別のウインドウで開きます")
        self.btn_open_sub_new_window.clicked.connect(
            self.open_sub_in_new_window)
        self.btn_open_sub_new_window.setEnabled(False)
        sub_title_row.addWidget(self.btn_open_sub_new_window)
        sub_title_row.addStretch(1)
        bottom_layout.addLayout(sub_title_row)

        self.sub_list_widget = SubPageListWidget(
            doc_provider=lambda: self.sub_pdf,
        )
        self.sub_list_widget.fileDroppedToSub.connect(self.load_sub_pdf_from_path)
        bottom_layout.addWidget(self.sub_list_widget)

        sub_rot_buttons = QHBoxLayout()
        self.btn_sub_rot_left = QPushButton("↺ 選択ページ左回転")
        self.btn_sub_rot_right = QPushButton("↻ 選択ページ右回転")
        self.btn_sub_rot_left.clicked.connect(lambda: self.rotate_sub_pages(-90))
        self.btn_sub_rot_right.clicked.connect(lambda: self.rotate_sub_pages(90))
        sub_rot_buttons.addWidget(self.btn_sub_rot_left)
        sub_rot_buttons.addWidget(self.btn_sub_rot_right)
        bottom_layout.addLayout(sub_rot_buttons)

        sub_buttons = QHBoxLayout()
        self.btn_open_sub = QPushButton("📂 取り込み元を開く")
        self.btn_insert_selected = QPushButton("⬆ 選択ページを挿入")

        self.btn_open_sub.clicked.connect(self.open_sub_pdf)
        self.btn_insert_selected.clicked.connect(self.insert_selected_sub_pages)

        sub_buttons.addWidget(self.btn_open_sub)
        sub_buttons.addWidget(self.btn_insert_selected)
        bottom_layout.addLayout(sub_buttons)

        splitter.addWidget(top_widget)
        splitter.addWidget(bottom_widget)
        splitter.setSizes([450, 350])

        self.list_widget.verticalScrollBar().valueChanged.connect(self._trigger_thumb_queue)
        self.sub_list_widget.verticalScrollBar().valueChanged.connect(self._trigger_thumb_queue)

        self.setWidget(splitter)

    def clear_thumbnail_cache(self):
        self._main_generation_id += 1
        self._sub_generation_id += 1
        self._pending_main_thumbs.clear()
        self._pending_sub_thumbs.clear()
        self._in_flight_main_thumbs.clear()
        self._in_flight_sub_thumbs.clear()

    def move_selected_pages(self, direction):
        viewer = self.main.viewer
        doc = viewer.current_pdf
        if not doc or len(doc) <= 1:
            return

        viewer.cancel_active_interaction()
        selected = self.list_widget.selectedItems()
        if selected:
            rows = sorted(set(self.list_widget.row(item) for item in selected))
        else:
            rows = [viewer.current_page_idx]

        rows = [r for r in rows if 0 <= r < len(doc)]
        if not rows:
            return

        order = list(range(len(doc)))
        selected_set = set(rows)

        if direction < 0:
            for i in range(1, len(order)):
                if order[i] in selected_set and order[i - 1] not in selected_set:
                    order[i - 1], order[i] = order[i], order[i - 1]
        else:
            for i in range(len(order) - 2, -1, -1):
                if order[i] in selected_set and order[i + 1] not in selected_set:
                    order[i], order[i + 1] = order[i + 1], order[i]

        if order == list(range(len(doc))):
            return

        new_rows = [order.index(r) for r in rows]
        next_page_idx = new_rows[0]
        self.on_reorder_requested(order, next_page_idx)

        for r in new_rows:
            if 0 <= r < self.list_widget.count():
                self.list_widget.item(r).setSelected(True)

    def reverse_all_pages(self):
        """全ページの並びを逆順にする（Undo可能）。"""
        viewer = self.main.viewer
        doc = viewer.current_pdf
        if not doc or len(doc) <= 1:
            return

        viewer.cancel_active_interaction()
        order = list(range(len(doc)))[::-1]
        # 今見ているページ（同じ内容のページ）を表示し続ける
        next_idx = len(doc) - 1 - viewer.current_page_idx
        self.on_reorder_requested(order, next_idx)

        self.main.statusBar().showMessage(
            f"全{len(doc)}ページを逆順にしました")

    def rotate_selected_main_pages(self, delta_angle):
        viewer = self.main.viewer
        doc = viewer.current_pdf

        if not doc:
            return

        viewer.cancel_active_interaction()
        selected = self.list_widget.selectedItems()
        if not selected:
            rows = [viewer.current_page_idx]
        else:
            rows = sorted(self.list_widget.row(item) for item in selected)

        rotations = []
        for idx in rows:
            if 0 <= idx < len(doc):
                old_rot = doc[idx].rotation
                new_rot = (old_rot + delta_angle) % 360
                if old_rot != new_rot:
                    rotations.append((idx, old_rot, new_rot))

        if not rotations:
            return

        cmd = RotatePagesCommand(rotations)
        cmd.redo(viewer)
        viewer.history.push(cmd)

        for r in rows:
            if 0 <= r < self.list_widget.count():
                self.list_widget.item(r).setSelected(True)

        self.main.statusBar().showMessage(
            f"{len(rotations)}ページを一括回転しました"
        )

    def open_sub_in_new_window(self):
        """取り込み元のPDFを新しいウインドウで開く。"""
        if not self.sub_filepath:
            return
        self.main.spawn_new_window(self.sub_filepath)

    def close_sub_pdf(self):
        if self.sub_pdf is not None:
            with FITZ_LOCK:
                self.sub_pdf.close()
            self.sub_pdf = None
        btn = getattr(self, "btn_open_sub_new_window", None)
        if btn is not None:
            btn.setEnabled(False)

    def sync_selection(self, page_idx):
        if 0 <= page_idx < self.list_widget.count():
            selected_rows = [self.list_widget.row(i) for i in self.list_widget.selectedItems()]
            if len(selected_rows) > 1 and page_idx in selected_rows:
                for r in selected_rows:
                    item = self.list_widget.item(r)
                    if item is not None:
                        item.setSelected(True)
            else:
                self.list_widget.setCurrentRow(page_idx)
        self.main.update_page_indicator()

    @staticmethod
    def draw_annotations_on_image(
        image: QImage,
        page_rect: fitz.Rect,
        crop_rect: fitz.Rect,
        page_rot: int,
        ann_list: List[AnnotationDict],
        font_family: str,
        skip_text_redact_fill: bool = False,
    ) -> None:
        if not ann_list or page_rect.width == 0 or page_rect.height == 0:
            return

        sx = image.width() / page_rect.width
        sy = image.height() / page_rect.height

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if not skip_text_redact_fill:
            # 画像を残す消去（OCR文字の不可視置換）は白塗りしない
            for r_raw in split_text_only_redact_raw_rects(ann_list)[0]:
                vr = raw_rect_to_view_rect_from_geom(crop_rect, page_rect, page_rot, r_raw)
                qrect = QRectF(
                    (vr.x0 - page_rect.x0) * sx,
                    (vr.y0 - page_rect.y0) * sy,
                    vr.width * sx,
                    vr.height * sy,
                )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(255, 255, 255, 255)))
                painter.drawRect(qrect)

        for idx, ann in enumerate(ann_list):
            if ann["type"] == "text_redact":
                continue

            later_erases = get_later_user_erase_rects(ann_list, idx, ann)

            if ann["type"] == "box_erase":
                vr = raw_rect_to_view_rect_from_geom(crop_rect, page_rect, page_rot, ann["rect"])
                qrect = QRectF(
                    (vr.x0 - page_rect.x0) * sx,
                    (vr.y0 - page_rect.y0) * sy,
                    vr.width * sx,
                    vr.height * sy,
                )
                painter.setPen(QPen(QColor(220, 0, 0, 180), 1))
                painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
                painter.drawRect(qrect)

            elif ann["type"] == "highlight":
                r = fitz.Rect(ann["rect"])
                r.normalize()
                if is_rect_substantially_erased(r, later_erases):
                    continue
                vr = raw_rect_to_view_rect_from_geom(crop_rect, page_rect, page_rot, r)
                qrect = QRectF(
                    (vr.x0 - page_rect.x0) * sx,
                    (vr.y0 - page_rect.y0) * sy,
                    vr.width * sx,
                    vr.height * sy,
                )
                alpha = int(round(float(ann.get("opacity", HIGHLIGHT_OPACITY)) * 255))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(rgb_tuple_to_qcolor(ann.get("color", (0.98, 0.90, 0.05)), alpha)))
                painter.drawRect(qrect)

            elif ann["type"] == "erase":
                pts = ann.get("points", [])
                if not pts:
                    continue
                width = max(2.0, ERASE_WIDTH * sx)
                pen = QPen(QColor(255, 255, 255, 235), width)
                pen.setCapStyle(Qt.PenCapStyle.SquareCap)
                pen.setJoinStyle(Qt.PenJoinStyle.BevelJoin)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                path = QPainterPath()
                p0 = raw_pdf_to_view_point_from_geom(crop_rect, page_rect, page_rot, pts[0])
                x0, y0 = (p0.x - page_rect.x0) * sx, (p0.y - page_rect.y0) * sy
                path.moveTo(x0, y0)
                has_len = False
                for px, py in pts[1:]:
                    vp = raw_pdf_to_view_point_from_geom(crop_rect, page_rect, page_rot, (px, py))
                    vx, vy = (vp.x - page_rect.x0) * sx, (vp.y - page_rect.y0) * sy
                    if math.hypot(vx - x0, vy - y0) >= 0.1:
                        has_len = True
                    path.lineTo(vx, vy)
                if not has_len:
                    path.lineTo(x0 + 0.1, y0 + 0.1)
                painter.drawPath(path)

            elif ann["type"] == "draw":
                segments = clip_stroke_by_rects(ann["points"], later_erases)
                stroke_color = rgb_tuple_to_qcolor(ann.get("color", DEFAULT_COLOR))
                stroke_width = max(1.0, float(ann.get("width", DRAW_WIDTH)) * sx)
                pen = QPen(stroke_color, stroke_width)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                for seg in segments:
                    if len(seg) < 2:
                        continue
                    path = QPainterPath()
                    p0 = raw_pdf_to_view_point_from_geom(crop_rect, page_rect, page_rot, seg[0])
                    x0, y0 = (p0.x - page_rect.x0) * sx, (p0.y - page_rect.y0) * sy
                    path.moveTo(x0, y0)
                    has_len = False
                    for px, py in seg[1:]:
                        vp = raw_pdf_to_view_point_from_geom(crop_rect, page_rect, page_rot, (px, py))
                        vx, vy = (vp.x - page_rect.x0) * sx, (vp.y - page_rect.y0) * sy
                        if math.hypot(vx - x0, vy - y0) >= 0.1:
                            has_len = True
                        path.lineTo(vx, vy)
                    if not has_len:
                        path.lineTo(x0 + 0.1, y0 + 0.1)
                    painter.drawPath(path)

            elif ann["type"] == "text":
                # 不可視テキスト（OCRレイヤーの置換）は字形を描かない
                if ann.get("invisible"):
                    continue
                text_bbox = estimate_text_bbox_in_pdf(ann)
                if is_rect_substantially_erased(text_bbox, later_erases):
                    continue
                fontsize = float(ann.get("fontsize", TEXT_FONT_SIZE))
                text_color = rgb_tuple_to_qcolor(ann.get("color", (0.0, 0.0, 0.0)))
                ann_rot = int(ann.get("rotation", page_rot)) % 360

                vp = raw_pdf_to_view_point_from_geom(crop_rect, page_rect, page_rot, ann["pos"])
                tx = (vp.x - page_rect.x0) * sx
                ty = (vp.y - page_rect.y0) * sy

                painter.save()
                painter.translate(tx, ty)
                painter.rotate((page_rot - ann_rot) % 360)
                font = create_configured_preview_font(
                    font_family or "Sans Serif",
                    max(2, int(round(fontsize * sx))),
                )
                painter.setFont(font)
                painter.setPen(QPen(text_color))
                for line_idx, line_str in enumerate(str(ann.get("text", "")).split("\n")):
                    if line_str:
                        y_off = fontsize * sy * (0.88 + line_idx * TEXT_LINE_SPACING)
                        painter.drawText(QPointF(0.0, y_off), line_str)
                painter.restore()

        painter.end()

    def _get_placeholder_icon(self):
        if self._placeholder_icon is None:
            pix = QPixmap(95, 130)
            pix.fill(QColor(246, 246, 248))
            painter = QPainter(pix)
            painter.setPen(QPen(QColor(205, 205, 212), 1))
            painter.drawRect(0, 0, 94, 129)
            painter.end()
            self._placeholder_icon = QIcon(pix)
        return self._placeholder_icon

    def _trigger_thumb_queue(self, *_args):
        if (self._pending_main_thumbs or self._pending_sub_thumbs) and not self._thumb_timer.isActive():
            self._thumb_timer.start()

    def _pick_priority_index(self, list_widget, pending_set, prefer_idx=None):
        if not pending_set:
            return None
        if prefer_idx is not None and prefer_idx in pending_set:
            return prefer_idx

        vp_rect = list_widget.viewport().rect()
        if not vp_rect.isEmpty():
            expanded_rect = vp_rect.adjusted(-50, -180, 50, 260)
            for idx in sorted(pending_set):
                item = list_widget.item(idx)
                if item is not None:
                    item_rect = list_widget.visualItemRect(item)
                    if not item_rect.isEmpty() and expanded_rect.intersects(item_rect):
                        return idx

        return min(pending_set)

    def _dispatch_pending_thumbnails(self):
        active_tasks = len(self._in_flight_main_thumbs) + len(self._in_flight_sub_thumbs)
        max_concurrent = max(2, min(4, self.thread_pool.maxThreadCount()))

        while active_tasks < max_concurrent and (self._pending_main_thumbs or self._pending_sub_thumbs):
            if self._pending_main_thumbs and self.main.viewer.current_pdf:
                idx = self._pick_priority_index(
                    self.list_widget,
                    self._pending_main_thumbs,
                    prefer_idx=self.main.viewer.current_page_idx,
                )
                if idx is not None:
                    self._pending_main_thumbs.discard(idx)
                    self._in_flight_main_thumbs.add(idx)
                    ann_list = self.main.viewer.page_annotations.get(idx, [])
                    task = ThumbnailTask(
                        list_type="main",
                        page_idx=idx,
                        doc=self.main.viewer.current_pdf,
                        gen_id=self._main_generation_id,
                        ann_list=ann_list,
                        thumb_scale=THUMBNAIL_RENDER_SCALE,
                        font_family=self.main.viewer.preview_font_family,
                    )
                    task.signals.rendered.connect(self._on_thumbnail_rendered)
                    self.thread_pool.start(task)
                    active_tasks += 1
                    continue

            if self._pending_sub_thumbs and self.sub_pdf:
                idx = self._pick_priority_index(
                    self.sub_list_widget,
                    self._pending_sub_thumbs,
                )
                if idx is not None:
                    self._pending_sub_thumbs.discard(idx)
                    self._in_flight_sub_thumbs.add(idx)
                    task = ThumbnailTask(
                        list_type="sub",
                        page_idx=idx,
                        doc=self.sub_pdf,
                        gen_id=self._sub_generation_id,
                        ann_list=[],
                        thumb_scale=THUMBNAIL_RENDER_SCALE,
                        font_family=self.main.viewer.preview_font_family,
                    )
                    task.signals.rendered.connect(self._on_thumbnail_rendered)
                    self.thread_pool.start(task)
                    active_tasks += 1
                    continue

            break

        if not self._pending_main_thumbs and not self._pending_sub_thumbs:
            self._thumb_timer.stop()

    def _on_thumbnail_rendered(self, list_type: str, page_idx: int, gen_id: int, image: QImage, label: str):
        expected_gen_id = (
            self._main_generation_id if list_type == "main"
            else self._sub_generation_id if list_type == "sub"
            else None
        )
        if expected_gen_id is None:
            return
        if gen_id != expected_gen_id:
            # Stale delivery (list was refreshed while this task ran):
            # free the in-flight slot and re-queue so the page is
            # re-rendered with the current generation instead of
            # staying a placeholder forever.
            if list_type == "main":
                self._in_flight_main_thumbs.discard(page_idx)
                if self.main.viewer.current_pdf and 0 <= page_idx < self.list_widget.count():
                    self._pending_main_thumbs.add(page_idx)
            else:
                self._in_flight_sub_thumbs.discard(page_idx)
                if self.sub_pdf and 0 <= page_idx < self.sub_list_widget.count():
                    self._pending_sub_thumbs.add(page_idx)
            self._trigger_thumb_queue()
            return

        pixmap = QPixmap.fromImage(image)
        icon = QIcon(pixmap)

        if list_type == "main":
            self._in_flight_main_thumbs.discard(page_idx)
            if self.main.viewer.current_pdf and page_idx < self.list_widget.count():
                item = self.list_widget.item(page_idx)
                if item is not None:
                    item.setText(label)
                    item.setIcon(icon)
        elif list_type == "sub":
            self._in_flight_sub_thumbs.discard(page_idx)
            if self.sub_pdf and page_idx < self.sub_list_widget.count():
                item = self.sub_list_widget.item(page_idx)
                if item is not None:
                    item.setText(str(page_idx + 1))
                    item.setIcon(icon)

        self._trigger_thumb_queue()

    def update_thumbnails(self, page_indices=None):
        doc = self.main.viewer.current_pdf
        if not doc:
            self._pending_main_thumbs.clear()
            self._in_flight_main_thumbs.clear()
            self.list_widget.clear()
            self.main.update_page_indicator()
            return

        if page_indices is None or self.list_widget.count() != len(doc):
            self.refresh_thumbnails()
            return

        if isinstance(page_indices, int):
            page_indices = [page_indices]

        ann_map = self.main.viewer.page_annotations
        target_set = {i for i in page_indices if 0 <= i < len(doc)}

        for i in target_set:
            item = self.list_widget.item(i)
            if item is not None:
                ann_list = ann_map.get(i, [])
                item.setText(f"{i + 1} *" if ann_list else str(i + 1))
            self._pending_main_thumbs.add(i)

        self.main.update_page_indicator()
        self._trigger_thumb_queue()

    def refresh_thumbnails(self):
        self._main_generation_id += 1
        self._pending_main_thumbs.clear()
        self._in_flight_main_thumbs.clear()
        self.list_widget.clear()

        doc = self.main.viewer.current_pdf
        if not doc:
            self.main.update_page_indicator()
            return

        ann_map = self.main.viewer.page_annotations
        placeholder = self._get_placeholder_icon()

        for i in range(len(doc)):
            ann_list = ann_map.get(i, [])
            label = f"{i + 1} *" if ann_list else str(i + 1)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setIcon(placeholder)
            self._pending_main_thumbs.add(i)
            self.list_widget.addItem(item)

        self.sync_selection(self.main.viewer.current_page_idx)
        self._trigger_thumb_queue()

    def refresh_sub_thumbnails(self, selected_rows=None):
        self._sub_generation_id += 1
        self._pending_sub_thumbs.clear()
        self._in_flight_sub_thumbs.clear()
        self.sub_list_widget.clear()
        if not self.sub_pdf:
            return

        placeholder = self._get_placeholder_icon()
        for i in range(len(self.sub_pdf)):
            item = QListWidgetItem(f"{i + 1}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setIcon(placeholder)
            self._pending_sub_thumbs.add(i)
            self.sub_list_widget.addItem(item)
            if selected_rows and i in selected_rows:
                item.setSelected(True)

        self._trigger_thumb_queue()

    def rotate_sub_pages(self, delta_angle):
        if not self.sub_pdf:
            return
        selected = self.sub_list_widget.selectedItems()
        if not selected:
            if len(self.sub_pdf) == 1:
                rows = [0]
            else:
                self.main.statusBar().showMessage("回転したい取り込み元ページを選択してください")
                return
        else:
            rows = [self.sub_list_widget.row(item) for item in selected]

        with FITZ_LOCK:
            for r in rows:
                if 0 <= r < len(self.sub_pdf):
                    page = self.sub_pdf[r]
                    page.set_rotation((page.rotation + delta_angle) % 360)

        self.refresh_sub_thumbnails(selected_rows=set(rows))

    def on_item_clicked(self, item):
        viewer = self.main.viewer
        if not viewer.current_pdf:
            return

        # Ctrl/Shift クリックは選択だけを変更し、ページ移動はしない。
        # （修飾キーなしの通常クリックだけがページ移動する）
        modifiers = QApplication.keyboardModifiers()
        if modifiers & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ):
            return

        selected_rows = [self.list_widget.row(i) for i in self.list_widget.selectedItems()]
        idx = self.list_widget.row(item)
        if idx != viewer.current_page_idx:
            viewer.load_pdf(viewer.current_pdf, idx, fit_view=True)
            if len(selected_rows) > 1:
                for r in selected_rows:
                    if 0 <= r < self.list_widget.count():
                        self.list_widget.item(r).setSelected(True)

    def on_reorder_requested(self, desired, next_idx):
        viewer = self.main.viewer
        doc = viewer.current_pdf
        if not doc:
            return

        viewer.cancel_active_interaction()
        prev_idx = viewer.current_page_idx
        cmd = ReorderPagesCommand(desired, prev_idx, next_idx)
        cmd.redo(viewer)
        viewer.history.push(cmd)

    def on_pages_dropped(self, target_idx, pdf_source):
        viewer = self.main.viewer
        doc = viewer.current_pdf

        viewer.cancel_active_interaction()
        in_proc_doc = None
        if isinstance(pdf_source, fitz.Document):
            in_proc_doc = pdf_source
        else:
            in_proc_doc = BasePageListWidget.take_in_process_drag_doc()
            if (
                in_proc_doc is None
                and isinstance(pdf_source, (bytes, bytearray, QByteArray))
                and pdf_source
                and bytes(pdf_source) != b"IN_PROCESS_DOC"
            ):
                with FITZ_LOCK:
                    in_proc_doc = fitz.open(stream=bytes(pdf_source), filetype="pdf")

        if in_proc_doc is None:
            return

        if len(in_proc_doc) == 0:
            in_proc_doc.close()
            return

        if not doc:
            viewer.current_file = None
            viewer.current_pdf = in_proc_doc
            viewer.current_pdf_was_encrypted = False
            viewer.reset_history()
            viewer.history.mark_unsaved()
            self.clear_thumbnail_cache()
            viewer.load_pdf(in_proc_doc, 0, fit_view=True)
            self.refresh_thumbnails()
            self.main.setWindowTitle(f"{APP_NAME} - (新規結合ドキュメント)")
            return

        insert_count = len(in_proc_doc)
        target_idx = max(0, min(target_idx, len(doc)))
        cmd = InsertPagesCommand(
            target_idx,
            in_proc_doc,
            insert_count,
            viewer.current_page_idx,
            take_ownership=True,
        )
        cmd.redo(viewer)
        viewer.history.push(cmd)
        self.main.statusBar().showMessage(
            f"{target_idx + 1}ページ目の位置に {insert_count} ページを挿入しました"
        )

    def on_files_dropped(self, target_idx, filepaths):
        if not filepaths:
            return

        viewer = self.main.viewer
        if not viewer.current_pdf:
            self.main.open_file_from_path(filepaths[0])
            if not viewer.current_pdf:
                return
            filepaths = filepaths[1:]
            target_idx = len(viewer.current_pdf)

        if not filepaths or not viewer.current_pdf:
            return

        with FITZ_LOCK:
            combined = fitz.open()
        for fp in filepaths:
            src = self.main.open_pdf_with_password(fp)
            if src is not None:
                with FITZ_LOCK:
                    combined.insert_pdf(src)
                    src.close()

        with FITZ_LOCK:
            has_pages = len(combined) > 0
            if not has_pages:
                combined.close()
        if has_pages:
            self.on_pages_dropped(target_idx, combined)

    def open_sub_pdf(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "取り込み元のPDFを選択",
            "",
            "PDF Files (*.pdf)",
        )
        if filepath:
            self.load_sub_pdf_from_path(filepath)

    def load_sub_pdf_from_path(self, filepath):
        doc = self.main.open_pdf_with_password(filepath)
        if doc is None:
            return

        if len(doc) == 0:
            doc.close()
            QMessageBox.warning(self, "エラー", "ページが存在しません。")
            return

        self.close_sub_pdf()
        self.sub_pdf = doc
        self.sub_filepath = filepath
        self.lbl_sub.setText(f"📥 取り込み元: {Path(filepath).name}")
        self.btn_open_sub_new_window.setEnabled(True)
        self.refresh_sub_thumbnails()

    def insert_selected_sub_pages(self):
        if not self.sub_pdf:
            QMessageBox.information(self, "案内", "先に取り込み元PDFを開いてください。")
            return

        selected = self.sub_list_widget.selectedItems()
        if not selected:
            QMessageBox.information(self, "案内", "挿入したいページを選択してください。")
            return

        rows = sorted(self.sub_list_widget.row(i) for i in selected)
        extracted_doc = PDFDocumentManager.extract_pages_as_doc(self.sub_pdf, rows)

        viewer = self.main.viewer
        target_idx = (
            viewer.current_page_idx + 1
            if viewer.current_pdf
            else 0
        )
        self.on_pages_dropped(target_idx, extracted_doc)

    def extract_selected_pages(self):
        """選択中のページだけを新しいPDFとして保存する（抽出）。

        注釈・墨消しは通常の保存と同じ bake_annotations_to_doc で焼き込み、
        画面で見ている状態のまま抽出する。抽出元の文書は変更しない。
        """
        viewer = self.main.viewer
        doc = viewer.current_pdf
        if not doc:
            QMessageBox.information(self, "案内", "先にPDFを開いてください。")
            return

        selected = self.list_widget.selectedItems()
        if not selected:
            QMessageBox.information(self, "案内", "抽出したいページを選択してください。")
            return

        rows = sorted(set(self.list_widget.row(item) for item in selected))
        rows = [r for r in rows if 0 <= r < len(doc)]
        if not rows:
            QMessageBox.information(self, "案内", "抽出したいページを選択してください。")
            return

        default_path = ""
        if viewer.current_file:
            src = Path(viewer.current_file)
            default_path = str(src.with_name(f"{src.stem}_抽出.pdf"))
        filepath, _ = QFileDialog.getSaveFileName(
            self,
            "選択ページを抽出して保存",
            default_path,
            "PDF Files (*.pdf)",
        )
        if not filepath:
            return
        if not filepath.lower().endswith(".pdf"):
            filepath += ".pdf"

        extracted = None
        try:
            extracted = PDFDocumentManager.extract_pages_as_doc(
                doc, rows, viewer.page_annotations
            )
            # 抽出元文書のメタデータ（タイトル・作成者など）を引き継ぐ
            try:
                extracted.set_metadata(dict(doc.metadata or {}))
            except Exception:
                logger.debug("Extract metadata copy skipped", exc_info=True)
            _saved_doc, err = PDFDocumentManager.save_safely(
                source_doc=extracted,
                page_annotations={},
                filepath=filepath,
                current_file=None,
            )
            if err is not None:
                raise err
        except Exception as e:
            QMessageBox.critical(
                self,
                "抽出エラー",
                f"選択ページの抽出に失敗しました。\n{e}",
            )
            return
        finally:
            if extracted is not None:
                try:
                    with FITZ_LOCK:
                        extracted.close()
                except Exception:
                    logger.debug("Failed to close extracted doc", exc_info=True)

        QMessageBox.information(
            self,
            "抽出完了",
            f"{len(rows)} ページを抽出して保存しました。\n保存先: {filepath}",
        )

    def delete_page(self):
        viewer = self.main.viewer
        doc = viewer.current_pdf
        if not doc:
            return

        viewer.cancel_active_interaction()
        selected = self.list_widget.selectedItems()
        if selected:
            rows = sorted(set(self.list_widget.row(item) for item in selected))
        else:
            rows = [viewer.current_page_idx]

        rows = [r for r in rows if 0 <= r < len(doc)]
        if not rows:
            return

        if len(rows) >= len(doc):
            QMessageBox.warning(
                self,
                "削除できません",
                "PDFには最低1ページ必要です。",
            )
            return

        if len(rows) == 1:
            msg = f"{rows[0] + 1}ページ目を削除しますか？"
        else:
            page_nums = ", ".join(str(r + 1) for r in rows[:8])
            if len(rows) > 8:
                page_nums += " ..."
            msg = f"選択された {len(rows)} ページ（{page_nums}ページ目）を削除しますか？"

        reply = QMessageBox.question(self, "確認", msg)
        if reply != QMessageBox.StandardButton.Yes:
            return

        cmd = DeletePagesCommand(doc, rows, viewer.page_annotations, viewer.current_page_idx)
        cmd.redo(viewer)
        viewer.history.push(cmd)

    def insert_pdf(self):
        viewer = self.main.viewer
        doc = viewer.current_pdf
        if not doc:
            return

        viewer.cancel_active_interaction()
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "挿入するPDFを選択",
            "",
            "PDF Files (*.pdf)",
        )

        if not filepath:
            return

        insert_doc = self.main.open_pdf_with_password(filepath)
        if insert_doc is None:
            return

        insert_count = len(insert_doc)
        if insert_count == 0:
            insert_doc.close()
            return

        row = viewer.current_page_idx
        target_idx = row + 1 if 0 <= row < len(doc) else len(doc)

        cmd = InsertPagesCommand(
            target_idx,
            insert_doc,
            insert_count,
            viewer.current_page_idx,
            take_ownership=True,
        )
        cmd.redo(viewer)
        viewer.history.push(cmd)
