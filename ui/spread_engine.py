# -*- coding: utf-8 -*-
"""
見開きリーダー用レイアウト計算・ページレンダリング・座標変換エンジン (ui/spread_engine.py)
FullscreenSpreadReader から計算・描画・座標変換の責務を分離したモジュール。
"""
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import fitz
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QImage

from core import split_text_only_redact_raw_rects, logger
from core.geometry import (
    MAX_RENDER_PIXELS,
    clamp_render_scales,
    raw_pdf_to_view_point_from_geom,
    view_point_to_raw_pdf_from_geom,
)


class SpreadItem:
    """見開き1画面分（単ページまたは左右2ページ）を表すデータクラス

    page_indices は実ページ番号のみ（白紙スロットを含まない）。
    slots は見開きを構成するスロット列で、実ページ番号か、
    表示だけの白紙ページを表す None のどちらか。
    """

    def __init__(
        self,
        page_indices: List[int],
        is_landscape: bool,
        slots: Optional[List[Optional[int]]] = None,
    ):
        self.page_indices = list(page_indices)
        self.is_landscape = bool(is_landscape)
        self.slots: List[Optional[int]] = (
            list(slots) if slots is not None else list(page_indices)
        )

    @property
    def is_spread(self) -> bool:
        return len(self.slots) == 2


