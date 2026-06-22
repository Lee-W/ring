"""RiNG 設定檔：``~/.config/ring/config.toml``。

缺檔、缺鍵、型別錯——全部安靜退回預設，所以零設定也能跑。範例：

    lang = "en"
    interval = 1.5
    show_all = false
    legend = true
    active_window_seconds = 21600     # 只看最近這段時間動過的 session（預設 6h）
    working_threshold_seconds = 90    # 多久沒動就從 🟢 工作中 變 🟡 閒置
    waiting_window_seconds = 1800     # IDLE 升 WAITING 的時間窗上限（預設 30 分）
    notify_sound = true               # 系統通知帶聲音
    notify_sound_name = "Glass"       # macOS / terminal-notifier sound name
    notify_repeat_seconds = [30, 120, 300]  # waiting 未解除時，多久後重複提醒
    notify_repeat_max = 3             # 重複提醒上限；0 = 不限
    focusers = ["tmux", "iTerm2", "Terminal"]   # 跳轉嘗試順序；省略＝內建預設
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "ring" / "config.toml"

# Rich 樣式字串。深淺底都看得到（避開 dim / ANSI blue）。config 的 [colors] 可逐項覆寫。
_DEFAULT_COLORS = {
    "waiting": "bold red",
    "working": "green",
    "idle": "yellow",
    "ended": "grey50",
    "project": "cyan",
    "location": "bright_blue",
    "muted": "grey50",
}


@dataclass(frozen=True)
class Config:
    lang: str | None = None
    interval: float = 2.0
    show_all: bool = False
    legend: bool = True
    active_window_seconds: int = 6 * 60 * 60
    working_threshold_seconds: int = 90
    waiting_window_seconds: int = 1800  # IDLE 升 WAITING 的時間窗上限（預設 30 分）
    notify_sound: bool = True
    notify_sound_name: str = "Glass"
    notify_repeat_seconds: tuple[int, ...] = (30, 120, 300)
    notify_repeat_max: int = 3  # 0 = 不限
    focusers: tuple[str, ...] = ()  # 空＝用內建預設順序
    colors: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_COLORS))


def _as_int(v: object, default: int) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else default


def _as_float(v: object, default: float) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return default


def _as_bool(v: object, default: bool) -> bool:
    return v if isinstance(v, bool) else default


def _as_str_tuple(v: object) -> tuple[str, ...]:
    if isinstance(v, list):
        return tuple(x for x in v if isinstance(x, str))
    return ()


def _as_positive_int_tuple(v: object, default: tuple[int, ...]) -> tuple[int, ...]:
    if isinstance(v, list):
        parsed = tuple(x for x in v if isinstance(x, int) and not isinstance(x, bool) and x > 0)
        return parsed or default
    return default


def _parse_colors(v: object) -> dict[str, str]:
    colors = dict(_DEFAULT_COLORS)
    if isinstance(v, dict):
        colors.update({k: val for k, val in v.items() if isinstance(k, str) and isinstance(val, str)})
    return colors


def load(path: Path | None = None) -> Config:
    p = path or CONFIG_PATH
    try:
        raw = tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return Config()
    d = Config()
    lang = raw.get("lang")
    return Config(
        lang=lang if isinstance(lang, str) else None,
        interval=_as_float(raw.get("interval"), d.interval),
        show_all=_as_bool(raw.get("show_all"), d.show_all),
        legend=_as_bool(raw.get("legend"), d.legend),
        active_window_seconds=_as_int(raw.get("active_window_seconds"), d.active_window_seconds),
        working_threshold_seconds=_as_int(raw.get("working_threshold_seconds"), d.working_threshold_seconds),
        waiting_window_seconds=_as_int(raw.get("waiting_window_seconds"), d.waiting_window_seconds),
        notify_sound=_as_bool(raw.get("notify_sound"), d.notify_sound),
        notify_sound_name=(
            raw["notify_sound_name"] if isinstance(raw.get("notify_sound_name"), str) else d.notify_sound_name
        ),
        notify_repeat_seconds=_as_positive_int_tuple(raw.get("notify_repeat_seconds"), d.notify_repeat_seconds),
        notify_repeat_max=max(0, _as_int(raw.get("notify_repeat_max"), d.notify_repeat_max)),
        focusers=_as_str_tuple(raw.get("focusers")),
        colors=_parse_colors(raw.get("colors")),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """整個 process 共用的設定（讀一次）。要繞過快取自訂就直接呼叫 load(path)。"""
    return load()
