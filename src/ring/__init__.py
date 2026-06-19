"""RiNG — Realtime Instance Notification Grid。

看所有 active 的 Claude Code session 上台的場館。session 需要你回話時，它「響鈴」叫你。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ring-cc")
except PackageNotFoundError:  # 直接從源碼跑、尚未安裝
    __version__ = "0.0.0+dev"
