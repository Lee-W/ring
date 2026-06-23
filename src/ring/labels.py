"""Session 自訂標籤——本機持久化「這個 session 是什麼」。

存在 ``~/.config/ring/labels.json``：``{session_id: label}``。純 stdlib，所有檔案操作
失敗一律安靜吞掉（標籤是錦上添花，不該打斷 TUI / 快照主流程）。``_LABELS_PATH`` 可在
測試中以 ``path=`` 參數覆寫隔離。
"""

from __future__ import annotations

import json
from pathlib import Path

_LABELS_PATH: Path = Path.home() / ".config" / "ring" / "labels.json"


def load_labels(*, path: Path | None = None) -> dict[str, str]:
    """讀整份標籤表；缺檔 / 壞檔 / 型別不符一律回空 dict。"""
    p = path or _LABELS_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str) and v}


def get_label(session_id: str, *, path: Path | None = None) -> str:
    """取單一 session 的標籤；沒有就回空字串。"""
    return load_labels(path=path).get(session_id, "")


def set_label(session_id: str, label: str, *, path: Path | None = None) -> None:
    """設定 / 更新某 session 的標籤；傳空字串（或全空白）則移除該標籤。"""
    p = path or _LABELS_PATH
    labels = load_labels(path=path)
    label = label.strip()
    if label:
        labels[session_id] = label
    elif session_id not in labels:
        return  # 本來就沒有，且要清空 → 不必寫檔
    else:
        labels.pop(session_id, None)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)  # atomic
    except Exception:
        pass
