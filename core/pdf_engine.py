# -*- coding: utf-8 -*-
"""PDF物理墨消し・注釈焼き込み・残存検証・アトミック保存エンジン"""
import copy
import math
import os
import shutil
import sys
import tempfile

import fitz

from core import (
    ERASE_WIDTH,
    HIGHLIGHT_OPACITY,
    TEXT_LINE_SPACING,
    annotation_to_erase_rects,
    clip_stroke_by_rects,
    collect_text_only_redact_raw_rects,
    get_later_user_erase_rects,
    is_rect_substantially_erased,
    logger,
)
from core.geometry import (
    DEFAULT_COLOR,
    DRAW_WIDTH,
    FITZ_LOCK,
    JAPANESE_FONT,
    TEXT_FONT_SIZE,
    estimate_text_bbox_in_pdf,
    get_crop_outside_rects,
    get_effective_cropbox,
    raw_pdf_to_view_point,
    raw_rect_to_view_rect,
)


class PDFDocumentManager:

    @staticmethod
    def _raw_rects_to_view_rects(page, raw_rects):
        page_bound = page.rect
        view_rects = []
        for raw_r in raw_rects:
            vr = raw_rect_to_view_rect(page, raw_r)
            vr &= page_bound
            if not vr.is_empty:
                view_rects.append(vr)
        return view_rects

    @staticmethod
    def _collect_surviving_char_bboxes(page, view_rects, pad=0.1, tol=0.35):
        if not view_rects:
            return []

        expanded_vrs = [
            fitz.Rect(vr.x0 - tol, vr.y0 - tol, vr.x1 + tol, vr.y1 + tol)
            for vr in view_rects
        ]
        char_bboxes = []
        raw_dict = page.get_text("rawdict")
        for block in raw_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for ch in span.get("chars", []):
                        c_str = ch.get("c", "").strip()
                        if not c_str:
                            continue
                        bbox = fitz.Rect(ch["bbox"])
                        center = fitz.Point(
                            (bbox.x0 + bbox.x1) / 2.0,
                            (bbox.y0 + bbox.y1) / 2.0,
                        )
                        if any(evr.contains(center) for evr in expanded_vrs):
                            padded = fitz.Rect(
                                bbox.x0 - pad,
                                bbox.y0 - pad,
                                bbox.x1 + pad,
                                bbox.y1 + pad,
                            )
                            char_bboxes.append(padded)
        return char_bboxes

    @classmethod
    def apply_text_only_redactions_to_page(cls, page, raw_rects):
        if not raw_rects:
            return

        view_rects = cls._raw_rects_to_view_rects(page, raw_rects)
        if not view_rects:
            return

        # OCR透明テキスト(0x010101)が含まれる領域は背景のスキャン文字ピクセルも白塗りで消去する
        try:
            from core.ocr_cleaner import OCR_TEXT_MARKER_SRGB_INT
            ocr_pixel_erase_rects = []
            raw_dict_check = page.get_text("rawdict")
            for blk in raw_dict_check.get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for ln in blk.get("lines", []):
                    for sp in ln.get("spans", []):
                        if sp.get("color") == OCR_TEXT_MARKER_SRGB_INT:
                            sp_rect = fitz.Rect(sp.get("bbox", (0, 0, 0, 0)))
                            for vr in view_rects:
                                if sp_rect.intersects(vr):
                                    ocr_pixel_erase_rects.append(vr)
            if ocr_pixel_erase_rects:
                cls._apply_redaction_view_rects_to_page(
                    page, ocr_pixel_erase_rects, fill_color=(1, 1, 1)
                )
        except Exception:
            logger.debug("Optional OCR pixel redaction check skipped", exc_info=True)

        img_none = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)
        gfx_none = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
        text_opt = getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0)

        clip_bounds = [
            fitz.Rect(vr.x0 - 1.5, vr.y0 - 1.5, vr.x1 + 1.5, vr.y1 + 1.5)
            for vr in view_rects
        ]
        for pad in (0.1, 0.35, 0.75, 1.2):
            char_bboxes = cls._collect_surviving_char_bboxes(page, view_rects, pad=pad)
            if not char_bboxes:
                break

            for r in char_bboxes:
                clipped_r = r
                for cb in clip_bounds:
                    inter = r & cb
                    if not inter.is_empty:
                        clipped_r = inter
                        break
                page.add_redact_annot(clipped_r, fill=False)

            try:
                page.apply_redactions(
                    images=img_none,
                    graphics=gfx_none,
                    text=text_opt,
                )
            except TypeError:
                page.apply_redactions(images=img_none)

            if not cls._collect_surviving_char_bboxes(page, view_rects, pad=0.0):
                break

        for extra_pad in (0.0, 1.5, 3.5, 6.0):
            surviving = cls._collect_surviving_char_bboxes(page, view_rects, pad=extra_pad)
            if not surviving:
                break
            for r in surviving:
                page.add_redact_annot(r, fill=False)
            for vr in view_rects:
                exp_vr = fitz.Rect(vr.x0 - extra_pad, vr.y0 - extra_pad, vr.x1 + extra_pad, vr.y1 + extra_pad)
                page.add_redact_annot(exp_vr, fill=False)
            try:
                page.apply_redactions(
                    images=img_none,
                    graphics=gfx_none,
                    text=text_opt,
                )
            except TypeError:
                page.apply_redactions(images=img_none)

    @classmethod
    def _apply_redaction_view_rects_to_page(cls, page, view_rects, fill_color=(1, 1, 1)):
        if not view_rects:
            return

        for vr in view_rects:
            page.add_redact_annot(vr, fill=fill_color)

        for existing in list(page.annots() or []):
            if existing.type[0] == fitz.PDF_ANNOT_REDACT:
                continue
            if any(existing.rect.intersects(vr) for vr in view_rects):
                page.delete_annot(existing)

        for link in list(page.get_links() or []):
            link_rect = fitz.Rect(link.get("from", (0, 0, 0, 0)))
            if any(link_rect.intersects(vr) for vr in view_rects):
                try:
                    page.delete_link(link)
                except Exception:
                    logger.debug("Optional PDF operation skipped", exc_info=True)

        if hasattr(page, "widgets") and hasattr(page, "delete_widget"):
            for widget in list(page.widgets() or []):
                if any(widget.rect.intersects(vr) for vr in view_rects):
                    try:
                        page.delete_widget(widget)
                    except Exception:
                        logger.debug("Optional widget deletion skipped", exc_info=True)

        img_opt = getattr(fitz, "PDF_REDACT_IMAGE_PIXELS", 2)
        graphics_opt = getattr(
            fitz,
            "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED",
            1,
        )
        text_opt = getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0)

        try:
            page.apply_redactions(
                images=img_opt,
                graphics=graphics_opt,
                text=text_opt,
            )
        except TypeError:
            try:
                page.apply_redactions(
                    images=img_opt,
                    graphics=graphics_opt,
                )
            except TypeError:
                page.apply_redactions(images=img_opt)

        img_none = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)
        gfx_none = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
        for pad in (0.5, 1.5, 2.5, 5.0, 10.0):
            surviving_bboxes = cls._collect_surviving_char_bboxes(
                page, view_rects, pad=pad, tol=0.0
            )
            if not surviving_bboxes:
                break
            for cb in surviving_bboxes:
                page.add_redact_annot(cb, fill=False)
            try:
                page.apply_redactions(
                    images=img_none,
                    graphics=gfx_none,
                    text=text_opt,
                )
            except TypeError:
                page.apply_redactions(images=img_none)

    @classmethod
    def bake_annotations_to_doc(cls, doc, page_annotations, verify=True):
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            ann_list = page_annotations.get(page_idx, [])

            saved_rot = page.rotation % 360
            saved_crop = get_effective_cropbox(page)
            cls._validate_annotations_for_bake(ann_list, page_idx)
            crop_outside_raw = get_crop_outside_rects(page.mediabox, saved_crop)

            try:
                cls._bake_page_annotations(
                    page, page_idx, ann_list, saved_rot, saved_crop, crop_outside_raw, verify
                )
            finally:
                # 途中で例外が発生しても回転・クロップを必ず復旧する (audit #3)。
                try:
                    if page.rotation != saved_rot:
                        page.set_rotation(saved_rot)
                except Exception:
                    logger.debug("Failed to restore page rotation after bake", exc_info=True)
                try:
                    if saved_crop != page.mediabox:
                        page.set_cropbox(saved_crop)
                except Exception:
                    logger.debug("Failed to restore page cropbox after bake", exc_info=True)

    @staticmethod
    def _validate_annotations_for_bake(ann_list, page_idx):
        """焼き込み前に注釈辞書の必須キーを検証し、ページを変更する前に明確な例外を出す (audit #2)。

        highlight は rect、text は text/pos、draw は points が必須。
        不足・不正な場合は ValueError を送出する（半端に焼き込まれた状態を作らない）。
        """
        for pos, ann in enumerate(ann_list):
            if not isinstance(ann, dict):
                raise ValueError(
                    f"Page {page_idx + 1}: annotation #{pos} is not a dict: {type(ann).__name__}"
                )
            if "type" not in ann:
                raise ValueError(
                    f"Page {page_idx + 1}: annotation #{pos} is missing required 'type'"
                )
            ann_type = ann.get("type")
            if ann_type == "highlight":
                if "rect" not in ann:
                    raise ValueError(
                        f"Page {page_idx + 1}: highlight annotation #{pos} is missing required 'rect'"
                    )
                try:
                    fitz.Rect(ann["rect"])
                except Exception as exc:
                    raise ValueError(
                        f"Page {page_idx + 1}: highlight annotation #{pos} has invalid 'rect': {ann['rect']!r}"
                    ) from exc
            elif ann_type == "text":
                if "text" not in ann:
                    raise ValueError(
                        f"Page {page_idx + 1}: text annotation #{pos} is missing required 'text'"
                    )
                if "pos" not in ann:
                    raise ValueError(
                        f"Page {page_idx + 1}: text annotation #{pos} is missing required 'pos'"
                    )
                try:
                    fitz.Point(*ann["pos"])
                except Exception as exc:
                    raise ValueError(
                        f"Page {page_idx + 1}: text annotation #{pos} has invalid 'pos': {ann['pos']!r}"
                    ) from exc
            elif ann_type == "draw":
                if "points" not in ann:
                    raise ValueError(
                        f"Page {page_idx + 1}: draw annotation #{pos} is missing required 'points'"
                    )

    @classmethod
    def _bake_page_annotations(cls, page, page_idx, ann_list, saved_rot, saved_crop, crop_outside_raw, verify):
        page.set_rotation(0)
        page.set_cropbox(page.mediabox)

        user_erase_raw = []
        for ann in ann_list:
            if ann["type"] in ("erase", "box_erase"):
                user_erase_raw.extend(annotation_to_erase_rects(ann))

        all_erase_raw = crop_outside_raw + user_erase_raw
        if all_erase_raw:
            all_erase_view = cls._raw_rects_to_view_rects(page, all_erase_raw)
            cls._apply_redaction_view_rects_to_page(
                page,
                all_erase_view,
                fill_color=(1, 1, 1),
            )
            if verify:
                cls._verify_page_redaction(page, page_idx, all_erase_view)

        text_only_redact_raw = collect_text_only_redact_raw_rects(ann_list)
        if text_only_redact_raw:
            cls.apply_text_only_redactions_to_page(page, text_only_redact_raw)
            if verify:
                text_only_view = cls._raw_rects_to_view_rects(page, text_only_redact_raw)
                cls._verify_page_text_only_redaction(page, page_idx, text_only_view)

        existing_font_names = {f[4] for f in (page.get_fonts() or []) if len(f) > 4}
        jp_font_alias = "jp_font"
        alias_idx = 1
        while jp_font_alias in existing_font_names:
            alias_idx += 1
            jp_font_alias = f"jp_font_{alias_idx}"

        for idx, ann in enumerate(ann_list):
            if ann["type"] == "text_redact":
                continue

            user_later_erases = get_later_user_erase_rects(ann_list, idx, ann)
            stroke_clip_erases = list(crop_outside_raw) + user_later_erases

            if ann["type"] in ("erase", "box_erase"):
                erase_rects = annotation_to_erase_rects(ann)
                shape = page.new_shape()
                has_erase_rect = False
                for er in erase_rects:
                    clipped_er = er & saved_crop
                    if not clipped_er.is_empty:
                        vr = raw_rect_to_view_rect(page, clipped_er)
                        shape.draw_rect(vr)
                        has_erase_rect = True
                if has_erase_rect:
                    shape.finish(color=None, fill=(1.0, 1.0, 1.0))
                    shape.commit()

            elif ann["type"] == "highlight":
                r = fitz.Rect(ann["rect"])
                r.normalize()
                r &= saved_crop
                if r.is_empty:
                    continue
                if is_rect_substantially_erased(r, user_later_erases):
                    continue
                vr = raw_rect_to_view_rect(page, r)
                hl_color = tuple(ann.get("color", (0.98, 0.90, 0.05)))
                hl_opacity = float(ann.get("opacity", HIGHLIGHT_OPACITY))
                shape = page.new_shape()
                shape.draw_rect(vr)
                shape.finish(
                    color=None,
                    fill=hl_color,
                    fill_opacity=hl_opacity,
                )
                shape.commit()

            elif ann["type"] == "draw":
                stroke_color = tuple(ann.get("color", DEFAULT_COLOR))
                stroke_width = float(ann.get("width", DRAW_WIDTH))
                segments = clip_stroke_by_rects(ann["points"], stroke_clip_erases)
                shape = page.new_shape()
                has_drawn = False
                for seg in segments:
                    view_points = [raw_pdf_to_view_point(page, fitz.Point(x, y)) for x, y in seg]
                    if len(view_points) >= 2:
                        p_first = view_points[0]
                        if all(math.hypot(p.x - p_first.x, p.y - p_first.y) < 1e-3 for p in view_points[1:]):
                            view_points = [p_first, fitz.Point(p_first.x + 0.01, p_first.y + 0.01)]
                        shape.draw_polyline(view_points)
                        has_drawn = True
                if has_drawn:
                    shape.finish(
                        color=stroke_color,
                        width=stroke_width,
                        lineCap=1,
                        lineJoin=1,
                    )
                    shape.commit()

            elif ann["type"] == "text":
                text_bbox = estimate_text_bbox_in_pdf(ann)
                if not saved_crop.intersects(text_bbox):
                    continue
                if is_rect_substantially_erased(text_bbox, user_later_erases):
                    continue

                fontsize = float(ann.get("fontsize", TEXT_FONT_SIZE))
                text_color = tuple(ann.get("color", (0.0, 0.0, 0.0)))
                ann_rot = int(ann.get("rotation", saved_rot)) % 360

                raw_pt = fitz.Point(*ann["pos"])
                if "baseline_pos" in ann:
                    raw_base_pt = fitz.Point(*ann["baseline_pos"])
                else:
                    raw_base_pt = fitz.Point(raw_pt.x, raw_pt.y + fontsize * 0.88)

                v_down = raw_base_pt - raw_pt
                v_len = math.hypot(v_down.x, v_down.y)
                if v_len > 1e-3:
                    u_down = fitz.Point(v_down.x / v_len, v_down.y / v_len)
                else:
                    u_down = fitz.Point(0.0, 1.0)
                line_step_raw = u_down * (fontsize * TEXT_LINE_SPACING)

                lines = str(ann.get("text", "")).split("\n")
                for line_idx, line_str in enumerate(lines):
                    if not line_str:
                        continue
                    line_raw_base = raw_base_pt + line_step_raw * line_idx
                    view_pos = raw_pdf_to_view_point(page, line_raw_base)

                    inserted = False
                    if JAPANESE_FONT:
                        try:
                            page.insert_text(
                                view_pos,
                                line_str,
                                fontsize=fontsize,
                                color=text_color,
                                rotate=ann_rot,
                                fontfile=JAPANESE_FONT,
                                fontname=jp_font_alias,
                            )
                            inserted = True
                        except Exception:
                            inserted = False

                    if not inserted:
                        page.insert_text(
                            view_pos,
                            line_str,
                            fontsize=fontsize,
                            color=text_color,
                            rotate=ann_rot,
                            fontname="japan",
                        )

    @classmethod
    def extract_pages_as_doc(cls, doc, page_indices, page_annotations=None):
        """tobytes()を介さず、指定ページを直接 insert_pdf した新しい fitz.Document を返す"""
        with FITZ_LOCK:
            sub_doc = fitz.open()
            mapped_annots = {}

            for new_idx, old_idx in enumerate(page_indices):
                if 0 <= old_idx < len(doc):
                    sub_doc.insert_pdf(doc, from_page=old_idx, to_page=old_idx)
                    if page_annotations and old_idx in page_annotations and page_annotations[old_idx]:
                        mapped_annots[new_idx] = copy.deepcopy(page_annotations[old_idx])

            if mapped_annots:
                # 焼き込み失敗は呼び出し側に伝播させる（audit #2）。
                # 部分的に焼き込まれたドキュメントを黙って返さない。
                try:
                    cls.bake_annotations_to_doc(sub_doc, mapped_annots, verify=False)
                except Exception:
                    sub_doc.close()
                    raise

            return sub_doc

    @classmethod
    def extract_pages_as_bytes(cls, doc, page_indices, page_annotations=None):
        with FITZ_LOCK:
            sub_doc = cls.extract_pages_as_doc(doc, page_indices, page_annotations)
            try:
                return sub_doc.tobytes(garbage=2, deflate=True)
            finally:
                sub_doc.close()

    @classmethod
    def _verify_page_text_only_redaction(cls, page, page_idx, view_rects):
        page_bound = page.rect
        clipped_rects = [vr & page_bound for vr in view_rects if not (vr & page_bound).is_empty]
        if not clipped_rects:
            return
        surviving = cls._collect_surviving_char_bboxes(page, clipped_rects, pad=0.0, tol=0.0)
        if surviving:
            raise RuntimeError(
                f"{page_idx + 1}ページ目の編集対象テキスト領域に元の文字データが残存しています。"
            )

    @classmethod
    def _verify_page_redaction(cls, page, page_idx, view_rects):
        page_bound = page.rect
        clipped_rects = [vr & page_bound for vr in view_rects if not (vr & page_bound).is_empty]
        if not clipped_rects:
            return

        surviving = cls._collect_surviving_char_bboxes(page, clipped_rects, pad=0.0, tol=0.0)
        if surviving:
            raise RuntimeError(
                f"{page_idx + 1}ページ目の墨消し領域内に文字データが残存しています。"
            )

        for link in page.get_links() or []:
            l_rect = fitz.Rect(link.get("from", (0, 0, 0, 0)))
            if any(l_rect.intersects(vr) for vr in clipped_rects):
                raise RuntimeError(
                    f"{page_idx + 1}ページ目の墨消し領域内にリンク情報が残存しています。"
                )

    @classmethod
    def _verify_saved_pdf(cls, temp_path, expected_pages):
        with FITZ_LOCK:
            verify_doc = fitz.open(temp_path)
            try:
                if len(verify_doc) != expected_pages:
                    raise RuntimeError("保存後のページ数が一致しません。")
                for page in verify_doc:
                    _ = page.rect
            finally:
                verify_doc.close()

    @staticmethod
    def _notify_shell_file_updated(filepath: str) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            abs_path = os.path.abspath(filepath)
            ctypes.windll.shell32.SHChangeNotify(0x00002000, 0x1005, abs_path, None)
        except Exception:
            logger.debug("SHChangeNotify failed after save", exc_info=True)

    @staticmethod
    def _atomic_replace_preserving_acl(temp_path: str, target_path: str, backup_path: str) -> None:
        if sys.platform == "win32" and os.path.exists(target_path):
            try:
                import ctypes
                replace_fn = ctypes.windll.kernel32.ReplaceFileW
                replace_fn.argtypes = [
                    ctypes.c_wchar_p,
                    ctypes.c_wchar_p,
                    ctypes.c_wchar_p,
                    ctypes.c_ulong,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ]
                replace_fn.restype = ctypes.c_int
                res = replace_fn(
                    str(target_path),
                    str(temp_path),
                    str(backup_path),
                    0x00000001,
                    None,
                    None,
                )
                if res != 0:
                    return
            except Exception:
                logger.debug("ReplaceFileW fallback to os.replace", exc_info=True)

        if os.path.exists(target_path):
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except OSError:
                    pass
            os.replace(target_path, backup_path)
        os.replace(temp_path, target_path)

    @staticmethod
    def _is_encrypted_document(doc):
        """暗号化された PDF かどうかを判定する (audit #7)。

        判定には is_encrypted（未認証の暗号化文書で True）と metadata の
        encryption 項目（認証後も残る）を使う。needs_pass はここでは絶対に
        読まないこと：PyMuPDF 2.1 で認証済みの暗号化文書に対して needs_pass
        を読むと、その後の insert_pdf によるコピーが壊れる（内容ストリームの
        復号に失敗しテキストが消える）ことが実測で確認されている。
        metadata は未認証の暗号化ドキュメントでは None になる場合がある。
        """
        try:
            if getattr(doc, "is_encrypted", False):
                return True
        except Exception:
            pass
        try:
            metadata = doc.metadata
            if metadata and metadata.get("encryption"):
                return True
        except Exception:
            pass
        return False

    @classmethod
    def save_safely(cls, source_doc, page_annotations, filepath, current_file, *, allow_strip_encryption=False):
        try:
            page_count = len(source_doc)
        except Exception:
            return source_doc, ValueError("Invalid source document")
        if page_count == 0:
            return source_doc, ValueError("0-page document cannot be saved")
        if not allow_strip_encryption and cls._is_encrypted_document(source_doc):
            return source_doc, ValueError(
                "Encrypted document cannot be saved without stripping encryption; "
                "pass allow_strip_encryption=True to strip it explicitly"
            )

        work_doc = None
        temp_path = None

        try:
            abs_filepath = os.path.abspath(filepath)
            dir_name = os.path.dirname(abs_filepath) or "."
            fd, temp_path = tempfile.mkstemp(suffix=".pdf", dir=dir_name)
            os.close(fd)

            with FITZ_LOCK:
                work_doc = fitz.open()
                work_doc.insert_pdf(source_doc)
                cls.bake_annotations_to_doc(work_doc, page_annotations, verify=True)

                try:
                    work_doc.set_metadata({})
                except Exception:
                    logger.debug("Optional PDF operation skipped", exc_info=True)
                if hasattr(work_doc, "del_xml_metadata"):
                    try:
                        work_doc.del_xml_metadata()
                    except Exception:
                        logger.debug("Optional XML metadata deletion skipped", exc_info=True)
                if hasattr(work_doc, "subset_fonts"):
                    try:
                        work_doc.subset_fonts()
                    except Exception:
                        logger.debug("Optional font subsetting skipped", exc_info=True)

                work_doc.save(
                    temp_path,
                    garbage=4,
                    deflate=True,
                    clean=True,
                )
                work_doc.close()
                work_doc = None

                cls._verify_saved_pdf(
                    temp_path,
                    expected_pages=len(source_doc),
                )

            backup_path = abs_filepath + ".bak"
            try:
                cls._atomic_replace_preserving_acl(temp_path, abs_filepath, backup_path)
                temp_path = None
            except Exception as replace_err:
                if os.path.exists(backup_path) and not os.path.exists(abs_filepath):
                    try:
                        os.replace(backup_path, abs_filepath)
                    except Exception:
                        logger.debug("Failed to restore backup file", exc_info=True)
                return source_doc, replace_err

            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except OSError:
                    pass

            cls._notify_shell_file_updated(abs_filepath)
            return source_doc, None

        finally:
            if work_doc is not None:
                work_doc.close()
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