class SpreadLayoutEngine:
    """見開き構成の構築、ズーム・パン矩形計算、メモリ保護付きページ描画を担うエンジン"""

    @staticmethod
    def is_page_landscape(doc: fitz.Document, page_idx: int, fitz_lock=None) -> bool:
        if fitz_lock is not None:
            with fitz_lock:
                rect = doc[page_idx].rect
                return rect.width > rect.height
        rect = doc[page_idx].rect
        return rect.width > rect.height

    @staticmethod
    def build_slot_sequence(
        page_count: int, blank_counts: Optional[Dict[int, int]] = None
    ) -> List[Optional[int]]:
        """ページ番号の列に、表示だけの白紙スロット（None）を差し込んだスロット列を返す。

        blank_counts は {ページ番号: そのページの直前に差し込む白紙の枚数}。
        PDF 自体は一切変更しない、表示上の並びだけを表す純粋関数。
        """
        slots: List[Optional[int]] = []
        counts = blank_counts or {}
        for i in range(max(0, int(page_count))):
            for _ in range(max(0, int(counts.get(i, 0)))):
                slots.append(None)
            slots.append(i)
        return slots

    @classmethod
    def _make_spread_item(
        cls, doc: fitz.Document, chunk: List[Optional[int]], fitz_lock=None
    ) -> SpreadItem:
        pages = [s for s in chunk if s is not None]
        is_land = (
            len(chunk) == 1
            and chunk[0] is not None
            and cls.is_page_landscape(doc, chunk[0], fitz_lock)
        )
        return SpreadItem(pages, is_land, slots=list(chunk))

    @classmethod
    def build_spreads_from_slots(
        cls,
        doc: fitz.Document,
        slots: List[Optional[int]],
        cover_single: bool = True,
        fitz_lock=None,
    ) -> List[SpreadItem]:
        """スロット列から見開きを組み立てる。

        ルールは従来の build_spreads と同じで、白紙スロット（None）は
        縦向きページと同じように扱う（横向きページは常に単独表示、
        表紙モードでは先頭スロットが単独表示）。
        """
        spreads: List[SpreadItem] = []
        total = len(slots)
        i = 0
        while i < total:
            slot = slots[i]
            if i == 0 and cover_single:
                spreads.append(cls._make_spread_item(doc, [slot], fitz_lock))
                i += 1
            elif slot is not None and cls.is_page_landscape(doc, slot, fitz_lock):
                spreads.append(cls._make_spread_item(doc, [slot], fitz_lock))
                i += 1
            else:
                paired = False
                if i + 1 < total:
                    nxt = slots[i + 1]
                    if nxt is None or not cls.is_page_landscape(doc, nxt, fitz_lock):
                        spreads.append(
                            cls._make_spread_item(doc, [slot, nxt], fitz_lock)
                        )
                        i += 2
                        paired = True
                if not paired:
                    spreads.append(cls._make_spread_item(doc, [slot], fitz_lock))
                    i += 1
        return spreads

    @classmethod
    def build_spreads(
        cls, doc: fitz.Document, cover_single: bool = True, fitz_lock=None
    ) -> List[SpreadItem]:
        return cls.build_spreads_from_slots(
            doc,
            cls.build_slot_sequence(len(doc)),
            cover_single=cover_single,
            fitz_lock=fitz_lock,
        )

    @staticmethod
    def apply_zoom_pan_to_entries(
        entries: List[Dict[str, Any]],
        screen_w: float,
        screen_h: float,
        zoom: float,
        pan_offset: QPointF,
    ) -> QPointF:
        """各ページエントリの base_rect に対してズームとパンを適用し、クランプ後の pan_offset を返す"""
        if not entries:
            return pan_offset

        cx = screen_w * 0.5
        cy = screen_h * 0.5
        clamped_pan = QPointF(pan_offset)

        if zoom <= 1.001:
            clamped_pan = QPointF(0.0, 0.0)
        else:
            union_base = QRectF()
            for entry in entries:
                br = entry.get("base_rect", entry["rect"])
                union_base = br if union_base.isEmpty() else union_base.united(br)
            scaled_w = union_base.width() * zoom
            scaled_h = union_base.height() * zoom
            max_pan_x = max(0.0, (scaled_w - screen_w) * 0.5 + 80.0)
            max_pan_y = max(0.0, (scaled_h - screen_h) * 0.5 + 80.0)
            clamped_pan.setX(max(-max_pan_x, min(max_pan_x, clamped_pan.x())))
            clamped_pan.setY(max(-max_pan_y, min(max_pan_y, clamped_pan.y())))

        for entry in entries:
            br: QRectF = entry.get("base_rect", entry["rect"])
            x = cx + (br.left() - cx) * zoom + clamped_pan.x()
            y = cy + (br.top() - cy) * zoom + clamped_pan.y()
            w = br.width() * zoom
            h = br.height() * zoom
            entry["rect"] = QRectF(x, y, w, h)

        return clamped_pan

    @staticmethod
    def render_page_entry(
        doc: fitz.Document,
        page_idx: int,
        target_w: float,
        target_h: float,
        zoom: float,
        dpr: float,
        ann_list: List[Dict[str, Any]],
        font_family: str,
        draw_annot_callback: Optional[Callable] = None,
        redact_page_callback: Optional[Callable] = None,
        fitz_lock=None,
    ) -> Dict[str, Any]:
        text_redact_rects, keep_image_rects = split_text_only_redact_raw_rects(ann_list)
        render_zoom = min(2.5, max(1.0, float(zoom)))
        eff_dpr = max(1.0, float(dpr))

        def _do_render():
            page = doc[page_idx]
            r = fitz.Rect(page.rect)
            if r.width <= 0 or r.height <= 0:
                return QImage(), r, r, 0, False, 1.0
            base_scale = min(max(1.0, target_w) / r.width, max(1.0, target_h) / r.height)
            desired_scale = base_scale * eff_dpr * render_zoom
            sx, sy = clamp_render_scales(
                r.width, r.height, desired_scale, desired_scale, max_pixels=MAX_RENDER_PIXELS
            )
            mat = fitz.Matrix(sx, sy)
            m = fitz.Rect(page.mediabox)
            c = fitz.Rect(page.cropbox) & m
            crop_r = m if c.is_empty else c
            rot = page.rotation % 360
            redacted_cleanly = False

            if (text_redact_rects or keep_image_rects) and redact_page_callback is not None:
                temp_doc = fitz.open()
                try:
                    temp_doc.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
                    temp_page = temp_doc[0]
                    temp_page.set_rotation(0)
                    temp_page.set_cropbox(temp_page.mediabox)
                    try:
                        redact_page_callback(temp_page, text_redact_rects, keep_image_rects)
                    except TypeError:
                        redact_page_callback(temp_page, text_redact_rects)
                    if temp_page.rotation != rot:
                        temp_page.set_rotation(rot)
                    if crop_r != temp_page.mediabox:
                        temp_page.set_cropbox(crop_r)
                    pix = temp_page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
                    redacted_cleanly = True
                finally:
                    temp_doc.close()
            else:
                pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)

            qimg = QImage(
                pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
            ).copy()
            actual_ratio = sx / max(1e-4, base_scale)
            return qimg, r, crop_r, rot, redacted_cleanly, actual_ratio

        if fitz_lock is not None:
            with fitz_lock:
                img, page_rect, crop_rect, page_rot, redacted_cleanly, actual_ratio = _do_render()
        else:
            img, page_rect, crop_rect, page_rot, redacted_cleanly, actual_ratio = _do_render()

        if not img.isNull() and ann_list and draw_annot_callback is not None:
            try:
                draw_annot_callback(
                    img, page_rect, crop_rect, page_rot, ann_list, font_family, redacted_cleanly
                )
            except TypeError:
                draw_annot_callback(img, page_rect, crop_rect, page_rot, ann_list, font_family)
            except Exception:
                logger.debug("Spread reader operation skipped on exception", exc_info=True)

        if not img.isNull():
            img.setDevicePixelRatio(max(1.0, actual_ratio))

        return {
            "page_idx": page_idx,
            "img": img,
            "page_rect": page_rect,
            "crop_rect": crop_rect,
            "page_rot": page_rot,
            "base_rect": QRectF(),
            "rect": QRectF(),
        }


