# -*- coding: utf-8 -*-
"""Undo/Redo 履歴管理 (HistoryManager) および操作単位コマンド群"""
import copy
import fitz
from PyQt6.QtWidgets import QMessageBox

from core import (
    logger,
    adjust_updated_after_on_remove,
    annotation_affects_base_pdf_text,
)
from core.geometry import FITZ_LOCK, MAX_UNDO_STEPS


class BaseCommand:
    def __init__(self):
        self.prev_state_id = 0
        self.next_state_id = 0

    def dispose(self):
        """コマンド破棄時に保持しているインメモリドキュメント等を解放する"""
        pass

    def undo(self, viewer):
        raise NotImplementedError

    def redo(self, viewer):
        raise NotImplementedError


def _clamp_page_idx(viewer, page_idx):
    """viewer.current_pdf の範囲内に page_idx を収める。ドキュメントが無い/0ページなら None。"""
    doc = getattr(viewer, "current_pdf", None)
    if doc is None:
        return None
    try:
        page_count = len(doc)
    except Exception:
        return None
    if page_count <= 0:
        return None
    return max(0, min(int(page_idx), page_count - 1))


def _set_current_page_safe(viewer, page_idx):
    """current_page_idx を 0..len(doc)-1 の範囲内に収めて設定する。範囲外には決して設定しない。"""
    clamped = _clamp_page_idx(viewer, page_idx)
    if clamped is not None:
        viewer.current_page_idx = clamped
    return clamped


def _find_annotation_from_end(ann_list, target):
    """target と等しい注釈を末尾から探し、そのインデックスを返す。無ければ -1。"""
    for idx in range(len(ann_list) - 1, -1, -1):
        if ann_list[idx] == target:
            return idx
    return -1


def _restore_removed_annotations(ann_list, removed_pairs):
    """削除された注釈を元の位置に復元する（audit #1 のフォールバック）。

    復元された注釈は元の updated_after をそのまま保持する。
    生き残った注釈の updated_after だけ、削除時の adjust_updated_after_on_remove
    の逆変換（削除位置より大きい閾値を +1）で戻す。復元注釈自体には触れない。
    なお削除前の updated_after が削除位置と等しかった場合（元の値が p か p+1 か）
    は復元後の状態だけでは判別できないため、完全な復元にはコンストラクタに
    渡す before_annotations スナップショットが必要。
    """
    restored_ids = set()
    for idx, ann in removed_pairs:
        insert_pos = max(0, min(int(idx), len(ann_list)))
        restored = copy.deepcopy(ann)
        ann_list.insert(insert_pos, restored)
        restored_ids.add(id(restored))
    for idx, _ in removed_pairs:
        for ann in ann_list:
            if id(ann) in restored_ids:
                continue
            if "updated_after" in ann and ann["updated_after"] > idx:
                ann["updated_after"] = int(ann["updated_after"]) + 1


def _set_page_annotations_verbatim(viewer, page_idx, annotations):
    """ページの注釈リストを丸ごと置き換える（空ならキーを削除）。"""
    if annotations:
        viewer.page_annotations[page_idx] = copy.deepcopy(annotations)
    else:
        viewer.page_annotations.pop(page_idx, None)


class AddAnnotationCommand(BaseCommand):
    def __init__(self, page_idx, annotation):
        super().__init__()
        self.page_idx = page_idx
        self.annotation = copy.deepcopy(annotation)

    def undo(self, viewer):
        ann_list = viewer.page_annotations.get(self.page_idx, [])
        found_idx = _find_annotation_from_end(ann_list, self.annotation)
        if found_idx < 0:
            logger.debug("AddAnnotationCommand.undo: annotation not found; no-op")
        else:
            ann_list.pop(found_idx)
            adjust_updated_after_on_remove(ann_list, found_idx)
        page_changed = viewer.current_page_idx != self.page_idx
        rebuild = page_changed or annotation_affects_base_pdf_text(self.annotation)
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=page_changed, rebuild_base=rebuild)
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])

    def redo(self, viewer):
        ann_list = viewer.page_annotations.setdefault(self.page_idx, [])
        ann_list.append(copy.deepcopy(self.annotation))
        page_changed = viewer.current_page_idx != self.page_idx
        rebuild = page_changed or annotation_affects_base_pdf_text(self.annotation)
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=page_changed, rebuild_base=rebuild)
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])


class RemoveAnnotationCommand(BaseCommand):
    def __init__(self, page_idx, ann_idx, removed_annotation, before_annotations=None):
        super().__init__()
        self.page_idx = page_idx
        self.ann_idx = ann_idx
        self.removed_annotation = copy.deepcopy(removed_annotation)
        # 削除前のページ注釈リスト全体のスナップショット（任意）。
        # 渡された場合、undo/redo はリストを丸ごと復元するため updated_after
        # のずれは発生しない（audit #1 の完全な修正。ReplacePageAnnotationsCommand
        # と同じ方式）。渡されない場合は _restore_removed_annotations による
        # フォールバックで復元する。呼び出し側が削除前のリストを渡すと完全になる。
        self._before_annotations = (
            copy.deepcopy(before_annotations) if before_annotations is not None else None
        )
        if self._before_annotations is not None:
            after = copy.deepcopy(self._before_annotations)
            if 0 <= ann_idx < len(after):
                del after[ann_idx]
                adjust_updated_after_on_remove(after, ann_idx)
            self._after_annotations = after
        else:
            self._after_annotations = None

    def undo(self, viewer):
        if self._before_annotations is not None:
            _set_page_annotations_verbatim(viewer, self.page_idx, self._before_annotations)
        else:
            ann_list = viewer.page_annotations.setdefault(self.page_idx, [])
            _restore_removed_annotations(ann_list, [(self.ann_idx, self.removed_annotation)])
        page_changed = viewer.current_page_idx != self.page_idx
        rebuild = page_changed or annotation_affects_base_pdf_text(self.removed_annotation)
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=page_changed, rebuild_base=rebuild)
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])

    def redo(self, viewer):
        if self._after_annotations is not None:
            _set_page_annotations_verbatim(viewer, self.page_idx, self._after_annotations)
        else:
            ann_list = viewer.page_annotations.get(self.page_idx, [])
            found_idx = _find_annotation_from_end(ann_list, self.removed_annotation)
            if found_idx < 0:
                logger.debug("RemoveAnnotationCommand.redo: annotation not found; no-op")
            else:
                ann_list.pop(found_idx)
                adjust_updated_after_on_remove(ann_list, found_idx)
        page_changed = viewer.current_page_idx != self.page_idx
        rebuild = page_changed or annotation_affects_base_pdf_text(self.removed_annotation)
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=page_changed, rebuild_base=rebuild)
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])


