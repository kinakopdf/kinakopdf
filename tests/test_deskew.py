# -*- coding: utf-8 -*-
"""多機能版「傾き補正」に対するテスト。

- core: core.deskew の角度検出（テキスト行の方向／画像の投影
  プロファイル）と build_deskewed_pages_doc（補正後の再検出が
  ほぼ 0° になること、文字データとページ寸法が保たれること）
- command: DeskewPagesCommand（差し替え・注釈の破棄/復元・Undo/Redo）
- UI: DeskewDialog のスモーク（検出・手入力・ビルド）と
  PageManager.deskew_selected_pages の配線
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz

PAGE_W, PAGE_H = 595.0, 842.0


def _make_text_doc(pages=1):
    """複数行のテキストを持つ文書を作る（検出には2行以上が必要）。"""
    doc = fitz.open()
    for p in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        for i in range(12):
            y = 90 + i * 55
            page.insert_text(
                (72, y),
                f"Page {p + 1} line number {i} of the scanned document.",
                fontsize=13)
    return doc


def _skew_doc(src, angle):
    """show_pdf_page(rotate=-angle) で、行の角度が +angle の文書を作る。"""
    out = fitz.open()
    for pno in range(len(src)):
        rect = src[pno].rect
        page = out.new_page(width=rect.width, height=rect.height)
        page.show_pdf_page(rect, src, pno, rotate=-angle,
                           keep_proportion=True)
    return out


def _make_image_doc(src, angle, dpi=150):
    """直立ページを angle 度回転して描画し、画像だけの文書にする。

    スキャン／FAX で傾いた画像ページの模擬。ページ寸法は描画結果の
    外接矩形に合わせ、角度が歪まないようにする。
    """
    out = fitz.open()
    for pno in range(len(src)):
        pix = src[pno].get_pixmap(
            matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0).prerotate(angle),
            colorspace=fitz.csRGB, alpha=False)
        w_pt = pix.width * 72.0 / dpi
        h_pt = pix.height * 72.0 / dpi
        page = out.new_page(width=w_pt, height=h_pt)
        page.insert_image(fitz.Rect(0, 0, w_pt, h_pt),
                          stream=pix.tobytes("png"))
    return out


class _DocTestBase(unittest.TestCase):
    def tearDown(self):
        for d in getattr(self, "_docs", []):
            try:
                d.close()
            except Exception:
                pass

    def _track(self, doc):
        self._docs = getattr(self, "_docs", [])
        self._docs.append(doc)
        return doc


class TestDetectSkewFromText(_DocTestBase):
    def test_detect_positive(self):
        from core.deskew import detect_skew_from_text

        sk = self._track(_skew_doc(self._track(_make_text_doc()), 1.5))
        self.assertAlmostEqual(detect_skew_from_text(sk[0]), 1.5, delta=0.3)

    def test_detect_negative(self):
        from core.deskew import detect_skew_from_text

        sk = self._track(_skew_doc(self._track(_make_text_doc()), -2.0))
        self.assertAlmostEqual(detect_skew_from_text(sk[0]), -2.0, delta=0.3)

    def test_straight_page_is_zero(self):
        from core.deskew import detect_skew_from_text

        doc = self._track(_make_text_doc())
        self.assertAlmostEqual(detect_skew_from_text(doc[0]), 0.0, delta=0.1)

    def test_blank_page_returns_none(self):
        from core.deskew import detect_skew_from_text

        doc = fitz.open()
        self.addCleanup(doc.close)
        doc.new_page(width=PAGE_W, height=PAGE_H)
        self.assertIsNone(detect_skew_from_text(doc[0]))


class TestDetectSkewFromImage(_DocTestBase):
    def test_detect_positive(self):
        from core.deskew import detect_skew_from_image

        img = self._track(_make_image_doc(self._track(_make_text_doc()), 1.5))
        self.assertAlmostEqual(detect_skew_from_image(img[0]), 1.5, delta=0.3)

    def test_detect_negative(self):
        from core.deskew import detect_skew_from_image

        img = self._track(_make_image_doc(self._track(_make_text_doc()), -2.0))
        self.assertAlmostEqual(detect_skew_from_image(img[0]), -2.0, delta=0.3)

    def test_blank_page_returns_none(self):
        from core.deskew import detect_skew_from_image

        doc = fitz.open()
        self.addCleanup(doc.close)
        doc.new_page(width=PAGE_W, height=PAGE_H)
        self.assertIsNone(detect_skew_from_image(doc[0]))

    def test_detect_page_skew_prefers_text(self):
        from core.deskew import detect_page_skew

        sk = self._track(_skew_doc(self._track(_make_text_doc()), 1.5))
        angle, method = detect_page_skew(sk[0])
        self.assertEqual(method, "text")
        self.assertAlmostEqual(angle, 1.5, delta=0.3)

    def test_detect_page_skew_falls_back_to_image(self):
        from core.deskew import detect_page_skew

        # 1行だけのページはテキスト検出の対象外 → 画像検出に回る
        one = fitz.open()
        self.addCleanup(one.close)
        page = one.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((72, 200),
                         "Only one single line of text on this page.",
                         fontsize=14)
        img = self._track(_make_image_doc(one, 1.5))
        angle, method = detect_page_skew(img[0])
        self.assertEqual(method, "image")
        self.assertAlmostEqual(angle, 1.5, delta=0.3)


class TestBuildDeskewedPagesDoc(_DocTestBase):
    def test_text_page_straightened(self):
        from core.deskew import build_deskewed_pages_doc, detect_skew_from_text

        sk = self._track(_skew_doc(self._track(_make_text_doc()), 1.5))
        out = self._track(build_deskewed_pages_doc(sk, [0], [1.5]))
        self.assertEqual(len(out), 1)
        page = out[0]
        self.assertAlmostEqual(detect_skew_from_text(page), 0.0, delta=0.2)
        # 文字データとページ寸法が保たれる
        self.assertIn("line number 3", page.get_text())
        self.assertAlmostEqual(page.rect.width, PAGE_W, places=3)
        self.assertAlmostEqual(page.rect.height, PAGE_H, places=3)

    def test_image_page_straightened(self):
        from core.deskew import build_deskewed_pages_doc, detect_skew_from_image

        img = self._track(_make_image_doc(self._track(_make_text_doc()), -2.0))
        w, h = img[0].rect.width, img[0].rect.height
        out = self._track(build_deskewed_pages_doc(img, [0], [-2.0]))
        page = out[0]
        self.assertAlmostEqual(detect_skew_from_image(page), 0.0, delta=0.3)
        self.assertAlmostEqual(page.rect.width, w, places=3)
        self.assertAlmostEqual(page.rect.height, h, places=3)
        self.assertTrue(page.get_images())

    def test_multiple_pages(self):
        from core.deskew import build_deskewed_pages_doc, detect_skew_from_text

        src = self._track(_make_text_doc(pages=2))
        # ページごとに違う角度で傾ける
        skewed = fitz.open()
        self._track(skewed)
        for pno, ang in enumerate((1.5, -2.0)):
            page = skewed.new_page(width=PAGE_W, height=PAGE_H)
            page.show_pdf_page(fitz.Rect(0, 0, PAGE_W, PAGE_H), src, pno,
                               rotate=-ang, keep_proportion=True)
        out = self._track(
            build_deskewed_pages_doc(skewed, [0, 1], [1.5, -2.0]))
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(
            detect_skew_from_text(out[0]), 0.0, delta=0.2)
        self.assertAlmostEqual(
            detect_skew_from_text(out[1]), 0.0, delta=0.2)
        self.assertIn("Page 1", out[0].get_text())
        self.assertIn("Page 2", out[1].get_text())

    def test_zero_angle_copies_page(self):
        from core.deskew import build_deskewed_pages_doc

        doc = self._track(_make_text_doc())
        out = self._track(build_deskewed_pages_doc(doc, [0], [0.0]))
        self.assertIn("line number 0", out[0].get_text())
        self.assertAlmostEqual(out[0].rect.width, PAGE_W, places=3)

    def test_invalid_arguments(self):
        from core.deskew import build_deskewed_pages_doc

        doc = self._track(_make_text_doc())
        with self.assertRaises(ValueError):
            build_deskewed_pages_doc(doc, [5], [1.0])
        with self.assertRaises(ValueError):
            build_deskewed_pages_doc(doc, [0], [1.0, 2.0])


class _WindowTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.main_window import MainWindow

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.window = MainWindow()
        self.window.show()
        self._app.processEvents()
        self.addCleanup(self._cleanup_window)

    def _cleanup_window(self):
        win = self.window
        if win is None:
            return
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

    def _write_skewed_pdf(self, name, angles):
        """ページごとに angles[i] 度傾いたテキストPDFを書き出す。"""
        src = _make_text_doc(pages=len(angles))
        out = fitz.open()
        for pno, ang in enumerate(angles):
            page = out.new_page(width=PAGE_W, height=PAGE_H)
            page.show_pdf_page(
                fitz.Rect(0, 0, PAGE_W, PAGE_H), src, pno,
                rotate=-ang, keep_proportion=True)
        path = Path(self._tmp.name) / name
        try:
            path.write_bytes(out.tobytes())
        finally:
            out.close()
            src.close()
        return str(path)

    def _open(self, path):
        self.window.open_file_from_path(path)
        self._app.processEvents()


class TestDeskewCommandViaWindow(_WindowTestBase):
    def _run_deskew(self, page_indices):
        from core.commands import DeskewPagesCommand
        from core.deskew import build_deskewed_pages_doc, detect_page_skew

        viewer = self.window.viewer
        doc = viewer.current_pdf
        angles = [detect_page_skew(doc[i])[0] for i in page_indices]
        deskewed = build_deskewed_pages_doc(doc, page_indices, angles)
        cmd = DeskewPagesCommand(
            doc, page_indices, deskewed,
            viewer.page_annotations, viewer.current_page_idx,
            take_ownership=True)
        cmd.redo(viewer)
        viewer.history.push(cmd)
        self._app.processEvents()
        return cmd

    def test_deskew_and_undo_redo(self):
        from core.deskew import detect_skew_from_text

        src = self._write_skewed_pdf("src.pdf", [0.0, 1.5, -2.0])
        self._open(src)
        viewer = self.window.viewer
        doc = viewer.current_pdf
        self.assertEqual(len(doc), 3)
        self.assertAlmostEqual(
            detect_skew_from_text(doc[1]), 1.5, delta=0.3)

        self._run_deskew([1, 2])
        doc = viewer.current_pdf
        self.assertEqual(len(doc), 3)
        self.assertAlmostEqual(
            detect_skew_from_text(doc[1]), 0.0, delta=0.2)
        self.assertAlmostEqual(
            detect_skew_from_text(doc[2]), 0.0, delta=0.2)
        self.assertIn("Page 1", doc[0].get_text())
        self.assertIn("Page 2", doc[1].get_text())

        viewer.history.undo(viewer)
        self._app.processEvents()
        doc = viewer.current_pdf
        self.assertAlmostEqual(
            detect_skew_from_text(doc[1]), 1.5, delta=0.3)
        self.assertAlmostEqual(
            detect_skew_from_text(doc[2]), -2.0, delta=0.3)

        viewer.history.redo(viewer)
        self._app.processEvents()
        self.assertAlmostEqual(
            detect_skew_from_text(viewer.current_pdf[1]), 0.0, delta=0.2)

    def test_annotations_discarded_and_restored(self):
        src = self._write_skewed_pdf("src.pdf", [0.0, 1.5, 0.0])
        self._open(src)
        viewer = self.window.viewer
        viewer.page_annotations[1] = [
            {"type": "highlight", "rect": (10.0, 10.0, 50.0, 30.0)}]
        viewer.page_annotations[2] = [{
            "type": "text", "text": "LATER_NOTE",
            "pos": (20.0, 100.0), "fontsize": 14.0,
        }]

        self._run_deskew([1])
        # 補正したページの注釈は焼き込み規約で破棄、他ページは不変
        self.assertFalse(viewer.page_annotations.get(1))
        self.assertEqual(viewer.page_annotations[2][0]["text"], "LATER_NOTE")

        viewer.history.undo(viewer)
        self._app.processEvents()
        self.assertEqual(len(viewer.page_annotations[1]), 1)
        self.assertEqual(
            viewer.page_annotations[1][0]["type"], "highlight")
        self.assertEqual(viewer.page_annotations[2][0]["text"], "LATER_NOTE")


class TestDeskewDialogSmoke(_WindowTestBase):
    def test_dialog_detects_and_builds(self):
        from core.deskew import detect_skew_from_text
        from ui.deskew_dialog import DeskewDialog

        src = self._write_skewed_pdf("src.pdf", [1.5, 0.0])
        self._open(src)
        viewer = self.window.viewer

        dialog = DeskewDialog(
            self.window, viewer.current_pdf, [0, 1],
            viewer.page_annotations)
        try:
            dialog.show()
            self._app.processEvents()
            detected = dialog.detected_angles
            self.assertAlmostEqual(detected[0], 1.5, delta=0.3)
            self.assertAlmostEqual(detected[1], 0.0, delta=0.3)
            # 0° のページは補正対象から外れる
            items = dialog.correction_items()
            self.assertEqual([i for i, _ in items], [0])

            indices, deskewed = dialog.build_deskewed_doc()
            try:
                self.assertEqual(indices, [0])
                self.assertEqual(len(deskewed), 1)
                self.assertAlmostEqual(
                    detect_skew_from_text(deskewed[0]), 0.0, delta=0.2)
            finally:
                deskewed.close()
        finally:
            dialog.close()
            dialog.dispose()

    def test_dialog_manual_angle_for_undetected_page(self):
        from ui.deskew_dialog import DeskewDialog

        # 空白ページは検出できないが、手入力で補正対象にできる
        doc = fitz.open()
        path = Path(self._tmp.name) / "blank.pdf"
        try:
            doc.new_page(width=PAGE_W, height=PAGE_H)
            path.write_bytes(doc.tobytes())
        finally:
            doc.close()
        self._open(str(path))
        viewer = self.window.viewer

        dialog = DeskewDialog(
            self.window, viewer.current_pdf, [0], viewer.page_annotations)
        try:
            dialog.show()
            self._app.processEvents()
            self.assertIsNone(dialog.detected_angles[0])
            self.assertEqual(dialog.correction_items(), [])

            dialog.set_angle(0, 1.2)
            self.assertEqual(dialog.correction_items(), [(0, 1.2)])
            indices, deskewed = dialog.build_deskewed_doc()
            try:
                self.assertEqual(indices, [0])
                self.assertEqual(len(deskewed), 1)
            finally:
                deskewed.close()
        finally:
            dialog.close()
            dialog.dispose()


class TestPageManagerDeskewWiring(_WindowTestBase):
    def test_deskew_selected_pages_via_page_manager(self):
        from PyQt6.QtWidgets import QDialog

        from core.deskew import detect_skew_from_text

        src = self._write_skewed_pdf("src.pdf", [0.0, 1.5, 0.0])
        self._open(src)
        pm = self.window.page_manager
        viewer = self.window.viewer

        class _FakeDialog:
            def __init__(self, parent, doc, page_indices, annots=None):
                self._doc = doc
                self._indices = list(page_indices)

            def exec(self):
                return QDialog.DialogCode.Accepted

            def correction_items(self):
                from core.deskew import detect_page_skew

                return [(i, detect_page_skew(self._doc[i])[0])
                        for i in self._indices]

            def build_deskewed_doc(self):
                from core.deskew import build_deskewed_pages_doc

                items = self.correction_items()
                indices = [i for i, _ in items]
                angles = [a for _, a in items]
                return indices, build_deskewed_pages_doc(
                    self._doc, indices, angles)

            def dispose(self):
                pass

        pm.list_widget.clearSelection()
        pm.list_widget.item(1).setSelected(True)

        with mock.patch("ui.deskew_dialog.DeskewDialog", _FakeDialog):
            pm.deskew_selected_pages()
        self._app.processEvents()

        doc = viewer.current_pdf
        self.assertEqual(len(doc), 3)
        self.assertAlmostEqual(
            detect_skew_from_text(doc[1]), 0.0, delta=0.2)
        self.assertIn("Page 2", doc[1].get_text())

        # ページ管理からの実行も Undo できる
        viewer.history.undo(viewer)
        self._app.processEvents()
        self.assertAlmostEqual(
            detect_skew_from_text(viewer.current_pdf[1]), 1.5, delta=0.3)


class TestDeskewKeepsEdges(unittest.TestCase):
    """端まで描いた原稿を補正しても、端の内容が欠けないこと。

    2026-11-24 MIWA 実機報告（小さい伝票で端が消える）の回帰テスト。
    以前の拡大（カバー）方式では 3° で上下の枠がほぼ消えた。
    """

    def _framed_doc(self):
        doc = fitz.open()
        page = doc.new_page(width=227.0, height=369.0)  # 伝票サイズ相当
        m = 6.0
        page.draw_rect(
            fitz.Rect(m, m, 227.0 - m, 369.0 - m),
            color=(0, 0, 0), width=2.0)
        return doc

    def _ink(self, page):
        pix = page.get_pixmap(
            matrix=fitz.Matrix(2, 2), colorspace=fitz.csGRAY)
        return pix, sum(1 for b in pix.samples if b < 100)

    def test_frame_survives_at_5_degrees(self):
        from core.deskew import build_deskewed_pages_doc

        doc = self._framed_doc()
        try:
            _, base = self._ink(doc[0])
            out = build_deskewed_pages_doc(doc, [0], [5.0])
            try:
                pix, ink = self._ink(out[0])
                # 縮小分を差し引いても枠の大部分が残る
                self.assertGreater(ink, base * 0.6)
                # 上辺・下辺が内寄りの位置に実在する（消えていない）
                w, h, s = pix.width, pix.height, pix.samples

                def band(y0, y1):
                    c = 0
                    for y in range(int(h * y0), int(h * y1)):
                        row = s[y * w:(y + 1) * w]
                        c += sum(
                            1 for x in range(int(w * 0.3), int(w * 0.7))
                            if row[x] < 100)
                    return c

                self.assertGreater(band(0.03, 0.12), 100)
                self.assertGreater(band(0.88, 0.97), 100)
            finally:
                out.close()
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main()


class TestRotatedCropBoxPage(unittest.TestCase):
    """回転＋CropBox窓のあるスキャンPDFでも見える範囲がずれないこと。

    2026-11-24 MIWA 実機報告の回帰テスト。スキャンアプリのPDFは
    A4全体の画像に、伝票だけの切り抜き窓（CropBox）と回転を
    組み合わせていることがある。remove_rotation() をそのまま使うと
    窓の位置がずれて内容が欠けるため、normalize_page_rotation で
    先に MediaBox を CropBox に合わせてから正規化する。
    """

    def _make_rotated_crop_doc(self):
        # A4縦のページに、部分領域だけを窓（CropBox）として見せる。
        # 窓の四隅ギリギリに枠を描く。CropBox と Rotate は実ファイル
        # （スキャナ製PDF）と同じく生のキーで設定し、一度保存して
        # 読み直したものを返す。
        import tempfile

        doc = fitz.open()
        page = doc.new_page(width=595.68, height=842.4)
        cb = fitz.Rect(100.0, 200.0, 400.0, 500.0)  # 300x300 の窓
        page.draw_rect(cb, color=(0, 0, 0), width=3.0)
        page.draw_rect(
            fitz.Rect(cb.x0 + 10, cb.y0 + 10, cb.x1 - 10, cb.y1 - 10),
            color=(0, 0, 0), width=1.5)
        pdf_cb = f"[{cb.x0} {842.4 - cb.y1} {cb.x1} {842.4 - cb.y0}]"
        doc.xref_set_key(page.xref, "CropBox", pdf_cb)
        doc.xref_set_key(page.xref, "Rotate", "270")
        tmp = tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False)
        tmp.close()
        doc.save(tmp.name)
        doc.close()
        return fitz.open(tmp.name)

    def test_normalize_preserves_visible_view(self):
        from core.geometry import normalize_page_rotation

        doc = self._make_rotated_crop_doc()
        try:
            before = doc[0].get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
            work = fitz.open()
            try:
                work.insert_pdf(doc, from_page=0, to_page=0)
                page = work[0]
                normalize_page_rotation(page)
                self.assertEqual(page.rotation, 0)
                after = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
                self.assertEqual(
                    (after.width, after.height),
                    (before.width, before.height))
                # 見た目がほぼ一致する（再配置による微差のみ）
                diff = sum(
                    1 for a, b in zip(after.samples, before.samples)
                    if abs(a - b) > 24)
                self.assertLess(diff, len(before.samples) * 0.02)
                # 枠のインク量もほぼ保たれる（端が消えていない）
                ink_before = sum(
                    1 for b in before.samples if b < 100)
                ink_after = sum(
                    1 for b in after.samples if b < 100)
                self.assertGreater(ink_after, ink_before * 0.9)
            finally:
                work.close()
        finally:
            doc.close()

    def test_deskew_on_rotated_crop_page_keeps_frame(self):
        from core.deskew import build_deskewed_pages_doc

        doc = self._make_rotated_crop_doc()
        try:
            out = build_deskewed_pages_doc(doc, [0], [3.0])
            try:
                page = out[0]
                # 結果ページは「見えていた窓」と同じ大きさ
                self.assertAlmostEqual(page.rect.width, 300.0, delta=1.0)
                self.assertAlmostEqual(page.rect.height, 300.0, delta=1.0)
                pix = page.get_pixmap(
                    matrix=fitz.Matrix(2, 2), colorspace=fitz.csGRAY)
                ink = sum(1 for b in pix.samples if b < 100)
                # 枠が残っている（消えていない）こと
                self.assertGreater(ink, 3000)
            finally:
                out.close()
        finally:
            doc.close()
