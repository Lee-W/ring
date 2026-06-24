"""列出原始碼裡可翻譯、但某語言 .po 還沒收錄的字串（resp. 翻譯漏掉的）。

為什麼需要：``en`` 的 ``.po`` 是「只放已翻譯條目」的手選子集（空 msgstr 會讓
``tests/test_i18n_quality.py`` 失敗），而多行 help / usage 區塊刻意不翻譯、留中文 fallback。
所以「漏翻」不會被測試擋下，只會在 ``--lang en`` 時默默退回中文。這支腳本把差集印出來，
讓你「主動」決定哪些要補、哪些刻意留著——不強制、不失敗，純資訊。

用法：``uv run poe i18n-check``（先 i18n-extract 重生 .pot，再跑這支）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parents[1]
LOCALE = ROOT / "src" / "ring" / "locale"
POT = LOCALE / "ring.pot"


def _ids(path: Path, *, translated_only: bool = False) -> set[str]:
    with path.open("rb") as f:
        catalog = read_po(f)
    out: set[str] = set()
    for m in catalog:
        if not m.id or isinstance(m.id, (list, tuple)):
            continue
        if translated_only and not m.string:
            continue
        out.add(m.id)
    return out


def main() -> int:
    if not POT.exists():
        print("找不到 ring.pot；先跑 `uv run poe i18n-extract`。", file=sys.stderr)
        return 1
    source_ids = _ids(POT)
    missing_any = False
    for po in sorted(LOCALE.glob("*/LC_MESSAGES/ring.po")):
        lang = po.parent.parent.name
        translated = _ids(po, translated_only=True)
        missing = sorted(source_ids - translated)
        if not missing:
            print(f"✅ {lang}: 原始碼字串全部都有翻譯。")
            continue
        missing_any = True
        print(f"⚠️ {lang}: {len(missing)} 個字串沒有翻譯（會 fallback 到原文）：")
        for mid in missing:
            preview = mid.replace("\n", "⏎")[:70]
            print(f"   - {preview}")
    # 純資訊：有漏也回 0，不擋 CI（多行 help 區塊本來就刻意不翻）。
    if not missing_any:
        print("沒有漏翻。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