class RemoveMultipleAnnotationsCommand(BaseCommand):
    def __init__(self, page_idx, removed_pairs, before_annotations=None):
        super().__init__()
        self.page_idx = page_idx
        self.removed_pairs = sorted(
            [(int(idx), copy.deepcopy(ann)) for idx, ann in removed_pairs],
            key=lambda x: x[0],
        )
        # 削除前のページ注釈リスト全体のスナップショット（任意）。
        # 渡された場合、undo/redo はリストを丸ごと復元するため updated_after
        # のずれは発生しない（audit #1 の完全な修正）。渡されない場合は
        # _restore_removed_annotations によるフォールバックで復元する。
        self._before_annotations = (
            copy.deepcopy(before_annotations) if before_annotations is not None else None
        )
        if self._before_annotations is not None:
            after = copy.deepcopy(self._before_annotations)
            for idx, _ in reversed(self.removed_pairs):
                if 0 <= idx < len(after):
                    del after[idx]
                    adjust_updated_after_on_remove(after, idx)
            self._after_annotations = after
        else:
            self._after_annotations = None

    def _needs_rebuild(self, page_changed):
        return page_changed or any(
            annotation_affects_base_pdf_text(ann) for _, ann in self.removed_pairs
        )

    def undo(self, viewer):
        if self._before_annotations is not None:
            _set_page_annotations_verbatim(viewer, self.page_idx, self._before_annotations)
        else:
            ann_list = viewer.page_annotations.setdefault(self.page_idx, [])
            _restore_removed_annotations(ann_list, self.removed_pairs)
        page_changed = viewer.current_page_idx != self.page_idx
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=page_changed, rebuild_base=self._needs_rebuild(page_changed))
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])

    def redo(self, viewer):
        if self._after_annotations is not None:
            _set_page_annotations_verbatim(viewer, self.page_idx, self._after_annotations)
        else:
            ann_list = viewer.page_annotations.get(self.page_idx, [])
            for _, ann in reversed(self.removed_pairs):
                found_idx = _find_annotation_from_end(ann_list, ann)
                if found_idx < 0:
                    logger.debug("RemoveMultipleAnnotationsCommand.redo: annotation not found; skip")
                    continue
                ann_list.pop(found_idx)
                adjust_updated_after_on_remove(ann_list, found_idx)
        page_changed = viewer.current_page_idx != self.page_idx
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=page_changed, rebuild_base=self._needs_rebuild(page_changed))
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])


class ReplacePageAnnotationsCommand(BaseCommand):
    """範囲テキスト消去など、1ページ内の複数注釈の削除・置換・追加を1ステップでUndo/Redoするコマンド"""

    def __init__(self, page_idx, old_annotations, new_annotations):
        super().__init__()
        self.page_idx = int(page_idx)
        self.old_annotations = copy.deepcopy(old_annotations)
        self.new_annotations = copy.deepcopy(new_annotations)

    def _needs_rebuild(self, page_changed):
        if page_changed:
            return True
        old_redacts = [
            (a.get("type"), a.get("rect"), a.get("redact_rect"))
            for a in self.old_annotations
            if annotation_affects_base_pdf_text(a)
        ]
        new_redacts = [
            (a.get("type"), a.get("rect"), a.get("redact_rect"))
            for a in self.new_annotations
            if annotation_affects_base_pdf_text(a)
        ]
        return old_redacts != new_redacts

    def undo(self, viewer):
        if self.old_annotations:
            viewer.page_annotations[self.page_idx] = copy.deepcopy(self.old_annotations)
        else:
            viewer.page_annotations.pop(self.page_idx, None)
        page_changed = viewer.current_page_idx != self.page_idx
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=page_changed, rebuild_base=self._needs_rebuild(page_changed))
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])

    def redo(self, viewer):
        if self.new_annotations:
            viewer.page_annotations[self.page_idx] = copy.deepcopy(self.new_annotations)
        else:
            viewer.page_annotations.pop(self.page_idx, None)
        page_changed = viewer.current_page_idx != self.page_idx
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=page_changed, rebuild_base=self._needs_rebuild(page_changed))
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])


