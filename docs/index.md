# RiNG 文件

[![PyPI](https://img.shields.io/pypi/v/ring-cli?label=PyPI)](https://pypi.org/project/ring-cli/)
[![Python](https://img.shields.io/pypi/pyversions/ring-cli)](https://pypi.org/project/ring-cli/)
[![License](https://img.shields.io/pypi/l/ring-cli)](https://github.com/Lee-W/ring/blob/main/LICENSE)

> **R**ealtime **I**nstance **N**otification **G**rid
> ——把所有 active 的 agent CLI session 放上同一張本機看板。

RiNG 把 Claude Code、Codex 與本機模型 session 放在同一張看板，讓需要你回應的工作排在最前面，並能直接跳回原本的終端。

```text
🎤 RiNG — 3 session 在場 · 2 個 agent process 跑著

  🔴 maigo            12s  → 等你確認權限
  🟢 pelican-osm       3s  → Edit
  🟡 commitizen        8m  跑完一回合、停著
```

## 適合誰用

- 你會同時開好幾個 Claude Code / Codex session。
- 你想分辨哪些 session 正在跑、跑完停著或正在等你。
- 你想從 TUI 直接跳回 session 所在的終端。
- 你願意安裝 hooks，換取精準的等待狀態與系統通知。

## 重點功能

- **一張看板看全部 session**：內建 Claude Code、Codex、Ollama 與 llama.cpp 支援，其他工具可接 `ring hook`。
- **等你優先**：需要你回應的 session 會排在最上面，並顯示具體在等什麼。
- **一鍵跳回終端**：支援 tmux、iTerm2、Terminal.app、Neovim terminal 與 Linux X11 視窗。
- **就地回覆與命名**：在支援的終端直接回覆權限請求，並替 session 取容易辨認的名字。
- **即時通知**：看板沒開也能發送桌面通知；ntfy / webhook 可把通知推到手機。
- **容易整合**：提供 status bar 單行摘要、JSON、provider-neutral hook 與 plugin 擴充點。

## 快速開始

需要 Python 3.13+。PyPI 套件名是 `ring-cli`，module 與 CLI 指令則叫 `ring`：

```sh
# 推薦：從 PyPI 裝成全域指令
uv tool install 'ring-cli[tui]'

# 或使用 pipx
pipx install 'ring-cli[tui]'

# 看一次快照或開啟互動 TUI
ring
ring --watch
ring --watch --interval 1

# 安裝後也能用 module 形式執行
python -m ring
```

`[tui]` 會安裝 Textual 互動介面；沒有這個 extra 時，`--watch` 會依序退回 Rich poll 或純文字模式。開啟 TUI 後用 `↑` / `↓` 選擇 session，按 `Enter` 或 `Space` 跳回它所在的終端，按 `q` 離開。

從原始碼開發時不必先安裝全域指令，在 repository 內執行：

```sh
uv sync --all-groups
uv run ring
uv run ring --watch
```

零設定模式會自動尋找本機 session。若要精準辨識 🔴 等你、顯示等待內容並發送系統通知，再執行：

```sh
ring install-hooks            # 合併寫入 Claude Code / Codex hook 設定
ring install-hooks --dry-run  # 只預覽，不修改設定
ring doctor                   # 檢查 hooks、通知與終端聚焦能力
ring gc --dry-run             # 預覽 stale 狀態檔清理
```

安裝 hooks 後要重開 Claude Code / Codex session。`ring doctor` 會檢查 hooks、通知後端與終端聚焦能力是否可用。

## 從哪裡繼續

- 想了解 TUI 快捷鍵、權限回覆、通知或手機推播：看[完整使用手冊](guide.md)。
- 想把摘要放進 tmux、SwiftBar 或 waybar：看[輸出格式](guide.md#status-bar-format)。
- 想接入其他 agent CLI、終端或通知服務：看[完整使用手冊的擴充說明](guide.md)。
- 想釐清 🔴/🟢/🟡/⚫ 如何判定：看 [Session 狀態](session-states.md)。
- 想參與開發：看[貢獻指南](contributing.md)。

## 文件導覽

- [完整使用手冊](guide.md)：所有常用指令、TUI 操作、終端跳轉、權限回覆、通知、hooks、設定、plugin 擴充、平台與隱私
- [Session 狀態](session-states.md)：hook 與零設定模式如何判定 🔴/🟢/🟡/⚫，以及完整事件對照
- [貢獻指南](contributing.md)：開發環境、測試、程式風格、i18n、commit 與 PR checklist

## 平台與隱私

- **平台**：支援 macOS 與 Linux；Windows 尚未支援。
- **隱私**：預設只讀本機 `~/.claude/`、`~/.codex/` 資料，只寫 `~/.config/ring/`，沒有 telemetry。只有你自行設定的 ntfy / webhook 通知會連網。

## 名字哪裡來

名字有三層意思：

1. 📞 session 需要回應時，它會 **ring** 你。
2. 🎤 RiNG 是 *BanG Dream!* 裡的 live house；就像一座場館，讓你看一個個 session 上台。
3. **R**ealtime **I**nstance **N**otification **G**rid 描述了這項工具本身。

原作裡的 RiNG 是 CiRCLE 的二號店：樂團數量變多，一間場館不夠用，於是用同一套後台再開一個觀測空間。這也正是本工具的定位——不取代 agent CLI，只替大量並行 session 多開一張看板。

## 不做什麼

RiNG 只關心 session 現在的狀態，以及是否需要你回去處理；它不是 agent 用量分析器或成本儀表板。Claude Code JSONL 的 token 數字不足以可靠計費，因此 RiNG 刻意不提供 token / 花費統計。

## License

MIT
