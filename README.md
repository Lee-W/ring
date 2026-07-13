# RiNG 🎤

**台灣漢語** · [English](README.en.md)

[![PyPI](https://img.shields.io/pypi/v/ring-cli?label=PyPI)](https://pypi.org/project/ring-cli/)
[![Python](https://img.shields.io/pypi/pyversions/ring-cli)](https://pypi.org/project/ring-cli/)
[![License](https://img.shields.io/pypi/l/ring-cli)](LICENSE)

> **R**ealtime **I**nstance **N**otification **G**rid
> ——看所有 active 的 agent CLI session 上台的**場館**（內建 Claude Code / Codex，可擴充）。

你同時開了好幾個 Claude Code / Codex，不知道哪個正在等你回話、哪個還在跑、
哪個早就停了。RiNG 把它們全部請上同一個舞台，一眼看完——**誰在等你，排最前面**。
session 需要你回話時，它「**ring** 你」。

```text
🎤 RiNG — 3 session 在場 · 2 個 agent process 跑著

  🔴 maigo            12s  → 等你確認權限
  🟢 pelican-osm       3s  → Edit
  🟡 commitizen        8m  跑完一回合、停著
```

## 適合誰用

- 你會同時開好幾個 Claude Code / Codex session。
- 你想用一張看板分辨「正在跑」、「跑完停著」、「正在等你」。
- 你想在 TUI 裡選 session 後直接跳回它所在的終端。
- 你願意裝 hook，換取精準的「等你」狀態與系統通知。

## 重點功能

- **一張看板看全部 session**：Claude Code / Codex 內建支援，其他工具可接 `ring hook`。
- **等你優先**：需要你回應的 session 會排最上面，避免被工作中的 session 淹掉。
- **一鍵跳回終端**：在 TUI 選 session 後按 `Enter` / `Space`，跳回 tmux、iTerm2、Terminal.app（macOS）或 Linux X11 視窗（`wmctrl`）裡的原本位置。
- **等你時通知，不必開著看板**：裝了 hook，session 轉 🔴 等你的**當下**就響鈴 + 發系統通知——關掉 RiNG 看板、關掉終端也照樣 ring 你。裝 `terminal-notifier` 後還能點通知跳回 session。
- **就地回覆權限請求**：游標停在 🔴 等你的列按 `p`，RiNG 讀出那個 session 終端畫面上的權限對話框選項，開浮層讓你選、代你按下——不必一個個跳過去（支援 tmux 內的 session，以及 macOS 上直接開在 iTerm2 分頁的 session）。
- **替 session 命名**：TUI 裡按 `n` 幫 session 取名，像「重構登入」；取了名，看板與通知就直接顯示名字，不再用專案目錄名猜它在做什麼。
- **看得到它在等什麼**：hook 模式下，🔴 等你的 session 會帶「具體在等什麼」（要跑的指令、問的問題），TUI 選中即顯示、通知內文也帶——小事可以先放著。
- **塞進 status bar**：`ring --format oneline` 印 `🔴2 🟢1 🟡3` 單行摘要給 tmux / SwiftBar / waybar；`--format json` 給腳本吃。
- **人不在座位也 ring 你**：內建 ntfy / webhook 遠端通知後端，等你的當下直接推到手機。
- **預設全本機、可擴充**：只讀本機 Claude Code / Codex 資料，只寫 `~/.config/ring/`；唯一會連網的是你自己設定的 ntfy / webhook 通知。source、focuser、notifier 都可插拔。

## 跑起來

需要 Python 3.13+。PyPI 發佈名是 `ring-cli`，但 module 與指令都叫 `ring`。
`ring` 要先裝成指令；光 clone 下來打 `ring` 會是 `command not found`。

```sh
# 推薦：從 PyPI 裝成全域指令
uv tool install 'ring-cli[tui]'

# 或用 pipx
pipx install 'ring-cli[tui]'

# 之後直接打 ring
ring                              # 快照
ring --watch
ring --watch --interval 1         # 自訂刷新秒數

# 安裝後也可用 module 形式跑
python -m ring
```

`[tui]` extra 會安裝 Textual 互動版；不裝也能跑，只是 `--watch` 會退回 Rich poll / 純文字模式。

開發時可以在 repo 裡直接跑，`uv` 會 build entry point：

```sh
uv run ring
uv run ring --watch
```

zero-config 不用設定就能看 Claude Code / Codex 的本機 session；要精準偵測 🔴 等你，
再裝 hook：

```sh
ring install-hooks            # 合併寫入 Claude Code / Codex 的 hook 設定
ring install-hooks --dry-run  # 只預覽，不改檔
ring doctor                   # 檢查 hook、通知後端、focuser、設定檔
ring gc --dry-run             # 預覽 RiNG 自己的 stale 狀態檔清理
```

hook 只對新開的 session 生效，所以裝完要重開 Claude Code / Codex session。

## 常用指令

| 指令 | 用途 |
|------|------|
| `ring` | 印一張當下快照 |
| `ring --watch` | 持續刷新；有 Textual 時進互動 TUI |
| `ring --watch --interval 1` | 每 1 秒刷新 |
| `ring --watch --count N` | 刷新 N 次後結束，方便測試 / CI |
| `ring --all` | 顯示已離場 session |
| `ring --no-legend` | 隱藏圖例 |
| `ring --lang en` | 切英文 UI |
| `ring focus SESSION_ID` | 聚焦指定 session；可用唯一前綴 |
| `ring config` | 顯示設定檔路徑與生效設定 |
| `ring config set KEY VALUE` | 寫入單一設定 |
| `ring doctor` | 唯讀環境診斷 |
| `ring digest --since 4h` | 離席摘要：最近 session 狀態與等待統計 |
| `ring gc --dry-run` | 預覽 RiNG 自己的 stale 狀態檔清理 |
| `ring gc` | 清理 RiNG 自己的 stale 狀態檔 |
| `ring --format json` | 整個看板的機器可讀快照（給 jq / 腳本） |
| `ring --format oneline` | `🔴2 🟢1 🟡3` 單行摘要（給 status bar） |
| `ring stats` | 等待統計：最近 7 天你讓 agent 🔴 等了多久 |
| `ring completion zsh` | 印 shell 補全腳本（zsh / bash） |

### 塞進 status bar（`--format`）

```sh
ring --format oneline        # 🔴2 🟢1 🟡3（沒 session 時輸出空字串，段落自然收起）
ring --format json | jq '.counts.waiting'
```

- **tmux**：`set -g status-right '#(ring --format oneline) …'`（配 `status-interval 5`）。
- **SwiftBar / xbar / waybar**：包一層腳本呼叫 `ring --format oneline` 或吃 JSON 自己排版。
- JSON 的鍵名視為穩定介面（只加不改），放心接腳本。

### Shell 補全（`ring completion`）

```sh
# ~/.zshrc
eval "$(ring completion zsh)"
# ~/.bashrc
eval "$(ring completion bash)"
```

子命令、旗標、`config set` 的鍵都補得到；`ring focus` 會提示 session id / 唯一前綴參數。

### `--watch` 的兩種樣子

- 裝了 **Textual**（`[tui]` extra）且在真終端 → **互動 TUI**：
  `↑/↓` 選 session、`Enter` / `Space` 跳到它所在的終端、`p` 就地回覆權限請求、`n` 命名、`a` 切換是否顯示已離場、`dd` 隱藏 session（有新活動會自動重新出現）、`r` 刷新、`q` 離場。
  如果你跟我一樣有 vim 手癖，也可以用 `j/k` 上下移動、`g/G` 跳到第一列 / 最後一列。
  選中 🔴 等你的列時，表格下方會多一行顯示**它具體在等什麼**（要跑的指令、問的問題；hook 模式才有）。
  Claude Code 背景 agent 以 `⚙` 標示；它沒有可跳轉的終端，選取時會顯示 `claude --resume` 接回提示，完成後預設收進已離場（按 `a` 仍可查看）。
- 否則 → **Rich poll**（清除畫面重畫）；連 Rich 都沒有就純文字。三層優雅降級。

### 跳到 session（`Enter` / `Space` / `ring focus`）

> 這裡的 `Space` 是鍵盤上的空白鍵，不是 BanG Dream! 裡的場館「SPACE」。

選一個 session 按 `Enter`，或用 `ring focus SESSION_ID`（完整 id 或唯一前綴），RiNG 把焦點帶到它真正所在的終端。
如果 TUI 正在跑，`ring focus` 會先把請求交給 TUI，讓游標選中該 session；沒有 TUI 時才直接跳終端。目前內建支援：

- **tmux**：`switch-client` 直接切到那個 pane（你跟它要在同一個 tmux server）。
- **iTerm2 / Terminal.app**（macOS）：用 session 的 `tty` 透過 AppleScript 聚焦對應分頁，
  自動分辨是哪個 app（沒在跑的 app 不會被喚醒）。第一次會跳系統「自動化」授權，准一次即可。
- **Linux X11 視窗**（`wmctrl`，best-effort fallback）：Linux 上沒跑 tmux 時的後備——
  從 `tty` 追到擁有它的終端視窗，用 `wmctrl` 帶到前景。**限制**：只支援 X11（Wayland 通常無效）、
  只能聚焦整個視窗無法選分頁、gnome-terminal 的 client/server 架構可能配不到。要先 `apt install wmctrl`。

「哪個 session 在哪個終端」靠它的 `tty` 對應——**hook 模式最精準**；
zero-config 下每個專案只開一個 session 時也對得上。Codex 沒裝 hook 時會走 zero-config：
同一個 cwd 只開一個 live Codex 時可跳轉；同 cwd 多個 Codex 只能保守顯示，避免跳錯。

### 就地回覆權限請求（`p`）

游標停在 🔴 等你的列按 `p`，RiNG 讀出那個 session 畫面上的權限對話框
（Claude Code 的「Do you want to proceed?」框，含背景 subagent 帶
「from the … agent」標頭的），把編號選項原文列成浮層讓你選；選定後 RiNG 會**再抓一次
畫面**確認對話框還在且沒變（防止你考慮期間它已被回掉），才代你按下那個數字，
並回頭驗證對話框確實消失。
確認回覆成功後，TUI 會立即清掉該筆「等你」；只有時間較新的 hook 事件（例如下一個權限請求）能再次把它標成等待。

- **tmux 內的 session**：用 `tmux capture-pane` 抓畫面、`tmux send-keys` 送鍵。
- **macOS 上直接開在 iTerm2 分頁的 session**（沒有 tmux）：用 session 的 `tty` 透過
  AppleScript 找到對應 iTerm2 分頁，抓畫面、送鍵都走 `osascript`。第一次用時 macOS
  會跳「允許控制 iTerm2」的自動化授權框，允許一次即可。

安全底線：畫面上解析不到可辨識的對話框（標記、編號、游標任一缺）就只提示、**絕不送鍵**——
對話框不在時按鍵會落進聊天輸入框變成文字；萬一送出的瞬間對話框剛好消失、數字落進輸入框，
RiNG 會自動補一個 Backspace 清掉並警告你。

**限制**：要有 tmux pane 座標、或（macOS 上）測得到 tty 的 iTerm2 session 才抓得到畫面；
其餘 session 請按 `Enter` 跳過去回。

### 等你時發通知

預設行為：裝了 hook 後，session 從工作中轉成 🔴 等你的**當下**，hook 就地**響鈴 + 發系統通知**——
**不必開著 RiNG 看板**，關掉終端也照樣 ring 你（通知由事件觸發，不靠輪詢）。
開著 TUI 時，若它一直停在等你，TUI 還會在 30s / 120s / 300s 各補一次 in-app 響鈴提醒。
（zero-config 測不到「等你」，所以這個需要 hook 模式。）

macOS 上若要點通知後直接跳回 RiNG TUI 並選中那個 session，需要安裝
[terminal-notifier](https://github.com/julienXX/terminal-notifier)（brew 外部 binary）：

```sh
brew install terminal-notifier
```

沒裝時退化為 macOS 原生純文字通知（不可點擊跳轉），RiNG 會在第一次走到這條路時提示一次。
通知聲音、重複提醒時間、後端選擇都能在 config 裡調整；通知後端也是可插拔的，見後面的
`Notifier` 擴充說明。

#### 推到手機（ntfy / webhook）

人不在座位時，桌面通知等於沒響。設定 [ntfy](https://ntfy.sh) topic URL 就能推到手機：

```toml
# ~/.config/ring/config.toml
notify_ntfy_url = "https://ntfy.sh/my-ring-topic"  # 手機裝 ntfy app、訂同一個 topic
notify_also = ["ntfy"]                             # 桌面通知照發，再「加發」一份到手機
```

`notify_backend = "ntfy"` 則是只推手機、不發桌面通知。要接 Slack / 自家 bot / IFTTT，
用 `notify_webhook_url` 走通用 webhook 後端（JSON POST，欄位穩定只加不改）。

### 清理 RiNG 狀態檔（`ring gc`）

RiNG 正常收到 `SessionEnd` 時會刪掉自己的 hook registry；如果 agent crash 或 hook 沒跑到結尾，
可能留下已離場的 `~/.config/ring/sessions/*.json`。這些檔案預設不會顯示在看板上，但可以用
`ring gc` 清掉。

```sh
ring gc --dry-run        # 預覽會刪哪些檔案
ring gc                  # 清掉已離場且超過 7 天的 registry，以及過期 IPC 檔
ring gc --older-than 1d  # 改成 1 天
ring gc --all-ended      # 清掉所有目前判定已離場的 registry
```

`ring gc` 只清 RiNG 自己寫在 `~/.config/ring/` 底下的狀態檔，不會碰 Claude Code / Codex 的
transcript 或 state。`ring doctor` 維持唯讀診斷，不會替你刪檔。

### 等待統計（`ring stats`）

hook 模式下，RiNG 會把 session 的**狀態轉換**記進 `~/.config/ring/events.jsonl`
（只記轉換、量很小、超過上限自動砍半保新）。`ring stats` 據此告訴你：最近這段時間，
每個專案 🔴 等了你幾次、平均 / 最長 / 總共等多久。

```sh
ring stats               # 最近 7 天
ring stats --since 12h   # 自訂時間窗
```

跟精準通知一樣，zero-config 測不到 🔴，所以 stats 也需要 hook 模式。

## Session 來源

RiNG 會從已註冊的 source 收集 session；目前內建這幾種：

| Source | 來源 | 狀態精度 |
|------|------|----------|
| **Claude Code zero-config**（預設） | 掃 `~/.claude/projects/**/*.jsonl` 的 mtime + 記錄裡的 `cwd` 欄位 | 免設定；可辨識近期活動與回合結束。需要回應的通知要靠 hook 才精準 |
| **Codex zero-config**（預設） | 讀 `~/.codex/state_5.sqlite` threads + rollout JSONL，並用 live `codex` process 配 tty | 免設定；可辨識 live / ended / 回合結束。同 cwd 多 session 建議裝 hook 取得精準跳轉 |
| **hook registry**（opt-in，精準） | RiNG hook 在 `Notification` / `UserPromptSubmit` / `Stop` / `SessionEnd` 即時寫 `~/.config/ring/sessions/` | 準（🔴 等你 / 🟢 工作中 / 🟡 跑完停著 / ⚫ 已離場） |

zero-config 不必設定就能用；想要精準的「誰在等你」，就讓 provider 的 hook 餵進 RiNG registry。
RiNG 內建 Claude Code / Codex hook 安裝器；其他工具可直接走 provider-neutral `ring hook` protocol。

## 狀態機

RiNG 把每個 session 壓成四種狀態。看板排序時，🔴 等你永遠排最上面並 highlight。

| 狀態 | 你可以怎麼理解 | RiNG 看到什麼 |
|------|----------------|---------------|
| 🔴 等你 | 需要你現在回去處理 | hook 收到權限請求、選項提問等需要回應的通知 |
| 🟢 工作中 | agent 正在跑，先不用管 | 你剛送出 prompt，或 session 最近仍有活動 |
| 🟡 跑完停著 | 這輪跑完了，停在那裡 | 收到 `Stop`，或超過 `working_threshold_seconds` 沒有新活動 |
| ⚫ 已離場 | session 已結束 | 收到 `SessionEnd`，或 process / 本機紀錄顯示它已離場 |

狀態更新規則：

| 發生的事 | 新狀態 |
|----------|--------|
| `SessionStart` / `UserPromptSubmit` | 🟢 工作中 |
| `Stop` | 🟡 跑完停著 |
| 權限請求、選項提問、`requires_action = true` | 🔴 等你 |
| `SessionEnd` / process 結束 | ⚫ 已離場 |
| zero-config 來源太久沒更新 | 🟢 工作中 退成 🟡 跑完停著 |

🔴 等你只有 hook 模式測得到；zero-config 只能判斷「最近有沒有活動」，
分不出「需要你做決策」和「剛跑完停著」。

## hook 模式（精準的「等你」）

zero-config 只靠檔案 mtime 或本機 state，**分不出「需要你做決策」還是「剛跑完」**。
裝 hook 後，RiNG 直接收 agent CLI 的事件，狀態才會精準——🔴 等你 ＋ 響鈴都要靠它。

### Claude Code / Codex：內建安裝器

hook 會被寫成「執行 `ring hook`」，所以 `ring` 要在 PATH 上、指向穩定路徑：

```sh
uv tool install 'ring-cli[tui]'
```

註冊 hook：

```sh
ring install-hooks            # 寫進 Claude Code / Codex hook 設定（合併，不覆蓋既有 hooks）
ring install-hooks --dry-run  # 只想先看會寫什麼、不動檔
```

Claude Code 會寫進 `~/.claude/settings.json`。如果你有在用 Codex（`~/.codex` 存在），
RiNG 也會寫進 `~/.codex/hooks.json`；Codex 會要求你信任新 hook，信任後才會執行。

Claude Code 註冊這幾個事件，對應到狀態：

| Claude Code 事件 | RiNG 狀態 |
|---|---|
| `SessionStart` / `UserPromptSubmit` | 🟢 工作中 |
| `Stop` | 🟡 跑完停著 |
| `Notification` 的 `permission_prompt` / `elicitation_dialog` | 🔴 等你（卡權限 / 需要選項） |
| `PermissionRequest` / `PreToolUse` 的 `AskUserQuestion` | 🔴 等你（權限 / 選項需要你決策） |
| `SessionEnd` | 從看板消失 |

Codex 目前註冊 Codex 支援的互動事件：`PreToolUse`、`PermissionRequest`、`Stop`。

hook 只對**新開的 session** 生效，所以裝完要重開。確認方法：

```sh
ls ~/.config/ring/sessions/   # 出現 <session_id>.json 就代表 hook 在寫了
```

RiNG 一偵測到 `~/.config/ring/sessions/` 有資料就自動切精準模式（hook 來源優先、
zero-config 掃描補上沒裝 hook 的 session）。

### 其他 provider：中立 hook protocol

RiNG 不相依 agent-hooks，也不要求 provider 一定要用內建安裝器。任何工具只要在事件發生時把 JSON
餵給 `ring hook` 即可：

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

支援的事件語意與 Claude Code 一致：`SessionStart` / `UserPromptSubmit` → 🟢 工作中，
`Stop` → 🟡 跑完停著，需要回應的 `Notification` / `PermissionRequest` → 🔴 等你，`SessionEnd` →
從看板移除。非 Claude provider 的 session id 會自動加上 provider prefix
（例如 `codex:thread-123`），避免不同工具撞 id。

provider 若能分辨「立刻需要使用者」與「只是等下一步」，請直接給明確欄位：
`requires_action = true/false` 或 `waiting_for = "permission" | "options" | "next_step"`。
RiNG 會優先相信這些欄位；沒有時才退回 event / notification type 推論。

### 系統通知（🔔 等你時自動通知 + 點擊聚焦）

這裡補細節：系統通知由 `ring hook` 在事件當下送出（headless `--watch` 與 TUI 都只負責顯示
看板，不發系統通知）。預設後端是 `auto`：有可點擊的 `terminal-notifier` 就優先用它；沒有時
退回 `osascript`（macOS）或 `notify-send`（Linux）。如果全部不可用，就只保留看板，不讓通知失敗打斷主流程。

- **設定**：`notify_sound`、`notify_sound_name`、`notify_ignore_dnd`、`notify_repeat_seconds`、
  `notify_repeat_max`、`notify_backend`、`waiting_cooldown_seconds` 都可在 `~/.config/ring/config.toml` 調整。
- **防翻轉轟炸**：背景 subagent 的權限請求可能讓 session 在等你／工作中之間快速翻轉。
  `waiting_cooldown_seconds`（預設 180 秒）讓 `ring hook` 的系統通知與 TUI 的響鈴／提醒，
  在 session 離開等你又很快轉回時，距上次通知未滿冷卻期就不再立即發；設 `0` 關閉冷卻
  （回到每次轉入都發）。
- **點擊聚焦 + 聲音**：需先安裝 `terminal-notifier`（brew）。點擊通知後會直接跳回 RiNG TUI
  並選中 session；若沒有 TUI 在跑，則直接跳到對應終端。

  ```sh
  brew install terminal-notifier
  ```

- **純文字通知**：未裝 `terminal-notifier` 則退化為 macOS 原生純文字通知（可帶聲音，點擊不可聚焦）。
- **擴充**：要接其他桌面通知、webhook 或自訂提醒方式，可以新增 `Notifier` 後端並註冊，
  詳見「其他通知後端」。

- **交給 agent-hooks**：`agent-hooks` 是另一個可選的 hook helper / 決策 UI。若你已安裝它，
  並把 `notify_backend` 設成 `agent-hooks`，`ring hook` 會照樣寫 RiNG registry，
  同時把原始 payload 交給 `agent-hooks callback` 處理同步決策視窗；RiNG 的 `--watch`
  不會再重複發通知。若 PATH 上找不到 `agent-hooks`，會自動退回 `auto`。

### 移除 hooks

不想再讓 agent CLI 事件寫進 RiNG registry 時，可以移除 RiNG 安裝的 hook 條目。
這不會刪掉 `~/.config/ring/` 裡的 session 紀錄，也不會動到 Claude Code / Codex 的其他 hooks。

```sh
ring remove-hooks            # 從 Claude Code / Codex hook 設定移除 RiNG hook 條目
ring remove-hooks --dry-run  # 只預覽，不改檔
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
waiting_window_seconds = 1800    # 跑完停著升等你的時間窗上限（預設 30 分）
notify_sound = true              # 系統通知是否播放聲音
notify_sound_name = "Glass"      # macOS / terminal-notifier sound name
notify_ignore_dnd = false        # macOS terminal-notifier 是否穿透勿擾 / Focus
notify_backend = "auto"          # auto / terminal-notifier / osascript / notify-send / agent-hooks / none
notify_repeat_seconds = [30, 120, 300]  # 持續等你時，幾秒後重複提醒
notify_repeat_max = 3            # 重複提醒上限；0 = 不限
waiting_cooldown_seconds = 180   # 離開等你又轉回時，距上次提醒未滿這段時間就不再立即提醒；0 = 關閉
notify_ntfy_url = ""             # 設完整 ntfy topic URL 啟用手機推播（如 https://ntfy.sh/my-topic）
notify_webhook_url = ""          # 設 URL 啟用通用 webhook 後端（JSON POST）
notify_also = []                 # 主後端之外「加發」的後端，如 ["ntfy"]（桌面＋手機各一份）
focusers = ["Neovim", "tmux", "iTerm2", "Terminal", "linux-wm"]   # 跳轉嘗試順序
plugins = []                     # 啟動時 import 的外部 plugin 模組（見「支援的工具與擴充」）

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

## 支援的工具與擴充

core 不綁死任何特定工具或終端。三個維度都可插拔，每個都是「寫個小類別 ＋ 註冊」、主流程零改動：

| 維度 | 在做什麼 | 內建 |
|------|----------|------|
| `SessionSource` | 從哪裡找到 session | Claude Code、Codex、RiNG hook registry |
| `Focuser` | 跳轉時把焦點帶去哪個終端 | tmux、iTerm2、Terminal.app、Linux X11（wmctrl）|
| `Notifier` | 等你時怎麼發系統通知 | terminal-notifier、osascript、notify-send、ntfy、webhook |

每個維度各自一個 package（`ring/sources/`、`ring/focus/`、`ring/notify/`），每個後端是裡面
一個模組；要加新的＝丟一個模組 ＋ `register_*()`。

### 讓裝好的 `ring` 載入你的 plugin

`register_*()` 要有人執行才算數。裝好的 `ring` 指令啟動時會自動載入兩種來源的 plugin：

1. **entry point**（發佈成套件時）——在你套件的 `pyproject.toml` 宣告，指向一個模組或
   callable（模組在 import 時自行 `register_*()`；callable 會被無參數呼叫一次）：

   ```toml
   [project.entry-points."ring.plugins"]
   mytool = "ring_mytool.plugin"
   ```

2. **config**（本機腳本）——`~/.config/ring/config.toml` 寫 `plugins = ["my_module"]`，
   模組要在 `sys.path` 上（site-packages 或 `PYTHONPATH`）。

單一 plugin 壞掉只會在 stderr 警告一行、不擋看板。

### 其他 agent CLI（`SessionSource`）

內建 `HookRegistrySource`（讀 `~/.config/ring/sessions/`）、`ClaudeCodeSource`（掃
`~/.claude`）與 `CodexSource`（讀 `~/.codex/state_5.sqlite`）。要監測其他工具，
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

### 其他終端（`Focuser`）

跳轉的終端整合也一樣——寫個符合 `Focuser` 協定的類別（`try_focus(session) ->
(ok, msg) | None`），呼叫 `ring.focus.register_focuser(MyFocuser())`。內建 Neovim terminal / tmux /
iTerm2 / Terminal.app / Linux X11 視窗（wmctrl），各自一個模組（`ring/focus/neovim.py`、
`ring/focus/tmux.py` …）。Neovim focuser 會先透過 `$NVIM` server socket 切到承載該 session 的
`:terminal` buffer，再交給外層 focuser 聚焦 pane 或視窗。
要再加 Ghostty / Kitty / WezTerm，就照這個模式新增 focuser；嘗試順序也能用 config 的 `focusers` 調整。

### 其他通知後端（`Notifier`）

系統通知也是可插拔的——寫個符合 `Notifier` 協定的類別，呼叫
`ring.notify.register_notifier(MyNotifier())`。內建 terminal-notifier（可點擊跳轉）/
osascript / notify-send，各自一個模組（`ring/notify/terminal_notifier.py` …）：

```python
from ring.notify import register_notifier


class MyNotifier:
    name = "mytool-notify"

    def available(self) -> bool: ...        # 這個後端現在能不能用（通常看 binary 在不在）
    def supports_click(self) -> bool: ...   # 點通知能不能跳回 session
    def send(self, sessions): ...           # 逐 session 各發一則通知


register_notifier(MyNotifier())
```

選哪個後端由 config 的 `notify_backend` 決定（`auto` / 指定名稱 / `none` 純看板不發通知）。

## 平台與隱私

- **平台**：macOS / Linux（靠 `ps` / `lsof` / `tmux` 偵測；Windows 未支援）。
- **隱私**：全程在你本機跑——只**讀** `~/.claude/`、`~/.codex/` 的本機資料，只**寫**
  `~/.config/ring/`。不連網、不上傳、不外送任何資料。

## 名字哪裡來

名字三重共鳴：

1. 📞 它 **ring** 你（等你時發通知）
2. 🎤 BanG Dream! 的 live house「RiNG」<br>
   （場館＝你坐著看一團一團、也就是一個一個 session 演出的地方）
3. **R**ealtime **I**nstance **N**otification **G**rid（它到底是什麼）

BanG Dream! 原作裡的 RiNG 是 CiRCLE 的**二號店**——因為少女樂團數量暴增、一間場館不夠用，
才在「老闆」的要求下蓋的；而且它跟 CiRCLE **共用同一套訂單／帳號系統**。
這把工具的定位剛好也是這樣：

- **一樣的誕生理由**：你的並行 agent CLI session 也暴增了，需要一個專門的地方一眼看完。
- **不取代、是分店**：RiNG 不是另一個 client——它讀的是 Claude Code / Codex 的本機後台，
  只是替你多開的第二個觀測視窗。

如果你覺得好用、順手，那就回去補番吧！

## 不做什麼

RiNG 的範圍只放在「session 現在是什麼狀態、要不要你回去處理」。它不是 agent
用量分析器，也不打算變成另一個成本儀表板。

### 為什麼不碰 token / 花費統計

Claude Code 的 JSONL token 數字是壞的（input 差約 100 倍、output 差約 10 倍），
所有靠它做帳的工具都中招。RiNG 只看**狀態**，不做 cost accounting——刻意避開這雷。

## License

MIT