class EditAnnotationCommand(BaseCommand):
    def __init__(self, page_idx, ann_idx, old_annotation, new_annotation):
        super().__init__()
        self.page_idx = page_idx
        self.ann_idx = ann_idx
        self.old_annotation = copy.deepcopy(old_annotation)
        self.new_annotation = copy.deepcopy(new_annotation)

    def _needs_rebuild(self, page_changed):
        return (
            page_changed
            or annotation_affects_base_pdf_text(self.old_annotation)
            or annotation_affects_base_pdf_text(self.new_annotation)
        )

    def _replace(self, viewer, expected_current, replacement):
        """ann_idx の注釈が expected_current と等しければ replacement に置換する。
        位置がずれていた場合は末尾から expected_current を探して置換し、
        見つからなければ何もしない。"""
        ann_list = viewer.page_annotations.get(self.page_idx, [])
        if 0 <= self.ann_idx < len(ann_list) and ann_list[self.ann_idx] == expected_current:
            ann_list[self.ann_idx] = copy.deepcopy(replacement)
            return
        found_idx = _find_annotation_from_end(ann_list, expected_current)
        if found_idx < 0:
            logger.debug("EditAnnotationCommand: expected annotation not found; no-op")
            return
        ann_list[found_idx] = copy.deepcopy(replacement)

    def undo(self, viewer):
        self._replace(viewer, self.new_annotation, self.old_annotation)
        page_changed = viewer.current_page_idx != self.page_idx
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=page_changed, rebuild_base=self._needs_rebuild(page_changed))
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])

    def redo(self, viewer):
        self._replace(viewer, self.old_annotation, self.new_annotation)
        page_changed = viewer.current_page_idx != self.page_idx
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=page_changed, rebuild_base=self._needs_rebuild(page_changed))
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])


class CropPageCommand(BaseCommand):
    def __init__(self, page_idx, old_cropbox, new_cropbox):
        super().__init__()
        self.page_idx = page_idx
        self.old_cropbox = fitz.Rect(old_cropbox)
        self.new_cropbox = fitz.Rect(new_cropbox)

    def undo(self, viewer):
        doc = viewer.current_pdf
        if doc is None or not (0 <= self.page_idx < len(doc)):
            logger.debug("CropPageCommand.undo: page_idx %s out of range; no-op", self.page_idx)
            return
        with FITZ_LOCK:
            page = doc[self.page_idx]
            page.set_cropbox(self.old_cropbox)
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=True, rebuild_base=True)
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])

    def redo(self, viewer):
        doc = viewer.current_pdf
        if doc is None or not (0 <= self.page_idx < len(doc)):
            logger.debug("CropPageCommand.redo: page_idx %s out of range; no-op", self.page_idx)
            return
        with FITZ_LOCK:
            page = doc[self.page_idx]
            page.set_cropbox(self.new_cropbox)
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=True, rebuild_base=True)
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])


class CropPagesCommand(BaseCommand):
    """複数ページのトリミングを1回の Undo 単位でまとめて行うコマンド。

    entries は (page_idx, old_cropbox, new_cropbox) の一覧。
    各ページの cropbox をまとめて設定し、描画は最後に1回だけ行う。
    """

    def __init__(self, entries):
        super().__init__()
        self.entries = [
            (int(page_idx), fitz.Rect(old_cropbox), fitz.Rect(new_cropbox))
            for page_idx, old_cropbox, new_cropbox in entries
        ]

    def _apply(self, viewer, use_new):
        doc = viewer.current_pdf
        if doc is None:
            logger.debug("CropPagesCommand: no document; no-op")
            return []
        affected = []
        with FITZ_LOCK:
            for page_idx, old_cropbox, new_cropbox in self.entries:
                if not (0 <= page_idx < len(doc)):
                    logger.debug("CropPagesCommand: page_idx %s out of range; skip", page_idx)
                    continue
                doc[page_idx].set_cropbox(new_cropbox if use_new else old_cropbox)
                affected.append(page_idx)
        if not affected:
            return affected
        # 別のページを閲覧中に Undo/Redo したとき、対象ページへ移動しないと
        # 画面上で変化が見えないため、対象外なら先頭の対象ページへ移動する。
        target_page = (
            viewer.current_page_idx
            if viewer.current_page_idx in affected
            else affected[0]
        )
        _set_current_page_safe(viewer, target_page)
        viewer.render_page(fit_view=True, rebuild_base=True)
        viewer.pageSelectionChanged.emit(viewer.current_page_idx)
        viewer.notify_pages_updated(affected)
        return affected

    def undo(self, viewer):
        self._apply(viewer, False)

    def redo(self, viewer):
        self._apply(viewer, True)


class AddAnnotationsMultiPageCommand(BaseCommand):
    """同じ注釈を複数ページへ追加するのを1回の Undo 単位で行うコマンド。

    entries は (page_idx, annotation) の一覧。undo では各ページの
    末尾から同じ注釈を探して削除する。
    """

    def __init__(self, entries):
        super().__init__()
        self.entries = [
            (int(page_idx), copy.deepcopy(annotation))
            for page_idx, annotation in entries
        ]

    def _apply(self, viewer, adding):
        affected = []
        for page_idx, annotation in self.entries:
            ann_list = viewer.page_annotations.setdefault(page_idx, [])
            if adding:
                ann_list.append(copy.deepcopy(annotation))
            else:
                found_idx = _find_annotation_from_end(ann_list, annotation)
                if found_idx < 0:
                    logger.debug(
                        "AddAnnotationsMultiPageCommand.undo: annotation not found on page %s; skip",
                        page_idx,
                    )
                    continue
                ann_list.pop(found_idx)
                adjust_updated_after_on_remove(ann_list, found_idx)
                if not ann_list:
                    viewer.page_annotations.pop(page_idx, None)
            affected.append(page_idx)
        if not affected:
            return
        page_changed = viewer.current_page_idx not in affected
        rebuild = page_changed or any(
            annotation_affects_base_pdf_text(ann) for _, ann in self.entries
        )
        # 別のページを閲覧中に Undo/Redo したとき、対象ページへ移動しないと
        # 画面上で変化が見えないため、対象外なら先頭の対象ページへ移動する。
        target_page = (
            viewer.current_page_idx if not page_changed else affected[0]
        )
        _set_current_page_safe(viewer, target_page)
        viewer.render_page(fit_view=False, rebuild_base=rebuild)
        viewer.pageSelectionChanged.emit(viewer.current_page_idx)
        viewer.notify_pages_updated(affected)

    def undo(self, viewer):
        self._apply(viewer, False)

    def redo(self, viewer):
        self._apply(viewer, True)


