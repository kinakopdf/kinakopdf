# -*- coding: utf-8 -*-
"""ベータ版3「マンガミーヤ風 見開き強化」に対するテスト。

- スロット/組立てモデル（純粋関数）: 表紙あり/なし × 白紙 × 横向き × 冊数。
- ReaderStore: 続き位置の往復、履歴の上限と重複排除、壊れた保存値への耐性。
- リーダー本体: 綴じ方向で配置とクリック側が入れ替わること、白紙の挿入で
  組がずれ・除去で戻ること（PDF 自体は不変）、スライダー/番号ジャンプ、
  全画面トグルと Esc、レジュームの記録と復帰、先読みの生成と無効化。
- MainWindow: 履歴からファイルを開くと続きから見開きが開くこと。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz


def _make_doc(page_sizes):
    doc = fitz.open()
    for i, (w, h) in enumerate(page_sizes):
        page = doc.new_page(width=w, height=h)
        page.insert_text((40, 60), f"page {i + 1}", fontsize=16)
    return doc


def _temp_store():
    from PyQt6.QtCore import QSettings

    from ui.reader_store import ReaderStore

    tmp = tempfile.TemporaryDirectory()
    settings = QSettings(
        str(Path(tmp.name) / "settings.ini"), QSettings.Format.IniFormat
    )
    store = ReaderStore(settings=settings)
    store._test_tmp = tmp  # TemporaryDirectory を生存させる
    return store


class TestSlotModel(unittest.TestCase):
    """SpreadLayoutEngine のスロット列と見開き組立て（純粋関数）。"""

    def test_build_slot_sequence(self):
        from ui.spread_engine import SpreadLayoutEngine

        self.assertEqual(
            SpreadLayoutEngine.build_slot_sequence(4), [0, 1, 2, 3]
        )
        self.assertEqual(
            SpreadLayoutEngine.build_slot_sequence(4, {1: 1}),
            [0, None, 1, 2, 3],
        )
        self.assertEqual(
            SpreadLayoutEngine.build_slot_sequence(3, {0: 2}),
            [None, None, 0, 1, 2],
        )
        self.assertEqual(SpreadLayoutEngine.build_slot_sequence(0), [])

    def test_spread_item_default_slots(self):
        from ui.spread_engine import SpreadItem

        item = SpreadItem([0, 1], False)
        self.assertEqual(item.slots, [0, 1])
        self.assertTrue(item.is_spread)
        single = SpreadItem([2], True)
        self.assertEqual(single.slots, [2])
        self.assertFalse(single.is_spread)

    def _slots_of(self, doc, counts, cover):
        from ui.spread_engine import SpreadLayoutEngine

        seq = SpreadLayoutEngine.build_slot_sequence(len(doc), counts)
        spreads = SpreadLayoutEngine.build_spreads_from_slots(
            doc, seq, cover_single=cover
        )
        return [s.slots for s in spreads]

    def test_cover_on_off_no_blanks(self):
        doc = _make_doc([(595, 842)] * 5)
        try:
            self.assertEqual(
                self._slots_of(doc, {}, True), [[0], [1, 2], [3, 4]]
            )
            self.assertEqual(
                self._slots_of(doc, {}, False), [[0, 1], [2, 3], [4]]
            )
        finally:
            doc.close()

    def test_blank_before_middle_cover_on(self):
        doc = _make_doc([(595, 842)] * 5)
        try:
            spreads = self._slots_of(doc, {1: 1}, True)
            self.assertEqual(spreads, [[0], [None, 1], [2, 3], [4]])
        finally:
            doc.close()

    def test_blank_before_first_cover_on_and_off(self):
        doc = _make_doc([(595, 842)] * 5)
        try:
            self.assertEqual(
                self._slots_of(doc, {0: 1}, True),
                [[None], [0, 1], [2, 3], [4]],
            )
            self.assertEqual(
                self._slots_of(doc, {0: 1}, False),
                [[None, 0], [1, 2], [3, 4]],
            )
        finally:
            doc.close()

    def test_blank_near_end_odd_counts(self):
        doc = _make_doc([(595, 842)] * 5)
        try:
            self.assertEqual(
                self._slots_of(doc, {4: 1}, False),
                [[0, 1], [2, 3], [None, 4]],
            )
        finally:
            doc.close()
        doc3 = _make_doc([(595, 842)] * 3)
        try:
            self.assertEqual(
                self._slots_of(doc3, {0: 1}, False), [[None, 0], [1, 2]]
            )
        finally:
            doc3.close()

    def test_landscape_pages_stay_single_with_blanks(self):
        doc = _make_doc([(595, 842), (595, 842), (842, 595), (595, 842)])
        try:
            self.assertEqual(
                self._slots_of(doc, {}, False), [[0, 1], [2], [3]]
            )
            # 横向きページの直前の白紙は、横向きと組まず単独になる
            self.assertEqual(
                self._slots_of(doc, {2: 1}, False),
                [[0, 1], [None], [2], [3]],
            )
        finally:
            doc.close()

    def test_page_indices_exclude_blanks(self):
        from ui.spread_engine import SpreadLayoutEngine

        doc = _make_doc([(595, 842)] * 5)
        try:
            seq = SpreadLayoutEngine.build_slot_sequence(5, {1: 1})
            spreads = SpreadLayoutEngine.build_spreads_from_slots(
                doc, seq, cover_single=True
            )
            self.assertEqual(
                [s.page_indices for s in spreads], [[0], [1], [2, 3], [4]]
            )
            self.assertTrue(spreads[1].is_spread)
        finally:
            doc.close()


class TestReaderStore(unittest.TestCase):
    def setUp(self):
        self.store = _temp_store()

    def test_position_roundtrip(self):
        self.assertIsNone(self.store.get_position("/tmp/book.pdf"))
        self.store.set_position("/tmp/book.pdf", 7)
        self.assertEqual(self.store.get_position("/tmp/book.pdf"), 7)
        self.store.set_position("/tmp/book.pdf", 2)
        self.assertEqual(self.store.get_position("/tmp/book.pdf"), 2)

    def test_history_newest_first_dedup_and_cap(self):
        for i in range(22):
            self.store.set_position(f"/tmp/book{i}.pdf", i)
        history = self.store.get_history()
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0]["path"], os.path.abspath("/tmp/book21.pdf"))
        self.assertEqual(history[0]["page"], 21)
        # 同じファイルを更新すると先頭に来て重複しない
        self.store.set_position("/tmp/book5.pdf", 42)
        history = self.store.get_history()
        paths = [h["path"] for h in history]
        self.assertEqual(paths[0], os.path.abspath("/tmp/book5.pdf"))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(history[0]["page"], 42)

    def test_corrupt_store_tolerated(self):
        settings = self.store._settings
        settings.setValue("reader_positions", "{not json")
        settings.setValue("reader_history", "###broken###")
        settings.setValue("reader_prefs", "[1,2")
        settings.sync()
        self.assertIsNone(self.store.get_position("/tmp/book.pdf"))
        self.assertEqual(self.store.get_history(), [])
        prefs = self.store.get_prefs()
        self.assertEqual(prefs, {"right_to_left": False, "cover_single": True})
        # 壊れた値の上からでも保存し直せる
        self.store.set_position("/tmp/book.pdf", 3)
        self.assertEqual(self.store.get_position("/tmp/book.pdf"), 3)

    def test_prefs_roundtrip_and_partial_update(self):
        self.store.set_prefs(right_to_left=True)
        self.assertEqual(
            self.store.get_prefs(), {"right_to_left": True, "cover_single": True}
        )
        self.store.set_prefs(cover_single=False)
        self.assertEqual(
            self.store.get_prefs(), {"right_to_left": True, "cover_single": False}
        )

    def test_remove_entry(self):
        self.store.set_position("/tmp/a.pdf", 1)
        self.store.set_position("/tmp/b.pdf", 2)
        self.store.remove_entry("/tmp/a.pdf")
        self.assertIsNone(self.store.get_position("/tmp/a.pdf"))
        self.assertEqual(self.store.get_position("/tmp/b.pdf"), 2)
        paths = [h["path"] for h in self.store.get_history()]
        self.assertNotIn(os.path.abspath("/tmp/a.pdf"), paths)


class _ReaderTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.store = _temp_store()
        self._readers = []
        self._docs = []

    def tearDown(self):
        for reader in self._readers:
            try:
                reader.close()
            except Exception:
                pass
        self._app.processEvents()
        for doc in self._docs:
            if not doc.is_closed:
                doc.close()

    def _make_reader(self, page_sizes, **kwargs):
        from ui.spread_reader import FullscreenSpreadReader

        doc = _make_doc(page_sizes)
        self._docs.append(doc)
        kwargs.setdefault("store", self.store)
        reader = FullscreenSpreadReader(doc=doc, **kwargs)
        self._readers.append(reader)
        reader.show()
        reader.resize(1000, 700)
        self._app.processEvents()
        reader._ensure_initial_render()
        self._app.processEvents()
        return reader

    def _doc_texts(self, doc):
        return [doc[i].get_text() for i in range(len(doc))]


class TestReaderDirection(_ReaderTestBase):
    def test_direction_toggle_swaps_placement_and_persists(self):
        reader = self._make_reader([(595, 842)] * 4, cover_single=False)
        self.assertFalse(reader.right_to_left)
        self.assertEqual(
            [e["page_idx"] for e in reader._cached_draw_items], [0, 1]
        )
        reader._toggle_reading_direction()
        self.assertTrue(reader.right_to_left)
        self.assertTrue(reader.btn_hud_dir.isChecked())
        self.assertEqual(
            [e["page_idx"] for e in reader._cached_draw_items], [1, 0]
        )
        self.assertTrue(self.store.get_prefs()["right_to_left"])
        reader._toggle_reading_direction()
        self.assertFalse(reader.right_to_left)
        self.assertEqual(
            [e["page_idx"] for e in reader._cached_draw_items], [0, 1]
        )
        self.assertFalse(self.store.get_prefs()["right_to_left"])

    def test_click_buttons_always_next_prev(self):
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        reader = self._make_reader([(595, 842)] * 6, cover_single=False)
        self.assertGreater(reader.width(), 400)
        w, h = reader.width(), reader.height()

        def press(x, button=Qt.MouseButton.LeftButton):
            ev = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(x, h * 0.4),
                QPointF(x, h * 0.4),
                button,
                button,
                Qt.KeyboardModifier.NoModifier,
            )
            reader.mousePressEvent(ev)

        # 左ボタンは押す場所によらず常に次へ、右ボタンは常に前へ
        self.assertEqual(reader.current_spread_idx, 0)
        press(w * 0.8)
        self.assertEqual(reader.current_spread_idx, 1)
        press(w * 0.2)  # 左半分でも次へ
        self.assertEqual(reader.current_spread_idx, 2)
        press(w * 0.2, Qt.MouseButton.RightButton)  # 左半分でも前へ
        self.assertEqual(reader.current_spread_idx, 1)
        press(w * 0.8, Qt.MouseButton.RightButton)  # 右半分でも前へ
        self.assertEqual(reader.current_spread_idx, 0)

        # 綴じ方向を右開きに変えてもクリックの進む／戻るは変わらない
        reader._toggle_reading_direction()
        press(w * 0.8)
        self.assertEqual(reader.current_spread_idx, 1)
        press(w * 0.2)
        self.assertEqual(reader.current_spread_idx, 2)
        press(w * 0.2, Qt.MouseButton.RightButton)
        self.assertEqual(reader.current_spread_idx, 1)
        press(w * 0.8, Qt.MouseButton.RightButton)
        self.assertEqual(reader.current_spread_idx, 0)


class TestReaderBlankPages(_ReaderTestBase):
    def test_insert_shifts_pairing_and_remove_restores(self):
        reader = self._make_reader([(595, 842)] * 5, cover_single=True)
        doc = reader.doc
        before_texts = self._doc_texts(doc)
        self.assertEqual(
            [s.slots for s in reader.spreads], [[0], [1, 2], [3, 4]]
        )
        reader.set_current_page(1)
        reader.insert_blank_before_current()
        self.assertEqual(reader._blank_counts, {1: 1})
        self.assertEqual(
            [s.slots for s in reader.spreads], [[0], [None, 1], [2, 3], [4]]
        )
        # アンカーのページを含む見開きに留まる
        self.assertIn(1, reader.spreads[reader.current_spread_idx].page_indices)
        # PDF 自体は変更されていない
        self.assertEqual(len(doc), 5)
        self.assertEqual(self._doc_texts(doc), before_texts)

        reader.remove_blank_for_current()
        self.assertEqual(reader._blank_counts, {})
        self.assertEqual(
            [s.slots for s in reader.spreads], [[0], [1, 2], [3, 4]]
        )
        self.assertEqual(len(doc), 5)
        self.assertEqual(self._doc_texts(doc), before_texts)

    def test_shift_aliases_match_blank_ops(self):
        reader = self._make_reader([(595, 842)] * 5, cover_single=True)
        reader.set_current_page(3)
        reader.shift_pairing_forward()
        self.assertEqual(reader._blank_counts, {3: 1})
        reader.shift_pairing_back()
        self.assertEqual(reader._blank_counts, {})

    def test_insert_before_cover_and_blank_only_spread_hud(self):
        reader = self._make_reader([(595, 842)] * 5, cover_single=True)
        reader.set_current_page(0)
        reader.insert_blank_before_current()
        self.assertEqual(
            [s.slots for s in reader.spreads], [[None], [0, 1], [2, 3], [4]]
        )
        self.assertIn(0, reader.spreads[reader.current_spread_idx].page_indices)
        # 白紙だけの見開きでも HUD が落ちない
        reader._goto_spread(0)
        self.assertIn("白紙", reader.hud_label.text())

    def test_cover_toggle_preserves_blanks(self):
        reader = self._make_reader([(595, 842)] * 5, cover_single=True)
        reader.set_current_page(1)
        reader.insert_blank_before_current()
        reader.btn_hud_cover.click()
        self.assertFalse(reader.cover_single)
        self.assertEqual(reader._blank_counts, {1: 1})
        self.assertEqual(
            [s.slots for s in reader.spreads], [[0, None], [1, 2], [3, 4]]
        )

    def test_one_page_doc_blank_cycle(self):
        reader = self._make_reader([(595, 842)], cover_single=True)
        self.assertEqual([s.slots for s in reader.spreads], [[0]])
        reader.insert_blank_before_current()
        self.assertEqual([s.slots for s in reader.spreads], [[None], [0]])
        self.assertEqual(reader.page_slider.maximum(), 1)
        reader.remove_blank_for_current()
        self.assertEqual([s.slots for s in reader.spreads], [[0]])
        self.assertEqual(reader.page_slider.maximum(), 0)
        reader.next_spread()
        reader.prev_spread()
        self.assertEqual(reader.current_spread_idx, 0)


class TestReaderNavigationUi(_ReaderTestBase):
    def test_slider_and_spin_jump(self):
        reader = self._make_reader([(595, 842)] * 6, cover_single=False)
        self.assertEqual(reader.page_slider.maximum(), 2)
        reader._goto_spread(2)
        self.assertEqual(reader.page_slider.value(), 2)
        self.assertEqual(reader.spin_page.value(), 5)
        # スピンボックスでページ指定（表示は見開きの先頭ページに揃う）
        reader.spin_page.setValue(2)
        reader._on_spin_page_jump()
        self.assertEqual(reader.current_spread_idx, 0)
        self.assertIn(1, reader.spreads[reader.current_spread_idx].page_indices)
        self.assertEqual(reader.spin_page.value(), 1)
        # スライダーで移動
        reader.page_slider.setValue(1)
        reader._jump_to_slider_value()
        self.assertEqual(reader.current_spread_idx, 1)
        self.assertEqual(reader.spin_page.value(), 3)

    def test_fullscreen_toggle_and_esc(self):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent

        reader = self._make_reader([(595, 842)] * 4, cover_single=False)
        self.assertTrue(reader._is_fullscreen)
        self.assertTrue(reader.btn_hud_fs.isChecked())

        def key(k):
            ev = QKeyEvent(
                QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier
            )
            reader.keyPressEvent(ev)

        # 全画面中の Esc は全画面を抜けるだけで、リーダーは閉じない
        key(Qt.Key.Key_Escape)
        self.assertFalse(reader._is_fullscreen)
        self.assertFalse(reader.btn_hud_fs.isChecked())
        self.assertTrue(reader.isVisible())
        # F で全画面に戻る
        key(Qt.Key.Key_F)
        self.assertTrue(reader._is_fullscreen)
        # ボタンでも切り替えられる
        reader.btn_hud_fs.click()
        self.assertFalse(reader._is_fullscreen)


class TestReaderResume(_ReaderTestBase):
    def test_resume_restores_saved_page(self):
        path = "/tmp/kinako-test-book.pdf"
        self.store.set_position(path, 3)
        reader = self._make_reader(
            [(595, 842)] * 6,
            cover_single=False,
            source_path=path,
            resume_page=self.store.get_position(path),
        )
        self.assertEqual(reader._resumed_page, 3)
        self.assertIn(3, reader.spreads[reader.current_spread_idx].page_indices)

    def test_position_recorded_on_navigation(self):
        path = "/tmp/kinako-test-book2.pdf"
        reader = self._make_reader(
            [(595, 842)] * 6, cover_single=False, source_path=path
        )
        reader.next_spread()
        self.assertEqual(self.store.get_position(path), 2)
        history = self.store.get_history()
        self.assertEqual(history[0]["path"], os.path.abspath(path))
        reader.next_spread()
        self.assertEqual(self.store.get_position(path), 4)

    def test_no_source_path_records_nothing(self):
        reader = self._make_reader([(595, 842)] * 4, cover_single=False)
        reader.next_spread()
        reader.close()
        self._app.processEvents()
        self.assertEqual(self.store.get_history(), [])

    def test_history_button_visibility_depends_on_callback(self):
        reader = self._make_reader([(595, 842)] * 4, cover_single=False)
        self.assertTrue(reader.btn_hud_history.isHidden())
        seen = []
        reader2 = self._make_reader(
            [(595, 842)] * 4,
            cover_single=False,
            history_open_callback=seen.append,
        )
        self.assertFalse(reader2.btn_hud_history.isHidden())


class TestReaderPrefetch(_ReaderTestBase):
    def test_prefetch_populates_and_invalidates_on_edit(self):
        reader = self._make_reader([(595, 842)] * 6, cover_single=False)
        reader._prefetch_adjacent_spreads()
        cached_pages = {key[0] for key in reader._prefetch_cache}
        self.assertTrue({2, 3} & cached_pages)
        # 注釈を付けると先読みは破棄される（古い画像を使い回さない）
        reader._record_and_apply_add(
            0, {"type": "highlight", "rect": (10, 10, 50, 30)}
        )
        self.assertEqual(len(reader._prefetch_cache), 0)

    def test_prefetched_entry_used_on_next_spread(self):
        reader = self._make_reader([(595, 842)] * 6, cover_single=False)
        reader._prefetch_adjacent_spreads()
        self.assertTrue(reader._prefetch_cache)
        reader.next_spread()
        self.assertEqual(
            [e["page_idx"] for e in reader._cached_draw_items], [2, 3]
        )


class TestMainWindowReaderHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.main_window import MainWindow

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.window = MainWindow()
        self.window._reader_store = _temp_store()
        self.window.show()
        self._app.processEvents()
        self.addCleanup(self._cleanup_window)

    def _cleanup_window(self):
        win = self.window
        if win is None:
            return
        try:
            win._close_active_reader()
        except Exception:
            pass
        try:
            win.page_manager._thumb_timer.stop()
        except Exception:
            pass
        try:
            win.viewer.current_pdf = None
        except Exception:
            pass
        try:
            win.viewer.history.mark_saved()
        except Exception:
            pass
        try:
            win.close()
        except Exception:
            pass
        self.window = None

    def _write_pdf(self, name, pages=6):
        doc = fitz.open()
        for i in range(pages):
            page = doc.new_page(width=300, height=300)
            page.insert_text((20, 40), f"PAGE {i + 1}", fontsize=12)
        path = Path(self._tmp.name) / name
        try:
            path.write_bytes(doc.tobytes())
        finally:
            doc.close()
        return str(path)

    def test_history_open_resumes_at_saved_page(self):
        path = self._write_pdf("book.pdf")
        self.window._reader_store.set_position(path, 4)
        self.window.open_file_in_reader_from_history(path)
        self._app.processEvents()
        reader = self.window._active_reader
        self.assertIsNotNone(reader)
        self.assertEqual(reader._resumed_page, 4)
        self.assertIn(4, reader.spreads[reader.current_spread_idx].page_indices)
        self.assertTrue(self.window._close_active_reader())

    def test_history_open_missing_file_is_graceful(self):
        missing = str(Path(self._tmp.name) / "gone.pdf")
        self.window._reader_store.set_position(missing, 1)
        with mock.patch("ui.main_window.QMessageBox.information"):
            self.window.open_file_in_reader_from_history(missing)
        self._app.processEvents()
        self.assertIsNone(self.window._active_reader)
        self.assertIsNone(self.window._reader_store.get_position(missing))


if __name__ == "__main__":
    unittest.main()
