"""i18n 衛生守衛：

1. committed 的 .mo 必須跟 .po 同步（防「改了 .po 忘了 poe i18n-compile」）。
2. 源碼字串必須是台灣漢語（擋常見中國用語）。
"""

from pathlib import Path

from babel.messages.pofile import read_po

import ring.i18n as i18n
from ring.i18n import gettext as _
from ring.i18n import ngettext, set_lang

# 常見中國用語 → 台灣漢語。只放「不會誤判」的詞
# （刻意避開 文件/程序/質量/用戶/設置/刷新/創建 這類兩岸都通用、會誤判的）。
_CHINA_ISMS = {
    "優化": "最佳化",
    "視頻": "影片",
    "音頻": "音訊",
    "屏幕": "螢幕",
    "軟件": "軟體",
    "硬件": "硬體",
    "網絡": "網路",
    "默認": "預設",
    "缺省": "預設",
    "內存": "記憶體",
    "數據庫": "資料庫",
    "服務器": "伺服器",
    "端口": "連接埠",
    "進程": "行程",
    "線程": "執行緒",
    "對象": "物件",
    "字符": "字元",
    "代碼": "程式碼",
    "接口": "介面",
    "兼容": "相容",
    "緩存": "快取",
    "信息": "資訊",
    "命令行": "命令列",
    "源碼": "原始碼",
    "清屏": "清除畫面",
    "中國大陸": "中國",
}


def test_en_mo_is_in_sync_with_po() -> None:
    """committed 的 en .mo 必須反映 en .po 的每一條翻譯，否則就是忘了重編。"""
    po_path = i18n._LOCALE_DIR / "en" / "LC_MESSAGES" / "ring.po"
    with po_path.open("rb") as f:
        catalog = read_po(f)
    set_lang("en")
    try:
        for message in catalog:
            if not message.id:  # 跳過 metadata header
                continue
            if isinstance(message.id, (list, tuple)):  # 複數
                singular, plural = message.id
                forms = message.string
                assert isinstance(forms, (list, tuple))
                assert ngettext(singular, plural, 1) == forms[0]
                assert ngettext(singular, plural, 2) == forms[1]
            else:
                assert isinstance(message.string, str)
                assert _(message.id) == message.string, f"stale .mo：{message.id!r} 沒同步，請跑 poe i18n-compile"
    finally:
        set_lang(None)


def test_source_uses_taiwanese_mandarin() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "ring"
    offenders: list[str] = []
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for bad, good in _CHINA_ISMS.items():
            if bad in text:
                offenders.append(f"{py.name}：「{bad}」應為「{good}」")
    assert not offenders, "源碼出現中國用語：\n" + "\n".join(offenders)
