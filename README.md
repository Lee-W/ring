# RiNG 🎤

**台灣華語** · [English](README.en.md)

[![PyPI](https://img.shields.io/pypi/v/ring-cli?label=PyPI)](https://pypi.org/project/ring-cli/)
[![Python](https://img.shields.io/pypi/pyversions/ring-cli)](https://pypi.org/project/ring-cli/)
[![License](https://img.shields.io/pypi/l/ring-cli)](LICENSE)

> **R**ealtime **I**nstance **N**otification **G**rid
> ——把所有 active 的 agent CLI session 放上同一張本機看板。

同時開很多 Claude Code、Codex 或本機模型時，RiNG 讓你一眼看出誰還在工作、誰已停下、誰正在等你；需要你回應的 session 會排在最前面。

```text
🎤 RiNG — 3 session 在場 · 2 個 agent process 跑著

  🔴 maigo            12s  → 等你確認權限
  🟢 pelican-osm       3s  → Edit
  🟡 commitizen        8m  跑完一回合、停著
```

## 能做什麼

- **集中看狀態**：內建 Claude Code、Codex、Ollama 與 llama.cpp session 來源。
- **需要你時優先**：精準標出 🔴 等你，顯示它在等什麼，並發送桌面或手機通知。
- **直接回到現場**：從 TUI 跳回 tmux、iTerm2、Terminal.app、Neovim terminal 或 Linux X11 視窗。
- **少切一次視窗**：支援的終端中可直接在 TUI 回覆權限請求，也能替 session 命名。
- **接進既有工具**：提供 status bar 單行輸出、JSON、provider-neutral hook 與 plugin 擴充點。

## 三步上手

需要 Python 3.13+：

```sh
uv tool install 'ring-cli[tui]'
ring --watch
ring install-hooks
```

1. `ring --watch` 開啟互動看板；方向鍵選 session，`Enter` 跳回原本的終端。
2. `ring install-hooks` 安裝 Claude Code / Codex hooks，取得精準的 🔴 等你狀態與系統通知。
3. 安裝 hook 後重開 agent session；需要你回話時，即使看板沒開，RiNG 也會通知你。

偏好 pipx 時可改用 `pipx install 'ring-cli[tui]'`。只想先看快照則執行 `ring`。

## 常用操作

| 操作 | 用途 |
|------|------|
| `ring` | 印出目前所有 session 的快照 |
| `ring --watch` | 開啟持續更新的互動 TUI |
| `Enter` / `Space` | 跳回選取 session 所在的終端 |
| `p` | 就地回覆選取 session 的權限請求 |
| `n` | 替選取的 session 命名 |
| `ring doctor` | 檢查 hooks、通知與終端聚焦設定 |
| `ring --format oneline` | 產生 tmux、SwiftBar、waybar 可用的摘要 |

不裝 hooks 也能用零設定模式查看 session；但 🔴 等你、等待內容與即時通知需要 hooks。安裝或更新 hooks 後，記得重開 Claude Code / Codex session。

## 接著看

- [完整使用手冊](https://lee-w.github.io/ring/guide/)：所有指令、TUI 操作、hooks、通知、設定、擴充與隱私說明
- [Session 狀態](https://lee-w.github.io/ring/session-states/)：🔴/🟢/🟡/⚫ 的判定方式
- [貢獻指南](CONTRIBUTING.md)：開發環境、測試與 PR checklist

macOS / Linux 可用；Windows 尚未支援。預設只讀本機 agent 資料、只寫 `~/.config/ring/`；只有你自行設定的 ntfy / webhook 通知會連網。

## License

MIT