class RotatePageCommand(BaseCommand):
    def __init__(self, page_idx, old_rotation, new_rotation):
        super().__init__()
        self.page_idx = page_idx
        self.old_rotation = old_rotation % 360
        self.new_rotation = new_rotation % 360

    def undo(self, viewer):
        doc = viewer.current_pdf
        if doc is None or not (0 <= self.page_idx < len(doc)):
            logger.debug("RotatePageCommand.undo: page_idx %s out of range; no-op", self.page_idx)
            return
        with FITZ_LOCK:
            page = doc[self.page_idx]
            page.set_rotation(self.old_rotation)
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=True, rebuild_base=True)
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])

    def redo(self, viewer):
        doc = viewer.current_pdf
        if doc is None or not (0 <= self.page_idx < len(doc)):
            logger.debug("RotatePageCommand.redo: page_idx %s out of range; no-op", self.page_idx)
            return
        with FITZ_LOCK:
            page = doc[self.page_idx]
            page.set_rotation(self.new_rotation)
        _set_current_page_safe(viewer, self.page_idx)
        viewer.render_page(fit_view=True, rebuild_base=True)
        viewer.pageSelectionChanged.emit(self.page_idx)
        viewer.notify_pages_updated([self.page_idx])


class RotatePagesCommand(BaseCommand):
    def __init__(self, rotations):
        super().__init__()
        self.rotations = [
            (int(page_idx), int(old_rot) % 360, int(new_rot) % 360)
            for page_idx, old_rot, new_rot in rotations
        ]

    def _apply(self, viewer, use_new):
        affected_indices = []
        doc = viewer.current_pdf
        if doc is None:
            logger.debug("RotatePagesCommand: no document; no-op")
            return
        with FITZ_LOCK:
            for page_idx, old_rot, new_rot in self.rotations:
                if not (0 <= page_idx < len(doc)):
                    logger.debug("RotatePagesCommand: page_idx %s out of range; skip", page_idx)
                    continue
                rotation = (new_rot if use_new else old_rot) % 360
                doc[page_idx].set_rotation(rotation)
                affected_indices.append(page_idx)

        if not affected_indices:
            return
        viewer.render_page(fit_view=True, rebuild_base=True)
        viewer.pageSelectionChanged.emit(viewer.current_page_idx)
        viewer.notify_pages_updated(affected_indices)

    def undo(self, viewer):
        self._apply(viewer, False)

    def redo(self, viewer):
        self._apply(viewer, True)


class ReorderPagesCommand(BaseCommand):
    def __init__(self, desired_order, prev_page_idx, next_page_idx):
        super().__init__()
        self.desired_order = list(desired_order)
        self.prev_page_idx = prev_page_idx
        self.next_page_idx = next_page_idx

    @staticmethod
    def _apply_permutation(doc, page_annotations, perm):
        if len(perm) != len(doc) or set(perm) != set(range(len(doc))):
            logger.debug("ReorderPagesCommand: permutation does not match document; no-op")
            return dict(page_annotations)
        new_annots = {}
        for new_idx, old_idx in enumerate(perm):
            if old_idx in page_annotations and page_annotations[old_idx]:
                new_annots[new_idx] = page_annotations[old_idx]

        with FITZ_LOCK:
            order = list(range(len(doc)))
            for target, original in enumerate(perm):
                current = order.index(original)
                if current != target:
                    doc.move_page(current, target)
                    moved = order.pop(current)
                    order.insert(target, moved)

        return new_annots

    def redo(self, viewer):
        viewer.clear_calc_selection()
        viewer.page_annotations = self._apply_permutation(
            viewer.current_pdf,
            viewer.page_annotations,
            self.desired_order,
        )
        viewer.load_pdf(viewer.current_pdf, self.next_page_idx, fit_view=True)
        viewer.notify_pages_updated(None)

    def undo(self, viewer):
        viewer.clear_calc_selection()
        inverse_order = [0] * len(self.desired_order)
        for new_idx, old_idx in enumerate(self.desired_order):
            inverse_order[old_idx] = new_idx

        viewer.page_annotations = self._apply_permutation(
            viewer.current_pdf,
            viewer.page_annotations,
            inverse_order,
        )
        viewer.load_pdf(viewer.current_pdf, self.prev_page_idx, fit_view=True)
        viewer.notify_pages_updated(None)


