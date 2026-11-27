# -*- coding: utf-8 -*-
"""共通定数・ロック・数値抽出・フォント検索・PDF座標変換・OS関連付けヘルパー"""
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import fitz
from PyQt6.QtGui import QFont, QFontDatabase, QFontMetricsF
from PyQt6.QtWidgets import QApplication

from core import TEXT_LINE_SPACING, logger

# MuPDF全体のアンチエイリアス品質を最大（8）に設定
try:
    fitz.Tools().set_aa_level(8)
except Exception:
    pass

# MuPDF (fitz) のスレッド競合を防ぐ再入可能ロック
FITZ_LOCK = threading.RLock()


def normalize_page_rotation(page):
    """ページの回転（/Rotate）を見た目どおりに焼き込み、回転を 0 にする。

    fitz の remove_rotation() は、CropBox が MediaBox より小さい
    ページ（スキャンアプリが切り抜き窓を持つPDFなど）で窓の位置を
    取り違え、見えている範囲がずれて内容が欠けてしまう。
    そこで、先に MediaBox を CropBox に合わせてから remove_rotation()
    し、最後に既知のずれ量（窓の中心と MediaBox の中心の縦方向の
    ずれの2倍）だけ内容を平行移動して補正する。ずれ量と方向は
    回転角ごとに実測で確認した（2026-11-24、MIWA の実ファイルと
    合成ファイルで検証）。回転がなければ何もしない。
    """
    if not page.rotation:
        return
    if page.cropbox == page.mediabox:
        page.remove_rotation()
        return
    rotation = page.rotation
    cb = page.cropbox
    mb = page.mediabox
    # 窓の中心と MediaBox の中心の縦方向のずれ（×2）
    s = (cb.y0 + cb.y1) - (mb.y0 + mb.y1)
    page.set_mediabox(cb)
    page.remove_rotation()
    if s:
        if rotation == 90:
            tx, ty = s, 0.0
        elif rotation == 180:
            tx, ty = 0.0, -s
        elif rotation == 270:
            tx, ty = -s, 0.0
        else:
            tx = ty = 0.0
        if tx or ty:
            try:
                import pymupdf
                from pymupdf import TOOLS, _format_g

                cmd = (_format_g((1, 0, 0, 1, tx, ty)) + " cm ").encode("utf8")
                TOOLS._insert_contents(page, cmd, False)
            except Exception:
                pass

RGBTuple = Tuple[float, float, float]
PointTuple = Tuple[float, float]
RectTuple = Tuple[float, float, float, float]
AnnotationDict = Dict[str, Any]
AnnotationMap = Dict[int, List[AnnotationDict]]

# 基本設定・スケール定数
APP_NAME = "Secure Local PDF Editor"
PROG_ID = "SecureLocalPDFEditor.pdf"
MIME_PDF_PAGES = "application/x-secure-pdf-pages"

RENDER_SCALE = 3.0
MIN_DYNAMIC_PDF_SCALE = 0.5
MAX_DYNAMIC_PDF_SCALE = 8.0
MAX_RENDER_PIXELS = 24_000_000
THUMBNAIL_RENDER_SCALE = 0.45


def clamp_render_scales(
    page_w: float,
    page_h: float,
    scale_x: float,
    scale_y: float,
    min_scale: float = MIN_DYNAMIC_PDF_SCALE,
    max_scale: float = MAX_DYNAMIC_PDF_SCALE,
    max_pixels: int = MAX_RENDER_PIXELS,
) -> Tuple[float, float]:
    """
    倍率上下限および最大総ピクセル数（大判PDF高倍率ズーム時のメモリ爆発防止）の
    両方を満たすようにレンダリング倍率 (scale_x, scale_y) をクランプする。
    """
    sx = max(1e-4, float(scale_x))
    sy = max(1e-4, float(scale_y))
    max_exact = max(sx, sy)
    if max_exact > max_scale:
        ratio = max_scale / max_exact
        sx *= ratio
        sy *= ratio
    elif max_exact < min_scale:
        ratio = min_scale / max_exact
        sx *= ratio
        sy *= ratio

    pw = max(1.0, float(page_w))
    ph = max(1.0, float(page_h))
    est_pixels = (pw * sx) * (ph * sy)
    if max_pixels > 0 and est_pixels > float(max_pixels):
        px_ratio = math.sqrt(float(max_pixels) / est_pixels)
        sx *= px_ratio
        sy *= px_ratio

    return sx, sy

