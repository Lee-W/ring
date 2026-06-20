"""RiNG — Realtime Instance Notification Grid。

看所有 active 的 agent CLI session 上台的場館（內建 Claude Code，可擴充）。
session 需要你回話時，它「響鈴」叫你。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ring")
except PackageNotFoundError:  # 直接從原始碼跑、尚未安裝
    __version__ = "0.0.0+dev"
