# RiNG 🎤

**台灣漢語** · [English](README.en.md)

> **R**ealtime **I**nstance **N**otification **G**rid
> ——看所有 active 的 agent CLI session 上台的**場館**（內建 Claude Code / Codex，可擴充）。

你同時開了好幾個 Claude Code / Codex，不知道哪個正在等你回話、哪個還在跑、
哪個早就停了。RiNG 把它們全部請上同一個舞台，一眼看完——**誰在等你，排最前面**。
session 需要你回話時，它「**ring** 你」。

名字三重共鳴：📞 它 **ring** 你（待回覆通知）＋ 🎤 BanG Dream! 的 live house「RiNG」
（場館＝你坐著看一團一團、也就是一個個 session 演出的地方）＋ **R**ealtime **I**nstance
**N**otification **G**rid（它到底是什麼）。覺得好用、順手回去補番，那就更好了。

## 為什麼是「二號店」

原作裡的 RiNG 是 CiRCLE 的**二號店**——因為少女樂團數量暴增、一間場館不夠用，
才在「老闆」的要求下蓋的；而且它跟 CiRCLE **共用同一套訂單／帳號系統**。
這把工具的定位講得剛剛好：

- **一樣的誕生理由**：你的並行 agent CLI session 也暴增了，需要一個專門的地方一眼看完。
- **不取代、是分店**：RiNG 不是另一個 client——它讀的是 Claude Code / Codex 的本機後台，
  只是替你多開的第二個觀測視窗。

## 跑起來

`ring` 是要先裝成指令的——光 clone 下來打 `ring` 會是 `command not found`。

```sh
# 開發時：在 repo 裡直接跑（uv 會 build entry point）
uv run ring                       # 快照
uv run ring --watch               # 持續刷新，Ctrl-C 離場
uv run ring --watch --interval 1  # 自訂刷新秒數

# 裝成全域指令，之後直接打 ring
uv tool install '.[tui]'          # [tui] 帶 Textual 互動版；或 pipx install '.[tui]'
ring --watch

# 安裝後也可用 module 形式跑
python -m ring
```

`--count N` 讓 `--watch` 刷新 N 格後自動結束（給 CI / 測試用）。

### `--watch` 的兩種樣子

- 裝了 **Textual**（`[tui]` extra）且在真終端 → **互動 TUI**：
  `↑/↓` 選 session、`Enter` 跳到它所在的終端、`a` 切換是否顯示已離場、`r` 刷新、`q` 離場。
- 否則 → **Rich poll**（清除畫面重畫）；連 Rich 都沒有就純文字。三層優雅降級。

### 跳到 session（`Enter` / `Space`）

選一個 session 按 `Enter`，RiNG 把焦點帶到它真正所在的終端。終端整合是**可插拔的
focuser**——core 不綁任何特定 vendor，加一個終端＝加一個 focuser、主流程零改動：

- **tmux**：`switch-client` 直接切到那個 pane（你跟它要在同一個 tmux server）。
- **iTerm2 / Terminal.app**（macOS）：用 session 的 `tty` 透過 AppleScript 聚焦對應分頁，
  自動分辨是哪個 app（沒在跑的 app 不會被喚醒）。第一次會跳系統「自動化」授權，准一次即可。

要再加 Ghostty / Kitty / WezTerm：寫一個符合 `Focuser` 協定的類別
（`try_focus(session) -> (ok, msg) | None`），呼叫 `ring.focus.register_focuser(MyFocuser())`
即可——core 零改動。嘗試順序也能用 config 的 `focusers` 調整。

「哪個 session 在哪個終端」靠它的 `tty` 對應——**Claude Code hook 模式最精準**；
zero-config 下每個專案只開一個 session 時也對得上。Codex 目前走 zero-config，
同一個 cwd 只開一個 live Codex 時可跳轉；同 cwd 多個 Codex 只能保守顯示，避免跳錯。

### 它會 ring 你 🔔

裝了 hook 後，有 session 從工作中轉成 🔴 待回覆時，RiNG 會**響鈴 + 跳通知**——
名副其實，它真的 ring 你。若它一直停在待回覆，RiNG 也會照設定再次提醒，
避免第一聲被你忽略。（zero-config 測不到 WAITING，所以這個需要 hook 模式。）