class SpreadCoordMapper:
    """見開き画面上のスクリーン座標とPDF未回転MediaBox座標の双方向変換クラス"""

    @staticmethod
    def clamp_point_to_rect(pt: QPointF, rect: QRectF) -> QPointF:
        return QPointF(
            max(rect.left(), min(pt.x(), rect.right())),
            max(rect.top(), min(pt.y(), rect.bottom())),
        )

    @staticmethod
    def screen_to_raw_pdf(pt: QPointF, entry: Dict[str, Any]) -> fitz.Point:
        r_scr: QRectF = entry["rect"]
        pr: fitz.Rect = entry["page_rect"]
        c: fitz.Rect = entry["crop_rect"]
        rot: int = entry["page_rot"] % 360

        if r_scr.width() <= 0 or r_scr.height() <= 0:
            return fitz.Point(c.x0, c.y0)

        vx = pr.x0 + ((pt.x() - r_scr.left()) / r_scr.width()) * pr.width
        vy = pr.y0 + ((pt.y() - r_scr.top()) / r_scr.height()) * pr.height
        return view_point_to_raw_pdf_from_geom(c, pr, rot, fitz.Point(vx, vy))

    @staticmethod
    def raw_pdf_to_screen(raw_pt, entry: Dict[str, Any]) -> QPointF:
        r_scr: QRectF = entry["rect"]
        pr: fitz.Rect = entry["page_rect"]
        c: fitz.Rect = entry["crop_rect"]
        rot: int = entry["page_rot"] % 360

        if pr.width <= 0 or pr.height <= 0:
            return r_scr.topLeft()

        view_pt = raw_pdf_to_view_point_from_geom(c, pr, rot, raw_pt)
        sx = r_scr.left() + ((view_pt.x - pr.x0) / pr.width) * r_scr.width()
        sy = r_scr.top() + ((view_pt.y - pr.y0) / pr.height) * r_scr.height()
        return QPointF(sx, sy)

    @classmethod
    def raw_rect_to_screen_rect(cls, raw_rect, entry: Dict[str, Any]) -> QRectF:
        r = fitz.Rect(raw_rect)
        r.normalize()
        p0 = cls.raw_pdf_to_screen((r.x0, r.y0), entry)
        p1 = cls.raw_pdf_to_screen((r.x1, r.y1), entry)
        return QRectF(p0, p1).normalized()

    @classmethod
    def compute_text_raw_positions(
        cls,
        screen_pos: QPointF,
        fontsize: float,
        entry: Dict[str, Any],
        ann_rot: Optional[int] = None,
    ) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        r_scr: QRectF = entry["rect"]
        pr: fitz.Rect = entry["page_rect"]
        cur_page_rot = int(entry["page_rot"]) % 360
        if ann_rot is None:
            ann_rot = cur_page_rot
        ann_rot = int(ann_rot) % 360

        scale_y = r_scr.height() / max(1.0, pr.height)
        dy_scr = fontsize * 0.88 * scale_y
        rel_angle_rad = math.radians((cur_page_rot - ann_rot) % 360)

        base_scr_pt = QPointF(
            screen_pos.x() - dy_scr * math.sin(rel_angle_rad),
            screen_pos.y() + dy_scr * math.cos(rel_angle_rad),
        )
        raw_top = cls.screen_to_raw_pdf(screen_pos, entry)
        raw_base = cls.screen_to_raw_pdf(base_scr_pt, entry)
        return (raw_top.x, raw_top.y), (raw_base.x, raw_base.y)
