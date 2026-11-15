# -*- coding: utf-8 -*-
"""UIコンポーネントパッケージ - Qtカラー変換・アイコン生成・テキスト座標補助ユーティリティ"""
import math
from typing import Sequence, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QComboBox


def rgb_tuple_to_qcolor(rgb, alpha: int = 255) -> QColor:
    """(0.0〜1.0, 0.0〜1.0, 0.0〜1.0) のRGBタプルを QColor に変換する"""
    if not rgb or len(rgb) < 3:
        return QColor(0, 0, 0, alpha)
    r, g, b = rgb[:3]
    return QColor(
        max(0, min(255, int(round(r * 255)))),
        max(0, min(255, int(round(g * 255)))),
        max(0, min(255, int(round(b * 255)))),
        alpha,
    )


def qcolor_to_rgb_tuple(qcolor: QColor) -> Tuple[float, float, float]:
    """QColor を PyMuPDF 用の (0.0〜1.0) RGBタプルに変換する"""
    return (
        round(qcolor.redF(), 4),
        round(qcolor.greenF(), 4),
        round(qcolor.blueF(), 4),
    )


def create_color_swatch_icon(rgb, size: int = 16) -> QIcon:
    """コンボボックス表示用の色見本アイコンを生成する"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(80, 80, 80), 1))
    painter.setBrush(QBrush(rgb_tuple_to_qcolor(rgb)))
    painter.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
    painter.end()
    return QIcon(pixmap)


def create_pen_eraser_icon(size: int = 16) -> QIcon:
    """ペン消しゴム用のアイコンを生成する"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(90, 90, 90), 1))
    painter.setBrush(QBrush(QColor(245, 130, 150)))
    painter.drawRoundedRect(1, 2, size - 2, size - 4, 3, 3)
    painter.setBrush(QBrush(QColor(70, 130, 210)))
    painter.drawRoundedRect(1, 2, max(4, (size - 2) // 2), size - 4, 2, 2)
    painter.end()
    return QIcon(pixmap)


def sync_color_combo(
    combo: QComboBox,
    rgb_tuple: Sequence[float],
    pen_eraser_active: bool = False,
) -> None:
    """カラー選択QComboBoxの選択状態を現在のRGB値またはペン消しゴム状態と同期する"""
    if pen_eraser_active:
        for i in range(combo.count()):
            if combo.itemData(i) == "pen_eraser":
                combo.blockSignals(True)
                combo.setCurrentIndex(i)
                combo.blockSignals(False)
                return

    for i in range(combo.count()):
        data = combo.itemData(i)
        if isinstance(data, tuple) and all(
            abs(a - b) < 0.03 for a, b in zip(data, rgb_tuple)
        ):
            combo.blockSignals(True)
            combo.setCurrentIndex(i)
            combo.blockSignals(False)
            return

    custom_idx = combo.count() - 1
    if custom_idx >= 0:
        qcol = rgb_tuple_to_qcolor(rgb_tuple)
        combo.blockSignals(True)
        combo.setItemIcon(custom_idx, create_color_swatch_icon(rgb_tuple))
        combo.setItemText(custom_idx, f"カスタム ({qcol.name()})")
        combo.setCurrentIndex(custom_idx)
        combo.blockSignals(False)


def compute_scaled_baseline_top_pos(
    old_pos: Tuple[float, float],
    baseline_pos: Tuple[float, float],
    new_fontsize: float,
) -> Tuple[float, float]:
    """ベースライン座標を維持したまま新しいフォントサイズに対応する上端pos座標を計算する"""
    bx, by = baseline_pos
    ox, oy = old_pos
    dist = math.hypot(ox - bx, oy - by)
    if dist > 1e-4:
        scale = (float(new_fontsize) * 0.88) / dist
        return (bx + (ox - bx) * scale, by + (oy - by) * scale)
    return old_pos
