# -*- coding: utf-8 -*-
"""見開きリーダーのレジューム位置・簡易履歴・表示設定の保存 (ui/reader_store.py)

マンガミーヤの「しおり」にあたる仕組み。保存先はアプリ全体で使っている
QSettings("SecureLocalPDFEditor", "SecureLocalPDFEditor") で、値は JSON 文字列。
すべてローカル保存のみで、ネットワーク通信は行わない。

- reader_positions: {正規化パス: {"page": int, "ts": float}}
- reader_history:   [{"path": str, "page": int, "ts": float}, ...] 新しい順・最大20件
- reader_prefs:     {"right_to_left": bool, "cover_single": bool}

JSON が壊れている場合は例外を出さず、既定値（未保存扱い）にフォールバックする。
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QSettings

from core import logger

_POSITIONS_KEY = "reader_positions"
_HISTORY_KEY = "reader_history"
_PREFS_KEY = "reader_prefs"
MAX_HISTORY_ENTRIES = 20


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class ReaderStore:
    """見開きリーダーの続き位置・履歴・設定を QSettings に保存する。"""

    def __init__(self, settings: Optional[QSettings] = None):
        self._settings = (
            settings
            if settings is not None
            else QSettings("SecureLocalPDFEditor", "SecureLocalPDFEditor")
        )

    # ---- 低レベル JSON 読み書き（壊れた値は既定値に倒す） ----

    def _load_json(self, key: str, default):
        raw = self._settings.value(key, None)
        if raw is None:
            return default
        if not isinstance(raw, str):
            return default
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("ReaderStore: broken JSON for key %s; using default", key)
            return default
        return data

    def _save_json(self, key: str, data) -> None:
        self._settings.setValue(key, json.dumps(data, ensure_ascii=False))
        self._settings.sync()

    # ---- 続き位置（レジューム）＋簡易履歴 ----

    def _load_positions(self) -> Dict[str, Dict[str, Any]]:
        data = self._load_json(_POSITIONS_KEY, {})
        if not isinstance(data, dict):
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for key, val in data.items():
            if isinstance(val, dict) and isinstance(val.get("page"), int):
                result[str(key)] = {
                    "page": int(val["page"]),
                    "ts": float(val.get("ts", 0.0) or 0.0),
                }
        return result

    def _load_history(self) -> List[Dict[str, Any]]:
        data = self._load_json(_HISTORY_KEY, [])
        if not isinstance(data, list):
            return []
        result: List[Dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict) and item.get("path"):
                page = item.get("page", 0)
                result.append(
                    {
                        "path": str(item["path"]),
                        "page": int(page) if isinstance(page, int) else 0,
                        "ts": float(item.get("ts", 0.0) or 0.0),
                    }
                )
        return result[:MAX_HISTORY_ENTRIES]

    def get_position(self, path: str) -> Optional[int]:
        """保存された続き位置（0始まりのページ番号）。なければ None。"""
        if not path:
            return None
        entry = self._load_positions().get(_norm_path(path))
        if entry is None:
            return None
        return entry["page"]

    def set_position(self, path: str, page: int) -> None:
        """続き位置を保存し、簡易履歴の先頭にも記録する。"""
        if not path:
            return
        page = max(0, int(page))
        ts = time.time()
        norm = _norm_path(path)

        positions = self._load_positions()
        positions[norm] = {"page": page, "ts": ts}
        self._save_json(_POSITIONS_KEY, positions)

        history = [h for h in self._load_history() if _norm_path(h["path"]) != norm]
        history.insert(
            0, {"path": os.path.abspath(str(path)), "page": page, "ts": ts}
        )
        self._save_json(_HISTORY_KEY, history[:MAX_HISTORY_ENTRIES])

    def get_history(self) -> List[Dict[str, Any]]:
        """簡易履歴（新しい順・最大20件）。各要素は {"path", "page", "ts"}。"""
        return self._load_history()

    def remove_entry(self, path: str) -> None:
        """指定ファイルの続き位置と履歴を取り除く（ファイル消失時など）。"""
        if not path:
            return
        norm = _norm_path(path)
        positions = self._load_positions()
        if norm in positions:
            del positions[norm]
            self._save_json(_POSITIONS_KEY, positions)
        history = [h for h in self._load_history() if _norm_path(h["path"]) != norm]
        self._save_json(_HISTORY_KEY, history)

    # ---- 表示設定（綴じ方向・表紙の有無） ----

    def get_prefs(self) -> Dict[str, bool]:
        data = self._load_json(_PREFS_KEY, {})
        if not isinstance(data, dict):
            data = {}
        return {
            "right_to_left": bool(data.get("right_to_left", False)),
            "cover_single": bool(data.get("cover_single", True)),
        }

    def set_prefs(
        self,
        *,
        right_to_left: Optional[bool] = None,
        cover_single: Optional[bool] = None,
    ) -> None:
        prefs = self.get_prefs()
        if right_to_left is not None:
            prefs["right_to_left"] = bool(right_to_left)
        if cover_single is not None:
            prefs["cover_single"] = bool(cover_single)
        self._save_json(_PREFS_KEY, prefs)
