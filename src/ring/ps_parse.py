"""``ps`` 輸出的純解析層：從 process 行判定 provider、抽出欄位。

這裡的函式全部是純函式（吃字串、吐結果），不碰 subprocess、不碰快取、不碰檔案系統，
所以可以獨立測試，也不必在意 ``registry`` 那些短快取的狀態。實際去跑 ``ps`` 並套用
快取的是 ``ring.registry``；本模組只負責它拿回來的那坨文字怎麼讀。
"""

from __future__ import annotations

from pathlib import Path

# args 內任一出現即可判定「這是 claude 安裝二進位在跑」的路徑標記。ps comm 對
# daemon-exec 的二進位常被截斷（如 `/Users/weilee/.l`），單看 comm 不可靠。
_CLAUDE_PATH_MARKERS = ("ClaudeCode.app", "claude/versions/", "/.claude/")


# Claude Code 每次 Bash 工具呼叫都會 spawn 一個短命（0-10 秒）的 shell 承載
# `source ~/.claude/shell-snapshots/snapshot-*.sh` 之類的初始化腳本，cwd 就是
# 專案目錄——外觀酷似真正的 claude session。這個 shell 的 comm 是完整路徑
# （例如 `/bin/zsh`），但 args 裡含 `/.claude/` 子字串，會命中上面的
# `_CLAUDE_PATH_MARKERS`，被誤判為 claude session process，導致 board 上 session
# 數在每次任何 session 跑指令時 flap（synthetic row 出現又消失 / live 名額被灌水）。
# 真實樣本見 2026-07-13/14 現場取證（proc_logger.log）。這裡的守門：comm basename
# 一旦是已知 shell 名稱，一律不是 claude session——真正的 claude session comm
# 只會是 `claude` 或被截斷的安裝路徑片段（如 `/Users/weilee/.l`），從不會是
# shell 執行檔本身。
_SHELL_COMM_BASENAMES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish"})


def _is_shell_comm(comm: str) -> bool:
    """comm basename 是否為常見 shell（含 login shell 的 ``-`` 前綴，如 ``-zsh``）。"""
    base = Path(comm.strip()).name
    if base.startswith("-"):
        base = base[1:]
    return base in _SHELL_COMM_BASENAMES


def _is_claude_session_line(comm: str, args: str) -> bool:
    """判定一行 ``ps`` 輸出是否為 claude session process（承載者或子行程皆算）。

    comm basename 為 ``claude`` 是最常見的情況；但 daemon 承載的 process，ps comm
    會被截斷成本機路徑片段（例如 ``/Users/weilee/.l``），此時改看 args 是否含可辨識
    的 claude 安裝路徑標記，或 args 內任一 token 的 basename 為 ``claude``
    （args 首 token 有時只是版本號如 ``2.1.187``，不能只看 args[0]）。第三個 fallback
    另外要求 args 內必須有 ``--session-id``，否則像 ``grep -r claude .``、
    ``less claude`` 這類完全無關但恰好帶 ``claude`` 字面的 process 會被誤收；
    真正被截斷 comm 的 claude session（daemon 承載者與其子行程）必然帶
    ``--session-id``，所以這個限定不會犧牲 fallback 能力。

    comm basename 為 shell（見 ``_is_shell_comm``）時提前回傳 ``False``：Bash 工具
    呼叫 spawn 的 shell wrapper args 常含 ``/.claude/``（source shell-snapshots 腳本），
    會誤觸下面的 path marker 分支，必須在那之前擋下。
    """
    if Path(comm.strip()).name == "claude":
        return True
    if _is_shell_comm(comm):
        return False
    if any(marker in args for marker in _CLAUDE_PATH_MARKERS):
        return True
    tokens = args.split()
    if "--session-id" not in tokens:
        return False
    return any(Path(tok).name == "claude" for tok in tokens)


def _normalize_tty(raw: str) -> str:
    """把 ``ps`` 的 tty 欄位正規化成 iTerm2 認得的 ``/dev/ttysNNN``；查無 tty 回空字串。"""
    tty = raw.strip()
    if not tty or tty in ("??", "?"):
        return ""
    return tty if tty.startswith("/dev/") else f"/dev/{tty}"