DEFAULT_COLOR: RGBTuple = (0.90, 0.05, 0.05)
DRAW_WIDTH = 1.5
MIN_DRAW_WIDTH = 0.5
MAX_DRAW_WIDTH = 30.0

TEXT_FONT_SIZE = 14.0
MIN_FONT_SIZE = 5.0
MAX_FONT_SIZE = 96.0

MAX_UNDO_STEPS = 50
MAX_RECENT_FILES = 20
WHEEL_PAGE_TURN_COOLDOWN = 0.18


def format_pt(val: float) -> str:
    """ポイント数値を表示用に整形する（14.0 -> '14', 10.5 -> '10.5'）"""
    rounded = round(float(val), 1)
    if abs(rounded - round(rounded)) < 1e-4:
        return str(int(round(rounded)))
    return f"{rounded:.1f}"


def extract_number_from_text(text: Any) -> Optional[float]:
    """
    PDF内の文字列（例: '1,234,567', '¥12,000', '▲500', '△1,200円', '-3.5%' など）から
    計算用の数値（float）を抽出する。数値が含まれない場合は None を返す。
    """
    if not text:
        return None

    normalized = unicodedata.normalize("NFKC", str(text)).strip()
    if not normalized:
        return None

    is_parenthesized_negative = (
        normalized.startswith("(") and normalized.endswith(")")
    )
    normalized = re.sub(r"[▲△－ー−﹣]", "-", normalized)
    cleaned = normalized.replace(",", "").replace(" ", "")
    cleaned = re.sub(r"([+-])[¥￥$]+", r"\1", cleaned)

    match = re.search(r"(?<![0-9A-Za-z])[+-]?(?:\d+\.?\d*|\.\d+)", cleaned)
    if not match:
        return None

    try:
        val = float(match.group(0))
        if is_parenthesized_negative and val > 0:
            val = -val
        return val
    except ValueError:
        return None


def format_number_value(val: float) -> str:
    """数値をカンマ区切り文字列に整形する（整数なら小数点なし、小数なら不要な末尾0を除去）"""
    if abs(val - round(val)) < 1e-7:
        return f"{int(round(val)):,}"
    formatted = f"{val:,.4f}".rstrip("0").rstrip(".")
    return formatted


def srgb_int_to_rgb_tuple(srgb_int: Any) -> RGBTuple:
    """PyMuPDFのget_text('rawdict')が返す整数カラー値を(0.0〜1.0)のRGBタプルに変換する"""
    if not isinstance(srgb_int, int):
        return (0.0, 0.0, 0.0)
    r = ((srgb_int >> 16) & 0xFF) / 255.0
    g = ((srgb_int >> 8) & 0xFF) / 255.0
    b = (srgb_int & 0xFF) / 255.0
    return (round(r, 4), round(g, 4), round(b, 4))


def create_configured_preview_font(family: str, pixel_size: float) -> QFont:
    """QGraphicsViewの拡大縮小表示でも文字の太さムラやガタつきが出ないQFontを生成する"""
    font = QFont(family)
    font.setPixelSize(max(1, int(round(pixel_size))))
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    font.setStyleStrategy(
        QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality
    )
    return font


# ==========================================
# 日本語フォント検索・埋め込み検証
# ==========================================

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\YuGothR.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/takao-gothic/TakaoGothic.ttf",
]