通知帶有點擊跳轉功能——點通知後直接跳回 RiNG TUI 並選中那個 session。
需要安裝 [terminal-notifier](https://github.com/julienXX/terminal-notifier)（brew 外部 binary）：

```sh
brew install terminal-notifier
```

沒裝時退化為 macOS 原生純文字通知（不可點擊跳轉），RiNG 會在第一次走到這條路時提示一次。

```
🎤 RiNG — 3 session 在場 · 2 agent process 跑著

  🔴 maigo            12s  → 等你確認權限
  🟢 pelican-osm       3s  → Edit
  🟡 commitizen        8m  跑完一回合、停著
```

## 兩種資料來源

| 模式 | 來源 | 狀態精度 |
|------|------|----------|
| **Claude Code zero-config**（預設） | 掃 `~/.claude/projects/**/*.jsonl` 的 mtime + 記錄裡的 `cwd` 欄位 | 中（可從 transcript 尾端猜回完一輪；測不到 permission notification） |
| **Codex zero-config**（預設） | 讀 `~/.codex/state_5.sqlite` threads + rollout JSONL，並用 live `codex` process 配 tty | 中（可辨識 live / ended / 回完一輪；同 cwd 多 session 跳轉不保證精準） |
| **hook registry**（opt-in，精準） | RiNG hook 在 `Notification` / `UserPromptSubmit` / `Stop` / `SessionEnd` 即時寫 `~/.config/ring/sessions/` | 準（🔴 等你 / 🟢 工作 / 🟡 idle / ⚫ 離場） |

zero-config 不必設定就能用；想要精準的「誰在等你」，就讓 provider 的 hook 餵進 RiNG registry。
RiNG 內建 Claude Code hook 安裝器；Codex 與未來工具可直接走 provider-neutral `ring hook` protocol。

## 狀態機

```
🔴 WAITING  在等你進場   ← 排最上面、highlight
🟢 WORKING  台上正在跑
🟡 IDLE     跑完一回合、停著
⚫ ENDED    已離場
```

## 為什麼不碰 token / 花費統計

Claude Code 的 JSONL token 數字是壞的（input 差約 100 倍、output 差約 10 倍），
所有靠它做帳的工具都中招。RiNG 只看**狀態**，不做 cost accounting——刻意避開這雷。

## hook 模式（精準的「待回覆」）

zero-config 只靠檔案 mtime 或本機 state，**分不出「需要你做決策」還是「剛跑完」**。
裝 hook 後，RiNG 直接收 agent CLI 的事件，狀態才會精準——🔴 待回覆 ＋ 響鈴都要靠它。

### Claude Code：內建安裝器

Claude Code hook 會被寫成「執行 `ring hook`」，所以 `ring` 要在 PATH 上、指向穩定路徑：

```sh
uv tool install '.[tui]'    # 在 repo 目錄裡
```

註冊 hook：

```sh
ring install-hooks            # 寫進 ~/.claude/settings.json（合併，不覆蓋既有 hooks）
ring install-hooks --dry-run  # 只想先看會寫什麼、不動檔
```

它註冊這幾個事件，對應到狀態：

| Claude Code 事件 | RiNG 狀態 |
|---|---|
| `SessionStart` / `UserPromptSubmit` | 🟢 工作中 |
| `Stop` | 🟡 跑完停著 |
| `Notification` 的 `permission_prompt` / `elicitation_dialog` | 🔴 待回覆（卡權限 / 需要選項） |
| `SessionEnd` | 從看板消失 |

hook 只對**新開的 session** 生效，所以裝完要重開。確認方法：

```sh
ls ~/.config/ring/sessions/   # 出現 <session_id>.json 就代表 hook 在寫了
```

RiNG 一偵測到 `~/.config/ring/sessions/` 有資料就自動切精準模式（hook 來源優先、
zero-config 掃描補上沒裝 hook 的 session）。

### Codex / 未來 provider：中立 hook protocol

RiNG 不相依 agent-hooks；任何工具只要在事件發生時把 JSON 餵給 `ring hook` 即可：

```sh
ring hook --provider codex
# 或簡寫：
ring hook codex
```

payload 欄位採寬鬆命名，至少需要 session id、event、cwd：

```json
{
  "provider": "codex",
  "session_id": "thread-123",
  "event": "Notification",
  "notification_type": "permission_prompt",
  "requires_action": true,
  "waiting_for": "permission",
  "cwd": "/repo/app",
  "tty": "/dev/ttys003",
  "last_action": "waiting for permission"
}
```

支援的事件語意與 Claude Code 一致：`SessionStart` / `UserPromptSubmit` → 🟢，
`Stop` → 🟡，actionable `Notification` / `PermissionRequest` → 🔴，`SessionEnd` →
從看板移除。非 Claude provider 的 session id 會自動加上 provider prefix
（例如 `codex:thread-123`），避免不同工具撞 id。

provider 若能分辨「立刻需要使用者」與「只是等下一步」，請直接給明確欄位：
`requires_action = true/false` 或 `waiting_for = "permission" | "options" | "next_step"`。
RiNG 會優先相信這些欄位；沒有時才退回 event / notification type 推論。

### 系統通知（🔔 waiting 自動通知 + 點擊聚焦）

hook 模式下，每當有 session 新轉為 🔴 待回覆，RiNG 就會發系統通知（headless `--watch`
與 TUI 兩條路徑都會通知）。如果 session 持續待回覆，也會依
`notify_repeat_seconds` 再提醒，預設在 30s / 120s / 300s 各補一次：

- **點擊聚焦 + 聲音**：需先安裝 `terminal-notifier`（brew）。點擊通知後會直接跳回 RiNG TUI
  並選中 session；若沒有 TUI 在跑，則直接跳到對應終端。
  ```sh
  brew install terminal-notifier
  ```
- **純文字通知**：未裝 `terminal-notifier` 則退化為 macOS 原生純文字通知（可帶聲音，點擊不可聚焦）。

### 要移除

```sh
ring remove-hooks             # 從 ~/.claude/settings.json 移除 ring hook 條目
ring remove-hooks --dry-run   # 只想先看會移除什麼、不動檔
```

## 設定（選用）

`~/.config/ring/config.toml`，全部選填、缺了就用預設：

```toml
lang = "zh-Hant"                 # 預設語言（CLI --lang 最優先，再來 RING_LANG / LANG）
interval = 2.0                   # watch 刷新秒數
show_all = false                 # 是否預設顯示已離場
legend = true                    # 是否預設顯示圖例
active_window_seconds = 21600    # 只看最近這段時間動過的 session（預設 6h）
working_threshold_seconds = 90   # 多久沒動 → 🟢 工作中 變 🟡 閒置
notify_sound = true              # 系統通知是否播放聲音
notify_sound_name = "Glass"      # macOS / terminal-notifier sound name
notify_repeat_seconds = [30, 120, 300]  # 持續 waiting 時，幾秒後重複提醒
notify_repeat_max = 3            # 重複提醒上限；0 = 不限
focusers = ["tmux", "iTerm2", "Terminal"]   # 跳轉嘗試順序

[colors]                         # Rich 樣式字串，逐項覆寫（深淺底安全色為預設）
waiting = "bold red"
working = "green"
idle = "yellow"
ended = "grey50"
project = "cyan"
location = "bright_blue"
muted = "grey50"
```

## 語言 / 翻譯

UI 用 **gettext**，msgid 直接是**台灣漢語**——所以預設（zh-Hant）不需要任何 `.mo`，
原始碼即翻譯。切語言：`--lang en`（或 `RING_LANG=en` / config 的 `lang`）。複數用
`ngettext` 正確處理（`1 session` vs `2 sessions`）。

加一個語言、或改了字串要重抽：

```sh
poe i18n-extract     # 從原始碼抽 msgid → src/ring/locale/ring.pot
# 複製 ring.pot 成 src/ring/locale/<lang>/LC_MESSAGES/ring.po，填 msgstr
poe i18n-compile     # 各 .po → .mo（.mo 要 commit；wheel 會自動帶上）
```

## 擴充

core 不綁死任何特定工具或終端。兩個擴充點都是「寫個小類別 ＋ 註冊」，主流程零改動。

### 別的 agent CLI（`SessionSource`）

內建 `HookRegistrySource`（讀 `~/.config/ring/sessions/`）、`ClaudeCodeSource`（掃
`~/.claude`）與 `CodexSource`（讀 `~/.codex/state_5.sqlite`）。要監測別的工具，
可以優先餵 `ring hook`；若工具沒有 hook，再寫一個 source 吐出 `Session`、註冊即可：

```python
from ring.registry import Session, Status
from ring.sources import register_source


class MyToolSource:
    name = "mytool"

    def discover(self) -> list[Session]:
        return [Session(session_id="…", cwd="…", status=Status.WORKING,
                        last_active=0.0, last_action="→ …", source="mytool")]


register_source(MyToolSource())
```

`Session` 是工具中立的（id / cwd / status / last_action / tty / todo…），各 source
自己決定怎麼填。

### 別的終端（`Focuser`）

跳轉的終端整合也一樣——寫個符合 `Focuser` 協定的類別（`try_focus(session) ->
(ok, msg) | None`），呼叫 `ring.focus.register_focuser(MyFocuser())`。內建 tmux /
iTerm2 / Terminal.app，各自一個模組（`ring/focus/tmux.py` …）。

## 平台與隱私

- **平台**：macOS / Linux（靠 `ps` / `lsof` / `tmux` 偵測；Windows 未支援）。
- **隱私**：全程在你本機跑——只**讀** `~/.claude/`、`~/.codex/` 的本機資料，只**寫**
  `~/.config/ring/`。不連網、不上傳、不外送任何資料。

## License

MIT
