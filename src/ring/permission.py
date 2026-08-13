"""面板內回覆權限請求——讀終端上的 Claude Code 權限對話框、代你按下選項。

流程：``backend.capture()`` 抓畫面 → ``parse_permission_dialog()`` 解析出編號選項 →
TUI 開浮層讓你選 → ``send_permission_reply()`` 送出前再抓一次確認對話框還在且沒變
（防 race），才送單一數字（**不帶 Enter**——對話框上按數字即選中；帶了 Enter 反而會
在對話框剛好消失時把字送進聊天輸入框）。

安全底線：任何解析不確定（抓不到對話框標記、選項編號不連續、沒有游標）一律回
``None`` / 不送鍵。誤送偵測：對話框不在時數字會落進聊天輸入框（``❯ 2``），此時補送
Backspace 清掉。

``send_permission_reply()`` 的流程本體只有一份，實際怎麼抓畫面／怎麼送鍵由
``PermissionBackend``（tmux / iTerm2 / kitty）決定——照 ``focus/base.py`` 的 Protocol 形狀走。
tmux 互動集中在這裡（照 ``focus/tmux.py`` 的形狀走 subprocess），iTerm2 互動走
``ring.osascript``（照 ``focus/applescript.py`` 的形狀），kitty 互動走 ``kitty @``
remote control（定位邏輯與 ``focus/kitty.py`` 的 focuser 共用 ``resolve_window()``）。
解析器是純函式，測試直接餵 PoC 抓下來的真實畫面（``tests/fixtures/permission/``）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ring.focus.kitty import resolve_window as kitty_resolve_window
from ring.osascript import osascript
from ring.registry import Session

# 對話框 footer：「 Esc to cancel · Tab to amend · ctrl+e to explain」。
# 注意大小寫——工作中狀態列的「esc to interrupt」是小寫 e，不會誤中。
_FOOTER_RE = re.compile(r"\bEsc to cancel\b")
# 編號選項列：「 ❯ 1. Yes」（游標）或「   2. Yes, and …」。限 1–2 位是刻意的：
# 「123. foo」這種列不該被當選項，交給續行/停止判準處理。
_OPTION_RE = re.compile(r"^\s*(?:❯\s*)?(\d{1,2})\.\s+(\S.*?)\s*$")
# 任何寬度的編號列（含 10./11./123.）——續行絕不可以長這樣，否則會把新選項吞掉。
_NUMBERED_RE = re.compile(r"^\s*(?:❯\s*)?\d+[.)]\s")
# 對話框標題列的 subagent 標頭：「Bash command · from the general-purpose agent」。
_AGENT_RE = re.compile(r"·\s*from the (.+?) agent\b")
# 對話框上緣的水平分隔線（整列 ─）。
_SEPARATOR_RE = re.compile(r"^\s*─{10,}\s*$")
# 聊天輸入框的提示列：「❯ <內容>」。誤送時數字會出現在這裡。
_INPUT_LINE_RE = re.compile(r"^\s*❯\s*(.+?)\s*$")

# 送出數字後驗證 UI 反應的最長秒數；期間短輪詢，對話框一消失就提早完成。
_VERIFY_DELAY = 0.4
_VERIFY_POLL_INTERVAL = 0.05


@dataclass(frozen=True)
class PermissionDialog:
    """解析出來的權限對話框。``options`` 是 (編號, 原文) 的序列，編號從 1 連續。"""

    options: tuple[tuple[int, str], ...]
    question: str = ""  # e.g. "Do you want to proceed?"
    title: str = ""  # e.g. "Bash command · from the general-purpose agent"
    agent: str = ""  # subagent 名稱（標題帶 "from the … agent" 才有），e.g. "general-purpose"


class ReplyOutcome(Enum):
    """``send_permission_reply()`` 的結果。只有 OK / MISFIRE 真的送過鍵。"""

    OK = "ok"  # 送出後對話框消失（或換成下一個請求）→ 成功
    NO_DIALOG = "no_dialog"  # 送出前的確認抓不到對話框 → 沒送
    CHANGED = "changed"  # 送出前的確認發現對話框變了 → 沒送
    SEND_FAILED = "send_failed"  # tmux send-keys 失敗
    STILL_PRESENT = "still_present"  # 送了，但同一個對話框還在 → 請跳過去確認
    MISFIRE = "misfire"  # 送了，數字落進聊天輸入框；已補 Backspace 清掉
    UNVERIFIED = "unverified"  # 送了，但驗證用的 capture 失敗 → 請跳過去確認


# ---------------------------------------------------------------------------
# 純解析（不碰 tmux）
# ---------------------------------------------------------------------------


def _is_option_continuation(line: str) -> bool:
    """判斷某一列是不是「上一個選項文字的續行」（選項太長被終端折成多行）。

    續行必須「有縮排」（至少一個前導空白），因為整個對話框本體都是縮排的，
    而它上方的 transcript 是齊左的——這一格縮排就把「齊左的對話紀錄」整類擋在外面。
    但光靠縮排還不夠：下面每一條都在排除「長得像續行、其實是別的結構列」的東西，
    排除掉才不會被誤併：

    - 空白列：今天的天然邊界，不排除的話選項區塊會跨過空行往上吃到 transcript。
    - 任何寬度的編號列（``_NUMBERED_RE``）：``10. Yes`` 必須被當成新選項，不是續行；
      不排除的話會安靜少一個選項、照樣送鍵——比大聲失敗更糟。
    - 含 ``❯``：只會出現在游標選項列或聊天輸入框；不排除的話誤送偵測用的輸入列
      （``❯ 2``）會被併進選項文字。
    - footer / 分隔線 / 標題列（``_FOOTER_RE`` / ``_SEPARATOR_RE`` / ``_AGENT_RE``）：
      都是對話框的結構錨點；不排除的話畫面上若有殘影或標題，會被吃進選項。
    - 以 ``?`` 結尾：問句列就在選項正上方、中間沒有空白列隔開，是最容易被誤併的一種——
      不排除的話問句會被併進選項 1，然後問句偵測往上撞到說明列，最終回 ``None``。
    """
    if not line.strip() or not line.startswith((" ", "\t")):
        return False
    if _NUMBERED_RE.match(line) or "❯" in line:
        return False
    if _FOOTER_RE.search(line) or _SEPARATOR_RE.match(line) or _AGENT_RE.search(line):
        return False
    return not line.rstrip().endswith("?")


def parse_permission_dialog(screen: str) -> PermissionDialog | None:
    """從 capture-pane 的畫面解析權限對話框；任何標記缺失就回 ``None``（絕不猜）。

    由下往上找，要求四個標記同時成立：
    1. footer 列（``Esc to cancel``）
    2. footer 上方是連續的編號選項列，編號從 1 起連續、至少 2 個。選項文字可跨行——
       續行必須有縮排，且不得長得像其他結構列（見 ``_is_option_continuation``）；
       有續行卻找不到歸屬（收集到畫面頂端仍有未歸位的續行）一律回 ``None``，
       寧可讓使用者自己去看，也不要拼出一份看起來合理但錯的選項清單。
    3. 至少一個選項列帶游標 ``❯``
    4. 選項上方第一個非空白列是問句（以 ``?`` 結尾，如 ``Do you want to proceed?``）
    """
    lines = screen.splitlines()
    footer_idx = next((i for i in range(len(lines) - 1, -1, -1) if _FOOTER_RE.search(lines[i])), None)
    if footer_idx is None:
        return None

    # footer 上方跳過空白列，往上收集連續的編號選項列（含續行）。
    i = footer_idx - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    collected: list[tuple[int, str]] = []
    pending: list[str] = []  # 續行緩衝：由下往上收集，遇到編號列才知道該接到哪個選項後面
    saw_cursor = False
    while i >= 0:
        m = _OPTION_RE.match(lines[i])
        if m is not None:
            text = m.group(2)
            if pending:
                text = f"{text} {' '.join(reversed(pending))}"
                pending = []
            collected.append((int(m.group(1)), text))
            if "❯" in lines[i]:
                saw_cursor = True
            i -= 1
            continue
        if _is_option_continuation(lines[i]):
            pending.append(lines[i].strip())
            i -= 1
            continue
        break
    if pending:
        return None  # 有續行找不到歸屬 → 畫面形狀不如預期，不猜
    options = tuple(reversed(collected))
    if len(options) < 2 or not saw_cursor:
        return None
    if [n for n, _text in options] != list(range(1, len(options) + 1)):
        return None

    # 問句：選項上方第一個非空白列，必須以 ? 結尾。
    while i >= 0 and not lines[i].strip():
        i -= 1
    if i < 0 or not lines[i].rstrip().endswith("?"):
        return None
    question = lines[i].strip()

    # 標題：往上找到對話框上緣的分隔線，其下第一個非空白列
    # （e.g. "Bash command · from the general-purpose agent"）。找不到就留空，不影響判定。
    title = ""
    sep_idx = next((j for j in range(i - 1, -1, -1) if _SEPARATOR_RE.match(lines[j])), None)
    if sep_idx is not None:
        title = next((lines[j].strip() for j in range(sep_idx + 1, i) if lines[j].strip()), "")
    agent_match = _AGENT_RE.search(title)
    agent = agent_match.group(1) if agent_match else ""
    return PermissionDialog(options=options, question=question, title=title, agent=agent)


def digit_in_input_line(screen: str, digit: str) -> bool:
    """對話框不在時送出的數字會落進聊天輸入框，變成一行「``❯ 2``」——偵測這種誤送。"""
    for line in screen.splitlines():
        m = _INPUT_LINE_RE.match(line)
        if m is not None and m.group(1) == digit:
            return True
    return False


# ---------------------------------------------------------------------------
# backend 協定——capture/send 可替換，流程本體（下面 send_permission_reply）只有一份
# ---------------------------------------------------------------------------


class PermissionBackend(Protocol):
    """一個終端的「抓畫面 / 送鍵」能力。照 ``focus/base.py`` 的 Protocol 形狀走。

    ``name`` 宣告成 read-only property：實作是 frozen dataclass，欄位不可寫，
    宣告成一般屬性會被 mypy 判定不相容。
    """

    @property
    def name(self) -> str: ...

    def capture(self) -> str | None: ...
    def send_digit(self, digit: str) -> bool: ...
    def send_backspace(self) -> bool: ...


# ---------------------------------------------------------------------------
# tmux 互動（全部走 subprocess，測試一律 mock）
# ---------------------------------------------------------------------------


def _run_tmux(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    if not shutil.which("tmux"):
        return None
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None


def capture_pane(target: str) -> str | None:
    """抓 pane 目前的可見畫面；tmux 不在 / target 無效 → ``None``。"""
    result = _run_tmux(["capture-pane", "-p", "-t", target])
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def send_key(target: str, key: str) -> bool:
    """對 pane 送一個鍵（tmux key 名，如 ``2`` / ``BSpace``）。"""
    result = _run_tmux(["send-keys", "-t", target, key])
    return result is not None and result.returncode == 0


@dataclass(frozen=True)
class TmuxBackend:
    """tmux pane 的 backend：``capture_pane`` / ``send_key`` 包一層給流程本體用。"""

    target: str
    name: str = "tmux"

    def capture(self) -> str | None:
        return capture_pane(self.target)

    def send_digit(self, digit: str) -> bool:
        return send_key(self.target, digit)

    def send_backspace(self) -> bool:
        return send_key(self.target, "BSpace")


# ---------------------------------------------------------------------------
# iTerm2 互動（走 osascript，測試一律 mock）
# ---------------------------------------------------------------------------

# 依 tty 找到對應 session 後執行 {action}；找不到就回 sentinel（真實畫面內容不可能剛好
# 撞上這個字串）。tty 定位邏輯與 ``focus/iterm2.py`` 的 focuser 同形，各自獨立維護——
# 一個是「聚焦分頁」，一個是「抓畫面／送鍵」，職責不同，硬併會讓兩邊都難改。
_ITERM_NO_SESSION = "__ring_no_session__"
_ITERM_SCRIPT = """
if application "iTerm2" is running then
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is "{tty}" then
            {action}
          end if
        end repeat
      end repeat
    end repeat
  end tell
