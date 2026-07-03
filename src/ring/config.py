"""RiNG 設定檔：``~/.config/ring/config.toml``。

缺檔、缺鍵、型別錯——全部安靜退回預設，所以零設定也能跑。範例：

    lang = "en"
    interval = 1.5
    show_all = false
    legend = true
    active_window_seconds = 21600     # 只看最近這段時間動過的 session（預設 6h）
    working_threshold_seconds = 90    # 多久沒動就從 🟢 工作中 變 🟡 閒置
    waiting_window_seconds = 1800     # 跑完停著升等你的時間窗上限（預設 30 分）
    notify_sound = true               # 系統通知帶聲音
    notify_sound_name = "Glass"       # macOS / terminal-notifier sound name
    notify_ignore_dnd = false         # terminal-notifier 是否加 -ignoreDnD（穿透勿擾 / Focus）
    notify_backend = "auto"           # auto / terminal-notifier / osascript / notify-send / agent-hooks / none
                                      #   terminal-notifier 被 macOS 擋掉時設 "osascript"
                                      #   （看得到通知，但點擊不跳轉）；
                                      #   "agent-hooks" = 決策+提醒交給 agent-hooks（ring hook 同步出 modal，
                                      #     沒裝時自動退回 auto 通知）；
                                      #   "none" = 完全不發通知（RiNG 當純看板）
    notify_repeat_seconds = [30, 120, 300]  # 持續等你時，多久後重複提醒
    notify_repeat_max = 3             # 重複提醒上限；0 = 不限
    notify_ntfy_url = "https://ntfy.sh/my-topic"   # 設了才啟用 ntfy 後端（推到手機）
    notify_webhook_url = "https://example.com/hook"  # 設了才啟用 webhook 後端（JSON POST）
    notify_also = ["ntfy"]            # 主後端之外「加發」的後端（例如桌面通知＋手機各一份）
    focusers = ["Neovim", "tmux", "iTerm2", "Terminal", "linux-wm"]  # 跳轉嘗試順序；省略＝內建預設
    plugins = ["my_ring_plugin"]      # 啟動時 import 的外部 plugin 模組（自行 register_*）
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
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
    waiting_window_seconds: int = 1800  # 跑完停著升等你的時間窗上限（預設 30 分）
    notify_sound: bool = True
    notify_sound_name: str = "Glass"
    notify_ignore_dnd: bool = False
    # 通知後端（完整說明見模組 docstring）：
    #   auto = 第一個可用後端（優先支援點擊跳轉的 terminal-notifier）；
    #   terminal-notifier / osascript / notify-send = 強制指定該後端；
    #   agent-hooks = 決策+提醒交給 agent-hooks（沒裝時自動退回 auto）；
    #   none = 完全不發通知（RiNG 當純看板）。
    notify_backend: str = "auto"
    notify_repeat_seconds: tuple[int, ...] = (30, 120, 300)
    notify_repeat_max: int = 3  # 0 = 不限
    notify_ntfy_url: str = ""  # 完整 ntfy topic URL；空＝ntfy 後端不可用
    notify_webhook_url: str = ""  # webhook URL；空＝webhook 後端不可用
    notify_also: tuple[str, ...] = ()  # 主後端之外加發的後端名（如 ["ntfy"]）
    focusers: tuple[str, ...] = ()  # 空＝用內建預設順序
    plugins: tuple[str, ...] = ()  # 啟動時 import 的外部 plugin 模組（entry point 之外的本機路）
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
    # 任意非空字串都接受（後端名稱由 notify 層的可插拔 registry 決定）；認不得的名稱
    # 會在送通知時退回 auto 選法。非字串 / 空字串 → 預設 "auto"。
    nb = raw.get("notify_backend")
    notify_backend = nb if isinstance(nb, str) and nb else d.notify_backend
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
        notify_ignore_dnd=_as_bool(raw.get("notify_ignore_dnd"), d.notify_ignore_dnd),
        notify_repeat_seconds=_as_positive_int_tuple(raw.get("notify_repeat_seconds"), d.notify_repeat_seconds),
        notify_repeat_max=max(0, _as_int(raw.get("notify_repeat_max"), d.notify_repeat_max)),
        notify_backend=notify_backend,
        notify_ntfy_url=(raw["notify_ntfy_url"] if isinstance(raw.get("notify_ntfy_url"), str) else ""),
        notify_webhook_url=(raw["notify_webhook_url"] if isinstance(raw.get("notify_webhook_url"), str) else ""),
        notify_also=_as_str_tuple(raw.get("notify_also")),
        focusers=_as_str_tuple(raw.get("focusers")),
        plugins=_as_str_tuple(raw.get("plugins")),
        colors=_parse_colors(raw.get("colors")),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """整個 process 共用的設定（讀一次）。要繞過快取自訂就直接呼叫 load(path)。"""
    return load()


# --------------------------------------------------------------------------- 寫入（ring config set）


class ConfigError(ValueError):
    """``ring config set/get`` 的使用者層級錯誤（未知鍵 / 值轉型失敗）。訊息直接給使用者看。"""


def _coerce_bool(s: str) -> bool:
    low = s.strip().lower()
    if low in {"true", "1", "yes", "on"}:
        return True
    if low in {"false", "0", "no", "off"}:
        return False
    raise ConfigError(f"'{s}' 不是布林值（用 true / false）")


def _coerce_int(s: str) -> int:
    try:
        return int(s.strip())
    except ValueError:
        raise ConfigError(f"'{s}' 不是整數") from None


def _coerce_float(s: str) -> float:
    try:
        return float(s.strip())
    except ValueError:
        raise ConfigError(f"'{s}' 不是數字") from None


def _coerce_int_list(s: str) -> list[int]:
    return [_coerce_int(part) for part in s.split(",") if part.strip()]


def _coerce_str_list(s: str) -> list[str]:
    return [part.strip() for part in s.split(",") if part.strip()]


# 可由 ``ring config set`` 寫入的純量 / 清單鍵 → 把 CLI 傳來的字串轉成對應型別。
# colors 是巢狀 table，用 ``colors.<name>`` 點記法另外處理（見 set_value）。
_SETTERS: dict[str, Callable[[str], object]] = {
    "lang": str,
    "interval": _coerce_float,
    "show_all": _coerce_bool,
    "legend": _coerce_bool,
    "active_window_seconds": _coerce_int,
    "working_threshold_seconds": _coerce_int,
    "waiting_window_seconds": _coerce_int,
    "notify_sound": _coerce_bool,
    "notify_sound_name": str,
    "notify_ignore_dnd": _coerce_bool,
    "notify_backend": str,
    "notify_repeat_seconds": _coerce_int_list,
    "notify_repeat_max": _coerce_int,
    "notify_ntfy_url": str,
    "notify_webhook_url": str,
    "notify_also": _coerce_str_list,
    "focusers": _coerce_str_list,
    "plugins": _coerce_str_list,
}


def settable_keys() -> list[str]:
    """``ring config set`` 接受的鍵（colors 子鍵用 colors.<name>，不在此列）。"""
    return list(_SETTERS)


def _toml_repr(value: object) -> str:
    """把一個 Python 值序列化成 TOML 字面值（只涵蓋本設定會用到的型別）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_repr(v) for v in value) + "]"
    raise ConfigError(f"無法序列化 {type(value).__name__} 值")