def find_verified_japanese_font() -> Optional[str]:
    """OS内の日本語フォントを検索し、PyMuPDFで実際に埋め込み可能か検証する"""
    candidates = list(FONT_CANDIDATES)

    if shutil.which("fc-match"):
        try:
            res = subprocess.run(
                ["fc-match", ":lang=ja", "-f", "%{file}"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            fc_path = res.stdout.strip()
            if fc_path and fc_path not in candidates:
                candidates.append(fc_path)
        except Exception:
            logger.debug("Optional operation failed in geometry helper", exc_info=True)

    for path in candidates:
        if os.path.isfile(path):
            try:
                fitz.Font(fontfile=path)
                return path
            except Exception:
                continue

    return None


JAPANESE_FONT = find_verified_japanese_font()
_cached_preview_font_family: Optional[str] = None


def get_preview_font_family() -> str:
    """Qtプレビュー表示用の日本語フォントファミリー名を解決・キャッシュして返す。

    成功した検索結果だけをキャッシュする。QApplication がまだ存在しない場合や
    フォント登録に失敗した場合は "Sans Serif" を返すがキャッシュしないため、
    後から QApplication が作られた際に再検索される (audit #13)。
    """
    global _cached_preview_font_family
    if _cached_preview_font_family is not None:
        return _cached_preview_font_family
    if not JAPANESE_FONT or QApplication.instance() is None:
        return "Sans Serif"
    font_id = QFontDatabase.addApplicationFont(JAPANESE_FONT)
    if font_id != -1:
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            _cached_preview_font_family = families[0]
            return _cached_preview_font_family
    return "Sans Serif"


# ==========================================
# 座標変換（未回転MediaBox座標 ⇔ 現在の表示座標）
# ==========================================

def get_effective_cropbox(page: fitz.Page) -> fitz.Rect:
    m = fitz.Rect(page.mediabox)
    c = fitz.Rect(page.cropbox) & m
    if c.is_empty:
        return m
    return c


def view_point_to_raw_pdf_from_geom(
    crop_rect: fitz.Rect,
    page_rect: fitz.Rect,
    rotation: int,
    view_pt: Union[fitz.Point, PointTuple],
) -> fitz.Point:
    pt = fitz.Point(view_pt)
    dx = pt.x - page_rect.x0
    dy = pt.y - page_rect.y0
    rot = rotation % 360

    if rot == 90:
        ux = dy
        uy = crop_rect.height - dx
    elif rot == 180:
        ux = crop_rect.width - dx
        uy = crop_rect.height - dy
    elif rot == 270:
        ux = crop_rect.width - dy
        uy = dx
    else:
        ux = dx
        uy = dy

    return fitz.Point(crop_rect.x0 + ux, crop_rect.y0 + uy)


def view_point_to_raw_pdf(page: fitz.Page, view_pt: Union[fitz.Point, PointTuple]) -> fitz.Point:
    c = get_effective_cropbox(page)
    return view_point_to_raw_pdf_from_geom(c, page.rect, page.rotation, view_pt)


def raw_pdf_to_view_point_from_geom(
    crop_rect: fitz.Rect,
    page_rect: fitz.Rect,
    rotation: int,
    raw_pt: Union[fitz.Point, PointTuple],
) -> fitz.Point:
    pt = fitz.Point(raw_pt)
    ux = pt.x - crop_rect.x0
    uy = pt.y - crop_rect.y0
    rot = rotation % 360

    if rot == 90:
        vx = page_rect.x0 + (crop_rect.height - uy)
        vy = page_rect.y0 + ux
    elif rot == 180:
        vx = page_rect.x0 + (crop_rect.width - ux)
        vy = page_rect.y0 + (crop_rect.height - uy)
    elif rot == 270:
        vx = page_rect.x0 + uy
        vy = page_rect.y0 + (crop_rect.width - ux)
    else:
        vx = page_rect.x0 + ux
        vy = page_rect.y0 + uy

    return fitz.Point(vx, vy)


def raw_rect_to_view_rect_from_geom(
    crop_rect: fitz.Rect,
    page_rect: fitz.Rect,
    rotation: int,
    raw_rect: Union[fitz.Rect, Sequence[float]],
) -> fitz.Rect:
    r = fitz.Rect(raw_rect)
    r.normalize()
    p0 = raw_pdf_to_view_point_from_geom(crop_rect, page_rect, rotation, fitz.Point(r.x0, r.y0))
    p1 = raw_pdf_to_view_point_from_geom(crop_rect, page_rect, rotation, fitz.Point(r.x1, r.y1))
    vr = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
    vr.normalize()
    return vr


def raw_pdf_to_view_point(page: fitz.Page, raw_pt: Union[fitz.Point, PointTuple]) -> fitz.Point:
    c = get_effective_cropbox(page)
    return raw_pdf_to_view_point_from_geom(c, page.rect, page.rotation, raw_pt)


def raw_rect_to_view_rect(page: fitz.Page, raw_rect: Union[fitz.Rect, Sequence[float]]) -> fitz.Rect:
    c = get_effective_cropbox(page)
    return raw_rect_to_view_rect_from_geom(c, page.rect, page.rotation, raw_rect)


def view_rect_to_raw_rect(page: fitz.Page, view_rect: Union[fitz.Rect, Sequence[float]]) -> fitz.Rect:
    vr = fitz.Rect(view_rect)
    vr.normalize()
    p0 = view_point_to_raw_pdf(page, fitz.Point(vr.x0, vr.y0))
    p1 = view_point_to_raw_pdf(page, fitz.Point(vr.x1, vr.y1))
    rr = fitz.Rect(p0.x, p0.y, p1.x, p1.y)
    rr.normalize()
    return rr


def text_annotation_rotation(dir_x: float, dir_y: float) -> int:
    """文字の進み方向（未回転＝raw座標系）から、テキスト注釈の rotation を返す。

    注釈の rotation は「回転なしページへ fitz の insert_text(rotate=...) で
    文字を入れるとき、その文字が元の進み方向を向く値」として定義する。
    insert_text の rotate=θ はベースライン方向角を −θ にするため
    （実測: θ=90 で方向は (0,-1)）、進み方向角 view_deg に対して
    rotation = (-view_deg) % 360 が必要になる。

    ページ自体の回転 (/Rotate) はこの値に含めない。焼き込み
    （PDFDocumentManager）はページ回転を外してから insert_text するので
    この値のまま渡せば元の向きが再現でき、ビューア表示ではビューア側が
    ページ回転と組み合わせて（item回転 = ページ回転 − rotation）描く。
    以前は (ページ回転 − view_deg) としており、回転したページでは
    挿入した文字がページ回転ぶんずれた向きになり、位置もずれていた。
    """
    view_deg = int(round(math.degrees(math.atan2(dir_y, dir_x)) / 90.0) * 90) % 360
    return (-view_deg) % 360


def estimate_text_bbox_in_pdf(ann: Dict[str, Any]) -> fitz.Rect:
    fontsize = max(4.0, float(ann.get("fontsize", TEXT_FONT_SIZE)))
    text_str = str(ann.get("text", ""))
    lines = text_str.split("\n") if text_str else [""]
    num_lines = max(1, len(lines))

    max_char_width_sum = max(
        (sum(1.05 if ord(c) > 127 else 0.68 for c in line) for line in lines),
        default=1.0,
    )
    est_width = max(fontsize * 0.8, fontsize * max_char_width_sum + 2.0)

    try:
        if QApplication.instance() is not None:
            fam = get_preview_font_family()
            font = create_configured_preview_font(fam, max(10, int(round(fontsize * 10.0))))
            fm = QFontMetricsF(font)
            measured_w = max((fm.horizontalAdvance(line) for line in lines), default=0.0) / 10.0
            est_width = max(est_width, measured_w + 3.0)
    except Exception:
        logger.debug("Optional environment/font operation failed in geometry", exc_info=True)

    top_offset = -fontsize * 0.16
    bottom_offset = fontsize * (1.16 + (num_lines - 1) * TEXT_LINE_SPACING)

    p_top = fitz.Point(*ann["pos"])
    if "baseline_pos" in ann:
        p_base = fitz.Point(*ann["baseline_pos"])
        v_down = p_base - p_top
        v_len = math.hypot(v_down.x, v_down.y)
        if v_len > 1e-3:
            u_down = fitz.Point(v_down.x / v_len, v_down.y / v_len)
            u_right = fitz.Point(v_down.y / v_len, -v_down.x / v_len)
            p_start = p_top + u_down * top_offset
            down_vec = u_down * (bottom_offset - top_offset)
            right_vec = u_right * est_width
            pts = [
                p_start,
                p_start + down_vec,
                p_start + right_vec,
                p_start + down_vec + right_vec,
            ]
            xs = [p.x for p in pts]
            ys = [p.y for p in pts]
            return fitz.Rect(min(xs), min(ys), max(xs), max(ys))

    return fitz.Rect(
        p_top.x,
        p_top.y + top_offset,
        p_top.x + est_width,
        p_top.y + bottom_offset,
    )


def get_crop_outside_rects(mediabox: fitz.Rect, cropbox: fitz.Rect) -> List[fitz.Rect]:
    m = fitz.Rect(mediabox)
    c = fitz.Rect(cropbox) & m
    if c.is_empty or c == m:
        return []

    outside = []
    if c.y0 > m.y0:
        outside.append(fitz.Rect(m.x0, m.y0, m.x1, c.y0))
    if c.y1 < m.y1:
        outside.append(fitz.Rect(m.x0, c.y1, m.x1, m.y1))
    if c.x0 > m.x0:
        outside.append(fitz.Rect(m.x0, c.y0, c.x0, c.y1))
    if c.x1 < m.x1:
        outside.append(fitz.Rect(c.x1, c.y0, m.x1, c.y1))

    return [r for r in outside if not r.is_empty]


# ==========================================
# Windows ファイル関連付け（既定のアプリ登録）ヘルパー
# ==========================================

def get_app_icon_path(prefer_ico: bool = False) -> Optional[str]:
    """きなこPDFのアプリアイコン (.ico / .png) のパスを返す"""
    bases = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(Path(meipass))
    if getattr(sys, "frozen", False):
        bases.append(Path(sys.executable).resolve().parent)
    bases.append(Path(__file__).resolve().parent.parent)

    names = (
        ("kinako_pdf.ico", "kinako_pdf.png")
        if prefer_ico
        else ("kinako_pdf.png", "kinako_pdf.ico")
    )
    for base in bases:
        for sub in ("assets", ""):
            for fname in names:
                cand = (base / sub / fname) if sub else (base / fname)
                if cand.is_file():
                    return str(cand.resolve())
    return None


def _resolve_main_entry_script(project_root: Path) -> str:
    """プロジェクトルート内のメイン起動スクリプト (.pyw / .py) を自動検出する"""
    main_mod = sys.modules.get("__main__")
    main_file = getattr(main_mod, "__file__", None) if main_mod is not None else None
    if main_file:
        cand = Path(main_file).resolve()
        if (
            cand.is_file()
            and cand.parent == project_root.resolve()
            and "patch" not in cand.name.lower()
            and cand.suffix.lower() in (".pyw", ".py")
        ):
            return str(cand)

    preferred = [
        "secure_local_pdf_editor.pyw",
        "secure_local_pdf_editor.py",
        "kinako_pdf.pyw",
        "kinako_pdf.py",
        "kinakopdf.pyw",
        "kinakopdf.py",
    ]
    for name in preferred:
        cand = project_root / name
        if cand.is_file():
            return str(cand.resolve())

    for ext in ("*.pyw", "*.py"):
        for cand in sorted(project_root.glob(ext)):
            if cand.is_file() and "patch" not in cand.name.lower():
                return str(cand.resolve())

    return str((project_root / "secure_local_pdf_editor.pyw").resolve())


def build_windows_open_command(main_script_path: Optional[str] = None) -> Tuple[str, str]:
    """WindowsでPDFをダブルクリックした際に実行されるコマンドライン文字列を生成する"""
    ico_path = get_app_icon_path(prefer_ico=True) if "get_app_icon_path" in globals() else None
    if getattr(sys, "frozen", False):
        exe_path = os.path.abspath(sys.executable)
        if not os.path.isfile(exe_path):
            raise FileNotFoundError(f"実行ファイルが見つかりません: {exe_path}")
        icon_spec = f'"{ico_path}"' if (ico_path and ico_path.lower().endswith(".ico")) else f'"{exe_path}",0'
        return f'"{exe_path}" "%1"', icon_spec

    py_exe = os.path.abspath(sys.executable)
    py_dir = os.path.dirname(py_exe)
    pythonw_candidate = os.path.join(py_dir, "pythonw.exe")
    if os.path.isfile(pythonw_candidate):
        py_exe = pythonw_candidate

    if main_script_path is None:
        project_root = Path(__file__).resolve().parent.parent
        script_path = _resolve_main_entry_script(project_root)
    else:
        script_path = os.path.abspath(main_script_path)

    if not os.path.isfile(script_path):
        raise FileNotFoundError(
            f"既定のアプリとして登録するスクリプトが見つかりません: {script_path}"
        )
    icon_spec = f'"{ico_path}"' if (ico_path and ico_path.lower().endswith(".ico")) else f'"{py_exe}",0'
    return f'"{py_exe}" "{script_path}" "%1"', icon_spec


def register_as_windows_pdf_handler(main_script_path: Optional[str] = None) -> str:
    """
    管理者権限不要の HKEY_CURRENT_USER に、
    このアプリを .pdf ファイルのプログラム候補として登録し、Windows標準のPDFサムネイルエンジンを設定する。
    """
    if sys.platform != "win32":
        raise RuntimeError("この自動登録機能はWindows環境専用です。")

    import ctypes
    import winreg

    def _is_valid_com_clsid(clsid_str: str) -> bool:
        if not clsid_str or not clsid_str.startswith("{"):
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid_str}") as ck:
                try:
                    winreg.QueryValueEx(ck, "AppID")
                    return True
                except OSError:
                    pass
                for sub in ("InprocServer32", "LocalServer32"):
                    try:
                        with winreg.OpenKey(ck, sub):
                            return True
                    except OSError:
                        pass
        except OSError:
            pass
        return False

    open_cmd, icon_spec = build_windows_open_command(main_script_path)

    prog_key_path = f"Software\\Classes\\{PROG_ID}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, prog_key_path) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"{APP_NAME} Document")
        winreg.SetValueEx(key, "FriendlyTypeName", 0, winreg.REG_SZ, APP_NAME)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{prog_key_path}\\Application") as app_key:
        winreg.SetValueEx(app_key, "ApplicationName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(app_key, "ApplicationDescription", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(app_key, "ApplicationIcon", 0, winreg.REG_SZ, icon_spec)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{prog_key_path}\\DefaultIcon") as icon_key:
        winreg.SetValueEx(icon_key, "", 0, winreg.REG_SZ, icon_spec)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{prog_key_path}\\shell\\open") as open_key:
        winreg.SetValueEx(open_key, "FriendlyAppName", 0, winreg.REG_SZ, APP_NAME)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{prog_key_path}\\shell\\open\\command") as cmd_key:
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, open_cmd)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.pdf") as pdf_ext_key:
        winreg.SetValueEx(pdf_ext_key, "Content Type", 0, winreg.REG_SZ, "application/pdf")
        winreg.SetValueEx(pdf_ext_key, "PerceivedType", 0, winreg.REG_SZ, "document")

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.pdf\OpenWithProgids") as ow_key:
        winreg.SetValueEx(ow_key, PROG_ID, 0, winreg.REG_NONE, b"")

    cap_path = r"Software\SecureLocalPDFEditor\Capabilities"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cap_path) as cap_key:
        winreg.SetValueEx(cap_key, "ApplicationName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(
            cap_key,
            "ApplicationDescription",
            0,
            winreg.REG_SZ,
            "ローカル環境で安全にPDFの閲覧・文字編集・墨消し・結合を行うPDFエディタ",
        )

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{cap_path}\\FileAssociations") as fa_key:
        winreg.SetValueEx(fa_key, ".pdf", 0, winreg.REG_SZ, PROG_ID)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications") as reg_app_key:
        winreg.SetValueEx(reg_app_key, "SecureLocalPDFEditor", 0, winreg.REG_SZ, cap_path)

    THUMB_GUID = "{E357FCCD-A995-4576-B01F-234630154E96}"
    EDGE_BLOCKED_CLSID = "{3A84F9C2-6164-485C-A7D9-4B27F8AC009E}"
    PREFERRED_PDF_THUMB = "{A4A41054-7D08-41CD-9CAA-CF95329F6E75}"
    thumb_clsid = PREFERRED_PDF_THUMB if _is_valid_com_clsid(PREFERRED_PDF_THUMB) else None

    if not thumb_clsid:
        for root_k, sub_k in (
            (winreg.HKEY_CLASSES_ROOT, rf"NuancePDF.Document\shellex\{THUMB_GUID}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Classes\NuancePDF.Document\shellex\{THUMB_GUID}"),
            (winreg.HKEY_CLASSES_ROOT, rf"AcroExch.Document.DC\shellex\{THUMB_GUID}"),
            (winreg.HKEY_CLASSES_ROOT, rf"AcroExch.Document\shellex\{THUMB_GUID}"),
            (winreg.HKEY_CLASSES_ROOT, rf"FoxitPhantomPDF.Document\shellex\{THUMB_GUID}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Classes\.pdf\shellex\{THUMB_GUID}"),
            (winreg.HKEY_CLASSES_ROOT, rf"SystemFileAssociations\.pdf\shellex\{THUMB_GUID}"),
        ):
            try:
                with winreg.OpenKey(root_k, sub_k) as k:
                    val, _ = winreg.QueryValueEx(k, "")
                    val_str = str(val).strip()
                    if (
                        val_str
                        and val_str.upper() != EDGE_BLOCKED_CLSID
                        and _is_valid_com_clsid(val_str)
                    ):
                        thumb_clsid = val_str
                        break
            except OSError:
                pass

    if thumb_clsid:
        for base_cls in (prog_key_path, r"Software\Classes\.pdf"):
            shellex_key_path = f"{base_cls}\\shellex\\{THUMB_GUID}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, shellex_key_path) as s_key:
                winreg.SetValueEx(s_key, "", 0, winreg.REG_SZ, thumb_clsid)

    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    except Exception:
        logger.debug("Optional environment/font operation failed in geometry", exc_info=True)

    return open_cmd
