# きなこPDF — サードパーティのライセンス表示（下書き 2026-11-15）

きなこPDF本体は AGPL-3.0 で公開する前提の下書きです。
これは法律相談ではありません。商用利用や有料化を考える場合は、別途専門家に確認してください。

## きなこPDF本体
- ライセンス：GNU Affero General Public License v3.0 (AGPL-3.0)
- 理由：使っている PyQt6 が GPLv3、PyMuPDF が AGPLv3 のため、無償で配布するには
  アプリ全体をコピーレフトのライセンスで公開し、ソースコードを受け取った人に
  渡せる形にする必要があります。AGPL-3.0 は両方の条件を満たす側の選択です。
- 公開方法：GitHub の公開リポジトリにソースコード一式を置き、exe と同じ
  Release にソースコード版 zip も添付します。`LICENSE` には AGPL-3.0 の全文を入れます。

## PyQt6 — GPLv3
- 著作権：Riverbank Computing Limited
- ライセンス：GNU General Public License v3.0、または商用ライセンス（有料）
- 今回の選択：GPLv3 側で使います。だから本体も GPLv3 と互換性のあるライセンス
  （ここでは AGPL-3.0）で公開します。ソースコード非公開で配布するには
  Riverbank の商用ライセンスが必要です（今回は買いません）。
- 入手先：https://www.riverbankcomputing.com/software/pyqt/

## Qt 6 — GPLv3 / LGPLv3 / 商用
- PyQt6 経由で使う Qt 本体です。PyQt6 が GPLv3 のため、今回は GPLv3 側の扱いです。
- 別途、商用ライセンスを買う選択肢もあります（今回は買いません）。

## PyMuPDF / MuPDF — AGPLv3
- 著作権：Artifex Software, Inc.
- ライセンス：GNU Affero General Public License v3.0、または商用ライセンス（有料・見積制）
- 今回の選択：AGPLv3 側で使います。生成した PDF の producer 表記は消さずに残します。
- 入手先：https://pymupdf.io/licensing

## Python — PSF License
- Python 本体は PSF License（BSD 系の寛容なライセンス）です。
- exe に同梱する場合は、Python の著作権表示とライセンス文を同梱します。

## PyInstaller — ビルド道具のみ（アプリの依存ではない）
- ライセンス：GNU General Public License v2（ブートローダー例外つき）
- ブートローダー例外により、作った exe 自体を別ライセンスで配布できます。
- PyInstaller 自体は exe の中身の依存ではなく、GitHub Actions の中でだけ使う
  ビルド道具です。exe に入るのはアプリ＋ PyQt6 / PyMuPDF です。

## Windows OCR
- Windows 標準の OCR を使います。Microsoft の利用条件が別途適用されます。
- OCR は Windows 専用です。

## NDLOCR-Lite（OCR強化版のみ）
- 国立国会図書館が開発・公開する日本語 OCR「NDLOCR-Lite」を使います。
  出典：国立国会図書館 NDLOCR-Lite（https://github.com/ndl-lab/ndlocr-lite）
- プログラムのライセンスは CC BY 4.0 です。
  （https://creativecommons.org/licenses/by/4.0/deed.ja）
- NDLOCR-Lite 本体と読み取り用モデル（ONNX）は、国立国会図書館の配布物を
  改変せずにそのまま使っています。
- レイアウト認識モデルには DINOv3 を蒸留したモデルが含まれており、
  Meta の DINOv3 ライセンスも適用されます。本文は同梱の
  `licenses/DINOv3-LICENSE.md` をご覧ください。軍事・核・諜報などの
  用途は DINOv3 ライセンスで禁止されています。
- あわせて使う部品：ONNX Runtime（MIT License、Microsoft）、
  OpenCV（Apache License 2.0）、NumPy（BSD License）、
  Pillow（MIT-CMU License）、lxml（BSD License）、
  PyYAML（MIT License）、tqdm（MIT License / MPL 2.0）。

## 配布時に入れるもの（チェックリスト）
- [ ] `KinakoPDF.exe`（PyInstaller で作成）
- [ ] ソースコード版 zip（同じ Release に添付）
- [x] `LICENSE`（AGPL-3.0 全文）— 2026-11-15に下書きへ追加済み
- [ ] この `THIRD_PARTY_NOTICES.md`（exe と同じフォルダ／Release に添付）
- [ ] 各ライブラリのライセンス全文（PyQt6 / Qt / PyMuPDF / Python）
  ※ 全文は Release 作成時に、各公式配布物からコピーして入れること。
  ここは一覧と方針の下書きで、全文そのものではありません。
- [ ] ホームページに「ソースコード公開」「ライセンス」のリンクを置くこと