def _parse_ps_claude_lines(out: str) -> list[tuple[int, str, str, bool]]:
    """把 ``ps`` 輸出解析成 claude session 行：``(pid, tty, args, is_bg_pty_host)``。

    tty 欄位（``ps -Ao pid,tty,comm,args`` 的第二欄）已正規化，供 ``_claude_procs``
    直接查表用，不必再對每個 pid 各開一次 ``ps -o tty= -p PID``。

    不論是否為背景 process（daemon / bg-spare / bg-pty-host）都收進來——背景判定
    交給呼叫端各自決定要不要濾除；``_hook_sessions`` 的活性判定與
    ``running_claude_pids`` 對「該濾誰」的答案不同，不能在這裡先幫忙決定。
    """
    entries: list[tuple[int, str, str, bool]] = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        tty = _normalize_tty(parts[1])
        comm = parts[2].strip()
        args = parts[3] if len(parts) == 4 else ""
        if not _is_claude_session_line(comm, args):
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        entries.append((pid, tty, args, "--bg-pty-host" in args.split()))
    return entries


def _arg_session_id(args: str) -> str | None:
    """解析 args 裡 ``--session-id`` 後面的那個 token；沒有就回 ``None``。"""
    tokens = args.split()
    for i, tok in enumerate(tokens):
        if tok == "--session-id" and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _is_claude_background_process(args: str) -> bool:
    """Claude daemon / bg pty host 暖機承載者 / bg-spare 不是使用者可聚焦的 CLI session。

    ``--bg-spare`` 是 Claude Code 預熱的備用 process（供下一個 ``claude`` 呼叫快速接手），
    跟尚未載入真 session 的 ``--bg-pty-host`` 承載者一樣不代表真正的使用者 session，卻會
    被 ``_claude_procs`` 合成假 session 列上看板，冒出幽靈列。token 形狀（`--bg-spare`，
    `--` flag，不是位置參數）取自本機 claude CLI 2.1.206 二進位的 strings 掃描（無法用
    ``ps`` 現場逮到活體 bg-spare process，掃描時機是巧合——它壽命短、隨用隨滅）：
    ``[a,...l,"--bg-pty-host",r,"200","50","--",a,...l,"--bg-spare",n]`` spawn 呼叫，
    以及 bg-spare process 自己啟動時對 ``process.argv`` 做的
    ``e.includes("--bg-spare", t+1)`` 檢查，兩處都證實是 ``--`` 前綴的 flag token。

    ``--bg-pty-host`` 本身不再一律濾除：暖機階段（spare sock，無 ``--session-id``）仍
    濾除；一旦掛上真正的 ``--session-id``（使用者已進入 agents、真背景 session），就不
    再視為背景 process——那是一個真人在跑的背景 agent，該讓它現身，只是要標成
    ``kind="agent"``（見 ``background_agent_session_ids``）。
    """
    tokens = args.split()
    if len(tokens) >= 3 and tokens[1:3] == ["daemon", "run"]:
        return True
    if "--bg-spare" in tokens:
        return True
    if "--bg-pty-host" in tokens:
        return _arg_session_id(args) is None
    return False


def _parse_ps_codex_lines(out: str) -> list[tuple[int, str, str]]:
    """把 ``_ps_codex_snapshot()`` 的輸出解析成 ``(pid, tty, args)``（僅 comm 為 codex 的行）。"""
    entries: list[tuple[int, str, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or Path(parts[2].strip()).name != "codex":
            continue
        tty = _normalize_tty(parts[1])
        args = parts[3] if len(parts) == 4 else ""
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        entries.append((pid, tty, args))
    return entries


def _is_codex_internal_process(args: str) -> bool:
    """Codex app 為工具呼叫啟動的同名內部 process，不是獨立互動 session。"""
    tokens = args.split()
    if not tokens:
        return False
    try:
        command_index = next(i for i, token in enumerate(tokens) if Path(token).name == "codex")
    except StopIteration:
        return False
    return command_index + 1 < len(tokens) and tokens[command_index + 1] in {"app-server", "sandbox"}