class DeletePagesCommand(BaseCommand):
    def __init__(self, doc, deleted_indices, page_annotations, prev_page_idx):
        super().__init__()
        self.deleted_indices = sorted(set(deleted_indices))
        self.prev_page_idx = prev_page_idx
        self.backup_doc = fitz.open()
        self.deleted_records = []

        with FITZ_LOCK:
            for doc_pos, idx in enumerate(self.deleted_indices):
                self.backup_doc.insert_pdf(doc, from_page=idx, to_page=idx)
                annots = copy.deepcopy(page_annotations.get(idx, []))
                self.deleted_records.append((idx, doc_pos, annots))

    def dispose(self):
        if getattr(self, "backup_doc", None) is not None:
            try:
                with FITZ_LOCK:
                    self.backup_doc.close()
            except Exception:
                logger.debug("Failed to close backup/insert doc on dispose", exc_info=True)
            self.backup_doc = None

    def redo(self, viewer):
        viewer.clear_calc_selection()
        doc = viewer.current_pdf
        if doc is None:
            logger.debug("DeletePagesCommand.redo: no document; no-op")
            return
        valid_indices = [idx for idx in self.deleted_indices if 0 <= idx < len(doc)]
        if not valid_indices:
            logger.debug("DeletePagesCommand.redo: no valid page indices; no-op")
            return
        deleted_set = set(valid_indices)

        new_annots = {}
        new_idx = 0
        for old_idx in range(len(doc)):
            if old_idx not in deleted_set:
                if old_idx in viewer.page_annotations and viewer.page_annotations[old_idx]:
                    new_annots[new_idx] = viewer.page_annotations[old_idx]
                new_idx += 1

        with FITZ_LOCK:
            for idx in reversed(valid_indices):
                doc.delete_page(idx)

        viewer.page_annotations = new_annots
        target_idx = min(valid_indices[0], len(doc) - 1) if len(doc) > 0 else 0
        viewer.load_pdf(doc, target_idx, fit_view=True)
        viewer.notify_pages_updated(None)

    def undo(self, viewer):
        viewer.clear_calc_selection()
        doc = viewer.current_pdf
        if doc is None:
            logger.debug("DeletePagesCommand.undo: no document; no-op")
            return
        with FITZ_LOCK:
            for idx, doc_pos, _ in self.deleted_records:
                insert_at = max(0, min(idx, len(doc)))
                doc.insert_pdf(self.backup_doc, from_page=doc_pos, to_page=doc_pos, start_at=insert_at)

        deleted_map = {idx: annots for idx, _, annots in self.deleted_records}
        new_annots = {}
        old_surviving_idx = 0
        for restored_idx in range(len(doc)):
            if restored_idx in deleted_map:
                if deleted_map[restored_idx]:
                    new_annots[restored_idx] = copy.deepcopy(deleted_map[restored_idx])
            else:
                if old_surviving_idx in viewer.page_annotations and viewer.page_annotations[old_surviving_idx]:
                    new_annots[restored_idx] = viewer.page_annotations[old_surviving_idx]
                old_surviving_idx += 1

        viewer.page_annotations = new_annots
        target = _clamp_page_idx(viewer, self.prev_page_idx)
        viewer.load_pdf(doc, target if target is not None else 0, fit_view=True)
        viewer.notify_pages_updated(None)


class InsertPagesCommand(BaseCommand):
    def __init__(self, target_idx, insert_source, insert_count, prev_page_idx, take_ownership=False):
        super().__init__()
        self.target_idx = target_idx
        with FITZ_LOCK:
            if isinstance(insert_source, fitz.Document):
                if take_ownership:
                    insert_doc = insert_source
                else:
                    insert_doc = fitz.open()
                    insert_doc.insert_pdf(insert_source)
            else:
                insert_doc = fitz.open(stream=bytes(insert_source), filetype="pdf")
            actual_count = len(insert_doc)
            if insert_count is not None and int(insert_count) != actual_count:
                try:
                    insert_doc.close()
                except Exception:
                    pass
                raise ValueError(
                    f"insert_count={insert_count} does not match "
                    f"inserted document page count={actual_count}"
                )
            self.insert_doc = insert_doc
            self.insert_count = actual_count
        self.prev_page_idx = prev_page_idx

    def dispose(self):
        if getattr(self, "insert_doc", None) is not None:
            try:
                with FITZ_LOCK:
                    self.insert_doc.close()
            except Exception:
                logger.debug("Failed to close backup/insert doc on dispose", exc_info=True)
            self.insert_doc = None

    def redo(self, viewer):
        viewer.clear_calc_selection()
        doc = viewer.current_pdf
        if doc is None:
            logger.debug("InsertPagesCommand.redo: no document; no-op")
            return
        target_idx = max(0, min(self.target_idx, len(doc)))
        with FITZ_LOCK:
            doc.insert_pdf(self.insert_doc, start_at=target_idx)
        self._effective_target_idx = target_idx

        new_annots = {}
        for k, v in viewer.page_annotations.items():
            if k < target_idx:
                new_annots[k] = v
            else:
                new_annots[k + self.insert_count] = v
        viewer.page_annotations = new_annots

        viewer.load_pdf(doc, target_idx, fit_view=True)
        viewer.notify_pages_updated(None)

    def undo(self, viewer):
        viewer.clear_calc_selection()
        doc = viewer.current_pdf
        if doc is None:
            logger.debug("InsertPagesCommand.undo: no document; no-op")
            return
        target_idx = getattr(self, "_effective_target_idx", None)
        if target_idx is None:
            target_idx = max(0, min(self.target_idx, len(doc)))
        with FITZ_LOCK:
            for _ in range(self.insert_count):
                if target_idx < len(doc):
                    doc.delete_page(target_idx)
                else:
                    break

        new_annots = {}
        for k, v in viewer.page_annotations.items():
            if k < target_idx:
                new_annots[k] = v
            elif k >= target_idx + self.insert_count:
                new_annots[k - self.insert_count] = v
        viewer.page_annotations = new_annots

        target = _clamp_page_idx(viewer, self.prev_page_idx)
        viewer.load_pdf(doc, target if target is not None else 0, fit_view=True)
        viewer.notify_pages_updated(None)