def dump_toml(data: dict[str, object]) -> str:
    """把設定 dict 寫成 TOML 文字。純量 / 清單在前，巢狀 table（如 [colors]）殿後。

    刻意樸素：不保留註解（``ring config set`` 重寫整檔），但所有鍵值都會保留。
    """
    lines = [f"{k} = {_toml_repr(v)}" for k, v in data.items() if not isinstance(v, dict)]
    for k, v in data.items():
        if isinstance(v, dict):
            lines.append(f"\n[{k}]")
            lines += [f"{sub} = {_toml_repr(val)}" for sub, val in v.items()]
    return "\n".join(lines) + "\n"


def _read_raw(path: Path) -> dict[str, object]:
    try:
        return dict(tomllib.loads(path.read_text()))
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"讀不到 {path}：{e}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} 不是合法 TOML：{e}") from None


def set_value(key: str, raw_value: str, path: Path | None = None) -> object:
    """把 ``key = raw_value`` 寫進設定檔，回傳轉型後的值。

    ``colors.<name>`` 點記法寫進 [colors] table（字串值）。其餘鍵須在 ``_SETTERS`` 內，
    依型別轉換。未知鍵 / 轉型失敗丟 ``ConfigError``（訊息給使用者）。重寫整檔（不保留註解）。
    """
    p = path or CONFIG_PATH
    data = _read_raw(p)

    if "." in key:
        table, sub = key.split(".", 1)
        if table != "colors" or not sub:
            raise ConfigError(f"未知的鍵：{key}")
        colors = data.get("colors")
        colors = dict(colors) if isinstance(colors, dict) else {}
        colors[sub] = raw_value
        data["colors"] = colors
        coerced: object = raw_value
    else:
        setter = _SETTERS.get(key)
        if setter is None:
            raise ConfigError(f"未知的鍵：{key}（可設：{', '.join(_SETTERS)}）")
        coerced = setter(raw_value)
        data[key] = coerced

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_toml(data))
    get_config.cache_clear()  # 同 process 內讓下次 get_config() 讀到新值
    return coerced