end if
return "{sentinel}"
"""


def _iterm_run(tty: str, action: str) -> tuple[int, str, str] | None:
    if not tty or not shutil.which("osascript"):
        return None
    return osascript(_ITERM_SCRIPT.format(tty=tty, action=action, sentinel=_ITERM_NO_SESSION))


def iterm_capture(tty: str) -> str | None:
    """抓 iTerm2 session 目前的可見畫面（``contents of session``）；找不到 → ``None``。"""
    result = _iterm_run(tty, "return contents of s")
    if result is None:
        return None
    rc, out, _err = result
    if rc != 0 or out == _ITERM_NO_SESSION:
        return None
    return out


def _iterm_write(tty: str, payload: str) -> bool:
    result = _iterm_run(tty, f'tell s to write text {payload} newline NO\n            return "ok"')
    return result is not None and result[0] == 0 and result[1] == "ok"


def iterm_send_digit(tty: str, digit: str) -> bool:
    """對 iTerm2 session 送一個數字——單一數字直接觸發選項，不帶 Enter。"""
    return _iterm_write(tty, f'"{digit}"')


def iterm_send_backspace(tty: str) -> bool:
    """對 iTerm2 session 送 Backspace（誤送補救用）。"""
    return _iterm_write(tty, "(ASCII character 8)")


@dataclass(frozen=True)
class ITermBackend:
    """iTerm2 session 的 backend：依 tty 定位 session，抓畫面／送鍵都透過 osascript。"""

    tty: str
    name: str = "iTerm2"

    def capture(self) -> str | None:
        return iterm_capture(self.tty)

    def send_digit(self, digit: str) -> bool:
        return iterm_send_digit(self.tty, digit)

    def send_backspace(self) -> bool:
        return iterm_send_backspace(self.tty)


# ---------------------------------------------------------------------------
# kitty 互動（走 kitty @ remote control，測試一律 mock）
# ---------------------------------------------------------------------------


def _run_kitty(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    if not shutil.which("kitty"):
        return None
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None


def kitty_capture(socket_path: str, window_id: int) -> str | None:
    """抓 kitty window 目前的可見畫面（``get-text`` 預設 extent=screen、ansi=no）；失敗 → ``None``。"""
    result = _run_kitty(["kitty", "@", "--to", f"unix:{socket_path}", "get-text", "--match", f"id:{window_id}"])
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def kitty_send_digit(socket_path: str, window_id: int, digit: str) -> bool:
    """對 kitty window 送一個數字——``send-text`` 不會自動加 newline。"""
    result = _run_kitty(
        ["kitty", "@", "--to", f"unix:{socket_path}", "send-text", "--match", f"id:{window_id}", "--", digit]
    )
    return result is not None and result.returncode == 0


def kitty_send_backspace(socket_path: str, window_id: int) -> bool:
    """對 kitty window 送 Backspace（誤送補救用）；用 ``send-key`` 送出 kitty 自己對這顆鍵的翻譯（``0x7f``）。"""
    result = _run_kitty(
        ["kitty", "@", "--to", f"unix:{socket_path}", "send-key", "--match", f"id:{window_id}", "--", "backspace"]
    )
    return result is not None and result.returncode == 0


@dataclass(frozen=True)
class KittyBackend:
    """kitty window 的 backend：依 ``(socket, window_id)`` 抓畫面／送鍵，全程走 ``kitty @`` remote control。"""

    socket_path: str
    window_id: int
    name: str = "kitty"

    def capture(self) -> str | None:
        return kitty_capture(self.socket_path, self.window_id)

    def send_digit(self, digit: str) -> bool:
        return kitty_send_digit(self.socket_path, self.window_id, digit)

    def send_backspace(self) -> bool:
        return kitty_send_backspace(self.socket_path, self.window_id)


def select_backend(session: Session) -> PermissionBackend | None:
    """依 session 現有座標選 backend：tmux 座標優先，其次確認 tty 是否屬於某個 kitty window，
    最後才是 macOS 上的 iTerm2。

    都沒有 → ``None``，呼叫端走既有的 toast（不送鍵）路徑。kitty 這關會跑子行程
    （每次按 ``p`` 一次，不在刷新迴圈裡），且不限平台——Linux 上的 kitty 也吃同一套協定。
    """
    target = session.tmux_pane or session.tmux_target
    if target:
        return TmuxBackend(target)
    if session.tty:
        resolved = kitty_resolve_window(session.tty)
        if resolved is not None:
            socket_path, window_id = resolved
            return KittyBackend(socket_path, window_id)
    if session.tty and sys.platform == "darwin":
        return ITermBackend(session.tty)
    return None


# ---------------------------------------------------------------------------
# 送鍵流程本體——backend 換 tmux / iTerm2，流程只有這一份
# ---------------------------------------------------------------------------


def send_permission_reply(
    backend: PermissionBackend,
    expected: PermissionDialog,
    number: int,
    *,
    delay: float = _VERIFY_DELAY,
) -> ReplyOutcome:
    """把選定的選項編號送進終端上的權限對話框，送出前後都驗證。

    1. 再 capture 一次：對話框不在 → ``NO_DIALOG``；跟 ``expected`` 不同 → ``CHANGED``。
       兩者都**不送鍵**（使用者在浮層裡想的期間，對話框可能已被本人回掉或換內容）。
    2. 送單一數字（不帶 Enter）。
    3. 最多用 ``delay`` 秒短輪詢 capture：對話框一消失就提早回 ``OK``；數字落進
       輸入框 → 補 Backspace、``MISFIRE``；期限到仍是同一個對話框 →
       ``STILL_PRESENT``。
    """
    if not any(number == n for n, _text in expected.options):
        return ReplyOutcome.CHANGED  # 編號不在選項裡，視同對話框對不上，不送
    screen = backend.capture()
    if screen is None:
        return ReplyOutcome.NO_DIALOG
    current = parse_permission_dialog(screen)
    if current is None:
        return ReplyOutcome.NO_DIALOG
    if current != expected:
        return ReplyOutcome.CHANGED
    if not backend.send_digit(str(number)):
        return ReplyOutcome.SEND_FAILED
    deadline = time.monotonic() + max(0.0, delay)
    while True:
        after = backend.capture()
        if after is not None:
            after_dialog = parse_permission_dialog(after)
            if after_dialog is None:
                if digit_in_input_line(after, str(number)):
                    backend.send_backspace()
                    return ReplyOutcome.MISFIRE
                return ReplyOutcome.OK
            if after_dialog != expected:
                return ReplyOutcome.OK  # 對話框換成下一個請求 → 原請求已被回覆

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ReplyOutcome.UNVERIFIED if after is None else ReplyOutcome.STILL_PRESENT
        time.sleep(min(_VERIFY_POLL_INTERVAL, remaining))