class MergeTwoPagesCommand(BaseCommand):
    """2ページを1ページに結合するコマンド（FAXで2枚に分かれた書類用）。

    merged_doc は結合済みの1ページだけを持つ文書
    （PDFDocumentManager.build_merged_two_pages_doc の結果）。
    元の2ページは backup_doc に退避し、Undo で元の2ページに戻す。
    結合側ページの注釈は merged_doc へ焼き込み済みの前提で、viewer 側の
    注釈は結合時に破棄する（ReplacePagesContentCommand と同じ規約）。
    結合位置は若い方のページ位置（first_idx）になる。
    """

    def __init__(self, doc, first_idx, second_idx, merged_doc,
                 page_annotations, prev_page_idx, take_ownership=False):
        super().__init__()
        if first_idx == second_idx:
            raise ValueError("結合する2ページに同じページが指定されています")
        self.first_idx, self.second_idx = sorted((first_idx, second_idx))
        self.prev_page_idx = prev_page_idx
        self.backup_doc = fitz.open()
        self.saved_annots = {}

        with FITZ_LOCK:
            for pos, idx in enumerate((self.first_idx, self.second_idx)):
                self.backup_doc.insert_pdf(doc, from_page=idx, to_page=idx)
                if page_annotations.get(idx):
                    self.saved_annots[idx] = copy.deepcopy(page_annotations[idx])

            if take_ownership:
                self.merged_doc = merged_doc
            else:
                self.merged_doc = fitz.open()
                self.merged_doc.insert_pdf(merged_doc)

    def dispose(self):
        for attr in ("backup_doc", "merged_doc"):
            d = getattr(self, attr, None)
            if d is not None:
                try:
                    with FITZ_LOCK:
                        d.close()
                except Exception:
                    logger.debug("Failed to close merge doc on dispose", exc_info=True)
                setattr(self, attr, None)

    def redo(self, viewer):
        viewer.clear_calc_selection()
        doc = viewer.current_pdf
        if doc is None:
            logger.debug("MergeTwoPagesCommand.redo: no document; no-op")
            return
        i, j = self.first_idx, self.second_idx
        if not (0 <= i < len(doc) and 0 <= j < len(doc)):
            logger.debug("MergeTwoPagesCommand.redo: page indices out of range; no-op")
            return

        with FITZ_LOCK:
            doc.insert_pdf(self.merged_doc, start_at=i)
            # 挿入で元ページは i+1 と j+1 にずれる。後ろから消す。
            doc.delete_page(j + 1)
            doc.delete_page(i + 1)

        # 注釈の付け替え: 結合した2ページ分は破棄（焼き込み済み）。
        # i より前は不変、i と j の間は「挿入+1」と「手前の削除-1」で相殺され不変、
        # j より後ろは1つ前に詰まる。
        new_annots = {}
        for old_idx, anns in viewer.page_annotations.items():
            if old_idx in (i, j) or not anns:
                continue
            if old_idx < i:
                new_idx = old_idx
            elif old_idx < j:
                new_idx = old_idx
            else:
                new_idx = old_idx - 1
            new_annots[new_idx] = anns
        viewer.page_annotations = new_annots

        viewer.load_pdf(doc, i, fit_view=True)
        viewer.notify_pages_updated(None)

    def undo(self, viewer):
        viewer.clear_calc_selection()
        doc = viewer.current_pdf
        if doc is None:
            logger.debug("MergeTwoPagesCommand.undo: no document; no-op")
            return
        i, j = self.first_idx, self.second_idx

        with FITZ_LOCK:
            if 0 <= i < len(doc):
                doc.delete_page(i)
            doc.insert_pdf(self.backup_doc, from_page=0, to_page=0, start_at=i)
            doc.insert_pdf(self.backup_doc, from_page=1, to_page=1, start_at=j)

        # 注釈の付け替え（redo の逆変換）。結合ページ（位置 i）の注釈は
        # 焼き込み済み前提のため元々存在しないが、防御的に破棄する。
        new_annots = {}
        for cur_idx, anns in viewer.page_annotations.items():
            if cur_idx == i or not anns:
                continue
            if cur_idx < i:
                old_idx = cur_idx
            elif cur_idx < j:
                old_idx = cur_idx
            else:
                old_idx = cur_idx + 1
            new_annots[old_idx] = anns
        for idx, anns in self.saved_annots.items():
            new_annots[idx] = copy.deepcopy(anns)
        viewer.page_annotations = new_annots

        target = _clamp_page_idx(viewer, self.prev_page_idx)
        viewer.load_pdf(doc, target if target is not None else i, fit_view=True)
        viewer.notify_pages_updated(None)


