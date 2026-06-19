# RiNG 🎤

> **R**ealtime **I**nstance **N**otification **G**rid
> ——看所有 active 的 Claude Code session 上台的**場館**。

你同時開了好幾個 Claude Code，不知道哪個正在等你回話、哪個還在跑、哪個早就停了。
RiNG 把它們全部請上同一個舞台，一眼看完——**誰在等你，排最前面**。
session 需要你回話時，它「**ring** 你」。

名字三重共鳴：📞 它 **ring** 你（待回覆通知）＋ 🎤 BanG Dream! 的 live house「RiNG」
（場館＝你坐著看一團一團、也就是一個個 session 演出的地方）＋ **R**ealtime **I**nstance
**N**otification **G**rid（它到底是什麼）。覺得好用、順手回去補番，那就更好了。

## 為什麼是「二號店」

原作裡的 RiNG 是 CiRCLE 的**二號店**——因為少女樂團數量暴增、一間場館不夠用，
才在「老闆」的要求下蓋的；而且它跟 CiRCLE **共用同一套訂單／帳號系統**。
這把工具的定位講得剛剛好：

- **一樣的誕生理由**：你的並行 Claude Code session 也暴增了，需要一個專門的地方一眼看完。
- **不取代、是分店**：RiNG 不是另一個 client——它讀的是同一套 `~/.claude` 後台、
  跟 Claude Code 共用資料，只是替你多開的第二個觀測視窗。

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
- 否則 → **Rich poll**（清屏重畫）；連 Rich 都沒有就純文字。三層優雅降級。

### 跳到 session（`Enter` / `Space`）

選一個 session 按 `Enter`，RiNG 把焦點帶到它真正所在的終端。終端整合是**可插拔的
focuser**——core 不綁任何特定 vendor，加一個終端＝加一個 focuser、主流程零改動：

- **tmux**：`switch-client` 直接切到那個 pane（你跟它要在同一個 tmux server）。
- **iTerm2 / Terminal.app**（macOS）：用 session 的 `tty` 透過 AppleScript 聚焦對應分頁，
  自動分辨是哪個 app（沒在跑的 app 不會被喚醒）。第一次會跳系統「自動化」授權，准一次即可。

要再加 Ghostty / Kitty / WezTerm：寫一個符合 `Focuser` 協定的類別
（`try_focus(session) -> (ok, msg) | None`），呼叫 `ring.focus.register_focuser(MyFocuser())`
即可——core 零改動。嘗試順序也能用 config 的 `focusers` 調整。

「哪個 session 在哪個終端」靠它的 `tty` 對應——**hook 模式最精準**；zero-config 下
每個專案只開一個 session 時也對得上。

### 它會 ring 你 🔔

裝了 hook 後，有 session 從工作中轉成 🔴 待回覆時，RiNG 會**響鈴 + 跳通知**——
名副其實，它真的 ring 你。（zero-config 測不到 WAITING，所以這個需要 hook 模式。）

```
🎤 RiNG — 3 session 在場 · 2 claude process 跑著

  🔴 maigo            12s  → 等你確認權限
  🟢 pelican-osm       3s  → Edit
  🟡 commitizen        8m  跑完一回合、停著
```

## 兩種資料來源

| 模式 | 來源 | 狀態精度 |
|------|------|----------|
| **zero-config**（預設） | 掃 `~/.claude/projects/**/*.jsonl` 的 mtime + 記錄裡的 `cwd` 欄位 | 粗（活躍度分層，測不到「正在等你」） |
| **hook**（opt-in，精準） | RiNG hook 在 `Notification` / `UserPromptSubmit` / `Stop` / `SessionEnd` 即時寫 `~/.config/ring/sessions/` | 準（🔴 等你 / 🟢 工作 / 🟡 idle / ⚫ 離場） |

zero-config 不必設定就能用；想要精準的「誰在等你」，再裝 hook。

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

## 精準的「待回覆」狀態（hook 模式）

zero-config 靠 mtime 猜不出「在等你回話」。裝 hook 後，RiNG 從 Claude Code 的
事件直接拿到精準狀態（Stop / Notification → 🔴 等你；SessionEnd → 立刻消失）：

```sh
uv tool install .          # 先裝成全域指令（讓 hook 指向穩定路徑）
ring install-hooks         # 註冊進 ~/.claude/settings.json（合併，不覆蓋）
ring install-hooks --dry-run   # 只想先看會寫什麼
```

裝完重開 session 即生效。RiNG 偵測到 `~/.config/ring/sessions/` 有資料就自動切精準模式。

## 設定（選用）

`~/.config/ring/config.toml`，全部選填、缺了就用預設：

```toml
lang = "zh-Hant"                 # 預設語言（CLI --lang 最優先，再來 RING_LANG / LANG）
interval = 2.0                   # watch 刷新秒數
show_all = false                 # 是否預設顯示已離場
legend = true                    # 是否預設顯示圖例
active_window_seconds = 21600    # 只看最近這段時間動過的 session（預設 6h）
working_threshold_seconds = 90   # 多久沒動 → 🟢 工作中 變 🟡 閒置
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
源碼即翻譯。切語言：`--lang en`（或 `RING_LANG=en` / config 的 `lang`）。複數用
`ngettext` 正確處理（`1 session` vs `2 sessions`）。

加一個語言、或改了字串要重抽：

```sh
poe i18n-extract     # 從源碼抽 msgid → src/ring/locale/ring.pot
# 複製 ring.pot 成 src/ring/locale/<lang>/LC_MESSAGES/ring.po，填 msgstr
poe i18n-compile     # 各 .po → .mo（.mo 要 commit；wheel 會自動帶上）
```

## 平台與隱私

- **平台**：macOS / Linux（靠 `ps` / `lsof` / `tmux` 偵測；Windows 未支援）。
- **隱私**：全程在你本機跑——只**讀** `~/.claude/projects/` 的 transcript、只**寫**
  `~/.config/ring/`。不連網、不上傳、不外送任何資料。

## Roadmap

- [x] zero-config 資料層 + 快照（Rich 表格，stdlib fallback）
- [x] hook 腳本 + `ring install-hooks`（精準「待回覆」狀態）
- [x] tests + ruff + mypy strict + CI
- [x] Textual live TUI（鍵盤導覽、tmux 一鍵跳）

## License

MIT

