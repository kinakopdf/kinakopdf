# -*- coding: utf-8 -*-
"""ツールバーのボタン配置をカスタマイズするダイアログ。

- メニューボタン（その他／消去／保存）の題名を自由に変更できる。
- 項目ごとに置き場所（バーに表示／その他メニュー／保存メニュー／
  消去メニュー／隠す）を選べる。
- 「初期状態に戻す」で全てのコントロールを既定配置に戻せる。

このダイアログはデータの受け渡しだけを行い、実際の設定保存と
再配置は MainWindow 側（_save_toolbar_layout / _apply_toolbar_layout）
が担当する。
"""
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

# 配置先の選択肢: (内部キー, 表示名) の順序はコンボボックスの表示順
LOCATION_CHOICES = [
    ("bar", "バーに表示"),
    ("more", "その他メニュー"),
    ("save", "保存メニュー"),
    ("erase", "消去メニュー"),
    ("hidden", "隠す"),
]
LOCATION_KEYS = [key for key, _label in LOCATION_CHOICES]

# 題名を変更できるボタン: (内部キー, 表示ラベル, 既定の題名)
TITLE_FIELDS = [
    ("more", "「その他」ボタンの題名", "🧰 その他"),
    ("erase", "「消去」ボタンの題名", "🧽 消去"),
    ("save", "「保存」ボタンの題名", "💾 保存"),
]
DEFAULT_TITLES = {key: default for key, _label, default in TITLE_FIELDS}


class ToolbarLayoutDialog(QDialog):
    """バー編集ダイアログ。

    items: [(key, label, default_location), ...] 表示順そのまま
    titles: {button_key: 現在の題名}（"" は既定名を使っている意味）
    places: {item_key: 現在の配置キー}
    """

    def __init__(self, parent, items, titles, places):
        super().__init__(parent)
        self.setWindowTitle("🛠 バーを編集")
        self.setMinimumWidth(460)
        self._items = list(items)
        self._defaults = {key: default for key, _label, default in self._items}

        layout = QVBoxLayout(self)

        intro = QLabel(
            "ボタンの題名と置き場所を自由に変えられます。\n"
            "「OK」を押すと、その場でツールバーに反映されます。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- 題名の変更 ---
        title_box = QGroupBox("ボタンの題名")
        title_form = QFormLayout(title_box)
        self._title_edits = {}
        for key, label, default in TITLE_FIELDS:
            edit = QLineEdit(self)
            edit.setText(titles.get(key, "") or "")
            edit.setPlaceholderText(f"空欄なら既定の「{default}」")
            edit.setClearButtonEnabled(True)
            title_form.addRow(label, edit)
            self._title_edits[key] = edit
        layout.addWidget(title_box)

        # --- 置き場所の変更 ---
        place_box = QGroupBox("項目の置き場所")
        place_outer = QVBoxLayout(place_box)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(260)
        inner = QWidget(self)
        place_form = QFormLayout(inner)
        self._place_combos = {}
        for key, label, _default in self._items:
            combo = QComboBox(self)
            for loc_key, loc_label in LOCATION_CHOICES:
                combo.addItem(loc_label, loc_key)
            current = places.get(key, self._defaults[key])
            idx = combo.findData(current)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            place_form.addRow(label, combo)
            self._place_combos[key] = combo
        scroll.setWidget(inner)
        place_outer.addWidget(scroll)
        layout.addWidget(place_box)

        # --- 下部ボタン ---
        bottom = QHBoxLayout()
        self.btn_reset = QPushButton("初期状態に戻す")
        self.btn_reset.setToolTip("題名と置き場所をすべて既定に戻します（OKを押すと反映されます）")
        self.btn_reset.clicked.connect(self._reset_to_defaults)
        bottom.addWidget(self.btn_reset)
        bottom.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)

    def _reset_to_defaults(self):
        for edit in self._title_edits.values():
            edit.clear()
        for key, combo in self._place_combos.items():
            idx = combo.findData(self._defaults[key])
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def result_titles(self):
        """{button_key: 題名}。空文字は「既定名に戻す」の意味。"""
        return {key: edit.text().strip() for key, edit in self._title_edits.items()}

    def result_places(self):
        """{item_key: 配置キー}"""
        return {key: combo.currentData() for key, combo in self._place_combos.items()}