class PerspectiveCorrectPageCommand(BaseCommand):
    """台形補正で1ページを補正済み画像ページに差し替えるコマンド。

    corrected_doc は補正済みの1ページだけを持つ文書
    （core.photo_to_pdf.build_corrected_image_page_doc の結果）。
    元ページは backup_doc に退避し、Undo で元に戻す。
    補正結果には元の注釈が焼き込み済みの前提で、viewer 側の注釈は
    差し替え時に破棄し、Undo で復元する（MergeTwoPagesCommand と同じ
    規約。ReplacePagesContentCommand と違い、補正でページの形が変わる
    ため注釈を画面上に残すことはしない）。
    ページ数は変わらないため、他のページの注釈には影響しない。
    """

    def __init__(self, doc, page_idx, corrected_doc,
                 page_annotations, prev_page_idx, take_ownership=False):
        super().__init__()
        self.page_idx = int(page_idx)
        self.prev_page_idx = prev_page_idx
        self.backup_doc = fitz.open()
        self.saved_annots = []

        with FITZ_LOCK:
            self.backup_doc.insert_pdf(
                doc, from_page=self.page_idx, to_page=self.page_idx)
            annots = (page_annotations or {}).get(self.page_idx)
            if annots:
                self.saved_annots = copy.deepcopy(annots)

            if take_ownership:
                self.corrected_doc = corrected_doc
            else:
                self.corrected_doc = fitz.open()
                self.corrected_doc.insert_pdf(corrected_doc)

    def dispose(self):
        for attr in ("backup_doc", "corrected_doc"):
            d = getattr(self, attr, None)
            if d is not None:
                try:
                    with FITZ_LOCK:
                        d.close()
                except Exception:
                    logger.debug(
                        "Failed to close perspective doc on dispose",
                        exc_info=True)
                setattr(self, attr, None)

    def _replace_with(self, viewer, src_doc):
        doc = viewer.current_pdf
        with FITZ_LOCK:
            doc.insert_pdf(src_doc, from_page=0, to_page=0,
                           start_at=self.page_idx)
            doc.delete_page(self.page_idx + 1)
        # 文字データが消えるため、テキストトークンのキャッシュを破棄する
        # （ReplacePagesContentCommand と同じ扱い）。
        if hasattr(viewer, "_pdf_tokens_cache"):
            viewer._pdf_tokens_cache.clear()
        viewer._cached_pdf_tokens = []
        viewer._cached_tokens_key = None

    def _refresh(self, viewer):
        _set_current_page_safe(viewer, self.prev_page_idx)
        viewer.render_page(fit_view=True, rebuild_base=True)
        viewer.pageSelectionChanged.emit(viewer.current_page_idx)
        viewer.notify_pages_updated([self.page_idx])

    def redo(self, viewer):
        viewer.clear_calc_selection()
        doc = viewer.current_pdf
        if doc is None or not (0 <= self.page_idx < len(doc)):
            logger.debug(
                "PerspectiveCorrectPageCommand.redo: no doc/page; no-op")
            return
        self._replace_with(viewer, self.corrected_doc)
        # 注釈は補正結果に焼き込み済みのため、画面上の注釈は破棄する
        viewer.page_annotations.pop(self.page_idx, None)
        self._refresh(viewer)

    def undo(self, viewer):
        viewer.clear_calc_selection()
        doc = viewer.current_pdf
        if doc is None or not (0 <= self.page_idx < len(doc)):
            logger.debug(
                "PerspectiveCorrectPageCommand.undo: no doc/page; no-op")
            return
        self._replace_with(viewer, self.backup_doc)
        _set_page_annotations_verbatim(
            viewer, self.page_idx, self.saved_annots)
        self._refresh(viewer)


class HistoryManager:
    def __init__(self, max_steps=MAX_UNDO_STEPS):
        self.max_steps = max_steps
        self.undo_stack = []
        self.redo_stack = []
        self._state_counter = 0
        self.current_state_id = 0
        self.saved_state_id = 0

    def clear(self):
        for cmd in self.undo_stack:
            cmd.dispose()
        for cmd in self.redo_stack:
            cmd.dispose()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._state_counter = 0
        self.current_state_id = 0
        self.saved_state_id = 0

    def mark_saved(self):
        self.saved_state_id = self.current_state_id

    def mark_unsaved(self):
        self.saved_state_id = -1

    @property
    def is_modified(self):
        return self.current_state_id != self.saved_state_id

    def push(self, command):
        command.prev_state_id = self.current_state_id
        self._state_counter += 1
        command.next_state_id = self._state_counter
        self.current_state_id = command.next_state_id

        self.undo_stack.append(command)
        if len(self.undo_stack) > self.max_steps:
            old_cmd = self.undo_stack.pop(0)
            old_cmd.dispose()
            if self.saved_state_id <= old_cmd.next_state_id:
                # 保存済み状態へ戻るには破棄されたコマンドの undo が必要になり
                # 到達不能になったため、未保存扱いにする (audit #5)。
                self.saved_state_id = -1
        for cmd in self.redo_stack:
            cmd.dispose()
        self.redo_stack.clear()

    def undo(self, viewer):
        if not self.undo_stack or not viewer.current_pdf:
            return
        viewer.cancel_active_interaction()
        cmd = self.undo_stack.pop()
        try:
            cmd.undo(viewer)
        except Exception as e:
            self.undo_stack.append(cmd)
            QMessageBox.warning(viewer, "元に戻すエラー", f"操作を取り消せませんでした。\n{e}")
            return
        self.current_state_id = cmd.prev_state_id
        self.redo_stack.append(cmd)

    def redo(self, viewer):
        if not self.redo_stack or not viewer.current_pdf:
            return
        viewer.cancel_active_interaction()
        cmd = self.redo_stack.pop()
        try:
            cmd.redo(viewer)
        except Exception as e:
            self.redo_stack.append(cmd)
            QMessageBox.warning(viewer, "やり直しエラー", f"操作をやり直せませんでした。\n{e}")
            return
        self.current_state_id = cmd.next_state_id
        self.undo_stack.append(cmd)


class ReplacePagesContentCommand(BaseCommand):
    """スキャン色抜き・OCR適用時にページ内容と注釈をUndo/Redo可能に差し替えるコマンド"""

    def __init__(self, doc, page_indices, new_pages_doc, page_annotations, prev_page_idx, take_ownership=False):
        super().__init__()
        self.page_indices = list(page_indices)
        self.prev_page_idx = prev_page_idx
        self.backup_doc = fitz.open()
        self.saved_annots = {}

        with FITZ_LOCK:
            for i, p_idx in enumerate(self.page_indices):
                self.backup_doc.insert_pdf(doc, from_page=p_idx, to_page=p_idx)
                if p_idx in page_annotations and page_annotations[p_idx]:
                    self.saved_annots[p_idx] = copy.deepcopy(page_annotations[p_idx])

            if take_ownership:
                self.new_doc = new_pages_doc
            else:
                self.new_doc = fitz.open()
                self.new_doc.insert_pdf(new_pages_doc)

    def dispose(self):
        for attr in ("backup_doc", "new_doc"):
            d = getattr(self, attr, None)
            if d is not None:
                try:
                    with FITZ_LOCK:
                        d.close()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _replace_pages(self, viewer, src_doc):
        doc = viewer.current_pdf
        with FITZ_LOCK:
            for doc_pos, p_idx in enumerate(self.page_indices):
                if 0 <= p_idx < len(doc):
                    doc.insert_pdf(src_doc, from_page=doc_pos, to_page=doc_pos, start_at=p_idx)
                    doc.delete_page(p_idx + 1)
        if hasattr(viewer, "_pdf_tokens_cache"):
            viewer._pdf_tokens_cache.clear()
        viewer._cached_pdf_tokens = []
        viewer._cached_tokens_key = None

    def redo(self, viewer):
        viewer.clear_calc_selection()
        self._replace_pages(viewer, self.new_doc)
        if self.page_indices and viewer.current_page_idx not in self.page_indices:
            _set_current_page_safe(viewer, self.prev_page_idx)
        viewer.render_page(fit_view=False, rebuild_base=True)
        viewer.pageSelectionChanged.emit(viewer.current_page_idx)
        viewer.notify_pages_updated(self.page_indices)

    def undo(self, viewer):
        viewer.clear_calc_selection()
        self._replace_pages(viewer, self.backup_doc)
        for p_idx in self.page_indices:
            if p_idx in self.saved_annots:
                viewer.page_annotations[p_idx] = copy.deepcopy(self.saved_annots[p_idx])
            else:
                viewer.page_annotations.pop(p_idx, None)
        _set_current_page_safe(viewer, self.prev_page_idx)
        viewer.render_page(fit_view=False, rebuild_base=True)
        viewer.pageSelectionChanged.emit(viewer.current_page_idx)
        viewer.notify_pages_updated(self.page_indices)


class ReplaceTocCommand(BaseCommand):
    """しおり（TOC）の変更を Undo/Redo 可能にするコマンド。

    ページ内容は変えず、文書のしおりだけを差し替える。
    toc は [[階層, タイトル, 1始まりページ], ...] のリスト。
    """

    def __init__(self, old_toc, new_toc):
        super().__init__()
        self.old_toc = [list(e) for e in (old_toc or [])]
        self.new_toc = [list(e) for e in (new_toc or [])]

    def _apply(self, viewer, toc):
        doc = viewer.current_pdf
        if doc is None:
            return
        with FITZ_LOCK:
            doc.set_toc([list(e) for e in toc])

    def redo(self, viewer):
        self._apply(viewer, self.new_toc)

    def undo(self, viewer):
        self._apply(viewer, self.old_toc)


class FillFormFieldsCommand(BaseCommand):
    """フォーム入力を Undo/Redo 可能にするコマンド。

    欄の値だけを書き換え、ページ置き換えはしない。insert_pdf でページを
    差し替えると欄名が変わってしまう（例: 「氏名」→「氏名 [17]」）ため、
    値のスナップショット方式にする。
    """

    def __init__(self, doc, values):
        super().__init__()
        self.values = {str(k): v for k, v in (values or {}).items()}
        # 旧値はウィジェット単位で記録する。同名の欄でもウィジェット自身が
        # /V を持っていると欄ごとに別の値になり得て、名前単位の記録だと
        # Undo で全部同じ値に戻ってしまうため。
        self.old_entries = []
        with FITZ_LOCK:
            from core.trial_tools import snapshot_form_field_values

            self.old_entries = snapshot_form_field_values(
                doc, names=set(self.values))
        # 名前→最初の値（従来形式。参照用に残す）
        self.old_values = {}
        for entry in self.old_entries:
            self.old_values.setdefault(entry["name"], entry["value"])
        self.affected_pages = sorted({e["page"] for e in self.old_entries})

    def _refresh_viewer(self, viewer):
        viewer.render_page(fit_view=False, rebuild_base=True)
        # 他のコマンドと同様に、サムネイル側にも更新を通知する
        viewer.notify_pages_updated(self.affected_pages or None)
        viewer.pageSelectionChanged.emit(viewer.current_page_idx)

    def _apply(self, viewer, values):
        from core.trial_tools import fill_form_fields

        doc = viewer.current_pdf
        if doc is None:
            return
        with FITZ_LOCK:
            fill_form_fields(doc, values)
        self._refresh_viewer(viewer)

    def redo(self, viewer):
        self._apply(viewer, self.values)

    def undo(self, viewer):
        from core.trial_tools import set_form_field_values_by_widget

        doc = viewer.current_pdf
        if doc is None:
            return
        with FITZ_LOCK:
            set_form_field_values_by_widget(doc, self.old_entries)
        self._refresh_viewer(viewer)
